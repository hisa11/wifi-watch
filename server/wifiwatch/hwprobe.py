"""実機の確認の道具（docs/HARDWARE_TEST.md）。rootで、モニタモードにしたIFに対して使う。

受信専用。数えて要約を出すだけで、フレームそのもの（pcap）は保存しない。データフレームはBPFで
MACヘッダまでに切ってから受ける。

  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 frames --ch 6g37 --width 160 --seconds 10
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 switch --count 300
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 survey
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 eht --seconds 30
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 reload-check
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 bench --ch 6g37 --width 160 --seconds 30
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 own-rssi --bssid aa:bb:.. --minutes 10
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter

from . import channels as ch
from .analyzer.core import Analyzer
from .capture import dot11, ies, radiotap
from .clock import RealClock
from .radio.base import RadioError, validate_tuning
from .radio.linux import LinuxRadio


def tuning_for(ref: str, width: int):
    f = ch.parse_channel_ref(ref)
    if width == 20:
        return validate_tuning(f, 20, f)
    b = ch.block_for(f, width)
    if b is None:
        raise SystemExit(f"{ch.label(f)} に {width}MHz のブロックがありません")
    return validate_tuning(f, width, b.center_freq)


def cmd_frames(r: LinuxRadio, a: argparse.Namespace) -> int:
    t = tuning_for(a.ch, a.width)
    r.tune(t)
    types: Counter[str] = Counter()
    bw: Counter[int] = Counter()
    bad = 0
    combos = []
    an = Analyzer(RealClock())
    end = time.monotonic() + a.seconds
    n = 0
    while time.monotonic() < end:
        b = r.capture(min(0.5, end - time.monotonic()))
        an.process_batch(b)
        for fr in b.frames:
            n += 1
            try:
                rx = radiotap.parse(fr)
                h = dot11.parse_header(fr, rx.length, len(fr))
            except (radiotap.RadiotapError, dot11.Dot11Error):
                types["壊れた"] += 1
                continue
            if rx.bad_fcs:
                bad += 1
            types[{0: "管理", 1: "制御", 2: "データ"}.get(h.ftype, "その他")] += 1
            if rx.ppdu_bw:
                bw[rx.ppdu_bw] += 1
            if rx.chains and len(combos) < 5:
                combos.append((rx.driver_signal, [s for _, s in rx.chains], rx.signal))
    print(f"[frames] {ch.label(t.freq)} {t.width}MHz を {a.seconds}秒：{n}フレーム（{n / a.seconds:.0f}/秒）")
    print(f"  種類：{dict(types)}  FCS不良：{bad}")
    print(f"  PPDUの帯域（radiotap）：{dict(sorted(bw.items())) or '情報なし'}")
    if combos:
        print("  強さの例（ドライバの値, アンテナごと, 合成）：")
        for c in combos:
            print(f"    {c}")
    print("  局：")
    for st in sorted(an.table.all(), key=lambda s: s.rssi or -200, reverse=True):
        print(f"    {st.bssid} {st.ssid or '(非公開)'!s:24} 主{ch.label(st.primary_freq)} {st.width_label}MHz "
              f"{st.rssi}dBm {st.standard} 受けたch={sorted(ch.freq_to_number(f) for f in st.seen_on)}")
    return 0


def cmd_switch(r: LinuxRadio, a: argparse.Namespace) -> int:
    freqs = list(ch.ALL_FREQS)
    times = []
    fails = Counter()
    for i in range(a.count):
        f = freqs[i % len(freqs)]
        t0 = time.monotonic()
        try:
            r.tune(validate_tuning(f, 20, f))
        except RadioError as e:
            fails[str(e)[:80]] += 1
        times.append((time.monotonic() - t0) * 1000)
        time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))
    times.sort()
    print(f"[switch] {a.count}回（{a.interval * 1000:.0f}msおき）：失敗 {sum(fails.values())}")
    print(f"  切替にかかった時間（ms）：中央値 {statistics.median(times):.1f}・95% {times[int(len(times) * 0.95) - 1]:.1f}・最大 {times[-1]:.1f}")
    for k, v in fails.most_common(5):
        print(f"  失敗：{v}回 {k}")
    return 0 if not fails else 1


def cmd_survey(r: LinuxRadio, a: argparse.Namespace) -> int:
    s = r.survey()
    if s is None:
        print("[survey] 使えません（survey dump が失敗）")
        return 1
    print(f"[survey] {len(s)}件")
    for e in s:
        busy = f"{e.busy_ms / e.active_ms * 100:.0f}%" if e.busy_ms and e.active_ms else "—"
        print(f"  {e.freq}MHz{'（使用中）' if e.in_use else ''} noise={e.noise} active={e.active_ms}ms busy={e.busy_ms}ms（{busy}）")
    return 0


def cmd_eht(r: LinuxRadio, a: argparse.Namespace) -> int:
    an = Analyzer(RealClock())
    per = a.seconds / len(ch.FREQS_BY_BAND[ch.BAND_6])
    for f in ch.FREQS_BY_BAND[ch.BAND_6]:
        r.tune(validate_tuning(f, 20, f))
        an.process_batch(r.capture(max(0.25, per)))
    six = [s for s in an.table.all() if s.band == ch.BAND_6]
    print(f"[eht] 6GHzの局 {len(six)}")
    for s in sorted(six, key=lambda s: s.primary_freq):
        print(f"  {s.bssid} {s.ssid or '(非公開)'!s:24} 主{ch.label(s.primary_freq)} 幅{s.width_label}（{s.op_source}） "
              f"規格{s.standard} 320MHz対応={s.eht_320_cap} 重複ビーコン={s.dup_beacon}")
    return 0


def cmd_reload(r: LinuxRadio, a: argparse.Namespace) -> int:
    print("[reload-check] iwlwifi を読み込み直します（受信が数秒止まります）")
    r.reload_driver()
    g1 = r.regulatory()
    print(f"  読み込み直した直後：国={g1.country} 6GHz={'有効' if g1.six_ghz_enabled else '無効'}（{len(g1.six_ghz_freqs)}/24ch）")
    n = r.managed_scan()
    g2 = r.regulatory()
    print(f"  管理モードでスキャン（{n}局）のあと：国={g2.country} 6GHz={'有効' if g2.six_ghz_enabled else '無効'}（{len(g2.six_ghz_freqs)}/24ch）")
    r.set_monitor()
    print("  モニタモードに戻しました")
    return 0 if g2.six_ghz_enabled else 1


def cmd_bench(r: LinuxRadio, a: argparse.Namespace) -> int:
    t = tuning_for(a.ch, a.width)
    r.tune(t)
    an = Analyzer(RealClock())
    end = time.monotonic() + a.seconds
    n = 0
    busy = 0.0
    while time.monotonic() < end:
        b = r.capture(0.5)
        t0 = time.process_time()
        an.process_batch(b)
        busy += time.process_time() - t0
        n += len(b.frames)
    print(f"[bench] {a.seconds}秒で{n}フレーム（{n / a.seconds:.0f}/秒）、解析のCPU時間 {busy:.2f}秒"
          f"（{busy / a.seconds * 100:.1f}%）→ 1コアで約{(n / busy if busy else 0):,.0f}フレーム/秒まで")
    return 0


def cmd_own(r: LinuxRadio, a: argparse.Namespace) -> int:
    bssid = a.bssid.lower()
    t = tuning_for(a.ch, a.width)
    r.tune(t)
    print(f"[own-rssi] {bssid} を {ch.label(t.freq)} で {a.minutes}分（10秒ごとに1行。CSV：時刻,中央値,最小,最大,個数）")
    end = time.monotonic() + a.minutes * 60
    while time.monotonic() < end:
        vals = []
        stop = time.monotonic() + 10
        while time.monotonic() < stop:
            for fr in r.capture(0.5).frames:
                try:
                    rx = radiotap.parse(fr)
                    h = dot11.parse_header(fr, rx.length, len(fr))
                except (radiotap.RadiotapError, dot11.Dot11Error):
                    continue
                if h.addr2 and h.addr2.hex(":") == bssid and rx.signal is not None:
                    vals.append(rx.signal)
        ts = time.strftime("%H:%M:%S")
        if vals:
            print(f"{ts},{statistics.median(vals):.0f},{min(vals)},{max(vals)},{len(vals)}", flush=True)
        else:
            print(f"{ts},,,,0", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hwprobe", description="実機の確認（受信専用・要約だけ出す）")
    ap.add_argument("--interface", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("frames")
    p.add_argument("--ch", default="6g1")
    p.add_argument("--width", type=int, default=20)
    p.add_argument("--seconds", type=float, default=10)
    p = sub.add_parser("switch")
    p.add_argument("--count", type=int, default=300)
    p.add_argument("--interval", type=float, default=0.25)
    sub.add_parser("survey")
    p = sub.add_parser("eht")
    p.add_argument("--seconds", type=float, default=30)
    sub.add_parser("reload-check")
    p = sub.add_parser("bench")
    p.add_argument("--ch", default="6g37")
    p.add_argument("--width", type=int, default=160)
    p.add_argument("--seconds", type=float, default=30)
    p = sub.add_parser("own-rssi")
    p.add_argument("--bssid", required=True)
    p.add_argument("--ch", default="6g37")
    p.add_argument("--width", type=int, default=20)
    p.add_argument("--minutes", type=float, default=10)
    a = ap.parse_args(argv)
    r = LinuxRadio(a.interface)
    try:
        return {"frames": cmd_frames, "switch": cmd_switch, "survey": cmd_survey, "eht": cmd_eht,
                "reload-check": cmd_reload, "bench": cmd_bench, "own-rssi": cmd_own}[a.cmd](r, a)
    except RadioError as e:
        print(f"失敗：{e}", file=sys.stderr)
        return 2
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
