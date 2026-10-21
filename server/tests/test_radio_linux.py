"""実機の層：コマンドの組み立て（shellなし・検証した値だけ）、iwの出力の読み取り、radiod越しの無線。"""
import os
import socket
import threading

import pytest

from wifiwatch import channels as ch
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.capture import build
from wifiwatch.clock import FakeClock, RealClock
from wifiwatch.radio.base import RadioError, validate_tuning
from wifiwatch.radio.fake import FakeRadio, Scenario, SimStation
from wifiwatch.radio.linux import (LinuxRadio, RunResult, parse_phy_freqs, parse_reg, parse_survey,
                                   tune_argvs)
from wifiwatch.radio.radiod import RadioServer
from wifiwatch.radio.remote import RemoteRadio

F = ch.parse_channel_ref

REG = """global
country JP: DFS-JP
\t(2402 - 2482 @ 40), (N/A, 20), (N/A)
\t(5925 - 6425 @ 320), (N/A, 23), (N/A), NO-OUTDOOR

phy#0 (self-managed)
country JP: DFS-UNSET
\t(2402 - 2437 @ 40), (6, 22), (N/A), AUTO-BW, NO-HT40MINUS, NO-80MHZ, NO-160MHZ
"""
PHY_OK = "\n".join(["\t\tFrequencies:"] + [f"\t\t\t* {f}.0 MHz [{ch.freq_to_number(f)}] (22.0 dBm)" for f in ch.FREQS_BY_BAND["6"]])
PHY_DISABLED = PHY_OK.replace("5975.0 MHz [5] (22.0 dBm)", "5975.0 MHz [5] (disabled)")
SURVEY = """Survey data from wlan0
\tfrequency:\t\t\t5955 MHz [in use]
\tnoise:\t\t\t\t-92 dBm
\tchannel active time:\t\t1234 ms
\tchannel busy time:\t\t56 ms
Survey data from wlan0
\tfrequency:\t\t\t5975 MHz
"""


