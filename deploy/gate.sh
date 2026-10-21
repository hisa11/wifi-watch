#!/usr/bin/env bash
# 合否ゲート（仕様書「起動前の確認」）：AX210を6GHzでモニタモードにできるかを先に確かめる。
# NICの状態を変える（このIFのWi-Fiが切れる）。受信専用で、送信はしない。
#   sudo deploy/gate.sh wlan0
set -u
IF="${1:-wlan0}"
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ $EUID -eq 0 ]] || { echo "sudo で実行してください（NICの状態を変えます）"; exit 2; }
command -v iw >/dev/null || { echo "iw がありません： sudo apt install iw"; exit 2; }
[[ -d /sys/class/net/$IF ]] || { echo "$IF がありません（ip -br link で名前を確かめてください）"; exit 2; }

step() { echo; echo "== $*"; }
run() { echo "+ $*"; "$@"; }
fail=0

step "カーネルとドライバ"
run uname -r
echo "ドライバ: $(basename "$(readlink -f /sys/class/net/$IF/device/driver)")"

step "管理モードでパッシブスキャン（周りのビーコンの国情報で6GHzを有効にさせる）"
run ip link set "$IF" up
n=$(iw dev "$IF" scan passive 2>/dev/null | grep -c '^BSS ' || true)  # パッシブ（送信しない）
echo "見えたBSS: $n"

step "国設定（phy側が country JP か）"
run iw reg get
PHY=$(cat "/sys/class/net/$IF/phy80211/name")
INFO=$(iw phy "$PHY" info)

step "6GHzの24ch（5925〜6425MHz）が disabled なしで並ぶか（$PHY）"
ok=0; dis=0; miss=0
for k in $(seq 0 23); do
  f=$((5955 + 20 * k))
  line=$(echo "$INFO" | grep -E "\* ${f}(\.0)? MHz" | head -1)
  if [[ -z "$line" ]]; then echo "  ${f}MHz: 無い"; miss=$((miss + 1));
  elif [[ "$line" == *disabled* ]]; then echo "  ${f}MHz: disabled"; dis=$((dis + 1));
  else ok=$((ok + 1)); fi
done
echo "使える ${ok}/24（disabled ${dis}・無い ${miss}）"
[[ $ok -eq 24 ]] || fail=1

# AX210 は無線を一度下ろすと国を忘れて（00）6GHzが全部 disabled になる（2026-10-21 本番PCで確かめた）。
# 本体（./start.sh pit）と同じく、$IF を上げたまま別のモニタ用IFを足して上げ、そのあとで $IF を下げる
MON=wwmon0
step "モニタモードにする（$IF を上げたまま $MON を足して上げ、そのあと $IF を下げる。国を保つ）"
[[ -d /sys/class/net/$MON ]] && run iw dev "$MON" del
run iw phy "$PHY" interface add "$MON" type monitor && run ip link set "$MON" up && run ip link set "$IF" down || fail=1
run iw reg get | sed -n '/phy#/,/^$/p' | head -3

step "6GHz ch1（5955MHz）に合わせる（仕様書の最後のコマンド）"
run iw dev "$MON" set freq 5955 || fail=1

step "参考：160MHz（ch1〜29、中心6025MHz）に合わせられるか"
if run iw dev "$MON" set freq 5955 160 6025 || run iw dev "$MON" set freq 5955 160MHz; then
  echo "160MHz: 可"
else
  echo "160MHz: 不可（LIVEの自ブロックの160MHz受信が使えない）"
fi
run iw dev "$MON" set freq 5955 || true
[[ -d /sys/class/net/$MON ]] && { ip link set "$MON" down; iw dev "$MON" del; }

echo
if [[ $fail -eq 0 ]]; then
  echo "合格：6GHzでモニタモードにできます。config.json の radio.kind を \"radiod\" にして、./start.sh pit で使います。"
  echo "（$IF は下がったまま。戻すには： sudo ip link set $IF up && sudo nmcli device set $IF managed yes）"
  exit 0
else
  echo "不合格：管理モードのパッシブスキャンに切り替えます（radiod を --scan-only で起動）。"
  echo "動くのはビーコンから分かる機能（局の一覧・占有・帯域幅・割当違反・署名）だけで、活動量・リトライ率・deauth・160MHz受信は使えません。"
  exit 1
fi
