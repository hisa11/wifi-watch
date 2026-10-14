"""巡回を仕様書の表と照合する（偽の時計で数十分ぶんを回す）。"""
import math
from collections import defaultdict

import pytest

from wifiwatch import channels as ch
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.clock import FakeClock
from wifiwatch.config import ScheduleConfig
from wifiwatch.engine import Engine
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation
from wifiwatch.scheduler import Busy, NeedsConfirm, Scheduler, SchedulerError

F = ch.parse_channel_ref
OWN = F("6g37")


def make(mode="LIVE", spare=(), stations=(), coldstart=False, **kw):
    clk = FakeClock()
    sched = Scheduler(ScheduleConfig(), clk, OWN, spare=spare, mode=mode, coldstart=coldstart, **kw)
    radio = FakeRadio(clk, Scenario(list(stations), seed=2, noise_db=0.5))
    eng = Engine(clk, radio, Analyzer(clk), sched)
    return clk, sched, eng


def visits(eng, kinds=None):
    v = defaultdict(list)
    for r in eng.slot_log:
        if kinds is None or r.slot.kind in kinds:
            v[r.slot.freq].append(r.t_start)
    return v


def max_gap(times):
    return max(b - a for a, b in zip(times, times[1:]))


def gaps_by_group(sched, eng):
    g = sched.groups()
    v = visits(eng, kinds={"in_block", "six", "five", "two4", "spare"})
    out = {}
    for k, fs in g.items():
        if fs:
            out[k] = max(max_gap(v[f]) for f in fs)
    return out


def test_live_allocation_and_own_ratio():
    clk, sched, eng = make()
    assert sched.allocation() == {"in_block": 4, "six": 4, "five": 3, "two4": 1}
    assert sched.own_ratio() == pytest.approx(0.70)
    eng.run_for(300)
    own = sum(r.receive_s for r in eng.slot_log if r.slot.kind == "own")
    total = clk.monotonic() - eng.slot_log[0].t_start
    assert 0.68 <= own / total <= 0.70  # 切替（30ms×4回/周期）を除いた実際の受信時間


def test_live_revisit_matches_spec_table():
    clk, sched, eng = make()
    eng.run_for(600)
    gaps = gaps_by_group(sched, eng)
    # 仕様書：自ブロック内の別ch 約20秒、6GHzのその他 40秒、5GHz 約70秒、2.4GHz 130秒
    assert gaps["in_block"] <= 20.5
    assert 39.5 <= gaps["six"] <= 40.5
    assert 60 <= gaps["five"] <= 70.5
    assert 129.5 <= gaps["two4"] <= 130.5
    # 自chの空白（巡回で離れている時間）は短い
    own_starts = [r for r in eng.slot_log if r.slot.kind == "own"]
    assert max(b.t_start - (a.t_start + a.slot.dwell) for a, b in zip(own_starts, own_starts[1:])) <= 0.76
    # 全部の6GHzのchを見る
    seen = {r.slot.freq for r in eng.slot_log}
    assert set(ch.FREQS_BY_BAND["6"]) <= seen


def test_quiet_ratio_and_liveness():
    clk, sched, eng = make(mode="QUIET")
    assert sched.own_ratio() == pytest.approx(0.8917, abs=0.001)  # 仕様書「約89%」
    eng.run_for(300)
    kinds = {r.slot.kind for r in eng.slot_log}
    assert kinds == {"own", "in_block", "liveness"}
    live = [r.t_start for r in eng.slot_log if r.slot.kind == "liveness"]
    assert max_gap(live) == pytest.approx(30, abs=0.5)
    assert all(ch.BY_FREQ[r.slot.freq].band == "5" for r in eng.slot_log if r.slot.kind == "liveness")
    ib = visits(eng, {"in_block"})
    assert len(ib) == 7


