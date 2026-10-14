"""受信停止の検知と復旧（仕様書「受信停止」）。

- 巡回の2.4/5GHzの枠（生存確認の枠）で1件も受信しない「周期」が2回続いたら、モニタを再設定する。
- 次の周期も受信しなければ、iwlwifiを読み込み直し、管理モードでスキャン → 国設定がJPで6GHzが有効か確認 → モニタ。
- それでも次の周期に受信しなければ「失敗」（crit を出す）。その後も一定間隔で復旧を試み続ける。
- 自chが静かなこと自体は故障とみなさない（静かな6GHzでは正常なため）。
- 自ブロックへのtuneが続けて失敗するとき（6GHzが無効になった等）も、同じ手順で復旧する。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .clock import Clock
from .engine import Engine, SlotResult
from .radio.base import RadioError

log = logging.getLogger(__name__)


class LivenessMonitor:
    def __init__(self, clock: Clock, engine: Engine, empty_periods: int = 2, own_tune_failures: int = 3,
                 retry_after_s: float = 300.0):
        self.clock = clock
        self.engine = engine
        self.radio = engine.radio
        self.needed = empty_periods
        self.own_tune_failures = own_tune_failures
        self.retry_after_s = retry_after_s
        self.state = "ok"  # ok / suspect / recovering / failed
        self.step = 0  # 0: まだ何もしていない 1: モニタを再設定した 2: ドライバを読み込み直した
        self.reason = ""
        self.empty = 0
        self.own_fail = 0
        self.period: int | None = None
        self.acc_slots = 0
        self.acc_frames = 0
        self.last_frames_epoch: float | None = None
        self.failed_since: float | None = None
        self.history: list[dict[str, Any]] = []
        self.on_change: list[Callable[[dict[str, Any]], None]] = []
        engine.on_slot.append(self.on_slot)

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "step": self.step, "reason": self.reason, "empty_periods": self.empty,
                "last_frames_at": self.last_frames_epoch}

    def _note(self, what: str, **kw: Any) -> None:
        ev = {"ts": self.clock.time(), "what": what, **kw}
        self.history.append(ev)
        del self.history[:-50]
        log.info("liveness: %s %s", what, kw)
        for f in self.on_change:
            f(ev)

    def on_slot(self, res: SlotResult) -> None:
        s = res.slot
        if s.kind in ("manual", "coldstart"):
            return
        if s.kind == "own" and res.error:
            self.own_fail += 1
            if self.own_fail >= self.own_tune_failures:
                self.own_fail = 0
                self._recover(f"自ブロックに合わせられない（{res.error}）")
            return
        if s.kind == "own":
            self.own_fail = 0
        if self.period is None:
            self.period = s.period
        if s.period != self.period:
            self._finalize()
            self.period = s.period
        if s.liveness:
            self.acc_slots += 1
            self.acc_frames += res.frames
            if res.frames:
                self.last_frames_epoch = self.clock.time()
            if s.liveness_last:
                self._finalize()  # この周期の生存確認の枠はこれで終わり
        if self.state == "failed" and self.failed_since is not None and \
                self.clock.monotonic() - self.failed_since >= self.retry_after_s:
            self.failed_since = self.clock.monotonic()
            self.step = 0
            self._recover("失敗のあとの再試行")

    def _finalize(self) -> None:
        slots, frames = self.acc_slots, self.acc_frames
        self.acc_slots = self.acc_frames = 0
        if slots == 0:
            return
        if frames > 0:
            if self.step >= 1 or self.state == "failed":  # 様子見から戻っただけなら記録しない
                self._note("recovered", after_step=self.step)
            self.state, self.step, self.empty, self.reason = "ok", 0, 0, ""
            self.failed_since = None
            return
        self.empty += 1
        if self.state == "ok":
            self.state = "suspect"
        if self.step == 0 and self.empty >= self.needed:
            self._recover(f"2.4/5GHzの生存確認で{self.empty}周期つづけて受信なし")
        elif self.step >= 1 and self.empty >= 1:
            self._recover(f"復旧の手順{self.step}のあとも受信なし")

    def _recover(self, why: str) -> None:
        self.empty = 0
        self.engine.current = None  # 次の枠で必ずtuneし直す
        if self.step == 0:
            self.state = "recovering"
            self._note("reset_monitor", why=why)
            try:
                self.radio.reset_monitor()
                self.step = 1
            except RadioError as e:
                self.step = 1
                self._note("reset_monitor_failed", error=str(e))
            return
        if self.step == 1:
            self.state = "recovering"
            self._note("reload_driver", why=why)
            try:
                self.radio.reload_driver()
                n = self.radio.managed_scan()
                reg = self.radio.regulatory()
                if reg.country != "JP" or not reg.six_ghz_enabled:
                    self.step = 2
                    self._fail(f"国設定が {reg.country} / 6GHz {'有効' if reg.six_ghz_enabled else '無効'}（スキャンで{n}局）")
                    try:
                        self.radio.set_monitor()
                    except RadioError:
                        pass
                    return
                self.radio.set_monitor()
                self.step = 2
            except RadioError as e:
                self.step = 2
                self._fail(f"ドライバの読み込み直しに失敗：{e}")
            return
        self._fail(f"復旧できません（{why}）")

    def _fail(self, reason: str) -> None:
        if self.state != "failed":
            self.failed_since = self.clock.monotonic()
        self.state = "failed"
        self.reason = reason
        self._note("failed", reason=reason)
