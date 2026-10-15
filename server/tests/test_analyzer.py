from wifiwatch import channels as ch
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.capture import build
from wifiwatch.clock import FakeClock
from wifiwatch.radio.base import FrameBatch, validate_tuning
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation

F = ch.parse_channel_ref
OWN_BSS = "02:00:00:00:00:01"
OWN_STA = "02:00:00:00:00:02"


def rt(freq, sig=-50, flags=0x10):
    return build.radiotap(freq, signal=sig, chains=[sig - 3, sig - 3], flags=flags)


def fr(freq, frame, sig=-50):
    return rt(freq, sig) + build.with_fcs(frame)


def bcn(freq_rx, bssid, ies, sig=-50):
    return rt(freq_rx, sig) + build.with_fcs(build.beacon(bssid, ies))


def batch(freq, width, frames, receive_s=1.0, t_end=1010.0, center=None):
    if center is None:
        center = freq if width == 20 else ch.block_for(freq, width).center_freq
    tun = validate_tuning(freq, width, center)
    return FrameBatch(tun, t_end - receive_s, t_end, frames)


def test_6ghz_beacons_primary_and_width():
    a = Analyzer(FakeClock())
    cases = [
        ("02:00:00:00:10:20", F("6g37"), 20, None),
        ("02:00:00:00:10:40", F("6g9"), 40, None),
        ("02:00:00:00:10:80", F("6g69"), 80, None),
        ("02:00:00:00:11:60", F("6g37"), 160, None),
        ("02:00:00:00:13:20", F("6g49"), 320, None),
        ("02:00:00:00:13:21", F("6g49"), 320, (4, 47, 63)),
    ]
    for bss, f, w, override in cases:
        ies = build.ap_ies(f, w, f"S{w}", eht_width_override=override)
        a.process_batch(batch(f, 20, [bcn(f, bss, ies)]))
    t = a.table
    assert t.get("02:00:00:00:10:20").width == 20
    assert t.get("02:00:00:00:10:40").spans[0].lo == 5985
    assert t.get("02:00:00:00:10:80").width == 80
    s160 = t.get("02:00:00:00:11:60")
    assert (s160.primary_freq, s160.width, s160.spans[0].lo, s160.spans[0].hi) == (6135, 160, 6105, 6265)
    s320 = t.get("02:00:00:00:13:20")
    assert (s320.width, s320.width_label, s320.standard) == (320, "320", "be")
    assert (s320.spans[0].lo, s320.spans[0].hi) == (5945, 6265)
    s320b = t.get("02:00:00:00:13:21")
    assert (s320b.spans[0].lo, s320b.spans[0].hi) == (6105, 6425)
    assert all(s.band == "6" for s in t.all())


def test_duplicate_beacon_counted_on_primary():
    a = Analyzer(FakeClock())
    ies = build.ap_ies(F("6g37"), 80, "DUP", dup_beacon=True)
    bss = "02:00:00:00:0d:01"
    # 主ch37で1回、ch41とch45で重複ビーコンを受ける
    a.process_batch(batch(F("6g37"), 20, [bcn(F("6g37"), bss, ies)]))
    a.process_batch(batch(F("6g41"), 20, [bcn(F("6g41"), bss, ies)]))
    a.process_batch(batch(F("6g45"), 20, [bcn(F("6g45"), bss, ies)]))
    assert len(a.table.all()) == 1
    st = a.table.get(bss)
    assert st.primary_freq == F("6g37")
    assert st.dup_beacon
    assert sorted(st.seen_on) == [F("6g37"), F("6g41"), F("6g45")]
    assert a.stats.beacons_by_primary == {F("6g37"): 3}


def test_first_seen_on_duplicate_still_uses_primary():
    a = Analyzer(FakeClock())
    ies = build.ap_ies(F("6g37"), 160, "DUP160", dup_beacon=True)
    a.process_batch(batch(F("6g57"), 20, [bcn(F("6g57"), "02:00:00:00:0d:02", ies)]))
    st = a.table.get("02:00:00:00:0d:02")
    assert st.primary_freq == F("6g37") and st.width == 160


def test_rates_use_receive_seconds_and_exclude_own():
    a = Analyzer(FakeClock(), own_addrs=[OWN_BSS, OWN_STA])
    other = "02:00:00:00:00:99"
    sta = "02:aa:00:00:00:01"
    frames = [fr(6135, build.data(other, sta, True)) for _ in range(30)]
    frames += [fr(6135, build.data(OWN_BSS, OWN_STA, True)) for _ in range(50)]
    a.process_batch(batch(F("6g37"), 160, frames, receive_s=0.25, t_end=1005.0))
    tot = a.stats.totals((F("6g37"), 160), 1005.0, 10)
    assert tot.frames == 30 and tot.own_frames == 50
    assert tot.receive_s == 0.25
    assert tot.rate() == 120.0  # 30 / 0.25受信秒（時計の10秒では割らない）


def test_retry_counts_data_and_nonbeacon_mgmt_only():
    a = Analyzer(FakeClock())
    bss, sta = "02:00:00:00:00:99", "02:aa:00:00:00:01"
    frames = [fr(6135, build.data(bss, sta, True, retry=i < 3)) for i in range(10)]
    frames += [fr(6135, build.ack(sta)) for _ in range(10)]
    frames += [bcn(6135, bss, build.ap_ies(6135, 20, "X"))]
    a.process_batch(batch(6135, 20, frames))
    tot = a.stats.totals((6135, 20), 1010.0, 10)
    assert (tot.retry_eligible, tot.retries) == (10, 3)
    assert (tot.data, tot.ctrl, tot.mgmt, tot.beacons) == (10, 10, 1, 1)


