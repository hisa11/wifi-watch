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
  sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 chancheck --chs 2g1,2g6,2g11,5g36,5g48 --seconds 2
"""
from __future__ import annotations

import argparse
import re
import select
import statistics
import sys
import struct
import time
from collections import Counter
from typing import Any

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


_ST_BEACON, _ST_PROBE_RESP = 8, 5


def _passes(spec: str) -> list[tuple[float, int]]:
    """「1chに居る秒数x周回数」をカンマで並べたもの（例 2x1,0.25x6）。"""
    out = []
    for x in (y.strip() for y in spec.split(",") if y.strip()):
        m = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+)", x)
        if not m or not 0.05 <= float(m[1]) <= 10 or not 1 <= int(m[2]) <= 50:
            raise SystemExit(f"--passes が不正です: {x}（例 2x1,0.25x6）")
        out.append((float(m[1]), int(m[2])))
    return out


def _read_ref(path: str) -> dict[str, tuple[int, float | None]]:
    """管理モードのスキャンの手本（1行に「BSSID 周波数 強さ」）。"""
    ref: dict[str, tuple[int, float | None]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            x = line.split()
            if len(x) >= 2 and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", x[0].lower()):
                try:
                    ref[x[0].lower()] = (int(float(x[1])), float(x[2]) if len(x) > 2 else None)
                except ValueError:
                    continue
    return ref


def _rt_words(fr: bytes, length: int) -> list[str]:
    words, off = [], 4
    while off + 4 <= length:
        w = struct.unpack_from("<I", fr, off)[0]
        words.append(f"{w:08x}")
        off += 4
        if not w & 0x80000000:
            break
    return words


def cmd_rtdump(r: LinuxRadio, a: argparse.Namespace) -> int:
    """APの強さの読み取りを確かめる。chごとに受けたビーコン・プローブ応答を、APごとにまとめて出す。

    - 本体と同じフレーム（保護なしのビーコン・プローブ応答、radiotap のchが合わせたchと同じ、FCSが正しい）を数える。
    - 「長」「短」…：--passes の1つずつ（1chに居る秒数x周回数）。APの主chで受けたフレームの読み取りの中央（最小〜最大）とフレーム数。
    - 「本体」：本体と同じ計算（隣のchで受けたものも含めて、受けた順に 0.8・0.2 の移動平均）にした値。画面の強さはこれ。
    - --ref があれば、管理モードのスキャンの強さ（手本）を並べる。
    - 外れた値（-25dBm以上、または中央より12dB以上強い）のフレームだけ、radiotap の頭（受信の情報。通信の中身ではない）を出す。
    データフレームは見ない。ビーコンからは主chとSSIDだけを読む。
    """
    chs = [tuning_for(x.strip(), 20) for x in a.chs.split(",") if x.strip()]
    passes = _passes(a.passes)
    ref = _read_ref(a.ref) if a.ref else {}
    freqs = {t.freq for t in chs}
    aps: dict[str, dict[str, Any]] = {}
    cnt = Counter()
    for pi, (dwell, rounds) in enumerate(passes):
        for _ in range(rounds):
            for t in chs:
                r.tune(t)
                end = time.monotonic() + dwell
                while (left := end - time.monotonic()) > 0:
                    b = r.capture(min(0.5, max(0.05, left)))
                    for fr in b.frames:
                        _rt_note(bytes(fr), t.freq, pi, len(passes), aps, cnt)
    names = ["長", "短"] if len(passes) == 2 and passes[0][0] > passes[1][0] else [f"P{i + 1}" for i in range(len(passes))]
    print(f"[rtdump] {len(chs)}ch。" + "・".join(f"{n}＝1chに{d:g}秒×{k}周" for n, (d, k) in zip(names, passes))
          + "。値は dBm、中央(最小〜最大)n=フレーム数")
    print(f"  受けた管理フレーム {cnt['mgmt']}・強さなし {cnt['nosig']}・ドライバの値が0以上 {cnt['drv_pos']}・"
          f"radiotapの読み違い {cnt['rt_err']}・ch違い {cnt['wrong_freq']}")

    def fmt(v: list[int]) -> str:
        if not v:
            return "     —        "
        return f"{round(statistics.median(v)):>4}({min(v)}〜{max(v)})n{len(v)}"

    rows = sorted(aps.items(), key=lambda kv: (kv[1]["freq"] > 3000, kv[1]["freq"],
                                               -(ref.get(kv[0], (0, None))[1] or -200)))
    for bssid, d in rows:
        rf = ref.get(bssid)
        line = f"  {ch.label(d['freq']).replace('2.4GHz ch', '2g').replace('GHz ch', 'g'):<5} {bssid} {(d['ssid'] or '(非公開)')[:14]:<14}"
        line += f" 手本{rf[1]:>4.0f}" if rf and rf[1] is not None else " 手本   —"
        for n, v in zip(names, d["p"]):
            line += f" | {n}{fmt(v)}"
        line += f" | 本体{round(d['ema']):>4}" if d["ema"] is not None else " | 本体  —"
        print(line)
    only = sorted(((b, f, s) for b, (f, s) in ref.items() if b not in aps and f in freqs and s is not None), key=lambda x: -x[2])
    if only:
        print(f"  スキャンだけで見えた（モニタで受けなかった）{len(only)}局：" + "、".join(f"{ch.label(f)} {s:.0f}" for _, f, s in only[:8]))
    odd = [(b, d["odd"]) for b, d in rows if d["odd"]]
    if odd:
        print("  外れた値のフレーム（APごとに一番強いもの）：")
    for bssid, (sig, drv, chains, pi, tf, sub, words, hx) in odd[:8]:
        print(f"  {bssid} {names[pi]} 受けたch={ch.label(tf)} {'ビーコン' if sub == _ST_BEACON else 'プローブ応答'} "
              f"読み取り={sig} ドライバ={drv} アンテナごと={chains} present={','.join(words)}")
        print(f"    頭={hx}")
    return 0


def _rt_note(fr: bytes, tuned: int, pi: int, npass: int, aps: dict[str, dict[str, Any]], cnt: Counter) -> None:
    try:
        rx = radiotap.parse(fr)
        h = dot11.parse_header(fr, rx.length, len(fr))
    except (radiotap.RadiotapError, dot11.Dot11Error):
        cnt["rt_err"] += 1
        return
    if h.ftype != 0 or h.subtype not in (_ST_BEACON, _ST_PROBE_RESP) or h.addr3 is None or h.flags & dot11.FLAG_PROTECTED:
        return
    if rx.bad_fcs:
        return
    if rx.freq is not None and rx.freq != tuned:
        cnt["wrong_freq"] += 1
        return
    cnt["mgmt"] += 1
    if rx.driver_signal is not None and rx.driver_signal >= 0:
        cnt["drv_pos"] += 1
    bssid = h.addr3.hex(":")
    d = aps.get(bssid)
    if d is None:
        end = len(fr) - 4 if rx.flags & radiotap.FLAG_FCS_AT_END else len(fr)
        body = rx.length + h.hdr_len + 12
        info = ies.parse_bss(fr, body, end) if body <= end else None
        if info is None:
            return  # 本体も、中身を読めるまでは局にしない
        d = aps[bssid] = {"freq": _beacon_freq(info) or tuned, "ssid": info.ssid, "p": [[] for _ in range(npass)],
                          "ema": None, "odd": None}
    sig = rx.signal
    if sig is None:
        cnt["nosig"] += 1
        return
    d["ema"] = sig if d["ema"] is None else d["ema"] * 0.8 + sig * 0.2  # 本体（analyzer/stations.py）と同じ
    if tuned == d["freq"]:
        d["p"][pi].append(sig)
    ref_v = [v for p in d["p"] for v in p]
    med = statistics.median(ref_v) if len(ref_v) >= 3 else None
    if sig >= -25 or (med is not None and sig >= med + 12):
        if d["odd"] is None or sig > d["odd"][0]:
            d["odd"] = (sig, rx.driver_signal, [s for _, s in rx.chains], pi, tuned, h.subtype,
                        _rt_words(fr, rx.length), fr[:min(rx.length, 64)].hex())


def _beacon_freq(info: ies.BssInfo) -> int | None:
    """ビーコンの中身（DS Parameter か HT Operation のch番号）だけから決めた主ch（MHz）。radiotap のchには頼らない。"""
    num = info.ds_channel if info.ds_channel else (info.ht.primary if info.ht else None)
    if not num:
        return None
    if 1 <= num <= 13:
        return 2407 + 5 * num
    if num == 14:
        return 2484
    if 32 <= num <= 177:
        return 5000 + 5 * num
    return None


def _near(f: int, g: int | None, band_width: int = 25) -> bool:
    return g is not None and abs(f - g) <= band_width


def cmd_chancheck(r: LinuxRadio, a: argparse.Namespace) -> int:
    """chを合わせたとき、無線が本当にそのchで受けているかを確かめる。

    受けたフレームを ch で捨てずにそのまま数え、radiotap に書かれた ch と、ビーコンの中身に書かれた主ch（APの本当のch）を
    くらべる。次のどちらかを見分ける：
      A：無線がchを変えていない（radiotap もビーコンの主chも、前のchのまま）
      B：無線は合っているが、ドライバが radiotap に書く ch がずれている（ビーコンの主chは合わせたchのあたり）
    """
    refs = []
    for x in (y.strip() for y in a.chs.split(",")):
        if not x:
            continue
        try:
            refs.append(ch.parse_channel_ref(x))
        except ValueError as e:  # 使えないchは飛ばして続ける（全体を止めない）
            print(f"  （{x} は飛ばします：{e}）")
    refs = list(dict.fromkeys(refs))
    how = "（chごとにIFを下げて上げてから）" if a.relink else ""
    print(f"[chancheck] {r.ifname}（ドライバ {r.driver() or '不明'}）{how}。chごとに {a.seconds}秒、受けたフレームを捨てずに数える")
    print(f"  {'合わせたch':<14}{'iwの表示':<12}{'フレーム':>8}  {'radiotapのch（多い順）':<34}{'ビーコンの主ch（多い順）':<34}判定")
    verdicts: Counter[str] = Counter()
    for f in refs:
        err = None
        try:
            if a.relink:  # 回避のやり方：IFを下げて上げてから合わせる
                r._close_sock()
                r._link(False)
                r._link(True)
                r.tuning = None
            r.tune(validate_tuning(f, 20, f))
        except RadioError as e:
            err = str(e)
        info = r._cmd(["iw", "dev", r.ifname, "info"], 3.0, check=False).out
        m = re.search(r"channel \d+ \((\d+) MHz\)", info)
        iwf = int(m.group(1)) if m else None
        if r.sock is None:
            r._reopen()
        assert r.sock is not None
        rt: Counter[Any] = Counter()
        bc: Counter[int] = Counter()
        n = 0
        end = time.monotonic() + a.seconds
        while time.monotonic() < end:
            rd, _, _ = select.select([r.sock], [], [], max(0.0, end - time.monotonic()))
            if not rd:
                continue
            while True:
                try:
                    k = r.sock.recv_into(r.buf)
                except (BlockingIOError, InterruptedError):
                    break
                if k <= 0:
                    break
                fr = bytes(r.buf[:k])
                n += 1
                try:
                    rx = radiotap.parse(fr)
                except radiotap.RadiotapError:
                    rt["壊れた"] += 1
                    continue
                rt[rx.freq] += 1
                try:
                    h = dot11.parse_header(fr, rx.length, len(fr))
                except dot11.Dot11Error:
                    continue
                if h.ftype == 0 and h.subtype == dot11.ST_BEACON:
                    stop = len(fr) - 4 if rx.flags & radiotap.FLAG_FCS_AT_END else len(fr)
                    body = rx.length + h.hdr_len + 12
                    if body <= stop:
                        try:
                            bf = _beacon_freq(ies.parse_bss(fr, body, stop))
                        except Exception:  # 壊れたビーコンは数えない
                            bf = None
                        if bf:
                            bc[bf] += 1
        top_rt = next((k for k, _ in rt.most_common() if isinstance(k, int)), None)
        top_bc = bc.most_common(1)[0][0] if bc else None
        if err:
            v = f"合わせられない：{err[:60]}"
        elif n == 0:
            v = "何も受からない（APがいないか、受けられていない）"
        elif top_rt == f or (top_rt is None and _near(f, top_bc)):
            v = "正常"
        elif top_bc is not None and _near(f, top_bc):
            v = f"B：radiotapのchがずれている（{ch.label(top_rt) if top_rt else '?'}と書かれる）"
        elif top_bc is not None and _near(top_rt or 0, top_bc):
            v = f"A：chが変わっていない（{ch.label(top_rt) if top_rt else '?'}のまま）"
        else:
            v = "分からない（ビーコンが少ない）"
        verdicts[v.split("：")[0]] += 1
        fmt = lambda c: "、".join(f"{ch.label(k) if isinstance(k, int) else k}×{v}" for k, v in c.most_common(3)) or "なし"  # noqa: E731
        print(f"  {ch.label(f):<14}{(str(iwf) + 'MHz') if iwf else '—':<12}{n:>8}  {fmt(rt):<34}{fmt(bc):<34}{v}")
    print(f"  まとめ：{dict(verdicts)}")
    if verdicts.get("A"):
        print("  → A：無線がchを変えていません。モニタモードにする前の接続（ch）のままの可能性。")
    if verdicts.get("B"):
        print("  → B：ドライバが radiotap に書く ch がずれています。受信そのものはできています。")
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
    p = sub.add_parser("chancheck")
    p.add_argument("--chs", default="2g1,2g6,2g11,5g36,5g48,5g100,5g120")
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--relink", action="store_true", help="chごとにIFを下げて上げてから合わせる")
    p = sub.add_parser("rtdump")
    p.add_argument("--chs", default=",".join(f"2g{n}" for n in range(1, 14)))
    p.add_argument("--passes", default="2x1,0.25x6")
    p.add_argument("--ref", default=None)
    p = sub.add_parser("own-rssi")
    p.add_argument("--bssid", required=True)
    p.add_argument("--ch", default="6g37")
    p.add_argument("--width", type=int, default=20)
    p.add_argument("--minutes", type=float, default=10)
    a = ap.parse_args(argv)
    r = LinuxRadio(a.interface)
    try:
        return {"frames": cmd_frames, "switch": cmd_switch, "survey": cmd_survey, "eht": cmd_eht,
                "reload-check": cmd_reload, "bench": cmd_bench, "own-rssi": cmd_own, "chancheck": cmd_chancheck, "rtdump": cmd_rtdump}[a.cmd](r, a)
    except RadioError as e:
        print(f"失敗：{e}", file=sys.stderr)
        return 2
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
