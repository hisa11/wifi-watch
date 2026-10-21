#!/usr/bin/env bash
# wifiwatch をこのPCで起動する。使うときにコマンドで起動し、Ctrl-C で止める（電源を入れただけでは立ち上がらない）。
#
#   ./start.sh pit          本番のミニPC（AX210・config.json）。モニタモード。動いている間この無線のWi-Fiは切れ、
#                           止めると元に戻る。sudoが要る（合否ゲートが不合格なら --scan-only を付ける）
#   ./start.sh              開発PC：どれで動かすか聞く
#   ./start.sh scan         開発PC：本物の2.4/5GHz（スキャンだけのモード）。Wi-Fiはつながったまま。sudoが要る
#   ./start.sh monitor      開発PC：本物（モニタモード）。動いている間このPCのWi-Fiは切れる。sudoが要る
#   ./start.sh fake         偽の無線（見本の会場）。sudoも無線も使わない
#
#   --if <IF名>             無線のIF名（pit は config.json の interface。ほかは省略すると自動で探す）
#   --scan-only             pit を、モニタモードではなくスキャンだけのモードで動かす（Wi-Fiはつながったまま）
#   --no-browser            ブラウザを開かない
#
# 画面は http://127.0.0.1:8080 。Ctrl-C で止める（root の補助 radiod も止め、無線を元に戻す）。
# 受信専用（送信・注入はしない）。スキャンだけのモードはパッシブスキャンだけ。
set -u
ROOT=$(cd "$(dirname "$0")" && pwd)
PY="$ROOT/server/.venv/bin/python"
URL="http://127.0.0.1:8080"
CFG_DEV="${WIFIWATCH_DEVPC_CONFIG:-$ROOT/data/devpc/config.json}"
SOCK="${WIFIWATCH_RADIOD_SOCK:-/run/wifiwatch/radiod.sock}"

MODE=""
IF=""
BROWSER=1
SCAN_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    pit|scan|monitor|fake) MODE="$1" ;;
    --if) IF="${2:-}"; shift ;;
    --scan-only) SCAN_ONLY=1 ;;
    --no-browser) BROWSER=0 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "知らない引数です：$1（./start.sh --help）"; exit 2 ;;
  esac
  shift
done

# ------------------------------------------------------------ 前提
if [[ ! -x "$PY" ]]; then
  echo "Pythonの環境がありません。最初に一度だけ："
  echo "  cd $ROOT/server && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 2
fi
if ss -ltn 2>/dev/null | grep -q '127.0.0.1:8080 '; then
  echo "127.0.0.1:8080 はもう使われています（wifiwatch がすでに動いているかもしれません）。"
  echo "動いているものを止めてから、もう一度実行してください。画面：$URL"
  exit 1
fi

if [[ -z "$MODE" ]]; then
  echo "どれで動かしますか？"
  echo "  1) 本物の2.4/5GHz・スキャンだけ（Wi-Fiはつながったまま。sudoが要る）"
  echo "  2) 本物・モニタモード（全部のフレームを見る。動いている間このPCのWi-Fiは切れる。sudoが要る）"
  echo "  3) 偽の無線（見本の会場。sudo不要）"
  read -r -p "番号 [1]: " n
  case "${n:-1}" in
    1) MODE=scan ;; 2) MODE=monitor ;; 3) MODE=fake ;;
    *) echo "1〜3で選んでください"; exit 2 ;;
  esac
fi

# ------------------------------------------------------------ 本番のミニPC（config.json）
if [[ "$MODE" == "pit" ]]; then
  CFG_PIT="$ROOT/config.json"
  [[ -f "$CFG_PIT" ]] || { echo "config.json がありません。先に deploy/setup.sh を動かしてください"; exit 2; }
  read -r KIND CIF < <("$PY" -c "import json,sys; c=json.load(open(sys.argv[1])); print((c.get('radio') or {}).get('kind','fake'), c.get('interface','wlan0'))" "$CFG_PIT")
  if [[ "$KIND" != "radiod" ]]; then
    echo "config.json の radio.kind が \"$KIND\" です。本番は \"radiod\" にしてください（deploy/setup.sh で直せます）"
    exit 2
  fi
  [[ -n "$IF" ]] || IF="$CIF"
fi

