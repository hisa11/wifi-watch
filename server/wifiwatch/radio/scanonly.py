"""合否ゲートが不合格のときの代わり：管理モードのパッシブスキャン（仕様書「起動前の確認」）。

`iw dev <if> scan passive` で探し、nl80211 から生のIEを受け取って、解析部にはビーコンとして渡す。
動くのはビーコンから分かる機能（局の一覧、占有表示、帯域幅、割当違反、署名による検知）だけ。
活動量・リトライ率・deauth・160MHz受信は使えない（データフレームが無い）。
6GHzはPSCと、2.4/5GHzのRNRで知ったchに限られる可能性がある（推測）。
"""
from __future__ import annotations

import logging
from typing import Callable

from ..capture import build
from ..clock import Clock, RealClock
from .base import FrameBatch, RadioError, RegInfo, SurveyEntry, Tuning, validate_ifname
from .linux import LinuxRadio, Runner, subprocess_runner
from .nl80211 import Nl80211, ScanBss

log = logging.getLogger(__name__)


class ScanOnlyRadio(LinuxRadio):
    """tune は覚えるだけ。capture はスキャン結果のうち、そのchの局をビーコンとして返す（スキャン1回につき1度）。"""

    scan_only = True

    def __init__(self, ifname: str, clock: Clock | None = None, runner: Runner = subprocess_runner,
                 dump: Callable[[str], list[ScanBss]] | None = None, max_age_s: float = 20.0,
                 sys_class_net: str = "/sys/class/net"):
        super().__init__(ifname, clock or RealClock(), runner, sys_class_net=sys_class_net)
        self._nl: Nl80211 | None = None
        self.dump = dump or self._nl_dump
        self.max_age_s = max_age_s
        self.cache: list[ScanBss] = []
        self.cache_at = -1e18
        self.generation = 0
        self.delivered: set[tuple[int, str]] = set()
        self.last_scan_ok = False

    def _nl_dump(self, ifname: str) -> list[ScanBss]:
        if self._nl is None:
            self._nl = Nl80211()
        return self._nl.scan_dump(ifname)

    def _refresh(self) -> None:
        r = self._cmd(["iw", "dev", self.ifname, "scan", "passive"], 30.0, check=False)
        if r.rc != 0:
            r = self._cmd(["iw", "dev", self.ifname, "scan"], 30.0, check=False)
        self.last_scan_ok = r.rc == 0
        try:
            self.cache = self.dump(self.ifname)
        except OSError as e:
            raise RadioError(f"スキャン結果を読めません：{e}") from None
        self.cache_at = self.clock.monotonic()
        self.generation += 1
        self.delivered = set()

    def tune(self, tuning: Tuning) -> None:
        self.tuning = tuning

    def capture(self, duration: float) -> FrameBatch:
        if self.tuning is None:
            raise RadioError("tuneしていません")
        if self.clock.monotonic() - self.cache_at > self.max_age_s:
            self._refresh()
        t0 = self.clock.monotonic()
        self.clock.sleep(min(duration, 0.05))
        frames = []
        want = self.tuning.freq
        for b in self.cache:
            if b.freq != want or (self.generation, b.bssid) in self.delivered:
                continue
            if b.seen_ms_ago is not None and b.seen_ms_ago > self.max_age_s * 1000 * 3:
                continue  # 古いスキャンの残り
            self.delivered.add((self.generation, b.bssid))
            sig = None if b.signal_dbm is None else int(round(b.signal_dbm))
            frames.append(build.radiotap(want, signal=sig) + build.beacon(b.bssid, b.ies))
        return FrameBatch(self.tuning, t0, self.clock.monotonic(), frames)

    def survey(self) -> list[SurveyEntry] | None:
        return None

    def set_monitor(self) -> None:
        # 管理モードのまま使う
        self._link(True)

    def reset_monitor(self) -> None:
        self._link(True)

    def managed_scan(self) -> int:
        self._refresh()
        return len(self.cache)

    def close(self) -> None:
        if self._nl is not None:
            self._nl.close()
