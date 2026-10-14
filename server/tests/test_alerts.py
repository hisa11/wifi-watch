"""警報ごとに「鳴るべき場面」と「鳴ってはいけない場面」を確かめる。"""
import copy
import json
from pathlib import Path

import pytest

from wifiwatch import channels as ch
from wifiwatch.analyzer.alerts import AlertManager
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.analyzer.detect import Detector
from wifiwatch.capture import build
from wifiwatch.classify import Classifier, OuiDb, load_signatures
from wifiwatch.clock import FakeClock
from wifiwatch.config import parse_config
from wifiwatch.radio.base import FrameBatch, validate_tuning
from wifiwatch.scheduler import Scheduler

ROOT = Path(__file__).resolve().parents[2]
F = ch.parse_channel_ref
EXAMPLE = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
SIGS = load_signatures(ROOT / "signatures.json")
OUI = OuiDb()
OWN_BSS, OWN_STA = "02:00:00:00:00:01", "02:00:00:00:00:02"


class H:
    def __init__(self, mode="LIVE", mut=None):
        d = copy.deepcopy(EXAMPLE)
        if mut:
            mut(d)
        self.cfg = cfg = parse_config(d)
        self.clk = clk = FakeClock()
        self.an = Analyzer(clk, own_addrs=cfg.own.bssids + cfg.own.station_macs)
        self.sched = Scheduler(cfg.schedule, clk, cfg.own.channel, cfg.own.spare, mode, coldstart=False)
        self.cls = Classifier(cfg, SIGS, OUI)
        self.once = set()
        self.am = AlertManager(clk, levels=cfg.thresholds.levels, once_store=self.once,
                               persist_once=lambda s: self.once.update(s))
        self.rx = {"state": "ok"}
        self.det = Detector(cfg, clk, self.an, self.sched, self.cls, self.am, SIGS, rx_status=lambda: self.rx)
        self.events = []

    def batch(self, freq, width=20, seconds=0.25, frames=()):
        center = freq if width == 20 else ch.block_for(freq, width).center_freq
        t0 = self.clk.monotonic()
        self.clk.advance(seconds)
        self.an.process_batch(FrameBatch(validate_tuning(freq, width, center), t0, self.clk.monotonic(), list(frames)))

    @staticmethod
    def bcn(rx, bssid, ssid, freq, width=20, rssi=-60, **ap):
        return build.radiotap(rx, signal=rssi) + build.beacon(bssid, build.ap_ies(freq, width, ssid, **ap))

    def see(self, bssid, ssid, ref, width=20, rssi=-60, rx=None, **ap):
        f = F(ref)
        rxf = F(rx) if rx else f
        self.batch(rxf, frames=[self.bcn(rxf, bssid, ssid, f, width, rssi, **ap)])

    def tick(self, n=1, dt=1.0):
        for _ in range(n):
            self.clk.advance(dt)
            self.events += self.det.evaluate()

    def active(self):
        return {s.type: s for s in self.am.active()}

    def notified(self, kind=None, type_=None):
        return [e for e in self.events if e.notify and (kind is None or e.kind == kind) and (type_ is None or e.type == type_)]


# ---------------------------------------------------------------- 割当違反


def test_offchannel_fires_for_known_team_outside_reservation():
    h = H()
    h.see("02:a0:00:00:00:02", "KOSEN-A-SPARE", "6g45")
    h.tick()
    a = h.active()
    assert "offchannel" in a and a["offchannel"].scope == "A高専"
    assert "6GHz ch45" in a["offchannel"].message
    assert h.notified("raised", "offchannel")


@pytest.mark.parametrize("ref", ["6g5", "6g9"])
def test_offchannel_silent_on_assigned_or_spare(ref):
    h = H()
    h.see("02:a0:00:00:00:01", "KOSEN-A-6G", ref)
    h.tick()
    assert "offchannel" not in h.active()


def test_offchannel_silent_for_unknown_team():
    h = H()
    h.see("02:c0:00:00:00:01", "SOMEONE", "6g45")
    h.tick()
    assert "offchannel" not in h.active()


def test_manual_tag_turns_unknown_into_team_check():
    h = H()
    h.see("02:c0:00:00:00:01", "SOMEONE", "6g45")
    h.cls.set_tag("02:c0:00:00:00:01", "B高専")
    h.tick()
    assert h.active()["offchannel"].scope == "B高専"


def test_encroach_wide_band_over_other_reservations():
    h = H()
    h.see("02:b0:00:00:00:01", "KOSEN-B-6G", "6g53", width=160)  # ch33〜61：自チームの37・41にかかる
    h.tick()
    a = h.active()
    assert "encroach" in a and a["encroach"].scope == "B高専"
    assert "自チーム" in a["encroach"].message
    assert "offchannel" not in a  # 主chは予約どおり


