"""PCの config.json に、スマホへの通知の宛先を入れる。試しに1件送ることもできる。

wifiwatch のアプリへのプッシュ通知（FCM）：
  server/.venv/bin/python tools/set_notify.py data/devpc/config.json --fcm-key ~/Downloads/xxx-firebase-adminsdk-xxx.json
      Firebase のサービスアカウントの鍵（JSON。秘密）を data/secrets/ に写して（権限600）、config.json に場所を入れる
  server/.venv/bin/python tools/set_notify.py data/devpc/config.json --fcm-test    # アプリに試しに1件送る（警告と同じ）

ntfy のアプリへの通知：

ntfy のトピックは「知っている人なら誰でも読める」名前なので、推測されにくい乱数で作る（スマホで打ちやすいよう、
l・1・o・0・i を除いた小文字と数字）。config.json は自分だけが読める権限（600）にして、コミットしない。
  server/.venv/bin/python tools/set_notify.py data/devpc/config.json          # トピックを作って入れる（もうあればそれを表示）
  server/.venv/bin/python tools/set_notify.py data/devpc/config.json --new    # トピックを作り直す（スマホでも登録し直す）
  server/.venv/bin/python tools/set_notify.py data/devpc/config.json --test   # 試しに1件送る（警告と同じ優先度4：音・バイブ・画面の上に出る）
  server/.venv/bin/python tools/set_notify.py data/devpc/config.json --off    # 通知をやめる
（start.sh の scan・monitor は data/devpc/config.json、fake と本番は config.json）
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from wifiwatch.config import ConfigError, atomic_write_json, parse_config  # noqa: E402

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 打ち間違えやすい l 1 o 0 i を除く


def new_topic() -> str:
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4)]  # 16文字（約80ビット）
    return "wifiwatch-" + "-".join(groups)


def send_test(server: str, topic: str, timeout: float) -> None:
    import httpx
    r = httpx.post(server.rstrip("/") + "/", timeout=timeout, headers={"Content-Type": "application/json; charset=utf-8"},
                   content=json.dumps({"topic": topic, "title": "wifiwatch・通知のテスト",
                                       "message": "PCからスマホへの通知が届きました（警告と同じ優先度4）。",
                                       "priority": 4, "tags": ["warning"]}, ensure_ascii=False).encode("utf-8"))
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description="config.json にスマホへの通知（ntfy）の宛先を入れる")
    ap.add_argument("config", help="書きかえる config.json")
    ap.add_argument("--new", action="store_true", help="トピックを作り直す")
    ap.add_argument("--test", action="store_true", help="試しに1件送る（書きかえない）")
    ap.add_argument("--off", action="store_true", help="通知をやめる（トピックを消す）")
    ap.add_argument("--server", default=None, help="ntfy のサーバ（既定 https://ntfy.sh）")
    ap.add_argument("--fcm-key", default=None, help="Firebase のサービスアカウントの鍵（JSON）を取り込む")
    ap.add_argument("--fcm-test", action="store_true", help="wifiwatch のアプリに試しに1件送る")
    a = ap.parse_args()

    path = Path(a.config)
    if not path.is_file():
        print(f"{path} がありません")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    notify = dict(data.get("notify") or {})
    server = a.server or notify.get("ntfy_server") or "https://ntfy.sh"

    if a.fcm_key or a.fcm_test:
        return fcm_main(a, path, data, notify)

    if a.test:
        topic = notify.get("ntfy_topic")
        if not topic:
            print("まだトピックがありません。先に --test なしで動かしてください")
            return 1
        try:
            send_test(server, topic, float(notify.get("timeout_s", 10)))
        except Exception as e:  # 回線・サーバの誤り
            print(f"送れませんでした：{e}")
            return 1
        print("送りました。スマホの ntfy に、音とバイブで届けば設定はできています")
        return 0

    if a.off:
        notify["ntfy_topic"] = None
    else:
        if a.new or not notify.get("ntfy_topic"):
            notify["ntfy_topic"] = new_topic()
        notify["ntfy_server"] = server
    data["notify"] = notify
    try:
        parse_config(data)
    except ConfigError as e:
        print(e)
        return 1
    atomic_write_json(path, data)
    os.chmod(path, 0o600)
    if a.off:
        print(f"{path}：通知をやめました。PCを起動し直すと効きます")
        return 0
    topic = notify["ntfy_topic"]
    host = server.split("://", 1)[-1].rstrip("/")
    print(f"""{path} に書きました。PCを起動し直すと効きます。

スマホでの受け取り方：
  1. Google Play で「ntfy」（作者 Philipp C. Heckel）を入れる
  2. 右下の「＋」→ Topic name に次を入れる（Use another server はオフのまま）
       {topic}
     「Instant delivery」（すぐに届ける）をオンにして Subscribe
     （スマホのブラウザで ntfy://{host}/{topic} を開いても登録できます）
  3. このPCで試しに1件送る：
       server/.venv/bin/python tools/set_notify.py {a.config} --test

トピックは「知っている人なら誰でも読める」名前です。人に見せたり、コミットしたりしないでください。""")
    return 0


def fcm_main(a: argparse.Namespace, path: Path, data: dict, notify: dict) -> int:
    from wifiwatch import fcm
    if a.fcm_key:
        src = Path(a.fcm_key).expanduser()
        try:
            key = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"鍵を読めません：{e}")
            return 1
        if key.get("type") != "service_account" or not key.get("private_key") or not key.get("project_id"):
            print("Firebase のサービスアカウントの鍵（JSON）ではありません（「新しい秘密鍵を生成」でダウンロードしたものを指定）")
            return 1
        dst = ROOT / "data" / "secrets" / "fcm-service-account.json"  # data/ はgitの外
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(dst.parent, 0o700)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(key, f)
        os.chmod(dst, 0o600)
        notify["fcm_service_account"] = str(dst)
        data["notify"] = notify
        try:
            parse_config(data)
            fcm.FcmSender(str(dst))
        except (ConfigError, ValueError) as e:
            print(e)
            return 1
        atomic_write_json(path, data)
        os.chmod(path, 0o600)
        print(f"鍵を {dst} に写し（権限600）、{path} に入れました（プロジェクト {key['project_id']}）。PCを起動し直すと効きます。")
        print(f"ダウンロードした元の鍵（{src}）は、もう要らないので消してかまいません。")
        return 0
    kp = notify.get("fcm_service_account")
    if not kp:
        print("まだ鍵がありません。先に --fcm-key で取り込んでください")
        return 1
    try:
        sender = fcm.FcmSender(kp, float(notify.get("timeout_s", 10)))
        sender.send(fcm.message(notify.get("fcm_topic") or "wifiwatch-alerts", "warn", "info", "wifiwatch・通知のテスト",
                                "PCからアプリへの通知が届きました（警告と同じ：音・バイブ・画面の上）。", "test"))
    except Exception as e:
        print(f"送れませんでした：{e}")
        return 1
    print("送りました。アプリを一度開いたスマホに、音とバイブで届けば設定はできています（届くまで数秒〜数十秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
