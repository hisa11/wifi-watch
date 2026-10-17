"""PCの config.json に中継（Vercel）のURLとPC用のトークンを入れる。

トークンは画面に出さずに聞く（getpass。シェルの履歴にも残らない）。書く前に設定として正しいかを確かめ、
同じ場所に原子的に書いて、自分だけが読める権限（600）にする。config.json はコミットしない。
  server/.venv/bin/python tools/set_relay.py data/devpc/config.json        # 開発PC（start.sh の scan・monitor）
  server/.venv/bin/python tools/set_relay.py config.json                   # 偽の無線（start.sh fake）・本番のミニPC
  server/.venv/bin/python tools/set_relay.py config.json --url https://wifiwatch-relay.vercel.app
  server/.venv/bin/python tools/set_relay.py config.json --off             # 中継を使わない（URLとトークンを消す）
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from wifiwatch.config import ConfigError, atomic_write_json, parse_config  # noqa: E402

DEFAULT_URL = "https://wifiwatch-relay.vercel.app"


def main() -> int:
    ap = argparse.ArgumentParser(description="config.json に中継のURLとPC用のトークンを入れる")
    ap.add_argument("config", help="書きかえる config.json")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"中継の本番ドメイン（既定 {DEFAULT_URL}）")
    ap.add_argument("--off", action="store_true", help="中継を使わない（URLとトークンを消す）")
    a = ap.parse_args()

    path = Path(a.config)
    if not path.is_file():
        print(f"{path} がありません")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    relay = dict(data.get("relay") or {})
    if a.off:
        relay.update(url=None, pc_token=None)
    else:
        token = getpass.getpass("PC用のトークン（PC_TOKEN。打っても表示されません）: ").strip()
        if len(token) < 16:
            print("トークンが短すぎます（中継は16文字未満のトークンを通しません）。PC_TOKEN と同じ値を貼ってください")
            return 1
        relay.update(url=a.url, pc_token=token)
    data["relay"] = relay
    try:
        parse_config(data)
    except ConfigError as e:
        print(e)
        return 1
    atomic_write_json(path, data)
    os.chmod(path, 0o600)
    print(f"{path} に書きました（中継：{'使わない' if a.off else relay['url']}）。PCを起動し直すと効きます")
    return 0


if __name__ == "__main__":
    sys.exit(main())