find_if() {
  # phy80211 がある（無線の）IFのうち最初のもの
  for d in /sys/class/net/*/phy80211; do
    [[ -e "$d" ]] && { basename "$(dirname "$d")"; return 0; }
  done
  return 1
}
if [[ "$MODE" != "fake" ]]; then
  [[ -n "$IF" ]] || IF=$(find_if) || { echo "無線のIFが見つかりません（--if で指定してください）"; exit 2; }
  [[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
  [[ -d /sys/class/net/$IF ]] || { echo "$IF がありません（ip -br link で名前を確かめてください）"; exit 2; }
  command -v iw >/dev/null || { echo "iw がありません： sudo apt install iw"; exit 2; }
  RF=$(cd "$ROOT/server" && "$PY" -c "from wifiwatch.radio import rfkill; print(rfkill.state('$IF') or '')")
  if [[ -n "$RF" ]]; then
    echo "無線が RF-kill で止められています（$RF）。Wi-Fi をオンにしてから、もう一度実行してください。"
    exit 1
  fi
fi

open_browser() {
  [[ $BROWSER -eq 1 ]] || return 0
  # 画面が開けるようになったら、ブラウザで開く（裏で待つ）
  (for _ in $(seq 1 120); do
     if curl -sf "$URL/api/state" >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true; exit 0; fi
     sleep 0.5
   done) &
}

ensure_dev_config() {
  [[ -f "$CFG_DEV" ]] && return 0
  # config.example.json をもとに、この無線と radiod を使う設定を作る（gitの外）
  mkdir -p "$(dirname "$CFG_DEV")"
  "$PY" - "$ROOT" "$IF" "$CFG_DEV" "$SOCK" <<'EOF'
import json, sys
root, ifname, out, sock = sys.argv[1:]
c = json.load(open(f"{root}/config.example.json", encoding="utf-8"))
c.update(interface=ifname, device_name="dev-pc", data_dir=f"{root}/data/devpc/pc", signatures=f"{root}/signatures.json")
c["radio"] = {"kind": "radiod", "radiod_socket": sock}
json.dump(c, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
EOF
  echo "このPC用の設定を作りました：$CFG_DEV"
}

# ------------------------------------------------------------ 偽の無線
if [[ "$MODE" == "fake" ]]; then
  CFG="$ROOT/config.json"
  if [[ ! -f "$CFG" ]]; then
    cp "$ROOT/config.example.json" "$CFG"
    echo "config.example.json から config.json を作りました（偽の無線）"
  fi
  echo "偽の無線で起動します。画面：$URL（止めるときは Ctrl-C）"
  open_browser
  cd "$ROOT/server" && exec "$PY" -m wifiwatch --config "$CFG"
fi

# ------------------------------------------------------------ モニタモード（Wi-Fiが切れる。手順は devpc_monitor.sh）
if [[ "$MODE" == "monitor" ]]; then
  open_browser
  exec "$ROOT/tools/devpc_monitor.sh" "$IF"
fi
if [[ "$MODE" == "pit" && $SCAN_ONLY -eq 0 ]]; then
  # 本番：AX210 は無線を一度下ろすと国を忘れて6GHzが無効になる（2026-10-21 tools/sixghz_check.sh で確かめた）ので、
  # 管理モードのIFを上げたままモニタ用IF（wwmon0）を足して受ける。止めると管理モードに戻して NetworkManager に返す
  mkdir -p "$ROOT/data"
  open_browser
  exec "$ROOT/tools/devpc_monitor.sh" "$IF" --config "$CFG_PIT" --keep-regdom --log "$ROOT/data/radiod.log"
fi

# ------------------------------------------------------------ スキャンだけのモード（Wi-Fiはつながったまま）
if [[ "$MODE" == "pit" ]]; then
  CFG_DEV="$CFG_PIT"
  LOG="$ROOT/data/radiod.log"
  mkdir -p "$ROOT/data"
else
  ensure_dev_config
  LOG="$ROOT/data/devpc/radiod.log"
fi
echo "本物の2.4/5GHzを、スキャンだけのモードで見ます（$IF。Wi-Fiはつながったまま）。"
echo "root の補助（radiod）を起動するので、sudo のパスワードを聞かれます。"
sudo -v || exit 1

RPID=""
PCPID=""
KEEP=""
stopped=0
cleanup() {
  [[ $stopped -eq 1 ]] && return
  stopped=1
  echo
  echo "止めます…"
  [[ -n "$PCPID" ]] && kill "$PCPID" 2>/dev/null
  [[ -n "$RPID" ]] && sudo kill "$RPID" 2>/dev/null
  [[ -n "$KEEP" ]] && kill "$KEEP" 2>/dev/null
  wait 2>/dev/null
  echo "止めました。"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# 止めるときに sudo のパスワードを聞き直さないよう、動いている間は sudo の確認を延ばしておく
(while true; do sudo -n -v 2>/dev/null; sleep 60; done) &
KEEP=$!

started=$(mktemp)
# server/ で動かす（wifiwatch のパッケージがそこにある。sudo は今いるフォルダを引き継ぐ）
(cd "$ROOT/server" && exec sudo "$PY" -m wifiwatch.radio.radiod --interface "$IF" --scan-only --socket "$SOCK" \
  --group "$(id -gn)" --allow-uid "$(id -u)") >"$LOG" 2>&1 &
RPID=$!
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

echo "画面：$URL（止めるときは Ctrl-C）"
PHY=$(cat "/sys/class/net/$IF/phy80211/name" 2>/dev/null)
if [[ -n "$PHY" ]] && ! iw phy "$PHY" info 2>/dev/null | grep -qE '\* 59[5-9][0-9](\.0)? MHz'; then
  echo "この無線（$IF）は6GHzを受けられないので、画面でモードを PRE（下見）にすると2.4/5GHzを速く回ります。"
fi
open_browser
(cd "$ROOT/server" && exec "$PY" -m wifiwatch --config "$CFG_DEV") &
PCPID=$!
wait "$PCPID"
