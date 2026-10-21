#!/usr/bin/env bash
# AX210 の 6GHz がどの段階で使えなくなるかを調べる（2分ほど。終わると Wi-Fi を元に戻す）。
# 受信専用：パッシブスキャンだけで、プローブ要求や接続はしない。結果は data/sixghz_check.txt に残す（短いので写真でも読める）。
#   tools/sixghz_check.sh wlp2s0
# 段階：
#   A. いまのまま（つながっていれば、つながったまま）
#   B. NetworkManager から外した（接続が切れる）直後
#   C. 管理モードでパッシブスキャンして10秒待ったあと（本体の起動と同じ手順）
#   D. モニタモードにしたあと
set -u
IF="${1:-}"
[[ -n "$IF" ]] || { echo "IF名を付けてください（例：tools/sixghz_check.sh wlp2s0。名前は ip -br link で）"; exit 2; }
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ -d /sys/class/net/$IF ]] || { echo "$IF がありません"; exit 2; }
command -v iw >/dev/null || { echo "iw がありません： sudo apt install iw"; exit 2; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/data/sixghz_check.txt"
PHY=$(cat "/sys/class/net/$IF/phy80211/name")

echo "$IF の 6GHz を4つの段階で調べます（2分ほど）。途中で Wi-Fi が切れ、終わると元に戻します。"
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
  sudo ip link set "$IF" down; sudo iw dev "$IF" set type managed; sudo ip link set "$IF" up
  sudo nmcli device set "$IF" managed yes
  echo "戻しました。結果：$OUT"
}
trap restore EXIT
trap 'exit 130' INT TERM

six() {  # 6GHzの24chのうち、使える・無効・送信不可（no IR）の数と、国設定
  local info country
  info=$(iw phy "$PHY" info)
  country=$(iw reg get | awk -v p="phy#${PHY#phy}" '$0 ~ p {f=1} f && /^country/ {sub(":", "", $2); print $2; exit}')
  [[ -n "$country" ]] || country=$(iw reg get | awk '/^country/ {sub(":", "", $2); print $2; exit}')
  local ok=0 dis=0 noir=0
  for k in $(seq 0 23); do
    f=$((5955 + 20 * k))
    line=$(echo "$info" | grep -E "\* ${f}(\.0)? MHz" | head -1)
    if [[ -z "$line" || "$line" == *disabled* ]]; then dis=$((dis + 1))
    elif [[ "$line" == *"no IR"* ]]; then noir=$((noir + 1)); ok=$((ok + 1))
    else ok=$((ok + 1)); fi
  done
  echo "  国=${country:-?}  6GHz 使える ${ok}/24（うち送信不可 ${noir}）・無効 ${dis}  [$(iw dev "$IF" info | awk '/type/ {print $2}')・$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="$IF" '$1==d {print $2}')]"
}

echo "== $(date '+%F %T') $IF（$PHY）"
echo "カーネル $(uname -r)・ドライバ $(basename "$(readlink -f /sys/class/net/$IF/device/driver)")・$(ethtool -i "$IF" 2>/dev/null | sed -n 's/^firmware-version: /FW /p')"
echo "A. いまのまま"; six
sudo nmcli device set "$IF" managed no; sleep 3
echo "B. NetworkManager から外した直後"; six
sudo ip link set "$IF" up
n=$(sudo iw dev "$IF" scan passive 2>/dev/null | grep -c '^BSS ' || true)
[[ "$n" -gt 0 ]] || { sleep 2; n=$(sudo iw dev "$IF" scan passive 2>/dev/null | grep -c '^BSS ' || true); }
sleep 10
echo "C. パッシブスキャン（${n}局）して10秒後"; six
sudo iw dev "$IF" scan dump 2>/dev/null | awk '/^BSS/ {b=$2} /Country:/ {print "  国情報を出していたAP：" $2; c++} END {if (!c) print "  国情報（Country）を出しているAPが見えない"}' | sort | uniq -c | head -5
sudo ip link set "$IF" down; sudo iw dev "$IF" set type monitor; sudo ip link set "$IF" up; sleep 3
echo "D. モニタモードにしたあと"; six
echo "-- iwlwifi のメッセージ（国・6GHz に関係するもの）"
sudo dmesg | grep -iE "iwlwifi.*(mcc|lar|regul|6e|uhb|lari|country)|regulatory" | tail -8