class FakeRunner:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = list(fail)  # 失敗させるコマンドの部分文字列

    def __call__(self, argv, timeout):
        argv = list(argv)
        assert all(isinstance(a, str) for a in argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if any(f in joined for f in self.fail):
            return RunResult(1, "", "command failed (-22)")
        if argv[:3] == ["iw", "reg", "get"]:
            return RunResult(0, REG, "")
        if argv[:2] == ["iw", "phy"]:
            return RunResult(0, PHY_OK, "")
        if argv[-2:] == ["survey", "dump"]:
            return RunResult(0, SURVEY, "")
        if "scan" in argv[-2:]:
            return RunResult(0, "BSS 02:00:00:00:00:01(on wlan0)\n\tfreq: 5955\nBSS 02:00:00:00:00:02(on wlan0)\n", "")
        return RunResult(0, "", "")


@pytest.fixture
def sysnet(tmp_path):
    d = tmp_path / "net" / "wlan0" / "phy80211"
    d.mkdir(parents=True)
    (d / "name").write_text("phy0\n")
    drv = tmp_path / "drivers" / "iwlwifi"
    drv.mkdir(parents=True)
    dev = tmp_path / "net" / "wlan0" / "device"
    dev.mkdir()
    (dev / "driver").symlink_to(drv)
    return tmp_path / "net"


def make_linux(runner, sysnet, sockpair=None, flags=()):
    a, b = sockpair or socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    a.setblocking(False)
    r = LinuxRadio("wlan0", RealClock(), runner=runner, monitor_flags=flags, socket_factory=lambda: a,
                   sys_class_net=str(sysnet))
    return r, b


def test_tune_argv_forms():
    t20 = validate_tuning(F("6g37"), 20, F("6g37"))
    t160 = validate_tuning(F("6g37"), 160, 6185)
    t40 = validate_tuning(F("5g36"), 40, 5190)
    assert tune_argvs("wlan0", t20)[0] == ["iw", "dev", "wlan0", "set", "freq", "6135", "20", "6135"]
    assert tune_argvs("wlan0", t160) == [["iw", "dev", "wlan0", "set", "freq", "6135", "160", "6185"],
                                         ["iw", "dev", "wlan0", "set", "freq", "6135", "160MHz"]]
    assert tune_argvs("wlan0", t40)[1][-1] == "HT40+"


def test_ifname_injection_rejected():
    for bad in ["wlan0; reboot", "wl an0", "a" * 16, "$(id)", ""]:
        with pytest.raises(RadioError):
            LinuxRadio(bad, runner=FakeRunner())


def test_tune_falls_back_and_remembers_style(sysnet):
    run = FakeRunner(fail=["set freq 6135 20 6135"])
    r, _ = make_linux(run, sysnet)
    t = validate_tuning(F("6g37"), 20, F("6g37"))
    r.tune(t)
    assert run.calls[-1][-1] == "HT20"
    run.calls.clear()
    r.tune(t)
    assert len(run.calls) == 1 and run.calls[0][-1] == "HT20"  # 通った書き方から試す


def test_tune_reports_failure(sysnet):
    r, _ = make_linux(FakeRunner(fail=["set freq"]), sysnet)
    with pytest.raises(RadioError, match="合わせられません"):
        r.tune(validate_tuning(F("6g37"), 160, 6185))


def test_monitor_and_recovery_commands(sysnet):
    run = FakeRunner()
    r, _ = make_linux(run, sysnet, flags=["fcsfail"])
    r.set_monitor()
    assert run.calls == [["ip", "link", "set", "wlan0", "down"], ["iw", "dev", "wlan0", "set", "type", "monitor"],
                         ["iw", "dev", "wlan0", "set", "monitor", "fcsfail"], ["ip", "link", "set", "wlan0", "up"]]
    run.calls.clear()
    assert r.managed_scan() == 2
    assert run.calls[:3] == [["ip", "link", "set", "wlan0", "down"], ["iw", "dev", "wlan0", "set", "type", "managed"],
                             ["ip", "link", "set", "wlan0", "up"]]
    run.calls.clear()
    r.reload_driver()
    assert run.calls == [["modprobe", "-r", "iwlmvm", "iwlwifi"], ["modprobe", "iwlwifi"]]


PHY_NO6 = """Wiphy phy0
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412.0 MHz [1] (20.0 dBm)
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180.0 MHz [36] (23.0 dBm)
\t\t\t* 5845.0 MHz [169] (disabled)
"""


class PhyNo6Runner(FakeRunner):
    def __call__(self, argv, timeout):
        if list(argv[:2]) == ["iw", "phy"]:
            self.calls.append(list(argv))
            return RunResult(0, PHY_NO6, "")
        return super().__call__(argv, timeout)


def test_channels_missing_from_hardware_are_skipped(sysnet):
    # 6GHz非対応の無線（開発PCのRTL8852BEなど）：6GHzには合わせず、失敗にもせず、受信していた秒0で返す
    run = PhyNo6Runner()
    r, feed = make_linux(run, sysnet)
    freqs = r.load_hw_freqs()
    assert freqs == {2412: True, 5180: True, 5845: False}
    assert r.unreceivable == [f for f in ch.ALL_FREQS if f not in (2412, 5180, 5845)]
    run.calls.clear()
    r.tune(validate_tuning(F("6g37"), 160, 6185))
    assert run.calls == []  # iw を呼ばない（合わせられないchに合わせようとしない）
    feed.send(build.radiotap(F("5g36"), signal=-50) + build.beacon("02:00:00:00:00:01", build.ap_ies(F("5g36"), 20, "A")))
    b = r.capture(0.05)
    assert b.receive_s == 0 and b.frames == []  # 見ていない（検知なしではない）
    r.tune(validate_tuning(F("5g36"), 20, F("5g36")))
    assert run.calls and run.calls[-1][:4] == ["iw", "dev", "wlan0", "set"]
    assert r.capture(0.05).receive_s > 0


def test_managed_scan_is_passive_only(sysnet):
    run = FakeRunner(fail=["scan"])
    r, _ = make_linux(run, sysnet)
    r.managed_scan()
    scans = [c for c in run.calls if "scan" in c]
    assert scans and all(c[-2:] == ["scan", "passive"] for c in scans)  # 受信専用：能動スキャンはしない


def test_reload_driver_only_for_iwlwifi(sysnet, tmp_path):
    # 開発PCの無線（rtw89など）のIFでは、読み込み直さない（ほかのドライバに触らない）
    other = tmp_path / "drivers" / "rtw89_8852be"
    other.mkdir(parents=True)
    link = sysnet / "wlan0" / "device" / "driver"
    link.unlink()
    link.symlink_to(other)
    run = FakeRunner()
    r, _ = make_linux(run, sysnet)
    with pytest.raises(RadioError, match="rtw89_8852be"):
        r.reload_driver()
    assert run.calls == []


def test_regulatory_parsing(sysnet):
    r, _ = make_linux(FakeRunner(), sysnet)
    g = r.regulatory()
    assert g.country == "JP" and g.six_ghz_enabled and len(g.six_ghz_freqs) == 24
    assert parse_reg(REG, "phy#0") == "JP" and parse_reg(REG, None) == "JP"
    freqs = parse_phy_freqs(PHY_DISABLED)
    assert freqs[5975] is False and freqs[5955] is True


def test_survey_parsing():
    s = parse_survey(SURVEY)
    assert s[0].freq == 5955 and s[0].in_use and s[0].noise == -92 and s[0].busy_ms == 56
    assert s[1].freq == 5975 and not s[1].in_use


def test_capture_keeps_only_tuned_channel(sysnet):
    r, feed = make_linux(FakeRunner(), sysnet)
    r.tune(validate_tuning(F("6g37"), 20, F("6g37")))
    good = build.radiotap(F("6g37"), signal=-50) + build.beacon("02:00:00:00:00:01", build.ap_ies(F("6g37"), 20, "A"))
    stale = build.radiotap(F("6g41"), signal=-50) + build.beacon("02:00:00:00:00:02", build.ap_ies(F("6g41"), 20, "B"))
    for fr in (good, stale, good, b"\x00\x00"):
        feed.send(fr)
    batch = r.capture(0.15)
    assert batch.frames == [good, good]
    assert 0.14 <= batch.receive_s <= 0.5


# ---------------------------------------------------------------- radiod越し


@pytest.fixture
def radiod(tmp_path):
    clk = RealClock()
    fake = FakeRadio(clk, Scenario([SimStation("02:00:00:00:00:01", "OWN", F("6g37"), 20, -50, data_rate=50),
                                    SimStation("02:00:00:00:00:05", "FIVE", F("5g36"), 20, -60)], seed=1),
                     switch_s=0.0)
    path = str(tmp_path / "radiod.sock")
    srv = RadioServer(fake, path, allow_uids=[os.getuid()])
    srv.bind()
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield path, fake, srv
    srv.stop()


def test_remote_radio_round_trip(radiod):
    path, fake, srv = radiod
    rr = RemoteRadio(path)
    assert rr.hello()["pid"] == os.getpid()
    rr.tune(validate_tuning(F("6g37"), 160, 6185))
    b = rr.capture(0.3)
    assert b.tuning.width == 160 and b.frames
    a = Analyzer(RealClock())
    a.process_batch(b)
    assert a.table.get("02:00:00:00:00:01").ssid == "OWN"
    reg = rr.regulatory()
    assert reg.country == "JP" and reg.six_ghz_enabled
    assert rr.managed_scan() == 2
    rr.set_monitor()
    rr.reset_monitor()
    rr.reload_driver()
    assert fake.calls[-4:] == ["managed_scan", "set_monitor", "reset_monitor", "reload_driver"]
    assert rr.unreceivable == []  # 偽の無線はすべて受けられる
    fake.unreceivable = list(ch.FREQS_BY_BAND[ch.BAND_6])  # 6GHz非対応の無線のつもり
    rr.hello()
    assert rr.unreceivable == list(ch.FREQS_BY_BAND[ch.BAND_6])
    rr.close()


def test_radiod_rejects_bad_requests(radiod):
    path, fake, srv = radiod
    rr = RemoteRadio(path)
    with pytest.raises(RadioError):
        rr._json("exec", cmd="reboot")  # 決まった操作だけ
    with pytest.raises(RadioError):
        rr._json("tune", freq=6136, width=20, center=6136)  # 日本のchでない
    with pytest.raises(RadioError):
        rr._json("tune", freq=6135, width=320, center=6105)  # 受信できない幅
    with pytest.raises(RadioError):
        rr.capture(30)  # 長すぎる
    rr.tune(validate_tuning(F("6g37"), 20, F("6g37")))  # 断ったあとも使える
    assert rr.capture(0.2).frames
    rr.close()


def test_radiod_refuses_other_uids(tmp_path):
    fake = FakeRadio(RealClock(), Scenario([]))
    path = str(tmp_path / "r.sock")
    srv = RadioServer(fake, path, allow_uids=[os.getuid() + 12345])
    srv.bind()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rr = RemoteRadio(path, timeout=2)
    with pytest.raises(RadioError):
        rr.hello()
    srv.stop()


def test_socket_permissions(radiod):
    path, fake, srv = radiod
    assert oct(os.stat(path).st_mode & 0o777) == "0o660"


def test_monitor_runs_over_radiod(radiod, tmp_path):
    import json
    from pathlib import Path
    from wifiwatch.config import Overrides, parse_config
    from wifiwatch.classify import OuiDb
    from wifiwatch.monitor import Monitor
    root = Path(__file__).resolve().parents[2]
    path, fake, srv = radiod
    d = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    d["signatures"] = str(root / "signatures.json")
    d["radio"] = {"kind": "radiod", "radiod_socket": path}
    cfg = parse_config(d)
    m = Monitor(cfg, RealClock(), overrides=Overrides(tmp_path / "o.json"), oui=OuiDb("/x"))
    m.scheduler.set_mode("LIVE")
    m.run_for(1.5)
    assert m.analyzer.table.get("02:00:00:00:00:01") is not None
    m.radio.close()


def _chancheck(sysnet, frames, capsys):
    import argparse
    from wifiwatch import hwprobe
    r, feed = make_linux(FakeRunner(), sysnet)
    r.tune = lambda t: None  # 合わせたことにする（受けたフレームを流し込む）
    for fr in frames:
        feed.send(fr)
    hwprobe.cmd_chancheck(r, argparse.Namespace(chs="2g1", seconds=0.2, relink=False))
    return capsys.readouterr().out


def test_chancheck_tells_stuck_radio_from_mislabeled_radiotap(sysnet, capsys):
    # 開発PC（rtw89）で、モニタモードなのに ch48 のフレームしか受からなかった件を見分ける道具
    ap1 = build.beacon("02:00:00:00:00:01", build.ap_ies(F("2g1"), 20, "CH1-AP", standard="n"))
    ap48 = build.beacon("02:00:00:00:00:30", build.ap_ies(F("5g48"), 20, "CH48-AP"))
    ok = _chancheck(sysnet, [build.radiotap(F("2g1"), signal=-50) + ap1] * 3, capsys)
    assert "正常" in ok
    mislabeled = _chancheck(sysnet, [build.radiotap(F("5g48"), signal=-50) + ap1] * 3, capsys)
    assert "B：radiotapのchがずれている" in mislabeled
    stuck = _chancheck(sysnet, [build.radiotap(F("5g48"), signal=-50) + ap48] * 3, capsys)
    assert "A：chが変わっていない" in stuck
    assert "何も受からない" in _chancheck(sysnet, [], capsys)


def test_separate_monitor_vif(sysnet):
    # rtw89（開発PCのRTL8852BE）は、IFそのものをモニタにすると前の接続のchから動かない。
    # 別のモニタ用IFを作ると全部のchで受かった（2026-10-16 の実機の確認）。そのやり方
    run = FakeRunner()
    a, feed = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    a.setblocking(False)
    r = LinuxRadio("wlan0", RealClock(), runner=run, socket_factory=lambda: a, sys_class_net=str(sysnet),
                   monitor_vif="wwmon0")
    assert r.capture_if == "wwmon0"
    r.set_monitor()
    assert run.calls == [["ip", "link", "set", "wlan0", "down"],
                         ["iw", "phy", "phy0", "interface", "add", "wwmon0", "type", "monitor"],
                         ["ip", "link", "set", "wwmon0", "up"]]
    run.calls.clear()
    r.tune(validate_tuning(F("2g6"), 20, F("2g6")))
    assert run.calls[0][:3] == ["iw", "dev", "wwmon0"]  # chの切り替えはモニタ用IFで
    good = build.radiotap(F("2g6"), signal=-50) + build.beacon("02:00:00:00:00:01", build.ap_ies(F("2g6"), 20, "A", standard="n"))
    feed.send(good)
    assert r.capture(0.1).frames == [good]
    # 管理モードのスキャン（起動時・復旧時）の前に、モニタ用IFを消す（このあと set_monitor で作り直す）
    (sysnet / "wwmon0").mkdir()
    run.calls.clear()
    r.managed_scan()
    assert run.calls[:2] == [["ip", "link", "set", "wwmon0", "down"], ["iw", "dev", "wwmon0", "del"]]
    assert ["iw", "dev", "wlan0", "scan", "passive"] in run.calls
    run.calls.clear()
    r.reset_monitor()  # 復旧：作り直す
    assert ["iw", "dev", "wwmon0", "del"] in run.calls
    assert run.calls[-1] == ["ip", "link", "set", "wwmon0", "up"]
    with pytest.raises(RadioError):
        LinuxRadio("wlan0", RealClock(), runner=run, sys_class_net=str(sysnet), monitor_vif="wlan0")


class PhyDisabled6Runner(PhyNo6Runner):
    """AX210 で、6GHzは載っているが全部 disabled（国の設定・ファームウェアが許していない）。合わせると失敗する。"""

    six_ok = False

    def __call__(self, argv, timeout):
        if argv[:2] == ["iw", "phy"] and argv[-1] == "info":
            lines = ["\t\t\t* 2412.0 MHz [1] (22.0 dBm)", "\t\t\t* 5180.0 MHz [36] (22.0 dBm)"]
            lines += [f"\t\t\t* {f}.0 MHz [{(f - 5950) // 5}] " + ("(22.0 dBm)" if self.six_ok else "(disabled)")
                      for f in ch.FREQS_BY_BAND[ch.BAND_6]]
            self.calls.append(argv)
            return RunResult(0, "\n".join(lines), "")
        if argv[:2] == ["iw", "dev"] and "set" in argv and int(argv[argv.index("freq") + 1]) > 5925 and not self.six_ok:
            self.calls.append(argv)
            return RunResult(1, "", "command failed: Invalid argument (-22) / kernel reports: (extension) channel is disabled")
        return super().__call__(argv, timeout)


def test_disabled_channels_are_not_tuned_and_recover_when_enabled(sysnet):
    # 本番PCの AX210 で、6GHz が全部「channel is disabled」になり、巡回のたびに失敗してログが埋まった
    run = PhyDisabled6Runner()
    r, _ = make_linux(run, sysnet)
    r.load_hw_freqs()
    six = list(ch.FREQS_BY_BAND[ch.BAND_6])
    assert r.disabled_freqs == six and set(six) <= set(r.unreceivable)  # 「見ていない（無線で無効）」として扱う
    run.calls.clear()
    r.tune(validate_tuning(F("6g37"), 20, F("6g37")))
    assert not any("set" in c for c in run.calls)  # 合わせに行かない（失敗にもしない）
    assert r.capture(0.05).receive_s == 0
    # 国の設定が変わって使えるようになったら、60秒ごとの読み直しで戻る
    run.six_ok = True
    r._hw_at = -1e9  # 60秒たったことにする
    r.tune(validate_tuning(F("6g37"), 20, F("6g37")))
    assert r.disabled_freqs == [] and not r.unreceivable_tuned


def test_keep_regdom_brings_monitor_vif_up_before_taking_managed_down(sysnet):
    # AX210：無線を一度下ろすと国を忘れて6GHzが disabled になる（本番PCで確かめた）。管理モードのIFを上げたまま
    # モニタ用IFを足して上げ、そのあとで管理モードのIFを下げる（無線を止めない）
    run = FakeRunner()
    a, _ = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    r = LinuxRadio("wlan0", RealClock(), runner=run, socket_factory=lambda: a, sys_class_net=str(sysnet),
                   monitor_vif="wwmon0", keep_regdom=True)
    r.managed_scan()
    run.calls.clear()
    r.set_monitor()
    seq = [" ".join(c) for c in run.calls]
    add = seq.index("iw phy phy0 interface add wwmon0 type monitor")
    up_vif = seq.index("ip link set wwmon0 up")
    down_if = seq.index("ip link set wlan0 down")
    assert add < up_vif < down_if  # モニタ用IFが上がってから、管理モードのIFを下げる
    assert "ip link set wlan0 up" in seq[:add]  # その前は管理モードのIFを上げたまま
    assert not any("set type monitor" in c for c in seq)  # 管理モードのIFそのものはモニタにしない
    assert r.capture_if == "wwmon0"
    # 復旧のモニタの再設定は、先に管理モードでスキャンして国を覚え直してから
    run.calls.clear()
    r.reset_monitor()
    seq = [" ".join(c) for c in run.calls]
    assert seq.index("iw dev wlan0 scan passive") < seq.index("iw phy phy0 interface add wwmon0 type monitor")
    with pytest.raises(RadioError):
        LinuxRadio("wlan0", RealClock(), runner=run, keep_regdom=True)  # 別のモニタ用IFが要る


def test_link_up_is_retried_after_io_error(sysnet):
    # モニタ用IFを消した直後、管理モードのIFを上げると「Input/output error」で断られた（本番PCの AX210）。少し待って上げ直す
    class FlakyUp(FakeRunner):
        fails = 1

        def __call__(self, argv, timeout):
            if list(argv) == ["ip", "link", "set", "wlan0", "up"] and self.fails:
                self.fails -= 1
                self.calls.append(list(argv))
                return RunResult(2, "", "RTNETLINK answers: Input/output error")
            return super().__call__(argv, timeout)

    run = FlakyUp()
    r, _ = make_linux(run, sysnet)
    r.managed_scan()
    assert [c for c in run.calls if c == ["ip", "link", "set", "wlan0", "up"]].__len__() == 2  # 1回断られて、2回目で上がった
