"""受信ループ：巡回の計画どおりに tune → capture → 解析 を繰り返す。

時計は注入できる（偽の時計なら実時間を待たずに何時間ぶんでも回せる）。
長い枠（自ブロックの1.75秒など）は chunk_s ごとに区切って解析し、画面と警報の更新を遅らせない。
同じ受信の設定が続くときは tune し直さない（切替の時間を無駄にしない）。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .analyzer.core import Analyzer
from .clock import Clock
from .radio.base import Radio, RadioError, Tuning
from .scan import ScanCollector
from .scheduler import Scheduler, Slot

log = logging.getLogger(__name__)


@dataclass
class SlotResult:
    slot: Slot
    t_start: float
    t_end: float = 0.0
    receive_s: float = 0.0
    frames: int = 0
    accepted: int = 0
    beacons: dict[str, int | None] = field(default_factory=dict)
    error: str | None = None


class Ticker:
    def __init__(self, interval: float, fn: Callable[[float], None]):
        self.interval = interval
        self.fn = fn
        self.next = -1e18


class Engine:
    def __init__(self, clock: Clock, radio: Radio, analyzer: Analyzer, scheduler: Scheduler,
                 chunk_s: float = 0.5, device: str = "pit-pc"):
        self.clock = clock
        self.radio = radio
        self.analyzer = analyzer
        self.scheduler = scheduler
        self.chunk_s = chunk_s
        self.device = device
        self.current: Tuning | None = None
        self.collector = ScanCollector()
        self.slot_log: deque[SlotResult] = deque(maxlen=2000)
        self.on_slot: list[Callable[[SlotResult], None]] = []
        self.on_scan_record: list[Callable[[dict[str, Any]], None]] = []
        self.classify: Callable[[Any], None] | None = None
        self.tickers: list[Ticker] = []
        self.tune_errors = 0
        self._mode_before_manual = scheduler.mode
        self.stopping = False
        self._tune_err_logged: dict[tuple[int, int], float] = {}

    def add_ticker(self, interval: float, fn: Callable[[float], None]) -> None:
        self.tickers.append(Ticker(interval, fn))

    def _tick(self) -> None:
        now = self.clock.monotonic()
        for t in self.tickers:
            if now >= t.next:
                t.next = now + t.interval
                try:
                    t.fn(now)
                except Exception:  # 監視を止めない
                    log.exception("ticker failed")

    def step(self) -> SlotResult:
        slot = self.scheduler.next_slot()
        if slot.kind == "manual" and (not self.slot_log or self.slot_log[-1].slot.kind != "manual"):
            self._mode_before_manual = self.scheduler.mode
        t0 = self.clock.monotonic()
        end = t0 + slot.dwell
        res = SlotResult(slot, t0)
        try:
            if self.current != slot.tuning:
                self.current = None
                self.radio.tune(slot.tuning)
                self.current = slot.tuning
        except RadioError as e:
            res.error = str(e)
            self.tune_errors += 1
            # 同じchの同じ失敗は10分に1回だけ書く（巡回のたびに同じ行でログが埋まらないように）
            key = (slot.tuning.freq, slot.tuning.width)
            now_m = self.clock.monotonic()
            if now_m - self._tune_err_logged.get(key, -1e18) >= 600:
                self._tune_err_logged[key] = now_m
                log.warning("tune failed: %s（同じchの同じ失敗は10分に1回だけ書きます）", e)
            self.clock.sleep(max(0.0, end - self.clock.monotonic()))
        else:
            while not self.stopping:
                rem = end - self.clock.monotonic()
                if rem <= 1e-6:
                    break
                try:
                    batch = self.radio.capture(min(rem, self.chunk_s))
                except RadioError as e:
                    res.error = str(e)
                    self.current = None
                    self.clock.sleep(max(0.0, end - self.clock.monotonic()))
                    break
                r = self.analyzer.process_batch(batch)
                res.receive_s += r.receive_s
                res.frames += r.frames
                res.accepted += r.accepted
                for b, s in r.beacons.items():
                    prev = res.beacons.get(b)
                    if b not in res.beacons or (s is not None and (prev is None or s > prev)):
                        res.beacons[b] = s
                self._tick()
        res.t_end = self.clock.monotonic()
        if slot.kind == "manual" and slot.scan_id:
            self.collector.add(slot.scan_id, slot.freq, res.receive_s, res.frames, res.accepted, res.beacons)
        done = self.scheduler.slot_done(slot)
        self.slot_log.append(res)
        for f in self.on_slot:
            try:
                f(res)
            except Exception:
                log.exception("on_slot failed")
        if done is not None:
            record = self.collector.finish(done, self.analyzer.table, self.device, self._mode_before_manual,
                                           self.classify)
            for f in self.on_scan_record:
                try:
                    f(record)
                except Exception:
                    log.exception("on_scan_record failed")
        self._tick()
        return res

    def run_for(self, seconds: float) -> None:
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end - 1e-9 and not self.stopping:
            self.step()

    def run_forever(self) -> None:
        while not self.stopping:
            self.step()
