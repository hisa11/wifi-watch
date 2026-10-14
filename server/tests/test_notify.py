"""通知：偽のHTTPサーバで、回線が切れても警報が失われず、戻ったら届くことを確かめる。"""
import copy
import json
from pathlib import Path

import pytest

from tests.fakehttp import FakeHttp
from wifiwatch.analyzer.alerts import AlertEvent
from wifiwatch.clock import FakeClock
from wifiwatch.config import NotifyConfig
from wifiwatch.notify import Buzzer, Notifier
from wifiwatch.store import Store

TOPIC = "wifiwatch-test-topic-0123456789"


@pytest.fixture
def srv():
    s = FakeHttp().start()
    yield s
    s.stop()


def ncfg(srv, **kw):
    d = dict(ntfy_server=srv.url, ntfy_topic=TOPIC, discord_webhook=srv.url + "/discord",
             healthcheck_url=srv.url + "/ping", timeout_s=2)
    d.update(kw)
    return NotifyConfig(**d)


def ev(kind="raised", level="warn", type_="offchannel", msg="A高専：X が 6GHz ch45 に出ています", parties=None):
    return AlertEvent(1_791_936_000.0, kind, type_, f"{type_}:x", level, "割当違反（予約外ch）", msg,
                      parties or [{"ssid": "X", "ch": "6GHz ch45", "bssid": "02:00:00:00:00:09"}], True)


def make(tmp_path, srv, **kw):
    clk = FakeClock()
    store = Store(tmp_path / "w.db")
    buzz = tmp_path / "tty"
    buzz.write_bytes(b"")
    n = Notifier(ncfg(srv, **kw), store, clk, buzzer=Buzzer(str(buzz)))
    return clk, store, n, buzz


def test_levels_route_and_priority(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv)
    n.handle_alert(ev(level="warn"))
    n.handle_alert(ev(level="crit", type_="rx_stop", msg="受信停止"))
    n.handle_alert(ev(kind="resolved", level="warn"))
    n.handle_alert(ev(kind="added", level="warn"))
    n.heartbeat()
    n.deliver_due()
    ntfy = srv.posts("/")
    assert [p["priority"] for p in ntfy] == [4, 5, 2, 4, 1]
    assert ntfy[0]["title"] == "警告｜割当違反（予約外ch）" and ntfy[0]["topic"] == TOPIC
    assert ntfy[1]["title"].startswith("緊急｜") and "ピットから見えたもの" in ntfy[1]["message"]
    assert ntfy[2]["title"].startswith("解消｜")
    assert ntfy[3]["title"].startswith("追加｜警告｜") and "増えた当事者" in ntfy[3]["message"]
    discord = srv.posts("/discord")
    assert len(discord) == 1 and "受信停止" in discord[0]["content"]  # critだけDiscordにも
    assert discord[0]["allowed_mentions"] == {"parse": []}
    assert buzz.read_bytes() == b"C"  # critで1回だけ鳴る


def test_not_notify_events_are_ignored(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv)
    e = ev()
    e.notify = False
    n.handle_alert(e)
    n.deliver_due()
    assert srv.posts() == []


def test_outage_then_recovery_loses_nothing_even_across_restart(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv)
    port = srv.port
    srv.stop()  # 回線が切れた（接続できない）
    for i in range(3):
        n.handle_alert(ev(level="warn", msg=f"warn{i}"))
    n.handle_alert(ev(level="crit", msg="crit0"))
    for _ in range(20):
        clk.advance(30)
        n.deliver_due()
    assert store.notify_backlog() == 5  # ntfy 4 + discord 1、無期限に溜まっている
    # PCを再起動（同じDBで作り直す）
    store.close()
    store2 = Store(tmp_path / "w.db")
    n2 = Notifier(n.cfg, store2, clk, buzzer=Buzzer(None))
    srv.start(port)  # 回線が戻る
    clk.advance(120)
    n2.deliver_due()
    msgs = [p["message"].split("\n")[0] for p in srv.posts("/")]
    assert msgs == ["warn0", "warn1", "warn2", "crit0"]  # 入れた順に全部届く
    assert len(srv.posts("/discord")) == 1
    assert store2.notify_backlog() == 0


