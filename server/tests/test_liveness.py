"""受信停止の検知と復旧を、偽の無線で通しで確かめる。"""
import copy
import json
from pathlib import Path

from wifiwatch import channels as ch
from wifiwatch.classify import OuiDb
from wifiwatch.clock import FakeClock
from wifiwatch.config import Overrides, parse_config
from wifiwatch.monitor import Monitor
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation

ROOT = Path(__file__).resolve().parents[2]
F = ch.parse_channel_ref
EXAMPLE = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
OUI = OuiDb("/nonexistent")


def stations(six=False):
    out = [SimStation(f"02:1d:00:00:01:{i:02x}", "ROBOCON_Free_Wi-Fi", f, 20, -65, data_rate=20)
           for i, f in enumerate(ch.FREQS_BY_BAND["5"])]
    out.append(SimStation("02:1d:00:00:02:01", "venue-2g", F("2g6"), 20, -60, standard="n"))
    if six:
        out.append(SimStation("02:00:00:00:00:01", "OUR-ROBOT-6G", F("6g37"), 20, -50))
    return out


def make(tmp_path, mode="LIVE", sts=None, mut=None):
    d = copy.deepcopy(EXAMPLE)
    d["signatures"] = str(ROOT / "signatures.json")
    if mut:
        mut(d)
    cfg = parse_config(d)
    clk = FakeClock()
    ov = Overrides(tmp_path / "overrides.json")
    ov.update(mode=mode)
    radio = FakeRadio(clk, Scenario(sts if sts is not None else stations(), seed=4, noise_db=0.5))
    m = Monitor(cfg, clk, radio, ov, oui=OUI)
    events = []
    m.on_alert_event.append(events.append)
    return m, radio, events


def test_quiet_6ghz_is_not_receive_stop(tmp_path):
    """6GHzに誰もいなくても（自chが静かでも）受信停止にはしない。"""
    m, radio, events = make(tmp_path)
    m.run_for(120)
    assert m.liveness.state == "ok" and radio.calls == []
    assert not [e for e in events if e.type == "rx_stop"]


def test_stall_fixed_by_monitor_reset(tmp_path):
    m, radio, events = make(tmp_path)
    m.run_for(30)
    radio.stalled = True
    radio.stall_fix = "monitor"
    m.run_for(60)
    assert radio.calls[:1] == ["reset_monitor"]
    assert "reload_driver" not in radio.calls
    assert m.liveness.state == "ok"
    assert not [e for e in events if e.type == "rx_stop"]


def test_stall_fixed_by_driver_reload_with_lar_scan(tmp_path):
    m, radio, events = make(tmp_path)
    m.run_for(30)
    radio.stalled = True
    radio.stall_fix = "reload"
    radio.reload_disables_six = True  # 読み込み直し直後は6GHzが無効（管理モードでスキャンすると戻る）
    m.run_for(90)
    assert radio.calls[:5] == ["reset_monitor", "reload_driver", "managed_scan", "regulatory", "set_monitor"]
    assert m.liveness.state == "ok" and radio.six_enabled
    assert not [e for e in events if e.type == "rx_stop"]
    # 戻ったあと自ブロック（6GHz）をまた受信している
    assert any(r.slot.kind == "own" and not r.error for r in list(m.engine.slot_log)[-20:])


def test_unrecoverable_stall_raises_crit_and_clears_when_back(tmp_path):
    m, radio, events = make(tmp_path)
    m.run_for(30)
    radio.stalled = True
    radio.stall_fix = "never"
    m.run_for(120)
    assert m.liveness.state == "failed"
    raised = [e for e in events if e.type == "rx_stop" and e.kind == "raised"]
    assert raised and raised[0].level == "crit" and raised[0].notify
    radio.stalled = False
    m.run_for(60)
    assert m.liveness.state == "ok"
    assert not [a for a in m.alerts.active() if a.type == "rx_stop"]


