"""カーネル内の切り詰め（BPF）を、解釈器で確かめる。"""
from tests.conftest import load_testdata
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.capture import build, dot11, radiotap
from wifiwatch.clock import FakeClock
from wifiwatch.radio import bpf
from wifiwatch.radio.base import FrameBatch, validate_tuning

PROG = bpf.truncation_program()
BSS, STA = "02:11:22:33:44:55", "02:66:77:88:99:aa"


def keep(pkt):
    return pkt[:bpf.run(PROG, pkt)]


def test_data_frames_cut_to_header():
    rt = build.radiotap(6135, signal=-50, chains=[-53, -53], flags=0x10)
    cases = [
        (build.data(BSS, STA, True, qos=False), 24),
        (build.data(BSS, STA, True, qos=True), 26),
    ]
    # 4アドレス＋QoS＋HTC（36バイト）
    wds = bytes([0x88, 0x83]) + b"\x00\x00" + dot11.mac_bytes(BSS) * 4 + b"\x00\x00" + b"\x00\x00" + b"\xAA\xBB\xCC\xDD" + b"\xEE" * 50
    cases.append((wds[:30] + wds[30:] , 36))
    for frame, hl in cases:
        pkt = rt + build.with_fcs(frame)
        out = keep(pkt)
        assert len(out) == len(rt) + hl
        assert b"\xEE" not in out[len(rt):]  # 詰め物（ペイロード）は1バイトも残らない
        h = dot11.parse_header(out, len(rt), len(out))
        assert h.hdr_len == hl


def test_beacons_kept_whole_and_others_short():
    rt = build.radiotap(6135, signal=-50)
    beacon = rt + build.with_fcs(build.beacon(BSS, build.ap_ies(6135, 160, "X")))
    assert keep(beacon) == beacon
    presp = rt + build.beacon(BSS, build.ap_ies(6135, 20, "Y"), probe_resp_to=STA)
    assert keep(presp) == presp
    de = rt + build.with_fcs(build.deauth(STA, BSS, BSS, reason=7))
    assert len(keep(de)) == len(de)  # 30バイト（24＋理由2＋FCS4）は32以内
    action = rt + build.mgmt(13, STA, BSS, BSS, b"\x7F" * 100)
    assert len(keep(action)) == len(rt) + 32
    ack = rt + build.with_fcs(build.ack(STA))
    assert keep(ack) == ack
    ba = rt + bytes([0x94, 0x00]) + b"\x00\x00" + dot11.mac_bytes(STA) + dot11.mac_bytes(BSS) + b"\x05" * 30
    assert len(keep(ba)) == len(rt) + 16


def test_garbage_is_dropped_not_crashed():
    assert bpf.run(PROG, b"") == 0
    assert bpf.run(PROG, b"\x00\x00\xff\x00") == 0


def test_analyzer_results_same_after_truncation():
    frames = [bytes.fromhex(c["hex"]) for c in load_testdata("frame_cases.json")]
    full = [f for f in frames if radiotap.parse(f).freq == 6135]
    full += [build.radiotap(6135, signal=-50, flags=0x10) + build.with_fcs(build.data(BSS, STA, i % 2 == 0, retry=i % 3 == 0)) for i in range(20)]
    cut = [keep(f) for f in full]
    tun = validate_tuning(6135, 20, 6135)
    res = []
    for fs in (full, cut):
        a = Analyzer(FakeClock())
        a.process_batch(FrameBatch(tun, 1000.0, 1001.0, fs))
        t = a.stats.totals((6135, 20), 1001.0, 10)
        res.append((t.frames, t.data, t.ctrl, t.mgmt, t.retries, t.retry_eligible, t.bad_fcs,
                    sorted(s.bssid for s in a.table.all()), sorted(a.table.clients), len(a.deauths),
                    [d.reason for d in a.deauths]))
    assert res[0] == res[1]