def test_wrong_freq_and_bad_fcs_dropped():
    a = Analyzer(FakeClock())
    good = bcn(6135, "02:00:00:00:00:01", build.ap_ies(6135, 20, "A"))
    stale = bcn(6155, "02:00:00:00:00:02", build.ap_ies(6155, 20, "B"))
    bad = rt(6135, flags=0x50) + build.with_fcs(build.ack("02:aa:00:00:00:01"), bad=True)
    a.process_batch(batch(6135, 20, [good, stale, bad, b"\x00\x00\x02"]))
    assert [s.ssid for s in a.table.all()] == ["A"]
    assert a.counters["wrong_freq"] == 1 and a.counters["bad_fcs"] == 1 and a.counters["radiotap_errors"] == 1


def test_parse_is_rate_limited_but_rssi_updates():
    clk = FakeClock()
    a = Analyzer(clk, parse_interval=5)
    bss = "02:00:00:00:00:05"
    a.process_batch(batch(6135, 20, [bcn(6135, bss, build.ap_ies(6135, 20, "OLD"), -60)], t_end=1001))
    a.process_batch(batch(6135, 20, [bcn(6135, bss, build.ap_ies(6135, 20, "NEW"), -40)], t_end=1002))
    st = a.table.get(bss)
    assert st.ssid == "OLD" and st.rssi == -40 and st.beacons == 2
    a.process_batch(batch(6135, 20, [bcn(6135, bss, build.ap_ies(6135, 20, "NEW"), -40)], t_end=1007))
    assert st.ssid == "NEW"


def test_channel_move_detected():
    a = Analyzer(FakeClock(), parse_interval=0)
    events = []
    a.table.on_change.append(lambda s, e: events.append((s.bssid, e, s.primary_freq)))
    bss = "02:00:00:00:00:07"
    a.process_batch(batch(5975, 20, [bcn(5975, bss, build.ap_ies(5975, 20, "MV"))]))
    a.process_batch(batch(6135, 20, [bcn(6135, bss, build.ap_ies(6135, 20, "MV"))]))
    assert events == [(bss, "new", 5975), (bss, "moved", 6135)]
    assert a.table.get(bss).prev_primary_freq == 5975


def test_clients_and_deauth_from_headers_only():
    a = Analyzer(FakeClock())
    bss, sta = "02:00:00:00:00:99", "da:aa:00:00:00:01"
    frames = [fr(6135, build.data(bss, sta, True)), fr(6135, build.data(bss, sta, False))]
    frames += [fr(6135, build.deauth(sta, bss, bss, reason=3)), fr(6135, build.deauth(sta, bss, bss, disassoc=True, protected=True))]
    a.process_batch(batch(6135, 20, frames))
    c = a.table.clients[sta]
    assert c.frames == 2 and c.bssid == bss and c.random_mac
    d = list(a.deauths)
    assert [(x.disassoc, x.protected, x.reason, x.dst) for x in d] == [(False, False, 3, sta), (True, True, None, sta)]


def test_fake_radio_end_to_end_own_block():
    clk = FakeClock()
    sc = Scenario([
        SimStation(OWN_BSS, "OWN", F("6g37"), 20, -50, data_rate=100, clients=[OWN_STA]),
        SimStation("02:00:00:00:00:03", "WIDE", F("6g33"), 160, -55, data_rate=50),
        SimStation("02:00:00:00:00:04", "IN", F("6g41"), 20, -50),
    ], seed=1, noise_db=0.5)
    r = FakeRadio(clk, sc)
    a = Analyzer(clk, own_addrs=[OWN_BSS, OWN_STA])
    b = ch.own_block(F("6g37"))
    r.tune(validate_tuning(F("6g37"), 160, b.center_freq))
    a.process_batch(r.capture(7.0))
    names = {s.ssid for s in a.table.all()}
    assert names == {"OWN"}  # 別chの局のビーコンは160MHz受信では入らない
    tot = a.stats.totals((F("6g37"), 160), clk.monotonic(), 20)
    assert tot.own_frames > 0 and tot.frames > 0  # 広帯域の局のデータは拾う
    assert tot.ppdu_bw == {}


def test_station_is_gone_after_missed_visits_not_just_time():
    # 「いない」は、時間ではなく「主chを見に行ったのに聞こえなかった回数」で決める（見に行けていない間は時間で待つ）
    from wifiwatch.analyzer.occupancy import is_present, missed_visits

    class Sched:
        def presence_window(self, f):
            return 600.0

    clk = FakeClock()
    a = Analyzer(clk)
    f, g = F("5g36"), F("5g40")
    beacon = build.radiotap(f, signal=-60) + build.beacon("02:00:00:00:00:05", build.ap_ies(f, 20, "S"))
    t = clk.monotonic()

    def visit(freq, frames=()):
        nonlocal t
        a.process_batch(FrameBatch(validate_tuning(freq, 20, freq), t, t + 0.25, list(frames)))
        t += 0.3
        clk.advance(10)

    visit(f, [beacon])
    st = a.table.get("02:00:00:00:00:05")
    visit(g)  # ほかのchを見ている間は数えない
    assert missed_visits(st, a) == 0 and is_present(st, a, Sched(), clk.monotonic())
    visit(f)
    visit(g)
    assert missed_visits(st, a) == 1 and is_present(st, a, Sched(), clk.monotonic())
    visit(f)
    assert missed_visits(st, a) == 2 and not is_present(st, a, Sched(), clk.monotonic())  # 40秒でも「いない」
    visit(g)
    visit(f, [beacon])
    assert missed_visits(st, a) == 0 and is_present(st, a, Sched(), clk.monotonic())
