"""起動：設定を読み、部品を組み立て、受信ループ・通知・（中継）を別スレッドで回し、APIを127.0.0.1で開く。

  .venv/bin/python -m wifiwatch --config ../config.json
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import channels as ch
from .clock import Clock, RealClock
from .config import Config, ConfigError, Overrides, load_config
from .controller import Controller
from .logs import FileLogs
from .monitor import Monitor
from .notify import Notifier
from .radio.base import Radio
from .store import Store

log = logging.getLogger("wifiwatch")


@dataclass
class App:
    cfg: Config
    clock: Clock
    store: Store
    monitor: Monitor
    notifier: Notifier
    controller: Controller
    relay: Any = None
    engine_thread: threading.Thread | None = None

    def start_background(self) -> None:
        def loop() -> None:
            eng = self.monitor.engine
            while not eng.stopping:
                try:
                    eng.run_forever()
                except Exception:
                    log.exception("受信ループが落ちたので1秒後に再開します")
                    time.sleep(1)

        self.engine_thread = threading.Thread(target=loop, name="engine", daemon=True)
        self.engine_thread.start()
        self.notifier.start()
        if self.relay is not None:
            self.relay.start()

    def stop(self) -> None:
        self.monitor.engine.stopping = True
        self.notifier.stop()
        if self.relay is not None:
            self.relay.stop()
        if self.engine_thread:
            self.engine_thread.join(timeout=5)
        self.monitor.radio.close()
        self.store.close()


def build(cfg: Config, clock: Clock | None = None, radio: Radio | None = None, poster: Any = None,
          relay_transport: Any = None) -> App:
    clock = clock or RealClock()
    data = Path(cfg.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "wifiwatch.db")
    logs = FileLogs(data)
    mon = Monitor(cfg, clock, radio, Overrides(data / "overrides.json"), tags=store.tags())

    def status() -> dict[str, Any]:
        a = mon.alerts.active()
        return {"mode": mon.scheduler.mode, "own": f"{mon.scheduler.block.label}", "alerts": len(a),
                "rx": {"ok": "正常", "suspect": "様子見", "recovering": "復旧中", "failed": "停止"}[mon.liveness.state]}

    notifier = Notifier(cfg.notify, store, clock, poster=poster, status=status)
    ctl = Controller(cfg, mon, store, logs, clock, notifier)
    app = App(cfg, clock, store, mon, notifier, ctl)
    if cfg.relay.url:
        from .relay_client import RelayClient
        app.relay = RelayClient(cfg.relay, ctl, store, clock, transport=relay_transport)
    sch = mon.scheduler
    ctl.log_event("system", "start", "info", None, "起動",
                  f"{cfg.device_name}：自ch {ch.label(sch.own_freq)}・受信ブロック {sch.block.label}・{sch.mode}")
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wifiwatch", description="NHK高専ロボコン向けWi-Fi監視（受信専用）")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    app = build(cfg)
    app.start_background()
    import uvicorn

    from .api import create_app
    try:
        uvicorn.run(create_app(app.controller, cfg.api.port), host="127.0.0.1", port=cfg.api.port,
                    log_level="warning", server_header=False, proxy_headers=False, access_log=False)
    finally:
        app.stop()
    return 0
