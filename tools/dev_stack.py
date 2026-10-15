"""開発用：ローカルの中継（Node＋偽のRedis）と、偽の無線で動くPCを一緒に起動する（M9の通しの確認用）。

トークンはその場で作り、data/dev/（gitの外）に置く。Vercelへのログインもデプロイも要らない。
  server/.venv/bin/python tools/dev_stack.py           # Ctrl-Cで止める
  server/.venv/bin/python tools/dev_stack.py --part relay   # 中継だけ（止めて戻す確認用）
  server/.venv/bin/python tools/dev_stack.py --part pc      # PCだけ
中継の偽のRedisの中身は data/dev/relay-redis.json に残す（止めて戻しても消えない＝Upstashと同じ）。
エミュレータのアプリには、表示される adb のコマンドで設定を入れる（デバッグ版だけ効く）。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay-port", type=int, default=8787)
    ap.add_argument("--api-port", type=int, default=8080)
    ap.add_argument("--data", default=str(ROOT / "data" / "dev"))
    ap.add_argument("--fresh", action="store_true", help="PCのデータと中継の中身を消してから始める")
    ap.add_argument("--part", choices=["both", "relay", "pc"], default="both", help="起動するもの")
    a = ap.parse_args()
    d = Path(a.data)
    d.mkdir(parents=True, exist_ok=True)
    tok_path = d / "tokens.json"
    if tok_path.exists():
        toks = json.loads(tok_path.read_text())
    else:
        toks = {"app": "dev-app-" + secrets.token_urlsafe(18), "pc": "dev-pc-" + secrets.token_urlsafe(18)}
        tok_path.write_text(json.dumps(toks))
        os.chmod(tok_path, 0o600)
    redis_state = d / "relay-redis.json"
    if a.fresh:
        import shutil
        if a.part in ("both", "pc") and (d / "pc").exists():
            shutil.rmtree(d / "pc")
        if a.part in ("both", "relay"):
            redis_state.unlink(missing_ok=True)
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    cfg["data_dir"] = str(d / "pc")
    cfg["signatures"] = str(ROOT / "signatures.json")
    cfg["api"] = {"host": "127.0.0.1", "port": a.api_port}
    cfg["relay"] = {"url": f"http://127.0.0.1:{a.relay_port}", "pc_token": toks["pc"], "idle_interval_s": 10, "fg_interval_s": 2}
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    procs: list[subprocess.Popen[bytes]] = []
    if a.part in ("both", "relay"):
        subprocess.run(["npm", "run", "-s", "build"], cwd=ROOT / "relay", check=True)
        env = {**os.environ, "APP_TOKEN": toks["app"], "PC_TOKEN": toks["pc"], "REDIS_STATE": str(redis_state)}
        procs.append(subprocess.Popen(["node", "dist/src/dev-server.js", str(a.relay_port)], cwd=ROOT / "relay", env=env))
        time.sleep(0.8)
    if a.part in ("both", "pc"):
        procs.append(subprocess.Popen([str(ROOT / "server/.venv/bin/python"), "-m", "wifiwatch", "--config", str(d / "config.json")],
                                      cwd=ROOT / "server"))
    print(f"""
ローカルの中継： http://127.0.0.1:{a.relay_port}
PCの画面：       http://127.0.0.1:{a.api_port}
エミュレータのアプリに設定を入れる（デバッグ版）：
  adb shell am start -S -n jp.robocon.wifiwatch/.MainActivity --es relay_url http://10.0.2.2:{a.relay_port} --es token {toks['app']}
（エミュレータの模擬モバイル回線でも 10.0.2.2 に届くので、モバイル回線への固定はオンのままでよい）
""", flush=True)

    def stop(*_: object) -> None:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        if any(p.poll() is not None for p in procs):
            print("止まったものがあります", file=sys.stderr)
            stop()
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
