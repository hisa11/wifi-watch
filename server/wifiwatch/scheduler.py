"""巡回の計画（仕様書「監視方式」）。

AX210は無線が1系統なので、1つのch（幅）しか同時に見られない。自ブロックの160MHz受信と、
局を見つけるための20MHzの巡回を時分割する。

  LIVE   10秒周期：自ブロック7秒（4つに分けて巡回の間に挟む）＋250msの巡回12枠
         自ブロック内の別ch 4枠・6GHzのその他 4枠・5GHz 3枠（予備chが5GHzなら1枠を専用）・2.4GHz 1枠
  QUIET  10秒周期：自ブロック内の別ch 4枠（1秒）、30秒ごとに5GHzを250ms（生存確認）、残りは自ブロック
  PRE    57chを250msずつ（1周約14秒）
  手動    指定のchを250msずつ。その間は自ブロックの受信と巡回を止め、終わったら元のモードに戻る
  初回    起動直後に自ブロックとPSCを200msずつ1周（グラフを約5秒で出す）

巡回から外した帯域・chの枠は残りに配り直す（自ブロック内の別chの枠と生存確認は動かさない）。
どの枠も200ms以上（切替を200ms未満の間隔にしない）。
"""
from __future__ import annotations

import math
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import channels as ch
from .clock import Clock
from .config import ScheduleConfig
from .radio.base import Tuning, validate_tuning

MODES = ("PRE", "LIVE", "QUIET")

# 巡回の組（優先の高い順）
G_IN_BLOCK = "in_block"
G_SIX = "six"
G_FIVE = "five"
G_SPARE = "spare"
G_TWO4 = "two4"
PATROL_GROUPS = (G_IN_BLOCK, G_SIX, G_FIVE, G_SPARE, G_TWO4)
LIVENESS_GROUPS = (G_FIVE, G_SPARE, G_TWO4)  # 生存確認に使う2.4/5GHzの枠


class SchedulerError(Exception):
    code = "invalid"


class Busy(SchedulerError):
    code = "busy"


class NeedsConfirm(SchedulerError):
    code = "needs_confirm"


@dataclass(frozen=True)
class Slot:
    kind: str  # own / in_block / six / five / spare / two4 / liveness / pre / manual / coldstart
    tuning: Tuning
    dwell: float  # 切替を含む枠の長さ（秒）
    period: int = 0  # 生存確認の「周期」の番号
    liveness: bool = False  # この枠の受信を生存確認に使うか
    scan_id: str | None = None

    @property
    def freq(self) -> int:
        return self.tuning.freq


@dataclass
class ManualScan:
    id: str
    freqs: list[int]
    origin: str  # desktop / app
    request_id: str | None
    requested_at: float  # UTC
    started_mono: float | None = None
    done: int = 0
    finished_at: float | None = None
    state: str = "queued"  # queued / running / done
    slot_s: float = 0.25

    @property
    def remaining_s(self) -> float:
        return max(0, len(self.freqs) - self.done) * self.slot_s

    def to_dict(self) -> dict:
        return {
            "id": self.id, "state": self.state, "origin": self.origin, "request_id": self.request_id,
            "freqs": list(self.freqs), "done": self.done, "total": len(self.freqs),
            "remaining_s": round(self.remaining_s, 2), "requested_at": self.requested_at,
            "finished_at": self.finished_at,
        }


def tuning20(freq: int) -> Tuning:
    return validate_tuning(freq, 20, freq)


def priority_order(own_freq: int, freqs: Iterable[int] | None = None) -> list[int]:
    """仕様書の優先：自ブロック（自chを先頭）→ PSC → その他の6GHz → 5GHz → 2.4GHz。"""
    block = ch.own_block(own_freq)
    bset = set(block.freqs)
    allf = list(freqs) if freqs is not None else list(ch.ALL_FREQS)
    sel = set(allf)

    def rank(f: int) -> tuple[int, int]:
        c = ch.BY_FREQ[f]
        if f == own_freq:
            return (0, f)
        if f in bset:
            return (1, f)
        if c.band == ch.BAND_6:
            return (2 if c.is_psc else 3, f)
        if c.band == ch.BAND_5:
            return (4, f)
        return (5, f)

    return sorted(sel, key=rank)


