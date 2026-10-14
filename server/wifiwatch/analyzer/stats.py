"""chごとの統計。

数えるのは「(主chのMHz, 受信の幅) ごと」。活動量やフレーム数は、そのchを実際に受信していた秒数
（切替の時間を除いた受信の時間）で割る。時計の秒では割らない。

PREモード（20MHzで250ms）とLIVEモード（160MHzで7秒）は同じchでも別の鍵になるので、
モードを切り替えただけで基準と比べられることはない。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable

BUCKET_S = 10.0


@dataclass
class Bucket:
    start: float  # monotonic
    receive_s: float = 0.0
    frames: int = 0  # 自分のアドレスを含むものを除いたフレーム
    own_frames: int = 0
    data: int = 0
    mgmt: int = 0
    ctrl: int = 0
    beacons: int = 0
    retry_eligible: int = 0  # データと、ビーコン以外の管理フレーム（自分を除く）
    retries: int = 0
    bad_fcs: int = 0
    ppdu_bw: dict[int, int] = field(default_factory=dict)

    def rate(self) -> float | None:
        return self.frames / self.receive_s if self.receive_s > 0 else None


Key = tuple[int, int]  # (主chのMHz, 受信の幅)


class ChannelStats:
    def __init__(self, keep_s: float = 900.0):
        self.keep_s = keep_s
        self.buckets: dict[Key, deque[Bucket]] = {}
        self.last_observed: dict[int, float] = {}  # 主chのMHz → 最後に受信を終えた時刻（monotonic）
        self.observed_total: dict[int, float] = {}  # 主chのMHz → 受信した秒数の合計
        self.beacons_by_primary: dict[int, int] = {}  # 局の主chで数えたビーコン（重複ビーコンを含む）
        self.lock = threading.Lock()

    def _bucket(self, key: Key, t: float) -> Bucket:
        dq = self.buckets.get(key)
        if dq is None:
            dq = deque()
            self.buckets[key] = dq
        start = (t // BUCKET_S) * BUCKET_S
        if not dq or dq[-1].start < start:
            dq.append(Bucket(start))
            while dq and dq[0].start < start - self.keep_s:
                dq.popleft()
        return dq[-1]

    def add_receive(self, key: Key, receive_s: float, t_end: float) -> Bucket:
        with self.lock:
            b = self._bucket(key, t_end)
            b.receive_s += receive_s
            self.last_observed[key[0]] = t_end
            self.observed_total[key[0]] = self.observed_total.get(key[0], 0.0) + receive_s
            return b

    def count_beacon_primary(self, primary_freq: int) -> None:
        with self.lock:
            self.beacons_by_primary[primary_freq] = self.beacons_by_primary.get(primary_freq, 0) + 1

    # ---------------------------------------------------------------- 読み出し
    def window(self, key: Key, now: float, seconds: float) -> list[Bucket]:
        with self.lock:
            dq = self.buckets.get(key)
            if not dq:
                return []
            return [b for b in dq if b.start + BUCKET_S > now - seconds]

    def totals(self, key: Key, now: float, seconds: float) -> Bucket:
        out = Bucket(now)
        for b in self.window(key, now, seconds):
            out.receive_s += b.receive_s
            out.frames += b.frames
            out.own_frames += b.own_frames
            out.data += b.data
            out.mgmt += b.mgmt
            out.ctrl += b.ctrl
            out.beacons += b.beacons
            out.retry_eligible += b.retry_eligible
            out.retries += b.retries
            out.bad_fcs += b.bad_fcs
            for k, v in b.ppdu_bw.items():
                out.ppdu_bw[k] = out.ppdu_bw.get(k, 0) + v
        return out

    def rate_history(self, key: Key, now: float, seconds: float, exclude_current: bool = True,
                     min_receive_s: float = 0.5) -> list[float]:
        """過去 seconds 秒の10秒の桶ごとの活動量（フレーム/受信秒）。受信が少なすぎる桶は除く。"""
        cur_start = (now // BUCKET_S) * BUCKET_S
        out = []
        for b in self.window(key, now, seconds):
            if exclude_current and b.start >= cur_start:
                continue
            if b.receive_s >= min_receive_s:
                out.append(b.frames / b.receive_s)
        return out

    def baseline(self, key: Key, now: float, seconds: float, min_buckets: int) -> float | None:
        h = self.rate_history(key, now, seconds)
        if len(h) < min_buckets:
            return None
        return median(h)

    def keys_for(self, freq: int) -> list[Key]:
        with self.lock:
            return [k for k in self.buckets if k[0] == freq]

    def observed_since(self, freq: int) -> float | None:
        return self.last_observed.get(freq)

    def all_keys(self) -> Iterable[Key]:
        with self.lock:
            return list(self.buckets)
