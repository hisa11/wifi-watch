#!/usr/bin/env bash
# 開発PCで、モニタモードのとき無線が本当にchを変えて受けているかを確かめる（2分ほど。終わるとWi-Fiを元に戻す）。
# 受信専用（送信・注入はしない。モニタの active フラグも使わない）。結果は data/devpc/monitor_check.txt に残す。
# フレームそのもの（pcap）は保存しない。tcpdump はビーコンを数えるだけ（-w /dev/null、先頭64バイト）。
#   tools/devpc_monitor_check.sh [IF名]      # 既定は自動で探す（例：wlo1）
#
# 試すやり方（それぞれ、いくつかのchで「受けたフレーム」を数える）：
#   1. ふつうにモニタ（ip link down → iw set type monitor → up）。本体の chancheck と tcpdump の両方で数える
#   2. 本体（radiod）と同じ手順：管理モードでパッシブスキャンしてからモニタ
#   3. モニタの受信フラグ（otherbss・control）を付けてモニタ
#   4. 別のモニタ用IF（iw phy … interface add … type monitor）を作って、そこで受ける
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/server/.venv/bin/python"
OUT="$ROOT/data/devpc/monitor_check.txt"
MON=wwmon0
IF="${1:-}"
if [[ -z "$IF" ]]; then
  for d in /sys/class/net/*/phy80211; do [[ -e "$d" ]] && { IF=$(basename "$(dirname "$d")"); break; }; done
fi
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ -d /sys/class/net/$IF ]] || { echo "$IF がありません（ip -br link で名前を確かめてください）"; exit 2; }
command -v iw >/dev/null || { echo "iw がありません： sudo apt install iw"; exit 2; }
RF=$(cd "$ROOT/server" && "$PY" -c "from wifiwatch.radio import rfkill; print(rfkill.state('$IF') or '')")
if [[ -n "$RF" ]]; then
  echo "無線が RF-kill で止められています（$RF）。Wi-Fi をオンにしてから、もう一度実行してください。"
  exit 1
fi

echo "モニタモードで、無線がchを変えて受けているかを、4つのやり方で確かめます（$IF・2分ほど）。"
echo "その間このPCのWi-Fiの通信は切れます。終わると元に戻します。画面のWi-Fiの切り替えや機内モードは押さないでください。"
read -r -p "続けますか？ [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || exit 0
sudo -v || exit 1
mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

PHY=$(cat "/sys/class/net/$IF/phy80211/name")
KEEP=""
restored=0
restore() {
  [[ $restored -eq 1 ]] && return
  restored=1
  echo
  echo "== Wi-Fiを元に戻します"
  [[ -n "$KEEP" ]] && kill "$KEEP" 2>/dev/null
  [[ -d /sys/class/net/$MON ]] && sudo iw dev "$MON" del
  sudo ip link set "$IF" down
  sudo iw dev "$IF" set type managed
  sudo ip link set "$IF" up
  sudo nmcli device set "$IF" managed yes
  echo "戻しました（NetworkManager がつなぎ直します）。結果：$OUT"
}
trap restore EXIT
trap 'exit 130' INT TERM
(while true; do sudo -n -v 2>/dev/null; sleep 60; done) &
KEEP=$!

# モニタにする前につながっていたch（無線がそこに残っていないかも見る）
PREF=$(iw dev "$IF" info | sed -n 's/.*channel [0-9]* (\([0-9]*\) MHz).*/\1/p' | head -1)
CHS="2g1,2g6,2g11,5g36,5g48,5g100,5g120${PREF:+,$PREF}"

echo "== $(date '+%F %T') $IF（$PHY）"
echo "ドライバ: $(basename "$(readlink -f /sys/class/net/$IF/device/driver)")  カーネル: $(uname -r)"
echo "ファームウェア: $(ethtool -i "$IF" 2>/dev/null | sed -n 's/^firmware-version: //p')"
echo "モニタにする前のch: ${PREF:-不明}MHz   調べるch: $CHS"
echo "-- iw dev（前）"; iw dev
echo "-- rfkill"; rfkill list 2>/dev/null
echo "-- NetworkManager"; nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null

probe() {  # $1 = IF名、$2 = 見出し
  echo "-- iw dev $1 info"; iw dev "$1" info | sed -n '/type\|channel/p'
  (cd "$ROOT/server" && sudo "$PY" -m wifiwatch.hwprobe --interface "$1" chancheck --chs "$CHS" --seconds 2)
  if command -v tcpdump >/dev/null; then
    echo "-- tcpdump（本体の受信の仕組みを使わずに、ビーコンを3秒数える。保存しない）"
    for c in 2412 2437 2462 ${PREF:-}; do
      sudo iw dev "$1" set freq "$c" 2>&1 | head -1
      n=$(sudo timeout -s INT 3 tcpdump -i "$1" -s 64 -w /dev/null 'type mgt subtype beacon' 2>&1 | sed -n 's/^\([0-9]*\) packets captured.*/\1/p')
      echo "  ${c}MHz：ビーコン ${n:-?}件"
    done
  fi
}

echo; echo "== NetworkManager から外す"
sudo nmcli device set "$IF" managed no; echo "nmcli managed no → $?"
sleep 2

echo; echo "== やり方1：ふつうにモニタ"
sudo ip link set "$IF" down; sudo iw dev "$IF" set type monitor; echo "set type monitor → $?"; sudo ip link set "$IF" up
probe "$IF"

echo; echo "== やり方2：本体（radiod）と同じ：管理モードでパッシブスキャンしてからモニタ"
sudo ip link set "$IF" down; sudo iw dev "$IF" set type managed; sudo ip link set "$IF" up
n=$(sudo iw dev "$IF" scan passive 2>&1 | grep -c '^BSS ')
echo "パッシブスキャン：${n}局"
sudo ip link set "$IF" down; sudo iw dev "$IF" set type monitor; sudo ip link set "$IF" up
probe "$IF"

echo; echo "== やり方3：受信フラグ otherbss・control を付けてモニタ"
sudo ip link set "$IF" down; sudo iw dev "$IF" set monitor otherbss control; echo "set monitor otherbss control → $?"; sudo ip link set "$IF" up
probe "$IF"

echo; echo "== やり方4：別のモニタ用IF（$MON）を作って受ける"
sudo ip link set "$IF" down; sudo iw dev "$IF" set type managed
sudo iw phy "$PHY" interface add "$MON" type monitor; echo "interface add $MON → $?"
if [[ -d /sys/class/net/$MON ]]; then
  sudo ip link set "$MON" up; echo "ip link set $MON up → $?"
  probe "$MON"
  sudo ip link set "$MON" down
  sudo iw dev "$MON" del
fi
