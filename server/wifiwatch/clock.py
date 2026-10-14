"""時刻の注入。巡回・警報・中継の問い合わせを、実時間を待たずにテストするため。

時刻はUTCのエポック秒で保存し、表示だけJSTにする。
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Protocol

JST = timezone(timedelta(hours=9), "JST")


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float:
        """UTCのエポック秒。"""

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def monotonic(self) -> float:
        return _time.monotonic()

    def time(self) -> float:
        return _time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


class FakeClock:
    """手で進める時計。sleep() は待たずに時計を進める。"""

    def __init__(self, start_epoch: float = 1_791_936_000.0) -> None:  # 2026-10-14 00:00 UTC
        self._mono = 1000.0
        self._epoch0 = start_epoch
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def time(self) -> float:
        with self._lock:
            return self._epoch0 + (self._mono - 1000.0)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("時計は戻せません")
        with self._lock:
            self._mono += seconds

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.advance(seconds)


def utc_iso(ts: float) -> str:
    """保存用のUTC文字列（ミリ秒まで）。"""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc_iso(s: str) -> float:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def jst_str(ts: float, with_date: bool = True) -> str:
    """表示用のJST文字列。"""
    dt = datetime.fromtimestamp(ts, JST)
    return dt.strftime("%m/%d %H:%M:%S" if with_date else "%H:%M:%S")
