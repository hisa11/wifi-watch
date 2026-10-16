#!/usr/bin/env bash
# 開発PCで、本番と同じモニタモード（radiod ＋ 本体）で動かす。動いている間、このPCの無線はアナライザー専用になり、
# Wi-Fiの通信は切れる（NetworkManagerから外す）。Ctrl-C で止めると、管理モードに戻して NetworkManager に返す。
# 受信専用（送信・注入はしない）。6GHzに対応していない無線では、6GHzは「見ていない（この無線では受けられない）」と出る。
#   tools/devpc_monitor.sh [IF名]        # 既定は wlo1。画面は http://127.0.0.1:8080
set -u
IF="${1:-wlo1}"
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ -d /sys/class/net/$IF ]] || { echo "$IF がありません（ip -br link で名前を確かめてください）"; exit 2; }
command -v iw >/dev/null || { echo "iw がありません： sudo apt install iw"; exit 2; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/server/.venv/bin/python"
CFG="${WIFIWATCH_DEVPC_CONFIG:-$ROOT/data/devpc/config.json}"
SOCK="${WIFIWATCH_RADIOD_SOCK:-/run/wifiwatch/radiod.sock}"

if [[ ! -f "$CFG" ]]; then
  # config.example.json をもとに、この無線と radiod を使う設定を作る（gitの外）
  mkdir -p "$(dirname "$CFG")"
  "$PY" - "$ROOT" "$IF" "$CFG" "$SOCK" <<'EOF'
import json, sys
root, ifname, out, sock = sys.argv[1:]
c = json.load(open(f"{root}/config.example.json", encoding="utf-8"))
c.update(interface=ifname, device_name="dev-pc", data_dir=f"{root}/data/devpc/pc", signatures=f"{root}/signatures.json")
c["radio"] = {"kind": "radiod", "radiod_socket": sock}
json.dump(c, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
EOF
fi

echo "これから $IF を NetworkManager から外し、モニタモードにします。動いている間、このPCのWi-Fiの通信は切れます。"
echo "（Claude など、ネットを使うアプリも止まります。Ctrl-C で止めるとWi-Fiを元に戻します）"
read -r -p "続けますか？ [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || exit 0
sudo -v || exit 1

RPID=""
PCPID=""
KEEP=""
restored=0
restore() {
  [[ $restored -eq 1 ]] && return
  restored=1
  echo
  echo "止めて、Wi-Fiを元に戻します…"
  [[ -n "$PCPID" ]] && kill "$PCPID" 2>/dev/null
  [[ -n "$RPID" ]] && sudo kill "$RPID" 2>/dev/null
  [[ -n "$KEEP" ]] && kill "$KEEP" 2>/dev/null
  wait 2>/dev/null
  sudo ip link set "$IF" down
  sudo iw dev "$IF" set type managed
  sudo ip link set "$IF" up
  sudo nmcli device set "$IF" managed yes
  echo "戻しました（NetworkManager がつなぎ直します）。"
}
trap restore EXIT
trap 'exit 130' INT TERM

# 戻すときに sudo のパスワードを聞き直さないよう、動いている間は sudo の確認を延ばしておく
# （ネットが切れている間にパスワードを聞かれて戻せない、を防ぐ）
(while true; do sudo -n -v 2>/dev/null; sleep 60; done) &
KEEP=$!
sudo nmcli device set "$IF" managed no
# radiod：起動時に管理モードでパッシブスキャン → モニタモード。決まった操作だけを受ける（root）
# server/ で動かす（wifiwatch のパッケージがそこにある。sudo は今いるフォルダを引き継ぐ）
LOG="$ROOT/data/devpc/radiod.log"
mkdir -p "$(dirname "$LOG")"
started=$(mktemp)
(cd "$ROOT/server" && exec sudo "$PY" -m wifiwatch.radio.radiod --interface "$IF" --socket "$SOCK" \
  --group "$(id -gn)" --allow-uid "$(id -u)") >"$LOG" 2>&1 &
RPID=$!
# 前の起動の古いソケットではなく、今回作られたソケットを待つ
for _ in $(seq 1 60); do
  [[ -S "$SOCK" && "$SOCK" -nt "$started" ]] && break
  kill -0 "$RPID" 2>/dev/null || break
  sleep 0.5
done
rm -f "$started"
if ! kill -0 "$RPID" 2>/dev/null || [[ ! -S "$SOCK" ]]; then
  echo "radiod が起動しませんでした。ログ（$LOG）："
  tail -n 20 "$LOG"
  exit 1
fi
# 本体：普通のユーザー。画面とAPIは 127.0.0.1 だけ
(cd "$ROOT/server" && exec "$PY" -m wifiwatch --config "$CFG") &
PCPID=$!
echo "画面： http://127.0.0.1:8080 （巡回を速くするなら画面でモードを PRE に）。止めるときは Ctrl-C。"
wait "$PCPID"
