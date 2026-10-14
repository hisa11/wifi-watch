from wifiwatch import channels as ch
from wifiwatch.capture import dot11, radiotap
from wifiwatch.clock import FakeClock
from wifiwatch.radio.base import RadioError, Tuning, validate_tuning
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation

F = ch.parse_channel_ref
import pytest


def _senders(batch):
    out = {}
    for fr in batch.frames:
        rx = radiotap.parse(fr)
        h = dot11.parse_header(fr, rx.length, len(fr) - 4)
        key = (h.ftype, h.subtype, dot11.mac_str(h.addr2))
        out[key] = out.get(key, 0) + 1
    return out


def make(stations, **kw):
    clk = FakeClock()
    return clk, FakeRadio(clk, Scenario(stations, seed=3, noise_db=0.5, **kw))


def own160():
    b = ch.own_block(F("6g37"))
    return validate_tuning(F("6g37"), 160, b.center_freq)


def test_validate_tuning():
    assert validate_tuning(F("6g37"), 20, F("6g37")).span.width == 20
    with pytest.raises(RadioError):
        validate_tuning(F("6g37"), 160, 6265)  # ch37を含まないブロック
    with pytest.raises(RadioError):
        validate_tuning(6136, 20, 6136)
    with pytest.raises(RadioError):
        validate_tuning(F("2g6"), 40, 2447)
    with pytest.raises(RadioError):
        validate_tuning(F("6g37"), 320, 6105)  # AX210は320MHzを受信できない


def test_reception_model_on_own_block():
    own = SimStation("02:00:00:00:00:01", "OWN", F("6g37"), 20, -50, data_rate=100)
    other_in_block = SimStation("02:00:00:00:00:02", "IN", F("6g41"), 20, -50, data_rate=100)
    wide = SimStation("02:00:00:00:00:03", "WIDE", F("6g33"), 160, -50, data_rate=100)
    dup = SimStation("02:00:00:00:00:04", "DUP", F("6g53"), 80, -50, dup_beacon=True)  # 49〜61：ch37を含まない
    dup2 = SimStation("02:00:00:00:00:05", "DUP2", F("6g33"), 80, -50, dup_beacon=True)  # 33〜45
    w320 = SimStation("02:00:00:00:00:06", "W320", F("6g37"), 320, -50, data_rate=100)
    clk, r = make([own, other_in_block, wide, dup, dup2, w320])
    r.tune(own160())
    b = r.capture(1.0)
    s = _senders(b)
    assert s.get((0, 8, "02:00:00:00:00:01"), 0) >= 9  # 自分の主chの局のビーコン
    assert (0, 8, "02:00:00:00:00:02") not in s  # 自ブロック内の別chの局は聞こえない
    assert (2, 8, "02:00:00:00:00:02") not in s
    assert s.get((2, 8, "02:00:00:00:00:03"), 0) > 0  # 自chにかかる160MHzの送信は聞こえる
    assert (0, 8, "02:00:00:00:00:03") not in s  # その局のビーコン（主ch33）は聞こえない
    assert (0, 8, "02:00:00:00:00:04") not in s
    assert s.get((0, 8, "02:00:00:00:00:05"), 0) > 0  # 重複ビーコンは聞こえる
    assert s.get((0, 8, "02:00:00:00:00:06"), 0) > 0  # 320MHzの局もビーコンは読める
    assert (2, 8, "02:00:00:00:00:06") not in s  # 320MHzのデータは受信できない
    assert abs(b.receive_s - 1.0) < 1e-9


def test_patrol_20mhz_hears_other_channel_beacons():
    other = SimStation("02:00:00:00:00:02", "IN", F("6g41"), 20, -50)
    clk, r = make([other])
    r.tune(validate_tuning(F("6g41"), 20, F("6g41")))
    assert _senders(r.capture(0.22)).get((0, 8, "02:00:00:00:00:02"), 0) >= 2


def test_sensitivity_and_stall():
    weak = SimStation("02:00:00:00:00:09", "WEAK", F("6g37"), 20, -100)
    clk, r = make([weak])
    r.tune(own160())
    assert r.capture(1).frames == []
    strong = SimStation("02:00:00:00:00:01", "S", F("6g37"), 20, -40)
    clk, r = make([strong])
    r.tune(own160())
    r.stalled = True
    assert r.capture(1).frames == []
    r.reset_monitor()
    assert r.capture(1).frames


def test_tune_costs_switch_time():
    clk, r = make([])
    t0 = clk.monotonic()
    r.tune(own160())
    assert abs(clk.monotonic() - t0 - 0.03) < 1e-9


def test_presence_window():
    st = SimStation("02:00:00:00:00:01", "LATE", F("6g37"), 20, -40, present=[[5.0, None]])
    clk, r = make([st])
    r.tune(own160())
    assert not r.capture(4.0).frames
    assert r.capture(2.0).frames