def test_pre_round_is_about_14s():
    clk, sched, eng = make(mode="PRE")
    eng.run_for(14.25 * 3)
    assert sched.pre_rounds >= 2
    pre = [r for r in eng.slot_log if r.slot.kind == "pre"]
    first_round = pre[:57]
    assert len({r.slot.freq for r in first_round}) == 57
    assert first_round[-1].t_start + 0.25 - first_round[0].t_start == pytest.approx(14.25)
    assert [r.slot.freq for r in first_round[:8]] == sorted(ch.own_block(OWN).freqs, key=lambda f: (f != OWN, f))


def test_manual_scan_durations_and_resume():
    st = SimStation("02:00:00:00:00:21", "PSC21", F("6g21"), 20, -50)
    clk, sched, eng = make(stations=[st])
    records = []
    eng.on_scan_record.append(records.append)
    eng.run_for(5)
    m = sched.request_manual(origin="app", request_id="r1")
    assert len(m.freqs) == 57
    t0 = clk.monotonic()
    while sched.manual_running:
        eng.step()
    # 枠の途中で依頼が来た場合の端数を除いて、全57chで約14秒
    dur = clk.monotonic() - t0
    assert 14.25 <= dur <= 14.25 + 7.0 + 0.01
    manual_time = sum(r.slot.dwell for r in eng.slot_log if r.slot.kind == "manual")
    assert manual_time == pytest.approx(14.25)
    assert records and records[0]["origin"] == "app" and records[0]["request_id"] == "r1"
    assert any(s["ssid"] == "PSC21" and s["freq"] == F("6g21") for s in records[0]["stations"])
    assert len(records[0]["channels"]) == 57
    # 元のモード（LIVE）に戻る
    eng.run_for(10)
    assert {r.slot.kind for r in list(eng.slot_log)[-10:]} >= {"own"}
    # 6GHzだけなら約6秒
    m2 = sched.request_manual(bands=["6"])
    n_before = len([r for r in eng.slot_log if r.slot.kind == "manual"])
    while sched.manual_running:
        eng.step()
    n_after = len([r for r in eng.slot_log if r.slot.kind == "manual"])
    assert (n_after - n_before) * 0.25 == pytest.approx(6.0)
    assert len(m2.freqs) == 24


def test_manual_scan_one_at_a_time_and_quiet_confirm():
    clk, sched, eng = make(mode="QUIET")
    with pytest.raises(NeedsConfirm):
        sched.request_manual()
    sched.request_manual(confirm=True)
    with pytest.raises(Busy):
        sched.request_manual(confirm=True)
    clk, sched, eng = make()
    with pytest.raises(SchedulerError):
        sched.request_manual(freqs=[1234])
    with pytest.raises(SchedulerError):
        sched.request_manual(bands=["7"])
    assert not sched.manual_running


def test_manual_default_targets_skip_excluded_but_explicit_allowed():
    clk, sched, eng = make()
    sched.set_exclusions(["2.4"], [F("5g36")])
    m = sched.request_manual()
    assert not any(ch.BY_FREQ[f].band == "2.4" for f in m.freqs) and F("5g36") not in m.freqs
    while sched.manual_running:
        eng.step()
    m2 = sched.request_manual(bands=["2.4"])
    assert len(m2.freqs) == 13


def test_band_off_redistributes_and_keeps_in_block_and_liveness():
    clk, sched, eng = make()
    sched.set_exclusions(["2.4"], [])
    a = sched.allocation()
    assert a["in_block"] == 4 and "two4" not in a
    assert sum(a.values()) == 12 and a["five"] == 4  # 2.4GHzの1枠が5GHzへ
    sched.set_exclusions(["2.4", "5"], [])
    a = sched.allocation()
    assert a == {"in_block": 4, "six": 8}
    eng.run_for(120)
    live = [r.t_start for r in eng.slot_log if r.slot.kind == "liveness"]
    assert len(live) >= 3 and max_gap(live) == pytest.approx(30, abs=0.5)  # 生存確認は止めない
    assert not any(ch.BY_FREQ[r.slot.freq].band == "2.4" for r in eng.slot_log)
    # 6GHzも外すと、自ブロックと自ブロック内の別chと生存確認だけ
    sched.set_exclusions(["2.4", "5", "6"], [])
    assert sched.allocation() == {"in_block": 4}
    assert sched.own_ratio() == pytest.approx((10 - 1 - 0.25 / 3) / 10)


