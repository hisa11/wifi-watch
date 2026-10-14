"""合成フレームとIEの試験データを testdata/ に書き出す（PC側・中継・アプリで共用）。

期待値は解析器の出力ではなく、生成の入力（作ろうとしたもの）から書く。
使い方：  .venv/bin/python tools/gen_testdata.py [出力先]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from wifiwatch import channels as ch  # noqa: E402
from wifiwatch.capture import build  # noqa: E402

F = ch.parse_channel_ref


def span(center: float, width: float) -> list[float]:
    return [center - width / 2, center + width / 2]


def ie_cases() -> list[dict]:
    cases: list[dict] = []

    def add(name: str, rx: str, ies: bytes, primary: str, width: int, spans: list[list[float]], *,
            label: str | None = None, standard: str | None = None, security: str | None = None,
            ssid: str | None = None, dup: bool | None = None, extra: dict | None = None) -> None:
        exp: dict = {
            "primary_freq": F(primary),
            "width": width,
            "width_label": label or str(width),
            "spans": spans,
        }
        if standard:
            exp["standard"] = standard
        if security:
            exp["security"] = security
        if ssid is not None:
            exp["ssid"] = ssid
        if dup is not None:
            exp["dup_beacon"] = dup
        if extra:
            exp.update(extra)
        cases.append({"name": name, "rx_freq": F(rx), "ies_hex": ies.hex(), "expect": exp})

    # 6GHz
    add("6g_20_psc", "6g37", build.ap_ies(F("6g37"), 20, "T6-20"), "6g37", 20, [span(6135, 20)],
        standard="ax", security="WPA3-SAE", ssid="T6-20", dup=False)
    add("6g_40", "6g9", build.ap_ies(F("6g9"), 40, "T6-40"), "6g9", 40, [span(6005, 40)], standard="ax")
    add("6g_80", "6g37", build.ap_ies(F("6g37"), 80, "T6-80"), "6g37", 80, [span(6145, 80)])
    add("6g_160", "6g37", build.ap_ies(F("6g37"), 160, "T6-160"), "6g37", 160, [span(6185, 160)])
    # 古い書き方：ccfs1=0でccfs0に160MHzの中心
    legacy = (build.ssid_ie("T6-160L") + build.rsn_ie() + build.he_cap_ie()
              + build.he_op_ie(3, six=(37, 3, 47, 0, False)))
    add("6g_160_ccfs1_zero", "6g37", legacy, "6g37", 160, [span(6185, 160)])
    add("6g_320_low", "6g49", build.ap_ies(F("6g49"), 320, "T6-320a"), "6g49", 320, [span(6105, 320)],
        standard="be")
    add("6g_320_high", "6g49",
        build.ap_ies(F("6g49"), 320, "T6-320b", eht_width_override=(4, 47, 63)), "6g49", 320,
        [span(6265, 320)], standard="be")
    # 重複ビーコン：主chはHE Operationのもの（受信したのはch45）
    add("6g_80_dup_rx_on_other_ch", "6g45", build.ap_ies(F("6g37"), 80, "T6-DUP", dup_beacon=True),
        "6g37", 80, [span(6145, 80)], dup=True)
    # EHT Operationに幅の情報がない（HEの幅を使う）
    add("6g_be_160_no_eht_info", "6g69", build.ap_ies(F("6g69"), 160, "T6-BE160", standard="be"),
        "6g69", 160, [span(6345, 160)], standard="be")
    # 5GHz
    add("5g_20", "5g36", build.ap_ies(F("5g36"), 20, "T5-20"), "5g36", 20, [span(5180, 20)],
        standard="ac", security="WPA2-PSK")
    add("5g_40_below", "5g40", build.ap_ies(F("5g40"), 40, "T5-40", standard="n"), "5g40", 40,
        [span(5190, 40)], standard="n")
    add("5g_80", "5g52", build.ap_ies(F("5g52"), 80, "T5-80"), "5g52", 80, [span(5290, 80)])
    add("5g_160", "5g100", build.ap_ies(F("5g100"), 160, "T5-160"), "5g100", 160, [span(5570, 160)])
    p8080 = (build.ssid_ie("T5-8080") + build.ht_cap_ie() + build.ht_op_ie(36, 1, True)
             + build.vht_cap_ie() + build.vht_op_ie(1, 42, 106))
    add("5g_80p80", "5g36", p8080, "5g36", 160, [span(5210, 80), span(5530, 80)], label="80+80",
        security="open")
    add("5g_ax_80", "5g116", build.ap_ies(F("5g116"), 80, "T5-AX", standard="ax"), "5g116", 80,
        [span(5610, 80)], standard="ax")
    # 2.4GHz
    add("2g_20", "2g1", build.ap_ies(F("2g1"), 20, "T2-20"), "2g1", 20, [span(2412, 20)], standard="n")
    add("2g_40_above", "2g6", build.ap_ies(F("2g6"), 40, "T2-40"), "2g6", 40, [span(2447, 40)])
    # 隠れSSID・暗号方式
    add("hidden_ssid", "6g5", build.ap_ies(F("6g5"), 20, "secret", hidden=True), "6g5", 20,
        [span(5975, 20)], ssid=None, extra={"ssid_hidden": True})
    add("owe", "6g21", build.ap_ies(F("6g21"), 20, "OPEN6", akms=[18]), "6g21", 20, [span(6055, 20)],
        security="OWE")
    add("sae_psk_mixed", "5g44", build.ap_ies(F("5g44"), 20, "MIX", akms=[2, 8], mfpr=False), "5g44", 20,
        [span(5220, 20)], security="WPA3-SAE/PSK", extra={"pmf_required": False})
    # WPS
    add("wps", "2g11",
        build.ap_ies(F("2g11"), 20, "Buffalo-G-1234", wps={"manufacturer": "BUFFALO INC.",
                                                         "model_name": "WSR-3200AX4S",
                                                         "model_number": "1.0"}),
        "2g11", 20, [span(2462, 20)],
        extra={"wps": {"manufacturer": "BUFFALO INC.", "model_name": "WSR-3200AX4S", "model_number": "1.0"}})
    # RNR（5GHzのビーコンが6GHzの局を知らせる）。長さ13・12・7・1の形
    rnr = build.rnr_ie([
        (131, 37, [build.RnrAp("aa:bb:cc:00:00:06", build.short_ssid("RNR-6G"), 0x42, -10)]),
        (134, 69, [build.RnrAp("aa:bb:cc:00:00:07", build.short_ssid("RNR-6G2"), 0x00)]),
        (133, 5, [build.RnrAp("aa:bb:cc:00:00:08")]),
        (131, 85, [build.RnrAp()]),
    ])
    add("rnr_from_5g", "5g36", build.ap_ies(F("5g36"), 20, "RNR-5G", standard="ax", rnr=rnr), "5g36", 20,
        [span(5180, 20)],
        extra={"rnr": [
            {"freq": F("6g37"), "bssid": "aa:bb:cc:00:00:06", "colocated": True, "same_ssid": True},
            {"freq": F("6g69"), "bssid": "aa:bb:cc:00:00:07", "colocated": False, "same_ssid": False},
            {"freq": F("6g5"), "bssid": "aa:bb:cc:00:00:08", "colocated": False, "same_ssid": False},
            {"freq": F("6g85"), "bssid": None, "colocated": False, "same_ssid": False},
        ]})
    return cases


def frame_cases() -> list[dict]:
    cases: list[dict] = []
    bss = "02:11:22:33:44:55"
    sta = "02:66:77:88:99:aa"

    def add(name: str, rt: bytes, frame: bytes, expect: dict) -> None:
        cases.append({"name": name, "hex": (rt + frame).hex(), "expect": expect})

    ies = build.ap_ies(F("6g37"), 160, "FRAME-6G")
    add("beacon_6g_two_chains_fcs",
        build.radiotap(6135, signal=-50, chains=[-53, -54], flags=0x10, tsft=1000),
        build.with_fcs(build.beacon(bss, ies)),
        {"freq": 6135, "signal": -50, "chains": [-53, -54], "fcs_at_end": True, "bad_fcs": False,
         "ftype": 0, "subtype": 8, "retry": False, "protected": False, "bssid": bss, "ssid": "FRAME-6G",
         "primary_freq": 6135, "width": 160})
    add("beacon_vendor_ns",
        build.radiotap(5180, signal=-61, chains=[-64, -64], flags=0, vendor_ns=True),
        build.beacon(bss, build.ap_ies(F("5g36"), 20, "VNS")),
        {"freq": 5180, "signal": -61, "chains": [-64, -64], "fcs_at_end": False, "bad_fcs": False,
         "ftype": 0, "subtype": 8, "bssid": bss, "ssid": "VNS"})
    add("beacon_single_signal_no_chains",
        build.radiotap(2412, signal=-70),
        build.beacon(bss, build.ap_ies(F("2g1"), 20, "ONE")),
        {"freq": 2412, "signal": -70, "chains": [], "ftype": 0, "subtype": 8, "ssid": "ONE"})
    add("deauth_plain",
        build.radiotap(6135, signal=-40, chains=[-43, -43], flags=0x10),
        build.with_fcs(build.deauth(sta, bss, bss, reason=7)),
        {"freq": 6135, "ftype": 0, "subtype": 12, "protected": False, "addr1": sta, "addr2": bss,
         "addr3": bss, "reason": 7})
    add("disassoc_protected",
        build.radiotap(6135, signal=-40, chains=[-43, -43]),
        build.deauth(sta, bss, bss, disassoc=True, protected=True),
        {"freq": 6135, "ftype": 0, "subtype": 10, "protected": True, "addr1": sta, "addr2": bss})
    add("data_to_ap_retry",
        build.radiotap(6135, signal=-55, chains=[-58, -58], flags=0x10),
        build.with_fcs(build.data(bss, sta, to_ap=True, retry=True)),
        {"freq": 6135, "ftype": 2, "subtype": 8, "retry": True, "protected": True, "bssid": bss,
         "client": sta, "hdr_len": 26})
    add("data_from_ap",
        build.radiotap(6135, signal=-55, chains=[-58, -58]),
        build.data(bss, sta, to_ap=False, retry=False, qos=False),
        {"freq": 6135, "ftype": 2, "subtype": 0, "retry": False, "bssid": bss, "client": sta, "hdr_len": 24})
    add("ack", build.radiotap(6135, signal=-60, chains=[-63, -63]), build.ack(sta),
        {"freq": 6135, "ftype": 1, "subtype": 13, "addr1": sta, "addr2": None})
    add("rts", build.radiotap(6135, signal=-60, chains=[-63, -63]), build.rts(bss, sta),
        {"freq": 6135, "ftype": 1, "subtype": 11, "addr1": bss, "addr2": sta})
    add("bad_fcs",
        build.radiotap(6135, signal=-80, chains=[-83, -83], flags=0x50),
        build.with_fcs(build.ack(sta), bad=True),
        {"freq": 6135, "fcs_at_end": True, "bad_fcs": True})
    add("vht_bw_160",
        build.radiotap(5180, signal=-60, vht_bw=160),
        build.data(bss, sta, to_ap=True),
        {"freq": 5180, "ppdu_bw": 160, "ftype": 2})
    add("he_bw_80",
        build.radiotap(6135, signal=-60, he_bw=80, tsft=5),
        build.data(bss, sta, to_ap=False),
        {"freq": 6135, "ppdu_bw": 80, "ftype": 2})
    return cases


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "ies_cases.json").write_text(json.dumps(ie_cases(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "frame_cases.json").write_text(json.dumps(frame_cases(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    channels = [{"freq": c.freq, "band": c.band, "number": c.number, "psc": c.is_psc} for c in ch.ALL_CHANNELS]
    (out / "channels.json").write_text(json.dumps(channels, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[2] / "testdata")
