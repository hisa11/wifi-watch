"""ローカルの中継（Node＋偽のRedis）を挟んだ通し：アプリ役 → 中継 → 偽の無線で動くPC → 中継 → アプリ役。"""
import copy
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.fakehttp import FakeHttp
from wifiwatch import channels as ch
from wifiwatch.app import build
from wifiwatch.clock import FakeClock
from wifiwatch.config import parse_config
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation, demo_scenario

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "relay"
APP_TOKEN = "app-token-e2e-0123456789abcdef"
PC_TOKEN = "pc-token-e2e-0123456789abcdef"
F = ch.parse_channel_ref


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalRelay:
    def __init__(self):
        self.port = free_port()
        self.proc = None

    def start(self):
        env = {**os.environ, "APP_TOKEN": APP_TOKEN, "PC_TOKEN": PC_TOKEN}
        self.proc = subprocess.Popen(["node", "dist/src/dev-server.js", str(self.port)], cwd=RELAY, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("ローカルの中継が起動しない")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            self.proc = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="module")
def built():
    if not shutil.which("node") or not shutil.which("npm"):
        pytest.skip("node がない")
    subprocess.run(["npm", "run", "-s", "build"], cwd=RELAY, check=True, capture_output=True)


@pytest.fixture
def relay(built):
    r = LocalRelay().start()
    yield r
    r.stop()


class AppRole:
    """テスト用のアプリ役（アプリ用のトークンで中継だけと話す）。"""

    def __init__(self, url):
        self.c = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {APP_TOKEN}"}, timeout=5)

    def poll(self, opened=False):
        return self.c.post("/api/app/poll", json={"opened": opened}).json()

    def command(self, rid, type_, params):
        return self.c.post("/api/app/commands", json={"request_id": rid, "type": type_, "params": params})

    def records(self, since=0):
        return self.c.get(f"/api/app/records?since={since}").json()["records"]

    def record(self, rid):
        return self.c.get(f"/api/app/records/{rid}").json()

    def label(self, rec_id, rid, label, edited_at):
        return self.c.patch(f"/api/app/records/{rec_id}", json={"request_id": rid, "label": label, "edited_at": edited_at})

    def walk(self, rid, rec):
        return self.c.post("/api/app/records", json={"request_id": rid, "record": rec})

    def ask_view(self):
        return self.c.post("/api/app/pcview", json={}).json()

    def view(self):
        return self.c.get("/api/app/pcview").json()


def make_pc(tmp_path, relay_url, ntfy_url=None, scenario=None):
    d = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    d["data_dir"] = str(tmp_path / "data")
    d["signatures"] = str(ROOT / "signatures.json")
    d["relay"] = {"url": relay_url, "pc_token": PC_TOKEN, "idle_interval_s": 10, "fg_interval_s": 2, "timeout_s": 3}
    if ntfy_url:
        d["notify"] = {"ntfy_server": ntfy_url, "ntfy_topic": "wifiwatch-e2e-topic-0123456", "timeout_s": 2}
    cfg = parse_config(d)
    clk = FakeClock()
    app = build(cfg, clk, FakeRadio(clk, scenario or demo_scenario()))
    return app, clk


def walk_record(rid="walk-0001"):
    return {"id": rid, "kind": "walk", "created_at": 1_791_936_100.0, "device": "pixel-9a",
            "settings": {"seconds": 30}, "stations": [
                {"bssid": "02:a0:00:00:00:01", "ssid": "KOSEN-A-6G", "freq": F("6g5"), "width": 20, "rssi": -58}],
            "label": {"location": "フィールド北", "tags": ["歩測"], "memo": "", "updated_at": 1_791_936_100.0}}


