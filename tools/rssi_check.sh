#!/usr/bin/env bash
# 2.4GHz の強さの読み取りを調べる（1分ほど。終わると Wi-Fi を元に戻す）。本体（./start.sh pit）は止めてから動かす。
# 受信専用：パッシブスキャンとモニタの受信だけ。結果は data/rssi_check.txt に残す（写真でも読める長さ）。
#   tools/rssi_check.sh wlp2s0
# 1. 管理モードでパッシブスキャン：カーネルが出す強さ（正しい値の手本）を、2.4GHz と 5GHz の数局ぶん出す
# 2. 本体と同じやり方（国を保ったまま wwmon0 でモニタ）で、同じchのビーコンの radiotap の強さと、本体の読み取りを並べる
set -u
IF="${1:-}"
[[ -n "$IF" ]] || { echo "IF名を付けてください（例：tools/rssi_check.sh wlp2s0）"; exit 2; }
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ -d /sys/class/net/$IF ]] || { echo "$IF がありません"; exit 2; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/server/.venv/bin/python"
OUT="$ROOT/data/rssi_check.txt"
PHY=$(cat "/sys/class/net/$IF/phy80211/name")
MON=wwmon0
if ss -ltn 2>/dev/null | grep -q '127.0.0.1:8080 '; then
  echo "wifiwatch が動いています。./start.sh pit を Ctrl-C で止めてから実行してください。"; exit 1
fi
echo "$IF の強さの読み取りを調べます（1分ほど）。途中で Wi-Fi が切れ、終わると元に戻します。"
read -r -p "続けますか？ [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || exit 0
sudo -v || exit 1
mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

restored=0
restore() {
  [[ $restored -eq 1 ]] && return
  restored=1
  echo; echo "== 元に戻します"
  [[ -d /sys/class/net/$MON ]] && sudo iw dev "$MON" del
  sudo ip link set "$IF" down; sudo iw dev "$IF" set type managed
  for _ in 1 2 3 4; do sudo ip link set "$IF" up && break; sleep 2; done
  sudo nmcli device set "$IF" managed yes
  echo "戻しました。結果：$OUT"
}
trap restore EXIT
trap 'exit 130' INT TERM

echo "== $(date '+%F %T') $IF（$PHY）$(ethtool -i "$IF" 2>/dev/null | sed -n 's/^firmware-version: /FW /p')"
sudo nmcli device set "$IF" managed no; sleep 3
sudo ip link set "$IF" up
sudo iw dev "$IF" scan passive >/dev/null 2>&1 || { sleep 2; sudo iw dev "$IF" scan passive >/dev/null 2>&1; }
echo "1. 管理モードのスキャンの強さ（手本）"
sudo iw dev "$IF" scan dump 2>/dev/null | awk '
  /^BSS / {b=substr($2,1,17)} /freq:/ {f=$2+0} /signal:/ {s=$2}
  /SSID:/ {if (b != "" && f > 0) {n[f>5000?"5":"2"]++; if (n[f>5000?"5":"2"] <= 5) printf "  %s %dMHz %s dBm\n", b, f, s; b=""}}'
echo "2. モニタ（国を保ったまま $MON）で受けたビーコン"
sudo iw phy "$PHY" interface add "$MON" type monitor && sudo ip link set "$MON" up && sudo ip link set "$IF" down
sleep 2
(cd "$ROOT/server" && sudo "$PY" -m wifiwatch.hwprobe --interface "$MON" rtdump --chs 2g1,2g6,2g11,5g36,5g48 --seconds 3 --per-ch 3)
