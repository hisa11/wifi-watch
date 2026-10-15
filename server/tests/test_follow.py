"""自APのchに自chを合わせる（follow.py と controller）。偽の無線で、自APがchを移るところを作る。"""
import json
from pathlib import Path

from wifiwatch import channels as ch
from wifiwatch.app import build
from wifiwatch.clock import FakeClock
from wifiwatch.config import parse_config
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation

ROOT = Path(__file__).resolve().parents[2]
F = ch.parse_channel_ref
OWN = "02:00:00:00:00:01"


class Rec:
    def __init__(self):
        self.sent = []

    def post_json(self, url, data, timeout):
        self.sent.append(data)

    def get(self, url, timeout):
        pass


def make(tmp_path, stations, mut=None):
    d = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    d["data_dir"] = str(tmp_path / "data")
    d["signatures"] = str(ROOT / "signatures.json")
    d["notify"]["ntfy_topic"] = "wifiwatch-test-topic-0123456789"
    if mut:
        mut(d)
    clk = FakeClock()
    poster = Rec()
    app = build(parse_config(d), clk, FakeRadio(clk, Scenario(stations, seed=1)), poster=poster)
    return app, clk, poster


def own_ap(freq, present, bssid=OWN, ssid="OUR-ROBOT-6G"):
    return SimStation(bssid, ssid, F(freq), 20, -48, present=present)


def events(app, kind):
    return [e for e in app.store.events(limit=200) if e["kind"] == kind]


def test_follows_own_ap_across_blocks_and_tells(tmp_path):
    # ロボットの再起動で、自APが ch37（ブロック33〜61）から ch21（ブロック1〜29）へ
    app, clk, poster = make(tmp_path, [own_ap("6g37", [[0, 60]]), own_ap("6g21", [[70, None]])])
    app.monitor.run_for(60)
    assert app.monitor.scheduler.own_freq == F("6g37")
    app.monitor.run_for(240)
    s = app.monitor.scheduler
    assert s.own_freq == F("6g21") and s.block.label.startswith("6GHz ch1〜29")
    assert app.cfg.own.channel == F("6g21")
    info = app.controller.follow_info()
    assert info["last"]["from"] == F("6g37") and info["last"]["to"] == F("6g21") and info["last"]["reserved"] is False
    ev = events(app, "follow")
    assert ev and "予約外" in ev[0]["message"]
    app.monitor.overrides.get("own_channel") == F("6g21")
    # アプリにも知らせる（状態の写し）
    cs = app.controller.compact_state()
    assert cs["own"]["followed"]["to"] == "6GHz ch21" and cs["own"]["followed"]["reserved"] is False
    app.notifier.deliver_due()
    assert any("自chを自APに合わせました" in m.get("title", "") for m in poster.sent)
    app.store.close()


def test_spare_channel_is_reserved_and_not_warned(tmp_path):
    app, clk, poster = make(tmp_path, [own_ap("6g37", [[0, 30]]), own_ap("6g41", [[40, None]])])
    app.monitor.run_for(240)
    assert app.monitor.scheduler.own_freq == F("6g41")
    assert app.controller.follow_info()["last"]["reserved"] is True  # 予備ch（config の spare）
    app.store.close()


def test_two_own_aps_on_different_channels_asks_instead(tmp_path):
    # SSIDだけで見分けているとき、同じ名前の自APが ch21 と ch53 に分かれた：勝手に選ばない
    stations = [own_ap("6g21", [[0, None]], "02:00:00:00:00:01"), own_ap("6g53", [[0, None]], "02:00:00:00:00:09")]
    app, clk, poster = make(tmp_path, stations, lambda d: (d["own"].__setitem__("bssids", []),
                                                           d["own"].__setitem__("station_macs", [])))
    app.monitor.run_for(240)
    assert app.monitor.scheduler.own_freq == F("6g37")  # 動かさない
    p = app.controller.follow_info()["pending"]
    assert p and [c["freq"] for c in p["choices"]] == [F("6g21"), F("6g53")]
    assert events(app, "follow_ambiguous")
    assert app.controller.compact_state()["own"]["follow_pending"] == ["6GHz ch21・20MHz", "6GHz ch53・20MHz"]
    # 画面で選ぶ（手で変える）と、選ばなかった方へは戻らない
    app.controller.set_own(F("6g53"), 20, "desktop")
    app.monitor.run_for(120)
    assert app.monitor.scheduler.own_freq == F("6g53") and app.controller.follow_info()["pending"] is None
    app.store.close()


def test_manual_choice_is_kept_until_the_ap_moves(tmp_path):
    app, clk, poster = make(tmp_path, [own_ap("6g37", [[0, 200]]), own_ap("6g21", [[210, None]])])
    app.monitor.run_for(30)
    app.controller.set_own(F("6g41"), 20, "desktop")  # 手で ch41 に（自APはまだ ch37）
    app.monitor.run_for(150)
    assert app.monitor.scheduler.own_freq == F("6g41")  # 自APが動いていないので戻さない
    app.monitor.run_for(240)
    assert app.monitor.scheduler.own_freq == F("6g21")  # 自APが動いたら合わせる
    app.store.close()


def test_follow_can_be_turned_off(tmp_path):
    app, clk, poster = make(tmp_path, [own_ap("6g37", [[0, 30]]), own_ap("6g21", [[40, None]])])
    app.controller.set_own_follow(False, "desktop")
    app.monitor.run_for(240)
    assert app.monitor.scheduler.own_freq == F("6g37")
    assert app.monitor.overrides.get("own_follow") is False
    app.store.close()
