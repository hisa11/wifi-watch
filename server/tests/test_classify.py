import copy
import json
from pathlib import Path

from wifiwatch import channels as ch
from wifiwatch.analyzer.core import Analyzer
from wifiwatch.capture import build
from wifiwatch.classify import Classifier, OuiDb, load_signatures
from wifiwatch.clock import FakeClock
from wifiwatch.config import parse_config
from wifiwatch.radio.base import FrameBatch, validate_tuning

ROOT = Path(__file__).resolve().parents[2]
F = ch.parse_channel_ref
EX = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
SIGS = load_signatures(ROOT / "signatures.json")
OUI = OuiDb()


def table_with(*aps):
    """aps: (bssid, ssid, ref, width, ap_kwargs)"""
    clk = FakeClock()
    a = Analyzer(clk)
    for bssid, ssid, ref, width, kw in aps:
        f = F(ref)
        fr = build.radiotap(f, signal=-60) + build.beacon(bssid, build.ap_ies(f, width, ssid, **kw))
        a.process_batch(FrameBatch(validate_tuning(f, 20, f), 0, 1, [fr]))
    return a.table


def classifier(mut=None, tags=None):
    d = copy.deepcopy(EX)
    if mut:
        mut(d)
    return Classifier(parse_config(d), SIGS, OUI, tags)


def test_kinds_and_priority():
    t = table_with(
        ("02:00:00:00:00:01", "OUR-ROBOT-6G", "6g37", 20, {}),
        ("02:aa:00:00:00:01", "OUR-ROBOT-6G", "6g41", 20, {}),  # 同名SSIDの別AP：自チームにしない
        ("02:a0:00:00:00:01", "KOSEN-A-6G", "6g5", 20, {}),
        ("02:52:46:00:00:01", "ROBOCON_Free_Wi-Fi", "5g100", 20, {}),
        ("02:52:46:00:00:02", "ROBOCON_Staff_1", "5g104", 20, {}),
        ("02:6c:5a:00:00:01", "CineEye2-1234", "5g44", 40, {"standard": "ac"}),
        ("02:99:00:00:00:01", "GEAR Arx6duc2", "5g48", 20, {}),
    )
    c = classifier()
    c.classify_all(t)
    k = {s.bssid: (s.kind, s.team, s.signature, s.negotiable) for s in t.all()}
    assert k["02:00:00:00:00:01"] == ("own", "自チーム", None, None)
    assert k["02:aa:00:00:00:01"][0] == "unknown"
    assert k["02:a0:00:00:00:01"] == ("team", "A高専", None, True)
    assert k["02:52:46:00:00:01"] == ("official_infra", None, "RoboconFree", False)
    assert k["02:52:46:00:00:02"] == ("official_infra", None, "RoboconStaff", False)
    assert k["02:6c:5a:00:00:01"] == ("broadcast", None, "CineEye", False)
    assert k["02:99:00:00:00:01"][0] == "unknown"  # GEAR Arx6duc2 は署名に入れない


def test_manual_tag_beats_ssid_pattern_and_known_ok():
    t = table_with(("02:a0:00:00:00:09", "KOSEN-A-X", "6g13", 20, {}), ("02:98:00:00:00:01", "PIT", "6g17", 20, {}))
    c = classifier(lambda d: d.__setitem__("known_ok_bssids", ["02:98:00:00:00:01"]), tags={"02:a0:00:00:00:09": "B高専"})
    c.classify_all(t)
    assert t.get("02:a0:00:00:00:09").team == "B高専" and t.get("02:a0:00:00:00:09").team_source == "tag"
    assert t.get("02:98:00:00:00:01").kind == "known_ok"
    c.set_tag("02:a0:00:00:00:09", None)
    c.classify_all(t)
    assert t.get("02:a0:00:00:00:09").team == "A高専" and t.get("02:a0:00:00:00:09").team_source == "ssid"


def test_model_guess_sources():
    t = table_with(
        ("00:1d:73:00:00:01", "Buffalo-G-1", "2g1", 20, {"wps": {"manufacturer": "BUFFALO INC.", "model_name": "WSR-3200AX4S"}, "standard": "n"}),
        ("24:6f:28:00:00:01", "ESP_1A2B3C", "2g6", 20, {"standard": "n"}),
        ("b8:27:eb:00:00:01", "robot-pi", "2g11", 20, {"standard": "n"}),
        ("02:12:34:56:78:9a", "DIRECT-xy-Android", "6g21", 20, {}),
        ("06:12:34:56:78:9a", "AndroidAP_1234", "5g36", 20, {}),
    )
    c = classifier()
    c.classify_all(t)
    g = {s.bssid: (s.model_guess, s.hints) for s in t.all()}
    assert g["00:1d:73:00:00:01"][0] == "BUFFALO INC. WSR-3200AX4S"
    assert g["24:6f:28:00:00:01"][0] == "ESP系マイコン" and any("Espressif" in h for h in g["24:6f:28:00:00:01"][1])
    assert any("Raspberry Pi" in h for h in g["b8:27:eb:00:00:01"][1])
    assert g["02:12:34:56:78:9a"][0] == "Wi-Fi Direct"
    assert "ランダムMAC（ローカル管理）" in g["02:12:34:56:78:9a"][1]
    assert g["06:12:34:56:78:9a"][0] == "スマホのテザリング（Android）"


def test_rnr_links_6ghz_to_24_5ghz_side_for_model_and_team():
    rnr = build.rnr_ie([(131, 5, [build.RnrAp("00:1d:73:00:06:01", build.short_ssid("KOSEN-A-6G"), 0x42)])])
    t = table_with(
        ("00:1d:73:00:05:01", "KOSEN-A-5G", "5g36", 20,
         {"rnr": rnr, "wps": {"manufacturer": "BUFFALO INC.", "model_name": "WXR18000BE10P"}, "standard": "ax"}),
        ("00:1d:73:00:06:01", "hidden6", "6g5", 20, {}),
    )
    c = classifier(lambda d: d["teams"][0].__setitem__("ssids", ["KOSEN-A-5G"]))
    c.classify_all(t)
    six = t.get("00:1d:73:00:06:01")
    assert "00:1d:73:00:05:01" in six.linked
    assert six.team == "A高専" and six.team_source == "rnr"
    assert six.model_guess == "BUFFALO INC. WXR18000BE10P"
    assert any("RNR" in h for h in six.hints)


def test_oui_db_lookup_longest_prefix():
    if not OUI.loaded:
        return
    assert "Raspberry" in OUI.lookup("b8:27:eb:12:34:56")
    assert OUI.lookup("02:00:00:00:00:00") is None


def test_signature_file_validation(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"signatures": [{"name": "X", "ssid": ["X*"], "kind": "alien"}]}), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        load_signatures(p)
    p.write_text(json.dumps({"signatures": [{"name": "Y", "ssid": [], "oui": ["AA:BB:CC"], "kind": "broadcast"}]}), encoding="utf-8")
    s = load_signatures(p)
    assert s[0].on_6ghz == "crit" and s[0].oui == ["aa:bb:cc"] and not s[0].negotiable