def test_own_block_cannot_be_excluded():
    clk, sched, eng = make()
    with pytest.raises(SchedulerError):
        sched.set_exclusions([], [F("6g41")])
    sched.set_exclusions(["6"], [])
    assert not sched.is_excluded(F("6g41")) and sched.is_excluded(F("6g5"))
    states = sched.channel_states()
    assert states[OWN]["state"] == "own"
    assert states[F("6g41")]["state"] == "in_block"
    assert states[F("6g5")]["state"] == "excluded" and states[F("6g5")]["revisit_s"] is None


def test_individual_channel_exclusion():
    clk, sched, eng = make()
    sched.set_exclusions([], [F("6g5"), F("6g9")])
    eng.run_for(200)
    seen = {r.slot.freq for r in eng.slot_log}
    assert F("6g5") not in seen and F("6g9") not in seen
    assert sched.allocation()["six"] == 4  # 14chに4枠のまま


def test_spare_5ghz_gets_dedicated_slot():
    spare = F("5g100")
    clk, sched, eng = make(spare=[OWN + 20, spare])
    a = sched.allocation()
    assert a["spare"] == 1 and a["five"] == 2
    eng.run_for(400)
    v = visits(eng)
    assert max_gap(v[spare]) == pytest.approx(10, abs=0.01)
    others = [f for f in ch.FREQS_BY_BAND["5"] if f != spare]
    assert max(max_gap(v[f]) for f in others) <= 100  # 仕様書：95%で約92秒


def test_all_slots_at_least_200ms():
    for mode in ("PRE", "LIVE", "QUIET"):
        for excl in ([], ["2.4"], ["2.4", "5"], ["2.4", "5", "6"]):
            clk, sched, eng = make(mode=mode, coldstart=True)
            sched.set_exclusions(excl, [])
            eng.run_for(65)
            assert min(r.slot.dwell for r in eng.slot_log) >= 0.2 - 1e-9
            # 実際のtuneの間隔も200ms以上
            tl = [t for t, _ in eng.radio.tune_log]
            assert all(b - a >= 0.2 - 1e-9 for a, b in zip(tl, tl[1:]))


def test_coldstart_covers_block_and_psc_quickly():
    clk, sched, eng = make(coldstart=True)
    t0 = clk.monotonic()
    eng.run_for(0.1)
    cs = []
    while True:
        r = eng.step() if not cs or cs[-1].slot.kind == "coldstart" else None
        if r is None:
            break
        cs.append(r)
    cs = [eng.slot_log[0]] + [r for r in cs if r.slot.kind == "coldstart"]
    freqs = {r.slot.freq for r in cs}
    assert set(ch.own_block(OWN).freqs) <= freqs
    assert {F("6g5"), F("6g21"), F("6g69"), F("6g85")} <= freqs
    assert cs[-1].t_end - t0 <= 5.0  # グラフを約5秒で出す


def test_presence_window_and_revisit():
    clk, sched, eng = make()
    rv = sched.revisit_s()
    assert rv[OWN] == 0
    assert rv[F("6g41")] == 20 and rv[F("6g5")] == 40 and rv[F("2g1")] == 130
    assert sched.presence_window(F("2g1")) == 325
    sched.set_exclusions(["2.4"], [])
    assert math.isinf(sched.revisit_s()[F("2g1")]) and sched.presence_window(F("2g1")) == 600


def test_mode_switch_takes_effect_next_slot():
    clk, sched, eng = make()
    eng.run_for(3)
    sched.set_mode("PRE")
    r = eng.step()
    assert r.slot.kind == "pre"
    with pytest.raises(SchedulerError):
        sched.set_mode("TURBO")
