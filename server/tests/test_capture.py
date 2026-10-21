"""解析を testdata/（生成の入力から書いた期待値）と照合する。"""
import struct

import pytest

from tests.conftest import load_testdata
from wifiwatch.capture import dot11, ies, radiotap

IE_CASES = load_testdata("ies_cases.json")
FRAME_CASES = load_testdata("frame_cases.json")


@pytest.mark.parametrize("case", IE_CASES, ids=[c["name"] for c in IE_CASES])
def test_ie_cases(case):
    b = bytes.fromhex(case["ies_hex"])
    info = ies.parse_bss(b, 0, len(b))
    op = ies.operating(info, case["rx_freq"])
    e = case["expect"]
    assert op.primary_freq == e["primary_freq"]
    assert op.width == e["width"]
    assert op.width_label == e["width_label"]
    assert [[s.lo, s.hi] for s in op.spans] == e["spans"]
    assert not op.suspect
    if "standard" in e:
        assert info.standard == e["standard"]
    if "security" in e:
        assert info.security == e["security"]
    if "ssid" in e:
        assert info.ssid == e["ssid"]
    if "ssid_hidden" in e:
        assert info.ssid_hidden == e["ssid_hidden"]
    if "dup_beacon" in e:
        assert bool(info.he and info.he.six and info.he.six.dup_beacon) == e["dup_beacon"]
    if "pmf_required" in e:
        assert info.rsn.mfpr == e["pmf_required"]
    if "wps" in e:
        w = info.wps
        assert {"manufacturer": w.manufacturer, "model_name": w.model_name, "model_number": w.model_number} == e["wps"]
    if "rnr" in e:
        got = [{"freq": r.freq, "bssid": r.bssid, "colocated": r.colocated, "same_ssid": r.same_ssid} for r in info.rnr]
        assert got == e["rnr"]


@pytest.mark.parametrize("case", FRAME_CASES, ids=[c["name"] for c in FRAME_CASES])
def test_frame_cases(case):
    buf = bytes.fromhex(case["hex"])
    e = case["expect"]
    rx = radiotap.parse(buf)
    end = len(buf) - (4 if rx.fcs_at_end else 0)
    for k in ("freq", "signal", "fcs_at_end", "bad_fcs", "ppdu_bw"):
        if k in e:
            assert getattr(rx, k) == e[k], k
    if "chains" in e:
        assert [s for _, s in rx.chains] == e["chains"]
    if "ftype" not in e:
        return
    h = dot11.parse_header(buf, rx.length, end)
    assert h.ftype == e["ftype"]
    for k in ("subtype", "retry", "protected", "hdr_len"):
        if k in e:
            assert getattr(h, k) == e[k], k
    for k in ("addr1", "addr2", "addr3"):
        if k in e:
            assert dot11.mac_str(getattr(h, k)) == e[k], k
    if "bssid" in e:
        assert dot11.mac_str(h.bssid) == e["bssid"]
    if "client" in e:
        assert dot11.mac_str(h.client) == e["client"]
    if "reason" in e:
        assert struct.unpack_from("<H", buf, rx.length + h.hdr_len)[0] == e["reason"]
    if "ssid" in e:
        info = ies.parse_bss(buf, rx.length + h.hdr_len + 12, end)
        assert info.ssid == e["ssid"]
        if "primary_freq" in e:
            op = ies.operating(info, rx.freq)
            assert (op.primary_freq, op.width) == (e["primary_freq"], e["width"])


def test_radiotap_rejects_garbage():
    for bad in [b"", b"\x01\x00\x08\x00\x00\x00\x00\x00", b"\x00\x00\xff\x00\x00\x00\x00\x00"]:
        with pytest.raises(radiotap.RadiotapError):
            radiotap.parse(bad)


def test_truncated_ies_do_not_crash():
    from wifiwatch.capture import build
    b = build.ap_ies(6135, 160, "X")
    for cut in range(0, len(b), 7):
        info = ies.parse_bss(b[:cut], 0, cut)
        ies.operating(info, 6135)


def test_dot11_short_frames():
    with pytest.raises(dot11.Dot11Error):
        dot11.parse_header(b"\x80\x00", 0, 2)
    with pytest.raises(dot11.Dot11Error):
        dot11.parse_header(b"\x80\x00" + b"\x00" * 10, 0, 12)


def test_tcpdump_cross_check(tmp_path):
    """独立した解析器（tcpdump）でradiotapと802.11の読みを照合する（tcpdumpがあれば）。

    書き出すのは合成フレームだけで、作業用の一時ディレクトリに置いて消す。"""
    import shutil
    import subprocess
    import time as _t
    if not shutil.which("tcpdump"):
        pytest.skip("tcpdump がない")
    path = tmp_path / "synthetic.pcap"
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127))  # LINKTYPE_IEEE802_11_RADIOTAP
        for c in FRAME_CASES:
            b = bytes.fromhex(c["hex"])
            f.write(struct.pack("<IIII", int(_t.time()), 0, len(b), len(b)) + b)
    out = subprocess.run(["tcpdump", "-nn", "-e", "-r", str(path)], capture_output=True, text=True, timeout=20)
    lines = out.stdout.strip().splitlines()
    assert len(lines) == len(FRAME_CASES)
    for c, line in zip(FRAME_CASES, lines):
        e = c["expect"]
        assert f"{e['freq']} MHz" in line, line
        if e.get("signal") is not None and not e.get("chains"):
            assert f"{e['signal']}dBm signal" in line, line
        if e.get("ssid"):
            assert f"({e['ssid']})" in line, line
        if e.get("bad_fcs"):
            assert "bad-fcs" in line, line
    path.unlink()


def _ax210_beacon_radiotap(signal: int, chains: tuple[int, int]) -> bytes:
    # 本番PC（AX210）で受けた 2.4GHz のビーコンと同じ並び（present a040402f,a0000820,00000820・長さ 0x38）
    b = bytearray(0x38)
    struct.pack_into("<BBHIII", b, 0, 0, 0, 0x38, 0xA040402F, 0xA0000820, 0x00000820)
    struct.pack_into("<Q", b, 16, 123456)          # TSFT
    b[24], b[25] = 0x10, 2                         # Flags（FCSあり）・Rate 1Mbps
    struct.pack_into("<HH", b, 26, 2412, 0x00A0)   # Channel
    struct.pack_into("<b", b, 30, signal)          # 合成の強さ（ドライバ）
    struct.pack_into("<H", b, 32, 0)               # RX flags
    struct.pack_into("<b", b, 52, chains[0]); b[53] = 0
    struct.pack_into("<b", b, 54, chains[1]); b[55] = 1
    return bytes(b)


def test_ax210_layout_reads_chains():
    rx = radiotap.parse(_ax210_beacon_radiotap(-51, (-51, -52)))
    assert (rx.length, rx.freq, rx.driver_signal) == (0x38, 2412, -51)
    assert rx.chains == ((0, -51), (1, -52)) and rx.signal == -48  # 2本の電力の和
    assert rx.fcs_at_end


def test_impossible_driver_signal_is_unknown():
    # 0dBm以上は受けられないほど強い（ドライバが値を持っていない）。強さ不明にし、書かれた値は残す
    from wifiwatch.capture import build
    for v in (0, 5, 127):
        rx = radiotap.parse(build.radiotap(2412, signal=v))
        assert rx.signal is None and rx.driver_signal == v
    rx = radiotap.parse(build.radiotap(2412, signal=0, chains=[-60, -62]))
    assert rx.signal == -58  # アンテナごとの値があれば、それを使う
    assert radiotap.parse(build.radiotap(2412, signal=-40)).signal == -40