def test_encroach_silent_within_own_reservations():
    h = H()
    h.see("02:a0:00:00:00:01", "KOSEN-A-6G", "6g5", width=40)  # ch5〜9はA高専の割当と予備
    h.tick()
    assert "encroach" not in h.active()


def test_encroach_320():
    h = H()
    h.see("02:a0:00:00:00:01", "KOSEN-A-6G", "6g5", width=320)
    h.tick()
    p = h.active()["encroach"].parties["02:a0:00:00:00:01"]
    assert set(p["overlaps_teams"]) == {"自チーム", "B高専"}


# ---------------------------------------------------------------- 新規参入×重なり


def warm_up(h):
    """自chを数秒見て、起動直後の局を基準にする。"""
    h.see(OWN_BSS, "OUR-ROBOT-6G", "6g37")
    h.see("02:a0:00:00:00:01", "KOSEN-A-6G", "6g5")
    h.batch(F("6g37"), 160, 2.0)
    h.tick(2)


def test_newcomer_fires_with_team_model_and_strength():
    h = H()
    warm_up(h)
    h.see("00:1d:73:aa:bb:cc", "Buffalo-G-1", "6g37", rssi=-52,
          wps={"manufacturer": "BUFFALO INC.", "model_name": "WXR18000BE10P"})
    h.tick()
    a = h.active()["newcomer"]
    p = a.parties["00:1d:73:aa:bb:cc"]
    assert p["model"] == "BUFFALO INC. WXR18000BE10P" and p["rssi"] == -52
    assert "所属不明" in a.message and "-52dBm" in a.message


def test_newcomer_silent_for_stations_present_at_start():
    h = H()
    h.see("02:c0:00:00:00:09", "ALREADY", "6g37")  # 起動直後に見えた局
    h.tick(3)
    assert "newcomer" not in h.active()


def test_newcomer_silent_when_not_overlapping_own_band():
    h = H()
    warm_up(h)
    h.batch(F("6g69"), 20, 1.0)
    h.tick()
    h.see("02:c0:00:00:00:08", "FAR", "6g69")
    h.tick()
    assert "newcomer" not in h.active()


def test_newcomer_on_channel_move_into_own_band():
    h = H()
    warm_up(h)
    h.see("02:c0:00:00:00:07", "MOVER", "6g69")
    h.tick(6)
    assert "newcomer" not in h.active()
    h.see("02:c0:00:00:00:07", "MOVER", "6g37")
    h.tick()
    assert "02:c0:00:00:00:07" in h.active()["newcomer"].parties


def test_newcomer_on_return_after_10min_and_expires():
    h = H()
    h.see("02:c0:00:00:00:06", "BACK", "6g37")
    warm_up(h)
    h.tick()
    assert "newcomer" not in h.active()
    h.tick(660)  # 11分いない
    h.see("02:c0:00:00:00:06", "BACK", "6g37")
    h.tick()
    assert "newcomer" in h.active()
    for _ in range(20):  # 10分たてば新規ではない（いる間は見え続ける）
        h.see("02:c0:00:00:00:06", "BACK", "6g37")
        h.tick(31)
    assert "newcomer" not in h.active()


def test_newcomer_ignores_own_and_known_ok():
    h = H(mut=lambda d: d.__setitem__("known_ok_bssids", ["02:99:00:00:00:01"]))
    warm_up(h)
    h.see("02:99:00:00:00:01", "PIT-AP", "6g37")
    h.see(OWN_BSS, "OUR-ROBOT-6G", "6g37")
    h.tick()
    assert "newcomer" not in h.active()


# ---------------------------------------------------------------- 正体不明の強力局・広帯域局


def test_unknown_wide_station():
    h = H()
    h.see("02:c0:00:00:00:01", "WIFI7-ROUTER", "6g49", width=320, rssi=-70, dup_beacon=True)
    h.tick()
    a = h.active()["unknown_strong"]
    assert "広帯域320MHz" in a.parties["02:c0:00:00:00:01"]["reasons"]


def test_known_team_wide_is_not_unknown():
    h = H()
    h.see("02:a0:00:00:00:01", "KOSEN-A-6G", "6g5", width=160)
    h.tick()
    assert "unknown_strong" not in h.active()


def test_unknown_strong_relative_to_median():
    h = H()
    for i, ref in enumerate(["6g1", "6g5", "6g9", "6g13", "6g17", "6g21"]):
        h.see(f"02:a0:00:00:01:{i:02x}", f"X{i}", ref, rssi=-75)
    h.see("02:c0:00:00:00:02", "LOUD", "6g25", rssi=-52)  # 中央値-75より23dB強い
    h.see("02:c0:00:00:00:03", "MILD", "6g29", rssi=-65)  # 10dB
    h.tick()
    ps = h.active()["unknown_strong"].parties
    assert "02:c0:00:00:00:02" in ps and "02:c0:00:00:00:03" not in ps


