#!/usr/bin/env bash
# 開発PCで、モニタモードのとき無線が本当にchを変えて受けているかを確かめる（1分ほど。終わるとWi-Fiを元に戻す）。
# 受信専用（送信・注入はしない）。結果は data/devpc/monitor_check.txt に残す（フレームそのものは保存しない）。
#   tools/devpc_monitor_check.sh [IF名]      # 既定は自動で探す（例：wlo1）
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/server/.venv/bin/python"
OUT="$ROOT/data/devpc/monitor_check.txt"
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

echo "モニタモードで、無線がchを変えて受けているかを確かめます（$IF・1分ほど）。"
echo "その間このPCのWi-Fiの通信は切れます。終わると元に戻します。画面のWi-Fiの切り替えや機内モードは押さないでください。"
read -r -p "続けますか？ [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || exit 0
sudo -v || exit 1
mkdir -p "$(dirname "$OUT")"
exec > >(tee "$OUT") 2>&1

KEEP=""
restored=0
restore() {
  [[ $restored -eq 1 ]] && return
  restored=1
  echo
  echo "== Wi-Fiを元に戻します"
  [[ -n "$KEEP" ]] && kill "$KEEP" 2>/dev/null
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

echo "== $(date '+%F %T') $IF"
echo "ドライバ: $(basename "$(readlink -f /sys/class/net/$IF/device/driver)")  カーネル: $(uname -r)"
PHY=$(cat "/sys/class/net/$IF/phy80211/name")
echo "-- iw dev（前）"; iw dev
echo "-- rfkill"; rfkill list 2>/dev/null
echo "-- NetworkManager"; nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null
echo "-- 使えるIFの組み合わせ（$PHY）"; iw phy "$PHY" info | sed -n '/valid interface combinations/,/^\s*$/p' | head -12

echo; echo "== NetworkManager から外して、モニタモードにする"
sudo nmcli device set "$IF" managed no; echo "nmcli managed no → $?"
sleep 2
sudo ip link set "$IF" down; echo "ip link down → $?"
sudo iw dev "$IF" set type monitor; echo "iw set type monitor → $?"
sudo ip link set "$IF" up; echo "ip link up → $?"
echo "-- iw dev（モニタ）"; iw dev

echo; echo "== 1回目：ふつうに ch を合わせる（本体と同じやり方）"
(cd "$ROOT/server" && sudo "$PY" -m wifiwatch.hwprobe --interface "$IF" chancheck --seconds 2)

echo; echo "== 2回目：IFを下げて上げてから ch を合わせる（回避のやり方を試す）"
(cd "$ROOT/server" && sudo "$PY" -m wifiwatch.hwprobe --interface "$IF" chancheck --seconds 2 --relink)
