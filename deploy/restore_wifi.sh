#!/usr/bin/env bash
# 本番のミニPCの無線を、普通の Wi-Fi に戻す（前の版の setup.sh で常駐・管理の外しをしたPCを、元に戻すとき）。
#   deploy/restore_wifi.sh wlp2s0
# やること（どれも sudo。1つずつ表示してから行う）：
#   1. pm2 で常駐させていれば、止めて一覧から消し、起動時に立ち上がらないようにする
#   2. NetworkManager の管理から外す設定ファイル（/etc/NetworkManager/conf.d/99-wifiwatch-unmanaged.conf）を消す
#   3. 無線をモニタモードから管理モードに戻し、NetworkManager に返す（つなぎ直しは NetworkManager が行う）
set -u
IF="${1:-}"
[[ -n "$IF" ]] || { echo "IF名を付けてください（例：deploy/restore_wifi.sh wlp2s0。名前は ip -br link で）"; exit 2; }
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ -d /sys/class/net/$IF ]] || { echo "$IF がありません（ip -br link で名前を確かめてください）"; exit 2; }
[[ $EUID -ne 0 ]] || { echo "sudo を付けずに実行してください（要るところで sudo します）"; exit 2; }
CONF=/etc/NetworkManager/conf.d/99-wifiwatch-unmanaged.conf
run() { echo "+ $*"; "$@"; }

echo "$IF を普通の Wi-Fi に戻します。"
sudo -v || exit 1

if sudo sh -c 'command -v pm2' >/dev/null 2>&1; then
  echo; echo "== 1. pm2 の常駐をやめる"
  run sudo pm2 delete all || true
  run sudo pm2 save --force || true
  run sudo pm2 unstartup systemd || true
fi

echo; echo "== 2. NetworkManager の管理に戻す"
if [[ -f "$CONF" ]]; then
  run sudo rm -f "$CONF"
  run sudo nmcli general reload
else
  echo "（$CONF は無い）"
fi

echo; echo "== 3. 管理モードに戻す"
run sudo ip link set "$IF" down
run sudo iw dev "$IF" set type managed
for _ in 1 2 3 4; do run sudo ip link set "$IF" up && break; sleep 2; done  # 直後は Input/output error のことがある
run sudo nmcli device set "$IF" managed yes

echo
echo "戻しました。数秒で NetworkManager がつなぎ直します（画面の Wi-Fi の一覧を確かめてください）。"
echo "それでも戻らなければ、PCを再起動してください（モニタモードは再起動で消え、上の設定はもう残っていません）。"