def test_quiet_mode_uses_30s_probes(tmp_path):
    m, radio, events = make(tmp_path, mode="QUIET")
    m.run_for(45)
    # 直前の生存確認のすぐ後で止める
    last = max(r.t_end for r in m.engine.slot_log if r.slot.kind == "liveness")
    m.run_for(max(0.0, last + 1 - m.clock.monotonic()))
    radio.stalled = True
    radio.stall_fix = "never"
    m.run_for(40)  # 空振りは1回だけ（30秒の周期が2回つづくまでは何もしない）
    assert radio.calls == []
    m.run_for(30)
    assert radio.calls[:1] == ["reset_monitor"]
    m.run_for(120)
    assert m.liveness.state == "failed"


def test_own_block_tune_failure_triggers_recovery(tmp_path):
    m, radio, events = make(tmp_path)
    m.run_for(20)
    radio.six_enabled = False  # 6GHzが無効になった
    m.run_for(60)
    assert "reset_monitor" in radio.calls and "managed_scan" in radio.calls
    assert radio.six_enabled  # 管理モードでスキャンして有効に戻った


def test_liveness_channel_is_busiest_5ghz(tmp_path):
    sts = [SimStation(f"02:1d:00:00:03:{i:02x}", f"AP{i}", F("5g100"), 20, -60) for i in range(5)]
    sts.append(SimStation("02:1d:00:00:04:01", "ONE", F("5g36"), 20, -60))
    m, radio, events = make(tmp_path, mode="QUIET", sts=sts)
    m.analyzer.table  # 先に5GHzの局を表に載せる（PREで1周）
    m.scheduler.set_mode("PRE")
    m.run_for(15)
    m.scheduler.set_mode("QUIET")
    m.run_for(65)
    live = [r.slot.freq for r in m.engine.slot_log if r.slot.kind == "liveness"]
    assert live and live[-1] == F("5g100")


def test_pre_calibration_after_5_minutes(tmp_path):
    sts = stations(six=True) + [SimStation(f"02:6a:00:00:00:{i:02x}", f"S{i}", F(f"6g{n}"), 20, -70 + i)
                                for i, n in enumerate((1, 5, 9, 13, 17, 21))]
    m, radio, events = make(tmp_path, mode="PRE", sts=sts)
    cals = []
    m.on_calibration.append(cals.append)
    m.run_for(290)
    assert not cals
    m.run_for(30)
    assert len(cals) == 1
    c = cals[0]
    assert c["rounds"] >= 1 and c["bands"]["6"]["stations"] == 7 and c["bands"]["5"]["stations"] == 20
    assert c["hints"]
    m.run_for(300)
    assert len(cals) == 1  # 同じPREの間は1回だけ


def test_rfkill_is_reported_instead_of_recovery_and_resumes(tmp_path):
    # 開発PCでモニタモードの間に、画面のWi-Fiのスイッチ（RF-kill）で無線が止められた（実際に起きた）
    m, radio, events = make(tmp_path)
    blocked = {"v": None}
    m.liveness.rfkill = lambda: blocked["v"]
    notes = []
    m.liveness.on_change.append(notes.append)
    m.run_for(30)
    radio.stalled = True
    radio.stall_fix = "monitor"
    blocked["v"] = "soft"
    m.run_for(240)
    assert m.liveness.state == "failed" and "RF-kill" in m.liveness.reason and "ソフト" in m.liveness.reason
    assert radio.calls == []  # モニタの再設定もドライバの読み込み直しもしない（効かない）
    assert len([n for n in notes if n["what"] == "failed"]) == 1  # 同じ失敗を周期ごとに記録しない
    assert [e for e in events if e.type == "rx_stop"]
    blocked["v"] = None  # Wi-Fi をオンに戻した
    m.run_for(60)
    assert radio.calls[:1] == ["reset_monitor"] and m.liveness.state == "ok"


def test_failed_is_recorded_once(tmp_path):
    m, radio, events = make(tmp_path)
    notes = []
    m.liveness.on_change.append(notes.append)
    m.run_for(30)
    radio.stalled = True
    radio.stall_fix = None
    m.run_for(200)  # 5分の再試行より短い間：失敗の記録は1回だけ
    assert m.liveness.state == "failed"
    assert len([n for n in notes if n["what"] == "failed"]) == 1