def test_unknown_strong_absolute_when_few_stations():
    h = H()
    h.see("02:c0:00:00:00:02", "LOUD", "6g25", rssi=-44)
    h.see("02:c0:00:00:00:03", "MILD", "6g29", rssi=-50)
    h.tick()
    ps = h.active()["unknown_strong"].parties
    assert list(ps) == ["02:c0:00:00:00:02"]


# ---------------------------------------------------------------- 署名（RoboconFree・CineEye）


def test_roboconfree_6ghz_notifies_once_even_after_restart():
    h = H()
    h.see("02:1d:00:00:01:01", "ROBOCON_Free_Wi-Fi", "5g100")
    h.tick()
    assert not h.am.active()  # 5GHzは既知の背景
    h.see("02:1d:00:00:06:01", "ROBOCON_Free_Wi-Fi", "6g5")
    h.tick()
    assert "sig6_once" in h.active()
    assert len(h.notified("raised", "sig6_once")) == 1
    h.tick(700)  # いなくなる → 戻る
    h.see("02:1d:00:00:06:01", "ROBOCON_Free_Wi-Fi", "6g5")
    h.tick()
    assert "sig6_once" in h.active()
    assert len(h.notified(type_="sig6_once")) == 1
    # 再起動しても覚えている
    h2 = H()
    h2.am.once_done = set(h.once)
    h2.see("02:1d:00:00:06:02", "ROBOCON_Free_Wi-Fi", "6g9")
    h2.tick()
    assert "sig6_once" in h2.active() and not h2.notified(type_="sig6_once")


def test_cineeye_6ghz_is_crit_and_5ghz_silent():
    h = H()
    h.see("02:6c:00:00:00:01", "CineEye2-1234", "5g44", width=40, rssi=-31, standard="ac")
    h.tick()
    assert "sig6_crit" not in h.active()
    h.see("02:6c:00:00:00:02", "CineEye2-9999", "6g69", width=160, rssi=-35)
    h.tick()
    a = h.active()["sig6_crit"]
    assert a.level == "crit" and "CineEye" in a.message
    assert "unknown_strong" not in h.active()  # 署名で正体が分かっている


# ---------------------------------------------------------------- 活動量・リトライ率


def data_frames(n, retry_every=0, rx="6g37"):
    f = F(rx)
    return [build.radiotap(f, signal=-60) + build.data("02:c0:00:00:00:01", "02:aa:00:00:00:01", True,
                                                      retry=bool(retry_every) and i % retry_every == 0)
            for i in range(n)]


def run_activity(h, rate, seconds, width=160):
    for _ in range(int(seconds)):
        h.batch(F("6g37"), width, 0.7, data_frames(int(rate * 0.7)))
        h.clk.advance(0.3)
        h.events += h.det.evaluate()


def test_activity_spike_fires():
    h = H()
    run_activity(h, 50, 300)
    assert "activity" not in h.active()
    run_activity(h, 400, 15)
    a = h.active()["activity"]
    assert a.data["rate"] >= 300 and a.data["width"] == 160


def test_activity_steady_high_is_silent():
    h = H()
    run_activity(h, 400, 300)
    assert "activity" not in h.active()


def test_activity_below_floor_is_silent():
    h = H()
    run_activity(h, 5, 200)
    run_activity(h, 60, 15)  # 12倍でも100/秒未満
    assert "activity" not in h.active()


def test_pre_to_live_switch_does_not_fire_activity():
    """PRE（20MHzで250ms）からLIVE（160MHzで長く）に変えても鳴らない（受信秒数で割り、鍵も別）。"""
    h = H(mode="PRE")
    for _ in range(40):  # PREでは自chは14秒に250msだけ
        h.batch(F("6g37"), 20, 0.25, data_frames(25))
        h.tick(14)
    h.sched.set_mode("LIVE")
    run_activity(h, 100, 120)
    assert "activity" not in h.active()
    assert not [e for e in h.events if e.type == "activity"]


def test_retry_rate():
    h = H()
    h.batch(F("6g37"), 160, 5.0, data_frames(60, retry_every=2))  # 50%
    h.tick()
    assert h.active()["retry"].data["rate"] == 0.5


def test_retry_needs_50_frames():
    h = H()
    h.batch(F("6g37"), 160, 5.0, data_frames(40, retry_every=1))
    h.tick()
    assert "retry" not in h.active()


def test_retry_low_rate_silent():
    h = H()
    h.batch(F("6g37"), 160, 5.0, data_frames(100, retry_every=5))  # 20%
    h.tick()
    assert "retry" not in h.active()


