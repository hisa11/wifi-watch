"""合否ゲートが不合格のときの代わり（管理モードのパッシブスキャン）と、nl80211の読み取り。"""
import struct

from wifiwatch import channels as ch
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.capture import build
from wifiwatch.clock import FakeClock
from wifiwatch.radio import nl80211 as nl
from wifiwatch.radio.base import validate_tuning
from wifiwatch.radio.linux import RunResult
from wifiwatch.radio.nl80211 import ScanBss
from wifiwatch.radio.scanonly import ScanOnlyRadio

F = ch.parse_channel_ref


def bss_msg(bssid, freq, mbm, ies, seq=1):
    inner = (nl.nla(nl.NL80211_BSS_BSSID, bytes.fromhex(bssid.replace(":", "")))
             + nl.nla(nl.NL80211_BSS_FREQUENCY, struct.pack("=I", freq))
             + nl.nla(nl.NL80211_BSS_SIGNAL_MBM, struct.pack("=i", mbm))
             + nl.nla(nl.NL80211_BSS_SEEN_MS_AGO, struct.pack("=I", 120))
             + nl.nla(nl.NL80211_BSS_INFORMATION_ELEMENTS, ies))
    payload = nl.nla(nl.NL80211_ATTR_IFINDEX, struct.pack("=I", 3)) + nl.nla(nl.NL80211_ATTR_BSS | nl.NLA_F_NESTED, inner)
    return nl.msg(0x1c, 0x2, seq, 34, payload)


def test_parse_scan_dump_messages():
    ies = build.ap_ies(F("6g37"), 160, "NL-6G")
    data = bss_msg("02:00:00:00:00:01", 6135, -5525, ies) + bss_msg("02:00:00:00:00:02", 5180, -7000, b"", 2)
    data += struct.pack("=IHHII", 20, nl.NLMSG_DONE, 0x2, 3, 0) + b"\x00" * 4
    msgs = nl.parse_messages(data)
    got = [nl.bss_from_attrs(nl.attrs(body)) for t, body in msgs if t != nl.NLMSG_DONE]
    assert got[0].bssid == "02:00:00:00:00:01" and got[0].freq == 6135 and got[0].signal_dbm == -55.25
    assert got[0].ies == ies and got[0].seen_ms_ago == 120
    assert got[1].freq == 5180 and got[1].ies == b""


def test_netlink_error_raises():
    err = struct.pack("=IHHII", 36, nl.NLMSG_ERROR, 0, 1, 0) + struct.pack("=i", -1) + b"\x00" * 16
    try:
        nl.parse_messages(err)
    except nl.NetlinkError:
        return
    raise AssertionError("例外にならない")


class Run:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return RunResult(0, "", "")


def test_scan_only_feeds_beacons_to_analyzer(tmp_path):
    clk = FakeClock()
    dumps = [[ScanBss("02:00:00:00:00:01", 6135, -55.0, build.ap_ies(F("6g37"), 160, "S6"), 100),
              ScanBss("02:00:00:00:00:02", 5180, -60.0, build.ap_ies(F("5g36"), 80, "S5"), 100)]]
    run = Run()
    r = ScanOnlyRadio("wlan0", clk, runner=run, dump=lambda ifn: dumps[0], sys_class_net=str(tmp_path))
    a = Analyzer(clk)
    r.tune(validate_tuning(F("6g37"), 160, 6185))
    b1 = r.capture(1.0)
    a.process_batch(b1)
    assert run.calls[0] == ["iw", "dev", "wlan0", "scan", "passive"]
    st = a.table.get("02:00:00:00:00:01")
    assert st.width == 160 and st.ssid == "S6" and st.rssi == -55
    assert r.capture(1.0).frames == []  # 同じスキャンの結果は1回だけ渡す
    r.tune(validate_tuning(F("5g36"), 20, F("5g36")))
    assert len(r.capture(0.25).frames) == 1
    clk.advance(30)  # 古くなったらスキャンし直す
    r.tune(validate_tuning(F("6g37"), 160, 6185))
    assert len(r.capture(1.0).frames) == 1 and len(run.calls) == 2