class Scheduler:
    def __init__(
        self,
        cfg: ScheduleConfig,
        clock: Clock,
        own_freq: int,
        spare: Iterable[int] = (),
        mode: str = "LIVE",
        excluded_bands: Iterable[str] = (),
        excluded_freqs: Iterable[int] = (),
        liveness_pick: Callable[[list[int]], int] | None = None,
        coldstart: bool = True,
    ):
        self.cfg = cfg
        self.clock = clock
        self.lock = threading.RLock()
        self.liveness_pick = liveness_pick or (lambda cands: cands[0])
        self._plan: deque[Slot] = deque()
        self._rot: dict[str, int] = {}
        self._cycle = 0
        self._period = 0
        self._manual: ManualScan | None = None
        self._last_manual: ManualScan | None = None
        self._coldstart = coldstart
        self.mode = "LIVE"
        self.mode_since_mono = clock.monotonic()
        self.pre_rounds = 0
        self._pre_built = 0
        self.own_freq = own_freq
        self.spare = list(spare)
        self.excluded_bands: set[str] = set()
        self.excluded_freqs: set[int] = set()
        self.on_manual_done: list[Callable[[ManualScan], None]] = []
        self.set_own(own_freq, self.spare)
        self.set_exclusions(excluded_bands, excluded_freqs)
        self.set_mode(mode)

    # ------------------------------------------------------------ 設定
    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise SchedulerError(f"モードは {', '.join(MODES)} のどれかです")
        with self.lock:
            if mode != self.mode:
                self.mode = mode
                self.mode_since_mono = self.clock.monotonic()
                self.pre_rounds = 0
                self._pre_built = 0
            self._plan.clear()

    def set_own(self, own_freq: int, spare: Iterable[int] | None = None) -> None:
        if not ch.is_known_freq(own_freq):
            raise SchedulerError(f"{own_freq} はchではありません")
        with self.lock:
            self.own_freq = own_freq
            if spare is not None:
                self.spare = [f for f in spare if ch.is_known_freq(f)]
            self.block = ch.own_block(own_freq)
            self.own_tuning = validate_tuning(own_freq, self.block.width, self.block.center_freq)
            # 自ブロックのchは外せないので、外す設定から除く
            self.excluded_freqs = {f for f in self.excluded_freqs if f not in self.block.freqs}
            self._plan.clear()

    def validate_exclusions(self, bands: Iterable[str], freqs: Iterable[int]) -> tuple[set[str], set[int]]:
        bset = set(bands)
        fset = set(freqs)
        bad = [b for b in bset if b not in ch.BANDS]
        if bad:
            raise SchedulerError(f"帯域が不正です：{bad}")
        badf = [f for f in fset if not ch.is_known_freq(f)]
        if badf:
            raise SchedulerError(f"chが不正です：{badf}")
        # 自ブロックのある帯域を外しても、自ブロック（自分のchを含む160MHz）は見続ける
        clash = sorted(f for f in fset if f in self.block.freqs)
        if clash:
            raise SchedulerError(f"自ブロック（{self.block.label}）のchは外せません：{', '.join(ch.label(f) for f in clash)}")
        return bset, fset

    def set_exclusions(self, bands: Iterable[str], freqs: Iterable[int]) -> None:
        with self.lock:
            b, f = self.validate_exclusions(bands, freqs)
            self.excluded_bands = b
            self.excluded_freqs = f
            self._plan.clear()

    def is_excluded(self, freq: int) -> bool:
        if freq in self.block.freqs:
            return False  # 自ブロックは外せない
        return freq in self.excluded_freqs or ch.BY_FREQ[freq].band in self.excluded_bands

    def included_freqs(self) -> list[int]:
        return [f for f in ch.ALL_FREQS if not self.is_excluded(f)]

    # ------------------------------------------------------------ 組と枠
    def groups(self) -> dict[str, list[int]]:
        bset = set(self.block.freqs)
        g: dict[str, list[int]] = {k: [] for k in PATROL_GROUPS}
        g[G_IN_BLOCK] = [f for f in self.block.freqs if f != self.own_freq]  # 自ブロックは外せない
        spare5 = [f for f in self.spare if ch.BY_FREQ[f].band == ch.BAND_5 and f not in bset and not self.is_excluded(f)]
        if spare5:
            g[G_SPARE] = [spare5[0]]
        for f in priority_order(self.own_freq):
            if f in bset or self.is_excluded(f) or f in g[G_SPARE]:
                continue
            band = ch.BY_FREQ[f].band
            g[{ch.BAND_6: G_SIX, ch.BAND_5: G_FIVE, ch.BAND_24: G_TWO4}[band]].append(f)
        return g

    def allocation(self, mode: str | None = None) -> dict[str, int]:
        """1周期あたりの枠の数。外した帯域の枠は残りに配り直す。"""
        mode = mode or self.mode
        g = self.groups()
        c = self.cfg
        if mode == "QUIET":
            n = round(c.quiet_in_block_s / c.slot_s)
            return {G_IN_BLOCK: min(n, len(g[G_IN_BLOCK]))}
        base = {G_IN_BLOCK: c.live_in_block_slots, G_SIX: c.live_six_slots, G_FIVE: c.live_five_slots,
                G_SPARE: 0, G_TWO4: c.live_two4_slots}
        if g[G_SPARE]:
            base[G_FIVE] -= 1
            base[G_SPARE] = 1
        slots: dict[str, int] = {}
        free = 0
        for k, n in base.items():
            slots[k] = min(n, len(g[k]))
            free += n - slots[k]
        redistributable = (G_SIX, G_FIVE, G_TWO4)
        while free > 0:
            cands = [k for k in redistributable if slots[k] < len(g[k])]
            if not cands:
                break  # 残りは自ブロックの受信に戻す
            k = max(cands, key=lambda k: (len(g[k]) / slots[k]) if slots[k] else math.inf)
            slots[k] += 1
            free -= 1
        return {k: v for k, v in slots.items() if v > 0}

    def _next_in(self, group: str, freqs: list[int]) -> int:
        i = self._rot.get(group, 0) % len(freqs)
        self._rot[group] = i + 1
        return freqs[i]

    @staticmethod
    def _interleave(alloc: dict[str, int]) -> list[str]:
        pos: list[tuple[float, int, str]] = []
        for k, n in alloc.items():
            for j in range(n):
                pos.append(((j + 0.5) / n, PATROL_GROUPS.index(k), k))
        return [k for _, _, k in sorted(pos)]

    def _liveness_slot(self) -> Slot:
        cands = [f for f in ch.FREQS_BY_BAND[ch.BAND_5] if f not in self.block.freqs]
        f = self.liveness_pick(cands)
        if f not in cands:
            f = cands[0]
        return Slot("liveness", tuning20(f), self.cfg.liveness_dwell_s, self._period, True)

    def _build_cycle(self) -> list[Slot]:
        c = self.cfg
        mode = self.mode
        g = self.groups()
        if mode == "PRE":
            return self._build_pre(g)
        alloc = self.allocation(mode)
        kinds = self._interleave(alloc)
        has_live_slots = any(k in LIVENESS_GROUPS for k in kinds)
        patrol: list[Slot] = []
        self._period = self._cycle
        for k in kinds:
            f = self._next_in(k, g[k])
            patrol.append(Slot(k, tuning20(f), c.slot_s, self._period, k in LIVENESS_GROUPS))
        every = max(1, round(c.liveness_period_s / c.cycle_s))
        if not has_live_slots and self._cycle % every == 0:
            self._period = self._cycle // every
            patrol.append(self._liveness_slot())
        own_total = c.cycle_s - sum(s.dwell for s in patrol)
        n_groups = max(1, min(c.live_groups, len(patrol))) if patrol else 1
        while n_groups > 1 and own_total / n_groups < c.min_switch_interval_s:
            n_groups -= 1
        seg = own_total / n_groups
        chunks: list[list[Slot]] = [[] for _ in range(n_groups)]
        for i, s in enumerate(patrol):
            chunks[i * n_groups // max(1, len(patrol))].append(s)
        out: list[Slot] = []
        for i in range(n_groups):
            out.append(Slot("own", self.own_tuning, seg, self._period))
            out.extend(chunks[i])
        return out

    def _build_pre(self, g: dict[str, list[int]]) -> list[Slot]:
        c = self.cfg
        freqs = [f for f in priority_order(self.own_freq) if f in set(self.block.freqs) or not self.is_excluded(f)]
        self._period = self._cycle
        out = [Slot("pre", tuning20(f), c.slot_s, self._period, ch.BY_FREQ[f].band != ch.BAND_6) for f in freqs]
        if not any(s.liveness for s in out):
            n = max(1, math.ceil(len(out) * c.slot_s / c.liveness_period_s))
            for j in range(n):
                out.insert((j + 1) * len(out) // (n + 1), self._liveness_slot())
        return out

    def _coldstart_slots(self) -> list[Slot]:
        d = self.cfg.coldstart_dwell_s
        out = [Slot("coldstart", self.own_tuning, d)]
        bset = set(self.block.freqs)
        for f in self.block.freqs:
            if f != self.own_freq:
                out.append(Slot("coldstart", tuning20(f), d))
        for f in ch.FREQS_BY_BAND[ch.BAND_6]:
            if ch.BY_FREQ[f].is_psc and f not in bset and not self.is_excluded(f):
                out.append(Slot("coldstart", tuning20(f), d))
        return out

    # ------------------------------------------------------------ 進行
    def next_slot(self) -> Slot:
        with self.lock:
            m = self._manual
            if m is not None:
                if m.state == "queued":
                    m.state = "running"
                    m.started_mono = self.clock.monotonic()
                    self._plan.clear()
                return Slot("manual", tuning20(m.freqs[m.done]), m.slot_s, scan_id=m.id)
            if self._coldstart:
                self._coldstart = False
                self._plan.extend(self._coldstart_slots())
            if not self._plan:
                cyc = self._build_cycle()
                self._cycle += 1
                if self.mode == "PRE":
                    self._pre_built += 1
                    self.pre_rounds = self._pre_built - 1  # 終わった周の数
                self._plan.extend(cyc)
            return self._plan.popleft()

    def slot_done(self, slot: Slot) -> ManualScan | None:
        """枠が終わったら呼ぶ。手動スキャンが終われば、その ManualScan を返す。"""
        with self.lock:
            m = self._manual
            if slot.kind != "manual" or m is None or slot.scan_id != m.id:
                return None
            m.done += 1
            if m.done < len(m.freqs):
                return None
            m.state = "done"
            m.finished_at = self.clock.time()
            self._last_manual = m
            self._manual = None
            self._plan.clear()  # 元のモードの最初の周期から
        for f in self.on_manual_done:
            f(m)
        return m

    # ------------------------------------------------------------ 手動スキャン
    def request_manual(self, freqs: Iterable[int] | None = None, origin: str = "desktop",
                       confirm: bool = False, request_id: str | None = None,
                       bands: Iterable[str] | None = None) -> ManualScan:
        with self.lock:
            if self._manual is not None:
                raise Busy("手動スキャンを実行中です（同時に走るのは1つだけ）")
            if self.mode == "QUIET" and not confirm:
                raise NeedsConfirm("QUIET中です。実行中は自chを見ない時間ができます。確認してから送ってください")
            if freqs is None and bands is None:
                targets = self.included_freqs()  # 既定は巡回に入っている全ch
            else:
                targets = []
                for b in bands or []:
                    if b not in ch.BANDS:
                        raise SchedulerError(f"帯域が不正です：{b}")
                    targets.extend(ch.FREQS_BY_BAND[b])  # 指定すれば巡回外の帯域もスキャンできる
                for f in freqs or []:
                    if not ch.is_known_freq(f):
                        raise SchedulerError(f"chが不正です：{f}")
                    targets.append(f)
            if not targets:
                raise SchedulerError("スキャンするchがありません")
            ordered = priority_order(self.own_freq, set(targets))
            m = ManualScan(uuid.uuid4().hex[:12], ordered, origin, request_id, self.clock.time(),
                           slot_s=self.cfg.slot_s)
            self._manual = m
            return m

    def manual_status(self) -> dict | None:
        with self.lock:
            m = self._manual or self._last_manual
            return m.to_dict() if m else None

    @property
    def manual_running(self) -> bool:
        return self._manual is not None

    # ------------------------------------------------------------ 表示用
    def revisit_s(self) -> dict[int, float]:
        """chごとの再訪の間隔の見込み（秒）。自chは0、巡回外は無限大。"""
        c = self.cfg
        out: dict[int, float] = {f: math.inf for f in ch.ALL_FREQS}
        out[self.own_freq] = 0.0
        g = self.groups()
        if self.mode == "PRE":
            n = len(self._build_pre_preview())
            for f in ch.ALL_FREQS:
                if f in set(self.block.freqs) or not self.is_excluded(f):
                    out[f] = n * c.slot_s
            return out
        alloc = self.allocation()
        for k, n in alloc.items():
            if n <= 0:
                continue
            per = math.ceil(len(g[k]) / n) * c.cycle_s
            for f in g[k]:
                out[f] = per
        return out

    def _build_pre_preview(self) -> list[int]:
        return [f for f in ch.ALL_FREQS if f in set(self.block.freqs) or not self.is_excluded(f)]

    def channel_states(self) -> dict[int, dict]:
        """chごとの「見ているか」。検知なしと見ていないを区別するため。"""
        rv = self.revisit_s()
        manual = self._manual
        out = {}
        for f in ch.ALL_FREQS:
            if manual is not None:
                state = "scanning" if f in manual.freqs[manual.done:manual.done + 1] else (
                    "scan_target" if f in manual.freqs else "paused")
            elif f == self.own_freq:
                state = "own"
            elif self.is_excluded(f):
                state = "excluded"
            elif f in self.block.freqs:
                state = "in_block"
            else:
                state = "patrol"
            out[f] = {"state": state, "revisit_s": None if math.isinf(rv[f]) else rv[f]}
        return out

    def own_ratio(self) -> float:
        """自chを受信している時間の割合（割り当て。切替の時間を含む）。"""
        c = self.cfg
        if self.mode == "PRE":
            n = len(self._build_pre_preview())
            return c.slot_s / (n * c.slot_s) if n else 0.0
        alloc = self.allocation()
        patrol = sum(alloc.values()) * c.slot_s
        has_live = any(k in LIVENESS_GROUPS for k in alloc)
        every = max(1, round(c.liveness_period_s / c.cycle_s))
        live = 0.0 if has_live else c.liveness_dwell_s / every
        return (c.cycle_s - patrol - live) / c.cycle_s

    def status(self) -> dict:
        with self.lock:
            g = self.groups()
            return {
                "mode": self.mode,
                "own_freq": self.own_freq,
                "own_block": {"label": self.block.label, "width": self.block.width, "center": self.block.center_freq,
                              "freqs": self.block.freqs},
                "spare": list(self.spare),
                "excluded_bands": sorted(self.excluded_bands),
                "excluded_freqs": sorted(self.excluded_freqs),
                "allocation": self.allocation() if self.mode != "PRE" else {},
                "groups": {k: v for k, v in g.items() if v},
                "own_ratio": round(self.own_ratio(), 4),
                "manual": self.manual_status(),
                "pre_rounds": self.pre_rounds,
                "mode_elapsed_s": round(self.clock.monotonic() - self.mode_since_mono, 1),
            }

    def presence_window(self, freq: int) -> float:
        """局を「いる」とみなす時間（最後に見てから）。再訪の間隔の2.5倍、最低60秒。"""
        rv = self.revisit_s().get(freq, math.inf)
        if math.isinf(rv):
            return 600.0
        return max(60.0, 2.5 * rv)
