"""部品の組み立て。設定から、無線・解析・巡回・分類・警報・受信停止の見張りをつなぐ。

時計と無線は外から渡せる（テストでは偽の時計と偽の無線）。保存・通知・中継はこの上にのせる。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import channels as ch
from .analyzer.alerts import AlertEvent, AlertManager
from .analyzer.core import Analyzer
from .analyzer.detect import Detector
from .calibration import calibrate
from .classify import Classifier, ModelBook, OuiDb, load_signatures
from .clock import Clock, RealClock
from .config import Config, Overrides
from .engine import Engine
from .liveness import LivenessMonitor
from .radio.base import Radio
from .scheduler import Scheduler

log = logging.getLogger(__name__)


def make_radio(cfg: Config, clock: Clock) -> Radio:
    kind = cfg.radio.kind
    if kind == "fake":
        from .radio.fake import FakeRadio, Scenario, demo_scenario
        sc = Scenario.load(cfg.radio.fake_scenario) if cfg.radio.fake_scenario else demo_scenario()
        return FakeRadio(clock, sc, switch_s=cfg.schedule.switch_s)
    if kind == "radiod":
        from .radio.remote import RemoteRadio
        return RemoteRadio(cfg.radio.radiod_socket, clock)
    if kind == "linux":
        from .radio.linux import LinuxRadio
        return LinuxRadio(cfg.interface, clock, monitor_flags=cfg.radio.monitor_flags)
    if kind == "scan_only":
        from .radio.scanonly import ScanOnlyRadio
        return ScanOnlyRadio(cfg.interface, clock)
    raise ValueError(kind)


class Monitor:
    def __init__(self, cfg: Config, clock: Clock | None = None, radio: Radio | None = None,
                 overrides: Overrides | None = None, oui: OuiDb | None = None,
                 once_store: set[str] | None = None, persist_once: Callable[[set[str]], None] | None = None,
                 tags: dict[str, str] | None = None):
        self.cfg = cfg
        self.clock = clock or RealClock()
        self.overrides = overrides or Overrides(Path(cfg.data_dir) / "overrides.json")
        ov = self.overrides.snapshot()
        if ov.get("own_channel"):  # 画面で変えた自chを、設定の値より優先する
            cfg.own.channel = ov["own_channel"]
            if ov.get("own_width") in ch.valid_widths(cfg.own.channel):
                cfg.own.width = ov["own_width"]
        # 画面で登録した自チームのAP（MACアドレス・SSID）を、設定の値より優先する
        if ov.get("own_bssids") is not None:
            cfg.own.bssids = list(ov["own_bssids"])
        if ov.get("own_ssids") is not None:
            cfg.own.ssids = list(ov["own_ssids"])
        own_freq = cfg.own.channel
        self.analyzer = Analyzer(self.clock, list(cfg.own.bssids) + list(cfg.own.station_macs))
        self.scheduler = Scheduler(
            cfg.schedule, self.clock, own_freq, cfg.own.spare, ov.get("mode", "LIVE"),
            ov.get("excluded_bands", []), [f for f in ov.get("excluded_freqs", []) if f not in ch.own_block(own_freq).freqs],
            liveness_pick=self._pick_liveness,
        )
        self.radio = radio or make_radio(cfg, self.clock)
        if hasattr(self.radio, "hello"):
            try:
                self.radio.hello()  # radiod の種類（スキャンだけのモードか）を知る
            except Exception:
                log.warning("radiod にまだ届きません（受信ループで再接続します）")
        self.engine = Engine(self.clock, self.radio, self.analyzer, self.scheduler, device=cfg.device_name)
        self.signatures = load_signatures(cfg.signatures)
        self.classifier = Classifier(cfg, self.signatures, oui if oui is not None else OuiDb(), tags,
                                     book=ModelBook(Path(cfg.data_dir) / "model_book.json"))
        self.engine.classify = lambda st: self.classifier.classify(st, self.analyzer.table)
        th = cfg.thresholds
        self.alerts = AlertManager(self.clock, th.clear_after_s, th.renotify_suppress_s, th.levels,
                                   once_store, persist_once)
        self.liveness = LivenessMonitor(self.clock, self.engine, th.liveness_empty_cycles)
        self.detector = Detector(cfg, self.clock, self.analyzer, self.scheduler, self.classifier, self.alerts,
                                 self.signatures, rx_status=self.liveness.status)
        self.on_alert_event: list[Callable[[AlertEvent], None]] = []
        self.on_calibration: list[Callable[[dict[str, Any]], None]] = []
        self.calibration: dict[str, Any] | None = None
        self._calibrated_for: float | None = None
        self.engine.add_ticker(1.0, self._evaluate)
        self.engine.add_ticker(10.0, self._maybe_calibrate)

    def _maybe_calibrate(self, now: float) -> None:
        """PREで約5分たち、全帯域を一度は見たら、下見のまとめを1回だけ作る。"""
        s = self.scheduler
        if s.mode != "PRE" or s.pre_rounds < 1 or now - s.mode_since_mono < 300:
            return
        if self._calibrated_for == s.mode_since_mono:
            return
        self._calibrated_for = s.mode_since_mono
        cal = calibrate(self.analyzer, s, self.cfg.thresholds, now)
        cal["at"] = self.clock.time()
        self.calibration = cal
        for f in self.on_calibration:
            f(cal)

    def _pick_liveness(self, cands: list[int]) -> int:
        """生存確認のch：最近いちばん多く局が見えた5GHzのch。"""
        counts: dict[int, int] = {}
        now = self.clock.monotonic()
        for st in self.analyzer.table.all():
            if st.primary_freq in cands and now - st.last_seen_mono < 900:
                counts[st.primary_freq] = counts.get(st.primary_freq, 0) + 1
        if not counts:
            return cands[0]
        return max(cands, key=lambda f: (counts.get(f, 0), -f))

    def _evaluate(self, now: float) -> None:
        for ev in self.detector.evaluate():
            for f in self.on_alert_event:
                try:
                    f(ev)
                except Exception:
                    log.exception("alert listener failed")

    def run_for(self, seconds: float) -> None:
        self.engine.run_for(seconds)
