"""合否ゲートが不合格のときの代わり：管理モードのパッシブスキャン（仕様書「起動前の確認」）。

`iw dev <if> scan passive` で探し、nl80211 から生のIEを受け取って、解析部にはビーコンとして渡す。
パッシブスキャンが失敗しても、能動スキャン（プローブ要求を送る）には切り替えない（受信専用）。
この無線が受けられないch（`iw phy` で disabled か、一覧に無いもの。例：6GHz非対応の無線の6GHz）は、
受信していた時間を0として返す（「検知なし」ではなく「見ていない」になるように）。
動くのはビーコンから分かる機能（局の一覧、占有表示、帯域幅、割当違反、署名による検知）だけ。
活動量・リトライ率・deauth・160MHz受信は使えない（データフレームが無い）。
6GHzはPSCと、2.4/5GHzのRNRで知ったchに限られる可能性がある（推測）。
"""
from __future__ import annotations

import logging
from typing import Callable

from .. import channels as ch
from ..capture import build
from ..clock import Clock, RealClock
from .base import FrameBatch, RadioError, RegInfo, SurveyEntry, Tuning, validate_ifname
from .linux import LinuxRadio, Runner, parse_phy_freqs, subprocess_runner
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
        self.dump_at = -1e18  # スキャン結果を読んだ時刻（monotonic）
        self.last_obs: dict[str, float] = {}  # BSSID → 渡した観測の時刻（カーネルが最後に聞いた時刻）
        self.looked: dict[int, int] = {}  # ch → そのchを「見た」と数えたスキャンの番号
        self.last_scan_ok = False
        self.receivable: set[int] | None = None  # None＝分からない（すべて受けられるとみなす）
        self.receivable_at = -1e18
        try:
            self._load_receivable()
        except Exception as e:  # 起動は止めない（あとのスキャンで読み直す）
            log.warning("この無線が受けられるchを読めません：%s", e)

    @property
    def unreceivable(self) -> list[int]:
        """この無線では受けられないch（画面で「見ていない」の理由として出す）。"""
        if self.receivable is None:
            return []
        return [f for f in ch.ALL_FREQS if f not in self.receivable]

    def _nl_dump(self, ifname: str) -> list[ScanBss]:
        if self._nl is None:
            self._nl = Nl80211()
        return self._nl.scan_dump(ifname)

    def _load_receivable(self) -> None:
        """この無線が受けられるch（`iw phy <phy> info` で disabled でないもの）。国情報で変わるので時々読み直す。"""
        self.receivable_at = self.clock.monotonic()
        phy = self.phy()
        if not phy or not phy.startswith("phy") or not phy[3:].isdigit():
            return
        r = self._cmd(["iw", "phy", phy, "info"], 5.0, check=False)
        freqs = parse_phy_freqs(r.out) if r.rc == 0 else {}
        if freqs:
            self.receivable = {f for f, ok in freqs.items() if ok}

    def _refresh(self) -> None:
        if self.clock.monotonic() - self.receivable_at > 60:
            self._load_receivable()
        # パッシブだけ。失敗（busy など）しても能動スキャンには切り替えない（送信しない）
        r = self._cmd(["iw", "dev", self.ifname, "scan", "passive"], 30.0, check=False)
        self.last_scan_ok = r.rc == 0
        if not self.last_scan_ok:
            log.info("パッシブスキャンに失敗（%s）：前の結果を使い、少しあとでやり直します", (r.err or r.out).strip()[:120])
        try:
            self.cache = self.dump(self.ifname)
        except OSError as e:
            raise RadioError(f"スキャン結果を読めません：{e}") from None
        self.dump_at = self.clock.monotonic()
        # 失敗したときは早めにやり直す（20秒待たずに5秒で）
        self.cache_at = self.clock.monotonic() - (0 if self.last_scan_ok else max(0.0, self.max_age_s - 5.0))
        self.generation += 1

    def tune(self, tuning: Tuning) -> None:
        self.tuning = tuning

    def capture(self, duration: float) -> FrameBatch:
        if self.tuning is None:
            raise RadioError("tuneしていません")
        if self.clock.monotonic() - self.cache_at > self.max_age_s:
            self._refresh()
        want = self.tuning.freq
        if self.receivable is not None and want not in self.receivable:
            # この無線では受けられないch：時間は過ぎるが「受信していた秒」は0
            self.clock.sleep(min(duration, 0.05))
            t = self.clock.monotonic()
            return FrameBatch(self.tuning, t, t, [])
        t0 = self.clock.monotonic()
        self.clock.sleep(min(duration, 0.05))
        frames = []
        for b in self.cache:
            if b.freq != want:
                continue
            if b.seen_ms_ago is not None and b.seen_ms_ago > self.max_age_s * 1000 * 3:
                continue  # 古いスキャンの残り
            # カーネルは、いなくなった局もしばらく結果に残す。前に渡したときより新しく聞こえたものだけ渡す
            # （古い結果をスキャンのたびに渡すと、いなくなった局の「最終確認」が進み続ける）
            obs = self.dump_at - (b.seen_ms_ago or 0) / 1000.0
            if obs <= self.last_obs.get(b.bssid, -1e18) + 0.5:
                continue
            self.last_obs[b.bssid] = obs
            sig = None if b.signal_dbm is None else int(round(b.signal_dbm))
            frames.append(build.radiotap(want, signal=sig) + build.beacon(b.bssid, b.ies))
        t1 = self.clock.monotonic()
        # 「このchを見た」と数えるのは、成功したスキャンのあとの最初の枠だけ。同じスキャンの結果を見直しても、
        # 失敗したスキャン（busy）でも、新しく見たことにはならない（受信していた秒0）
        if self.last_scan_ok and self.looked.get(want) != self.generation:
            self.looked[want] = self.generation
            return FrameBatch(self.tuning, t0, t1, frames)
        return FrameBatch(self.tuning, t1, t1, frames)

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
