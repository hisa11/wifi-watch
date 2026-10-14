#!/usr/bin/env bash
# ミニPCのセットアップ。普通のユーザーで実行し、sudo が要る段は1つずつ確認してから実行する。
#   deploy/setup.sh wlan0
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IF="${1:-wlan0}"
ME="${SUDO_USER:-$USER}"
[[ "$IF" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || { echo "IF名が不正です: $IF"; exit 2; }
[[ $EUID -ne 0 ]] || { echo "sudo を付けずに実行してください（必要な段だけ確認してから sudo します）"; exit 2; }

ask() { read -r -p "$1 [y/N] " a; [[ "$a" == y || "$a" == Y ]]; }
step() { echo; echo "== $*"; }

step "1. 必要なもの"
missing=()
command -v python3 >/dev/null || missing+=(python3)
python3 -c "import venv, ensurepip" 2>/dev/null || missing+=(python3-venv)
command -v iw >/dev/null || missing+=(iw)
[[ -f /usr/share/ieee-data/oui.csv ]] || missing+=(ieee-data)
command -v node >/dev/null || missing+=(nodejs)
if ((${#missing[@]})); then
  echo "足りないもの: ${missing[*]}"
  echo "入れ方： sudo apt install ${missing[*]}"
  echo "（pm2 は： sudo npm install -g pm2）"
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10以上が要ります"'
command -v pm2 >/dev/null || { echo "pm2 がありません： sudo npm install -g pm2"; exit 1; }
echo "OK"

step "2. Pythonの仮想環境と依存（版を固定）"
[[ -x "$ROOT/server/.venv/bin/python" ]] || python3 -m venv "$ROOT/server/.venv"
"$ROOT/server/.venv/bin/pip" install -q -r "$ROOT/server/requirements.txt"
echo "OK"

step "3. config.json"
if [[ ! -f "$ROOT/config.json" ]]; then
  cp "$ROOT/config.example.json" "$ROOT/config.json"
  echo "config.json を作りました。自チーム・各チームの予約表・ntfyのトピック・中継のURLとトークンを書き、"
  echo "interface を \"$IF\"、radio.kind を \"radiod\" にしてから、もう一度実行してください。"
  exit 0
fi
"$ROOT/server/.venv/bin/python" - "$ROOT/config.json" <<'PY'
import sys
sys.path.insert(0, sys.argv[1].rsplit("/", 1)[0] + "/server")
from wifiwatch.config import load_config, ConfigError
try:
    c = load_config(sys.argv[1])
except ConfigError as e:
    print(e); sys.exit(1)
if c.radio.kind not in ("radiod", "scan_only"):
    print(f"注意：radio.kind が {c.radio.kind} です（実機では \"radiod\"）")
print("config.json OK：", c.interface, c.radio.kind)
PY

step "4. グループ（radiod のソケットと警報音のシリアル）"
if id -nG "$ME" | grep -qw wifiwatch && id -nG "$ME" | grep -qw dialout; then
  echo "OK（$ME は wifiwatch と dialout に入っている）"
elif ask "sudo groupadd -f wifiwatch && sudo usermod -aG wifiwatch,dialout $ME を実行しますか？"; then
  sudo groupadd -f wifiwatch
  sudo usermod -aG wifiwatch,dialout "$ME"
  echo "入れました（反映には一度ログアウトが要ります）"
fi

step "5. NetworkManagerの管理から $IF を外す（作業中のPCのWi-Fiが切れます）"
CONF=/etc/NetworkManager/conf.d/99-wifiwatch-unmanaged.conf
if [[ -f "$CONF" ]] && grep -q "interface-name:$IF" "$CONF"; then
  echo "OK（$CONF）"
elif ask "$CONF を書いて NetworkManager を読み込み直しますか？"; then
  printf '[keyfile]\nunmanaged-devices=interface-name:%s\n' "$IF" | sudo tee "$CONF" >/dev/null
  sudo nmcli general reload
  echo "外しました"
fi

step "6. 合否ゲート（6GHzでモニタモードにできるか）"
if ask "sudo deploy/gate.sh $IF を実行しますか？（NICの状態を変えます）"; then
  if sudo "$ROOT/deploy/gate.sh" "$IF"; then echo "合格"; else echo "不合格：ecosystem.config.js が --scan-only で起動するよう WIFIWATCH_SCAN_ONLY=1 を付けて次へ"; fi
fi

step "7. pm2 で常駐（radiod は root、本体は $ME）"
echo "起動： sudo WIFIWATCH_USER=$ME pm2 start $ROOT/deploy/ecosystem.config.js"
echo "保存： sudo pm2 save && sudo pm2 startup systemd"
if ask "今すぐ起動しますか？"; then
  sudo WIFIWATCH_USER="$ME" pm2 start "$ROOT/deploy/ecosystem.config.js"
  sudo pm2 save
  echo "画面： http://127.0.0.1:$("$ROOT/server/.venv/bin/python" -c "import json;print(json.load(open('$ROOT/config.json')).get('api',{}).get('port',8080))")"
fi