def test_round_trip_all_operations(tmp_path, relay):
    pc, clk = make_pc(tmp_path, relay.url)
    rc = pc.relay
    app = AppRole(relay.url)
    pc.monitor.run_for(20)
    assert rc.sync_once() and rc.ok
    # アプリを開く → PCが前面に気づいて状態の写しを置く
    assert app.poll(opened=True)["state"] is None
    rc.sync_once()
    assert rc.fg
    rc.sync_once()
    st = app.poll()
    assert st["state"]["mode"] == "LIVE" and st["state_age_ms"] < 5000
    assert len(json.dumps(st["state"]).encode()) <= 8 * 1024
    assert st["state"]["own"]["label"] == "6GHz ch37"
    # 手動スキャン
    r = app.command("app-scan-1", "scan", {"bands": ["6"]})
    assert r.status_code == 200
    rc.sync_once()
    assert pc.controller.command_status["app-scan-1"]["state"] == "running"
    pc.monitor.run_for(8)
    assert pc.controller.command_status["app-scan-1"]["state"] == "done"
    rc.sync_once()
    assert rc.push_once() >= 1
    rec_id = pc.controller.command_status["app-scan-1"]["record"]
    st = app.poll()
    assert st["state"]["commands"]["app-scan-1"]["state"] == "done"
    lst = app.records()
    assert any(x["id"] == rec_id and x["origin"] == "app" for x in lst)
    detail = app.record(rec_id)
    assert detail["kind"] == "pc_scan" and detail["stations"]
    # 同じ要求IDを二重に送っても、スキャンは1回だけ
    n_before = len(pc.store.list_records(kind="pc_scan"))
    assert app.command("app-scan-1", "scan", {"bands": ["6"]}).status_code == 200
    rc.sync_once()
    pc.monitor.run_for(8)
    assert len(pc.store.list_records(kind="pc_scan")) == n_before
    assert not pc.monitor.scheduler.manual_running
    # 帯域ON/OFF
    app.command("app-bands-1", "bands", {"excluded_bands": ["2.4"], "excluded_freqs": []})
    rc.sync_once()
    assert pc.monitor.scheduler.excluded_bands == {"2.4"}
    rc.sync_once()
    st = app.poll()["state"]
    assert st["bands"]["excluded_bands"] == ["2.4"]
    assert {c[1] for c in st["channels"] if c[0] < 3000} == {"x"}  # 巡回外は「x」
    # ラベル（アプリで変えた → PCが正本に入れ → 写しが戻る）
    app.label(rec_id, "app-label-1", {"location": "客席東", "tags": ["試合前"], "memo": "アプリから"}, clk.time() + 5)
    rc.sync_once()
    assert pc.store.get_record(rec_id)["label"]["location"] == "客席東"
    rc.push_once()
    assert app.record(rec_id)["label"]["location"] == "客席東"
    # 歩測の記録
    assert app.walk("app-walk-1", walk_record()).status_code == 200
    rc.sync_once()
    assert pc.store.get_record("walk-0001")["label"]["location"] == "フィールド北"
    rc.push_once()
    assert any(x["id"] == "walk-0001" for x in app.records())
    # 受信箱は空になっている（保存したと返した）
    rc.sync_once()
    assert rc.sync_once() and rc.version is not None
    pc.store.close()


def test_pc_view_only_when_app_asks(tmp_path, relay):
    """スマホからPCが今見ている局を見る。頼んだときだけPCが1回置く（常には配らない）。PCの動きは変えない。"""
    pc, clk = make_pc(tmp_path, relay.url)
    rc = pc.relay
    app = AppRole(relay.url)
    pc.monitor.run_for(30)
    mode = pc.monitor.scheduler.mode
    rc.sync_once()
    rc.sync_once()
    assert app.view()["view"] is None  # 頼まれていなければ置かない
    asked = app.ask_view()["requested_at"]
    rc.sync_once()
    v = app.view()
    assert v["for"] == asked
    view = v["view"]
    assert view["stations"] and {"b", "f", "sp", "r", "k", "p"} <= set(view["stations"][0])
    assert view["state"]["own"]["label"] == "6GHz ch37" and view["state"]["channels"]
    assert view["strong"] == pc.controller.cfg.thresholds.strong_rssi_dbm
    # 同じ頼みには1回だけ：もう一度問い合わせても置き直さない
    at = v["received_at"]
    rc.sync_once()
    assert app.view()["received_at"] == at
    # 一覧を置いても、PCのモード・巡回は変わらない（指示ではない）
    assert pc.monitor.scheduler.mode == mode and not pc.monitor.scheduler.manual_running
    # 次に頼んだら、新しい一覧を置く
    time.sleep(0.01)
    asked2 = app.ask_view()["requested_at"]
    rc.sync_once()
    assert app.view()["for"] == asked2
    pc.store.close()