def test_own_frames_do_not_count_for_activity_or_retry():
    h = H()
    own = [build.radiotap(F("6g37"), signal=-50) + build.data(OWN_BSS, OWN_STA, True, retry=True) for _ in range(500)]
    run_activity(h, 20, 300)
    h.batch(F("6g37"), 160, 3.0, own)
    h.tick()
    assert "retry" not in h.active() and "activity" not in h.active()


# ---------------------------------------------------------------- 同名SSID・deauth


def test_ssid_clone_is_crit():
    h = H()
    h.see(OWN_BSS, "OUR-ROBOT-6G", "6g37")
    h.tick()
    assert "ssid_clone" not in h.active()
    h.see("02:66:66:66:66:66", "OUR-ROBOT-6G", "6g41")
    h.tick()
    a = h.active()["ssid_clone"]
    assert a.level == "crit" and "02:66:66:66:66:66" in a.parties


def deauths(n, dst=OWN_STA, src=OWN_BSS, rx="6g37"):
    f = F(rx)
    return [build.radiotap(f, signal=-50) + build.deauth(dst, src, src, reason=7) for _ in range(n)]


def test_deauth_flood_warn_on_6ghz_pmf():
    h = H()
    h.batch(F("6g37"), 160, 2.0, deauths(12))
    h.tick()
    a = h.active()["deauth"]
    assert a.level == "warn" and a.data["count"] == 12 and a.data["pmf_required"]


def test_deauth_flood_crit_without_pmf():
    def mut(d):
        d["own"]["channel"] = "5g36"
        d["own"]["spare"] = []
        d["own"]["pmf_required"] = False
    h = H(mut=mut)
    h.batch(F("5g36"), 160, 2.0, deauths(12, rx="5g36"))
    h.tick()
    assert h.active()["deauth"].level == "crit"


def test_deauth_below_count_or_not_mine_silent():
    h = H()
    h.batch(F("6g37"), 160, 2.0, deauths(9))
    h.batch(F("6g37"), 160, 0.5, deauths(20, dst="02:55:00:00:00:01", src="02:55:00:00:00:02"))
    h.tick()
    assert "deauth" not in h.active()


def test_deauth_window_is_3_seconds():
    h = H()
    for _ in range(5):  # 2秒おきに4個：どの3秒の窓でも8個以下
        h.batch(F("6g37"), 160, 2.0, deauths(4))
        h.events += h.det.evaluate()
    assert "deauth" not in h.active()


# ---------------------------------------------------------------- 受信停止


def test_rx_stop_alert_from_monitor_state():
    h = H()
    h.tick()
    assert "rx_stop" not in h.active()
    h.rx = {"state": "failed", "reason": "テスト"}
    h.tick()
    assert h.active()["rx_stop"].level == "crit"
    h.rx = {"state": "ok"}
    h.tick(11)
    assert "rx_stop" not in h.active()


# ---------------------------------------------------------------- 抑制・追加・解消


def test_suppression_addition_and_resolution():
    h = H()
    h.see("02:a0:00:00:00:02", "KOSEN-A-X", "6g45")
    h.tick()
    assert len(h.notified("raised", "offchannel")) == 1
    # 当事者が増えたら30秒以内でも「追加」
    h.see("02:a0:00:00:00:03", "KOSEN-A-Y", "6g49")
    h.tick()
    adds = h.notified("added", "offchannel")
    assert len(adds) == 1 and adds[0].new_parties == ["02:a0:00:00:00:03"]
    # 条件が消える（局がいなくなる）→10秒で解消。ただし発生の通知から30秒は解消を知らせない
    h.tick(200)
    assert "offchannel" not in h.active()
    resolved = h.notified("resolved", "offchannel")
    assert len(resolved) == 1


def test_flapping_within_30s_is_not_renotified():
    h = H()
    st = "02:a0:00:00:00:02"
    h.see(st, "KOSEN-A-X", "6g45")
    h.tick()
    h.am.clear_after = 10
    # いなくなったことにする（局の表から最後の時刻を古くする）
    h.an.table.get(st).last_seen_mono -= 1000
    h.tick(11)  # 解消（cleared）。まだ30秒たっていないので解消は知らせない
    assert "offchannel" not in h.active()
    assert not h.notified("resolved", "offchannel")
    h.see(st, "KOSEN-A-X", "6g45")  # すぐ戻る
    h.tick()
    assert "offchannel" in h.active()
    assert len(h.notified("raised", "offchannel")) == 1  # 発生は1回だけ
    assert not h.notified("resolved", "offchannel")


def test_level_override_from_config():
    h = H(mut=lambda d: d.setdefault("thresholds", {}).__setitem__("levels", {"offchannel": "crit"}))
    h.see("02:a0:00:00:00:02", "KOSEN-A-X", "6g45")
    h.tick()
    assert h.active()["offchannel"].level == "crit"
