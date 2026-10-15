"""ローカルAPI：Hostヘッダと独自ヘッダの検査（断る場面と通す場面）、各操作。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wifiwatch import channels as ch
from wifiwatch.api import create_app
from wifiwatch.app import build
from wifiwatch.clock import FakeClock
from wifiwatch.config import parse_config
from wifiwatch.radio.fake import FakeRadio, demo_scenario

ROOT = Path(__file__).resolve().parents[2]
F = ch.parse_channel_ref
W = {"X-Wifiwatch": "1"}


class NullPoster:
    def post_json(self, url, data, timeout):
        pass

    def get(self, url, timeout):
        pass


@pytest.fixture
def env(tmp_path):
    d = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    d["data_dir"] = str(tmp_path / "data")
    d["signatures"] = str(ROOT / "signatures.json")
    cfg = parse_config(d)
    clk = FakeClock()
    app = build(cfg, clk, FakeRadio(clk, demo_scenario()), poster=NullPoster())
    app.monitor.run_for(30)
    client = TestClient(create_app(app.controller, 8080), base_url="http://127.0.0.1:8080")
    yield app, client, clk
    app.store.close()


# ---------------------------------------------------------------- 守り


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080", "127.0.0.1", "localhost"])
def test_allowed_hosts(env, host):
    app, c, clk = env
    assert c.get("/api/state", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8080", "192.168.1.10:8080", "127.0.0.1.nip.io:8080", "localhost:9999", ""])
def test_rejected_hosts_dns_rebinding(env, host):
    app, c, clk = env
    r = c.get("/api/state", headers={"host": host})
    assert r.status_code == 403 and r.json()["error"] == "bad_host"
    r = c.get("/", headers={"host": host})
    assert r.status_code == 403


def test_writes_need_custom_header(env):
    app, c, clk = env
    r = c.post("/api/mode", json={"mode": "PRE"})
    assert r.status_code == 403 and r.json()["error"] == "missing_header"
    r = c.post("/api/mode", json={"mode": "PRE"}, headers={"X-Wifiwatch": "0"})
    assert r.status_code == 403
    assert app.monitor.scheduler.mode == "LIVE"
    r = c.post("/api/mode", json={"mode": "PRE"}, headers=W)
    assert r.status_code == 200 and app.monitor.scheduler.mode == "PRE"


def test_cross_origin_writes_rejected(env):
    app, c, clk = env
    r = c.post("/api/mode", json={"mode": "QUIET"}, headers={**W, "Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    r = c.post("/api/mode", json={"mode": "QUIET"}, headers={**W, "Origin": "http://127.0.0.1:8080"})
    assert r.status_code == 200


def test_preflight_and_no_cors_headers(env):
    app, c, clk = env
    r = c.options("/api/mode", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
                                        "Access-Control-Request-Headers": "x-wifiwatch"})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    r = c.get("/api/state", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_security_headers_and_static(env):
    app, c, clk = env
    r = c.get("/")
    assert r.status_code == 200 and "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert c.get("/static/app.js").status_code == 200


def test_no_docs_endpoints(env):
    app, c, clk = env
    for p in ("/docs", "/openapi.json", "/redoc"):
        assert c.get(p).status_code == 404


# ---------------------------------------------------------------- 操作


def test_state_has_everything_the_dashboard_needs(env):
    app, c, clk = env
    s = c.get("/api/state").json()
    for k in ("mode", "own", "rx", "alerts", "alert_counts", "channels", "stations", "suggestions", "manual", "teams"):
        assert k in s
    assert len(s["channels"]) == 57
    states = {r["state"] for r in s["channels"]}
    assert "own" in states and "patrol" in states
    assert any(st["ssid"] == "OUR-ROBOT-6G" and st["kind"] == "own" for st in s["stations"])
    assert all(len(x["spans"]) >= 1 for x in s["stations"])


def test_manual_scan_flow_creates_record(env):
    app, c, clk = env
    r = c.post("/api/scan", json={"bands": ["6"]}, headers=W)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["total"] == 24
    assert c.post("/api/scan", json={}, headers=W).json()["error"] == "busy"
    s = c.get("/api/state").json()
    assert s["manual"]["state"] in ("queued", "running")
    assert {x["state"] for x in s["channels"] if x["band"] == "2.4"} <= {"paused"}  # 見ていないことが分かる
    app.monitor.run_for(8)
    st = c.get(f"/api/scan/{sid}").json()
    assert st["state"] == "done" and st["record"] == f"scan-{sid}"
    rec = c.get(f"/api/records/scan-{sid}").json()
    assert rec["kind"] == "pc_scan" and rec["origin"] == "desktop" and rec["stations"]
    lst = c.get("/api/records?kind=pc_scan").json()
    assert lst[0]["id"] == f"scan-{sid}" and lst[0]["n_stations"] == len(rec["stations"])


def test_quiet_scan_needs_confirm(env):
    app, c, clk = env
    c.post("/api/mode", json={"mode": "QUIET"}, headers=W)
    r = c.post("/api/scan", json={}, headers=W)
    assert r.status_code == 409 and r.json()["error"] == "needs_confirm"
    assert c.post("/api/scan", json={"confirm": True}, headers=W).status_code == 200


def test_bands_toggle_persist_and_lock(env, tmp_path):
    app, c, clk = env
    r = c.put("/api/bands", json={"excluded_bands": ["2.4"], "excluded_freqs": [F("5g36")]}, headers=W)
    assert r.status_code == 200 and r.json()["excluded_bands"] == ["2.4"]
    r = c.put("/api/bands", json={"excluded_bands": [], "excluded_freqs": [F("6g41")]}, headers=W)
    assert r.status_code == 400  # 自ブロックは外せない
    s = c.get("/api/state").json()
    assert {x["state"] for x in s["channels"] if x["band"] == "2.4"} == {"excluded"}
    ov = json.loads((Path(app.cfg.data_dir) / "overrides.json").read_text())
    assert ov["excluded_bands"] == ["2.4"] and ov["excluded_freqs"] == [F("5g36")]
    ev = c.get("/api/events?category=bands").json()
    assert ev and ev[0]["origin"] == "desktop"


def test_records_label_lww_and_export(env):
    app, c, clk = env
    sid = c.post("/api/scan", json={"bands": ["6"]}, headers=W).json()["id"]
    app.monitor.run_for(8)
    rid = f"scan-{sid}"
    r = c.patch(f"/api/records/{rid}", json={"location": "ピット", "tags": ["下見"], "memo": "テスト"}, headers=W)
    assert r.status_code == 200 and r.json()["applied"]
    # アプリから古い編集が来ても新しい方を残す
    res = app.controller.update_label(rid, {"location": "客席東"}, "app", edited_at=clk.time() - 100)
    assert not res["applied"]
    assert c.get(f"/api/records/{rid}").json()["label"]["location"] == "ピット"
    assert c.get("/api/records?location=ピット").json()[0]["id"] == rid
    assert c.patch(f"/api/records/{rid}", json={"memo": "x" * 2000}, headers=W).status_code == 400
    j = c.get("/api/records/export?format=json")
    assert j.status_code == 200 and json.loads(j.content)[0]["id"] == rid
    cs = c.get("/api/records/export?format=csv")
    assert cs.status_code == 200 and "record_id" in cs.content.decode("utf-8-sig").splitlines()[0]
    assert "attachment" in cs.headers["content-disposition"]


def test_own_channel_change_and_tag(env):
    app, c, clk = env
    r = c.post("/api/own", json={"channel": "6g41", "width": 20}, headers=W)
    assert r.status_code == 200 and app.monitor.scheduler.own_freq == F("6g41")
    assert c.post("/api/own", json={"channel": "6g38", "width": 20}, headers=W).status_code == 400
    r = c.post("/api/tags", json={"bssid": "00:1d:73:11:22:33", "team": "C高専"}, headers=W)
    assert r.status_code == 200
    app.monitor.run_for(2)
    st = c.get("/api/stations/00:1d:73:11:22:33").json()
    assert st["team"] == "C高専" and st["team_source"] == "tag"
    assert app.store.tags()["00:1d:73:11:22:33"] == "C高専"


def test_model_book_api(env):
    app, c, clk = env
    st = c.get("/api/stations/02:a0:00:00:00:01").json()  # KOSEN-A-6G（見本）
    fp = st["model_fp"]
    assert fp and len(fp) == 10
    assert c.post("/api/models", json={"fp": fp, "label": "GL.iNet GL-BE3600"}).status_code == 403  # 独自ヘッダが要る
    assert c.post("/api/models", json={"fp": "zz", "label": "x"}, headers=W).status_code == 400
    r = c.post("/api/models", json={"fp": fp, "label": "GL.iNet GL-BE3600", "bssid": "02:A0:00:00:00:01"}, headers=W)
    assert r.status_code == 200
    st = c.get("/api/stations/02:a0:00:00:00:01").json()
    assert st["model_guess"] == "GL.iNet GL-BE3600" and any("覚え帳" in h for h in st["hints"])
    book = c.get("/api/models").json()
    assert book["entries"][0]["fp"] == fp and book["entries"][0]["now"] >= 1 and book["rules"] > 10
    assert {"label": "GL.iNet GL-BE3600", "category": "other", "fp": [fp]} in book["export"]
    with pytest.raises(Exception):
        app.controller.remember_model(fp, "x", None, "app")  # アプリ・中継からは変えられない
    assert c.post("/api/models", json={"fp": fp, "label": None}, headers=W).status_code == 200
    assert c.get("/api/models").json()["entries"] == []


def test_idempotent_request_ids(env):
    app, c, clk = env
    a = app.controller.manual_scan(bands=["6"], origin="app", request_id="req-1")
    b = app.controller.manual_scan(bands=["6"], origin="app", request_id="req-1")
    assert b["id"] == a["id"] and b.get("duplicate")


def test_desktop_only_operations(env):
    app, c, clk = env
    from wifiwatch.controller import ControlError
    with pytest.raises(ControlError):
        app.controller.set_mode("PRE", origin="app")
    with pytest.raises(ControlError):
        app.controller.set_own(F("6g41"), 20, origin="app")


def test_logs_written(env):
    app, c, clk = env
    app.monitor.run_for(25)
    d = Path(app.cfg.data_dir)
    ch_lines = (d / "channels.csv").read_text(encoding="utf-8").splitlines()
    assert ch_lines[0].startswith("ts,freq,band") and len(ch_lines) >= 1 + 57 * 2
    own = (d / "own.csv").read_text(encoding="utf-8").splitlines()
    assert own[0] == "ts,bssid,ssid,freq,rssi,rssi_avg,seen_ago_s" and "02:00:00:00:00:01" in own[1]
    ev = [json.loads(x) for x in (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ev[0]["category"] == "system" and ev[0]["ts"].endswith("Z")