def test_server_errors_back_off_and_retry(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv)
    srv.mode = "fail"
    n.handle_alert(ev())
    n.deliver_due()
    row = store.notify_pending(clk.time())[0]
    assert row["attempts"] == 1 and row["next_at"] > clk.time()
    n.deliver_due()  # 待ち時間の前は送らない
    assert store.notify_pending(clk.time())[0]["attempts"] == 1
    for _ in range(8):
        clk.advance(61)
        n.deliver_due()
    assert store.notify_pending(clk.time())[0]["attempts"] >= 8
    srv.mode = "ok"
    clk.advance(61)
    n.deliver_due()
    assert len(srv.posts("/")) == 1 and store.notify_backlog() == 0


def test_429_respects_retry_after(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv)
    srv.mode = "429"
    n.handle_alert(ev())
    n.deliver_due()
    row = store.notify_pending(clk.time())[0]
    assert row["next_at"] - clk.time() >= 5


def test_heartbeat_expires_and_is_replaced(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv, discord_webhook=None)
    srv.stop()
    n.heartbeat()
    clk.advance(600)
    n.heartbeat()  # 古い稼働中は捨てて新しいものに置き換える
    assert store.notify_backlog() == 1
    clk.advance(16 * 60)
    n.deliver_due()
    assert store.notify_backlog() == 0  # 15分で捨てる（critやwarnとは違う）


def test_tick_schedules_heartbeat_and_health_ping(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv)
    for _ in range(31 * 60):
        n.tick()
        clk.advance(1)
    hb = [p for p in srv.posts("/") if p["priority"] == 1]
    assert len(hb) == 3  # 起動時と15分ごと
    pings = [r for r in srv.requests if r[0] == "GET" and r[1] == "/ping"]
    assert 30 <= len(pings) <= 32  # 1分ごと
    assert n.health_ok is True


def test_health_ping_failure_is_not_queued(tmp_path, srv):
    clk, store, n, buzz = make(tmp_path, srv, ntfy_topic=None, discord_webhook=None)
    srv.stop()
    n.ping_health()
    assert n.health_ok is False and store.notify_backlog() == 0


def test_buzzer_missing_device_does_not_raise(tmp_path):
    b = Buzzer(str(tmp_path / "nope" / "ttyACM0"))
    assert b.beep() is False and b.last_error


def test_end_to_end_fake_radio_to_ntfy_and_discord(tmp_path, srv):
    """偽の無線でCineEyeが6GHzに出る → crit → ntfyとDiscordに届き、警報音が鳴る。"""
    from wifiwatch import channels as ch
    from wifiwatch.classify import OuiDb
    from wifiwatch.config import Overrides, parse_config
    from wifiwatch.monitor import Monitor
    from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation

    root = Path(__file__).resolve().parents[2]
    d = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    d["signatures"] = str(root / "signatures.json")
    d["notify"] = {"ntfy_server": srv.url, "ntfy_topic": TOPIC, "discord_webhook": srv.url + "/discord", "timeout_s": 2}
    cfg = parse_config(d)
    clk = FakeClock()
    sts = [SimStation("02:6c:5a:00:00:09", "CineEye2-6G", ch.parse_channel_ref("6g69"), 160, -35),
           SimStation("02:52:46:00:00:01", "venue", ch.parse_channel_ref("5g36"), 20, -60)]
    m = Monitor(cfg, clk, FakeRadio(clk, Scenario(sts, seed=1)), Overrides(tmp_path / "o.json"), oui=OuiDb("/x"))
    store = Store(tmp_path / "w.db")
    buzz = tmp_path / "tty"
    buzz.write_bytes(b"")
    n = Notifier(cfg.notify, store, clk, buzzer=Buzzer(str(buzz)))
    m.on_alert_event.append(n.handle_alert)
    m.run_for(60)
    n.deliver_due()
    crit = [p for p in srv.posts("/") if p["priority"] == 5]
    assert crit and "CineEye" in crit[0]["title"]
    assert srv.posts("/discord") and buzz.read_bytes().startswith(b"C")
