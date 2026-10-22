#!/usr/bin/env bash
# 2.4GHz の強さの読み取りを調べる（2分ほど。終わると Wi-Fi を元に戻す）。本体（./start.sh pit）は止めてから動かす。
# 受信専用：パッシブスキャンとモニタの受信だけ。結果は data/rssi_check.txt に残す（写真でも読める長さ）。
#   tools/rssi_check.sh wlp2s0
#   tools/rssi_check.sh wlp2s0 --restart   # 前の版の本体と同じく、スキャンの前に無線を一度止める（ip link set down）。
#                                          # 本体だけで強さが 40〜55dB 強く狂った件が、これで起きるかを見る
# 1. 管理モードでパッシブスキャン：カーネルが出す強さ（正しい値の手本）を、見えた局すべてについて控える
# 2. 本体と同じやり方（国を保ったまま wwmon0 でモニタ）で 2.4GHz の13chを回り、APごとに1行で並べる：
#    手本・長（1chに1秒）・短（0.25秒ずつ切り替え）・5G後（各chの前に 5GHz ch144。本体の PRE で ch1 の前と同じ）・
#    6G後（各chの前に 6GHz ch37 の160MHz）・本体（画面に出る強さの計算）。
#    外れた値（-25dBm以上など）は、回ごとの数と合わせてからの時間、APごとに一番強いものの radiotap の頭を出す
set -u
IF="${1:-}"
RESTART=0
[[ "${2:-}" == "--restart" ]] && RESTART=1
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
echo "$IF の強さの読み取りを調べます（2分ほど）。途中で Wi-Fi が切れ、終わると元に戻します。"
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
if [[ $RESTART -eq 1 ]]; then
  echo "0. 前の版の本体と同じく、無線を一度止めてから上げる"
  sudo ip link set "$IF" down; sudo iw dev "$IF" set type managed
fi
for _ in 1 2 3 4; do sudo ip link set "$IF" up && break; sleep 2; done
sudo iw dev "$IF" scan passive >/dev/null 2>&1 || { sleep 2; sudo iw dev "$IF" scan passive >/dev/null 2>&1; }
REF=$(mktemp)
sudo iw dev "$IF" scan dump 2>/dev/null | awk '
  /^BSS / {b=substr($2,1,17); f=0; s=""} /freq:/ {f=$2+0} /signal:/ {s=$2}
  /SSID:/ {if (b != "" && f > 0 && s != "") {print b, f, s; b=""}}' > "$REF"
echo "1. 管理モードのスキャン：2.4GHz $(awk '$2 < 3000' "$REF" | wc -l)局・5GHz $(awk '$2 > 5000 && $2 < 5950' "$REF" | wc -l)局（強さは下の「手本」）"
echo "2. モニタ（国を保ったまま $MON）で 2.4GHz を回る（1分半ほど）"
sudo iw phy "$PHY" interface add "$MON" type monitor && sudo ip link set "$MON" up && sudo ip link set "$IF" down
sleep 2
(cd "$ROOT/server" && sudo "$PY" -m wifiwatch.hwprobe --interface "$MON" rtdump --ref "$REF")
rm -f "$REF"