def test_mode_change_is_not_accepted_via_relay(tmp_path, relay):
    app = AppRole(relay.url)
    r = app.command("bad-1", "mode", {"mode": "PRE"})
    assert r.status_code == 400


def test_expired_command_is_not_executed_on_pc(tmp_path, relay):
    pc, clk = make_pc(tmp_path, relay.url)
    item = {"kind": "command", "type": "scan", "params": {"bands": ["6"]}, "request_id": "old-1",
            "accepted_at": 1_000_000, "field": "cmd:scan"}
    pc.relay._apply(item, 1_000_000 + 61_000)
    assert pc.controller.command_status["old-1"]["state"] == "expired"
    assert not pc.monitor.scheduler.manual_running
    pc.store.close()


def test_relay_outage_keeps_monitoring_and_resends(tmp_path, relay):
    ntfy = FakeHttp().start()
    try:
        sc = Scenario([SimStation("02:6c:5a:00:00:09", "CineEye2-6G", F("6g69"), 160, -35),
                       SimStation("02:52:46:00:00:01", "venue", F("5g36"), 20, -60)], seed=1)
        pc, clk = make_pc(tmp_path, relay.url, ntfy.url, sc)
        rc = pc.relay
        app = AppRole(relay.url)
        assert rc.sync_once()
        relay.stop()  # 中継が止まった
        assert not rc.sync_once() and not rc.ok
        # 監視と通知は止まらない
        pc.monitor.run_for(60)
        pc.notifier.deliver_due()
        assert any("CineEye" in p["title"] for p in ntfy.posts("/"))
        assert pc.controller.state()["relay"]["ok"] is False
        # 止まっている間に画面から手動スキャン → 記録は溜まる
        pc.controller.manual_scan(bands=["6"], origin="desktop")
        pc.monitor.run_for(8)
        assert rc.push_once() == 0 and pc.store.relay_backlog() >= 1
        relay.start()  # 戻る（偽のRedisは空：中身が消えても困らない）
        clk.advance(120)
        assert rc.sync_once() and rc.ok
        assert rc.push_once() >= 1
        assert pc.store.relay_backlog() == 0
        assert any(x["kind"] == "pc_scan" for x in app.records())
        pc.store.close()
    finally:
        ntfy.stop()


def test_pc_restart_loses_nothing(tmp_path, relay):
    pc, clk = make_pc(tmp_path, relay.url)
    app = AppRole(relay.url)
    app.command("rb-1", "bands", {"excluded_bands": ["5"], "excluded_freqs": []})
    # 受け取ったが保存する前に落ちた（ackを返していない）
    orig = pc.relay._apply

    def crash(item, now):
        raise RuntimeError("落ちた")

    pc.relay._apply = crash
    pc.relay.sync_once()
    assert pc.monitor.scheduler.excluded_bands == set()
    pc.relay._apply = orig
    pc.relay.stop()
    pc.store.close()
    # 再起動（同じデータの場所）
    pc2, clk2 = make_pc(tmp_path, relay.url)
    assert pc2.relay.version is None  # 起動直後は版数なしで全部受け取る
    pc2.relay.sync_once()
    assert pc2.monitor.scheduler.excluded_bands == {"5"}
    # 再起動後も設定は残る
    pc2.relay.stop()
    pc2.store.close()
    pc3, _ = make_pc(tmp_path, relay.url)
    assert pc3.monitor.scheduler.excluded_bands == {"5"}
    pc3.store.close()
