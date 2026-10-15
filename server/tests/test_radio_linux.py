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
        if argv[-1] == "scan":
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
