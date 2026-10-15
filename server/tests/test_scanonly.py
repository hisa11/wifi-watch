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


PHY_24_5 = """Wiphy phy0
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412.0 MHz [1] (20.0 dBm)
\t\t\t* 2484.0 MHz [14] (disabled)
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180.0 MHz [36] (23.0 dBm)
"""


class RunPhy:
    """iw phy の答えを返し、パッシブスキャンは busy で失敗させる。"""

    def __init__(self, scan_rc=0):
        self.calls = []
        self.scan_rc = scan_rc

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[:2] == ["iw", "phy"]:
            return RunResult(0, PHY_24_5, "")
        if "scan" in argv:
            return RunResult(self.scan_rc, "", "command failed: Device or resource busy (-16)")
        return RunResult(0, "", "")


def phy_sysnet(tmp_path):
    d = tmp_path / "wlan0" / "phy80211"
    d.mkdir(parents=True)
    (d / "name").write_text("phy0\n")
    return str(tmp_path)


def test_scan_only_never_falls_back_to_active_scan(tmp_path):
    clk = FakeClock()
    run = RunPhy(scan_rc=240)
    r = ScanOnlyRadio("wlan0", clk, runner=run, dump=lambda ifn: [], sys_class_net=phy_sysnet(tmp_path))
    r.tune(validate_tuning(F("5g36"), 20, F("5g36")))
    r.capture(0.25)
    scans = [c for c in run.calls if "scan" in c]
    assert scans == [["iw", "dev", "wlan0", "scan", "passive"]]  # 受信専用：能動スキャン（プローブ要求）に切り替えない
    clk.advance(6)  # 失敗したときは5秒ほどでやり直す（それでもパッシブだけ）
    r.capture(0.25)
    assert [c for c in run.calls if "scan" in c] == [["iw", "dev", "wlan0", "scan", "passive"]] * 2


def test_scan_only_unreceivable_channels_are_not_observed(tmp_path):
    # 6GHz非対応の無線：6GHzのchは「受信していた秒」を0にして、検知なしと見ていないを混ぜない
    clk = FakeClock()
    dump = [ScanBss("02:00:00:00:00:02", 5180, -60.0, build.ap_ies(F("5g36"), 20, "S5"), 100)]
    r = ScanOnlyRadio("wlan0", clk, runner=RunPhy(), dump=lambda ifn: dump, sys_class_net=phy_sysnet(tmp_path))
    a = Analyzer(clk)
    r.tune(validate_tuning(F("5g36"), 20, F("5g36")))
    b = r.capture(0.25)
    assert b.receive_s > 0 and len(b.frames) == 1
    a.process_batch(b)
    r.tune(validate_tuning(F("6g37"), 20, F("6g37")))
    b6 = r.capture(1.0)
    assert b6.receive_s == 0 and b6.frames == []
    a.process_batch(b6)
    assert F("6g37") not in a.stats.last_observed and F("5g36") in a.stats.last_observed
    assert r.receivable == {2412, 5180}  # disabled のchは受けられないものとして扱う


def test_scan_only_does_not_redeliver_kernel_leftovers(tmp_path):
    # いなくなった局をカーネルが結果に残していても、新しく聞こえていなければ渡さない（最終確認を進めない）
    clk = FakeClock()
    seen = {"ms": 100}
    dump = lambda ifn: [ScanBss("02:00:00:00:00:02", 5180, -60.0, build.ap_ies(F("5g36"), 20, "S5"), seen["ms"])]
    r = ScanOnlyRadio("wlan0", clk, runner=RunPhy(), dump=dump, sys_class_net=phy_sysnet(tmp_path))
    r.tune(validate_tuning(F("5g36"), 20, F("5g36")))
    assert len(r.capture(0.25).frames) == 1
    clk.advance(25)  # 次のスキャン。この局はもう聞こえていない（カーネルの記録は25秒前のまま）
    seen["ms"] = 25_100
    b = r.capture(0.25)
    assert b.frames == [] and b.receive_s > 0  # 見に行った（新しいスキャン）が、聞こえなかった
    assert r.capture(0.25).receive_s == 0  # 同じスキャンの結果を見直しても、もう一度見たことにはしない
    clk.advance(25)
    seen["ms"] = 300  # また聞こえた
    assert len(r.capture(0.25).frames) == 1


def test_scan_only_failed_scan_is_not_an_observation(tmp_path):
    clk = FakeClock()
    r = ScanOnlyRadio("wlan0", clk, runner=RunPhy(scan_rc=240), dump=lambda ifn: [], sys_class_net=phy_sysnet(tmp_path))
    r.tune(validate_tuning(F("5g36"), 20, F("5g36")))
    assert r.capture(0.25).receive_s == 0  # busy で失敗したスキャンでは、そのchを見たことにしない
