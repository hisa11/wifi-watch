"""動いている本体（./start.sh pit など）から、局の強さとして外れた値を受けたフレームと、2.4GHz の局の直近の読み取りを出す。

本番PCで 2.4GHz の強さが -2〜-12dBm と出た件を、本体の中で起きたまま捕まえる（tools/rssi_check.sh では出なかった）。
本体の画面と同じ 127.0.0.1 のAPIを読むだけ（無線には触らない・sudo は要らない）。写真で読める長さにして、
data/rssi_odd.txt にも残す。
  server/.venv/bin/python tools/rssi_odd.py               # config.json のポート（無ければ 8080）
  server/.venv/bin/python tools/rssi_odd.py --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))


def _port(cfg: Path) -> int:
    try:
        return int(json.loads(cfg.read_text(encoding="utf-8")).get("api", {}).get("port", 8080))
    except (OSError, ValueError, AttributeError):
        return 8080


def _ch(f: int | None) -> str:
    if not f:
        return "?"
    if 2412 <= f <= 2472:
        return f"2g{(f - 2407) // 5}"
    if f == 2484:
        return "2g14"
    if 5955 <= f <= 7115:
        return f"6g{(f - 5950) // 5}"
    if 5160 <= f <= 5885:
        return f"5g{(f - 5000) // 5}"
    return str(f)


def _tm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, JST).strftime("%H:%M:%S")


def render(d: dict) -> list[str]:
    c = d.get("counters", {})
    out = [f"== {_tm(d['now'])} 強さの外れ（本体が動いてから）：外れ {c.get('rssi_odd', 0)}・ドライバの値が0以上か-127以下 "
           f"{c.get('driver_bad', 0)}（受けたフレーム {c.get('frames', 0)}）"]
    out.append("-- 2.4GHz の局（中央値の強い順）：ch BSSID後ろ3つ SSID 中央/最新/最大 直近の読み取り")
    for x in d.get("stations_24", [])[:20]:
        med = "—" if x["rssi_avg"] is None else f"{x['rssi_avg']:.0f}"
        out.append(f" {_ch(x['primary']):<5}{x['bssid'][9:]} {(x['ssid'] or '(非公開)')[:12]:<12} "
                   f"{med}/{x['rssi'] if x['rssi'] is not None else '—'}/{x['rssi_max'] if x['rssi_max'] is not None else '—'} "
                   f"{','.join(str(v) for v in x['recent'])}")
    odd = list(reversed(d.get("odd", [])))[:12]
    out.append(f"-- 外れた値のフレーム（新しい順・{len(odd)}件）" if odd else "-- 外れた値のフレームはまだありません")
    for e in odd:
        pt = e.get("prev_tuning")
        prev = f"{_ch(pt[0])}/{pt[1]}" if pt else "—"
        med = "—" if e.get("median_before") is None else f"{e['median_before']:.0f}"
        out.append(f" {_tm(e['at'])} {e['bssid'][9:]} 主{_ch(e['primary'])} 受{_ch(e['tuned'][0])}/{e['tuned'][1]} 前{prev} "
                   f"{'ビーコン' if e['kind'] == 'beacon' else 'プローブ応答'} 読み{e['signal']} ドライバ{e['driver']} "
                   f"アンテナ{e['chains']} 中央{med} 束{e['index']}/{e['batch_frames']}・{e['batch_s']}秒")
        out.append(f"   present={','.join(e['present'])} 頭={e['head']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="本体が受けた、強さの外れた値のフレームを出す")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    a = ap.parse_args()
    port = a.port or _port(Path(a.config))
    url = f"http://127.0.0.1:{port}/api/diag/rssi"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
    except OSError as e:
        print(f"本体に届きません（{url}：{e}）。./start.sh pit を動かしたまま、別の端末で実行してください。")
        return 2
    lines = render(d)
    print("\n".join(lines))
    out = ROOT / "data" / "rssi_odd.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"（{out} にも残しました）")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
