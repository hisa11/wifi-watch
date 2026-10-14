"""警報の状態と、通知するかどうかの判断（仕様書「通知」の抑制）。

- 警報は「種類:範囲」の鍵で1つ。条件が成り立つ間「発生中」。当事者（局）を持つ。
- 発生：通知する。ただし利用者がまだ「発生中」と思っている間（解消をまだ知らせていない）は再通知しない。
- 追加：当事者が、最後に知らせた当事者より増えたら通知する（30秒以内でも）。
- 解消：条件が10秒消えたら解消。通知は「最後に発生を知らせてから30秒」より早くは出さない
  （出す前に再び成り立てば、解消も再発生も知らせない）。これで同じ警報を30秒以内に何度も知らせない。
- 「1回だけ」の警報（RoboconFreeの6GHz出現）は、鍵ごとに一度だけ通知する（再起動しても覚えている）。
  画面では、成り立っている間は発生中として出す。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..clock import Clock

LEVELS = ("info", "warn", "crit")
LEVEL_RANK = {k: i for i, k in enumerate(LEVELS)}

TITLES = {
    "offchannel": "割当違反（予約外ch）",
    "encroach": "割当違反（帯域の食い込み）",
    "newcomer": "新規参入×重なり",
    "unknown_strong": "正体不明の強力局・広帯域局",
    "sig6_once": "6GHzに初出現",
    "sig6_crit": "6GHzに出現",
    "activity": "活動量の急増",
    "retry": "リトライ率",
    "ssid_clone": "同名SSIDの別AP",
    "deauth": "deauth/disassocの洪水",
    "rx_stop": "受信停止",
}

DEFAULT_LEVELS = {
    "offchannel": "warn", "encroach": "warn", "newcomer": "warn", "unknown_strong": "warn",
    "sig6_once": "warn", "sig6_crit": "crit", "activity": "warn", "retry": "warn",
    "ssid_clone": "crit", "deauth": "warn", "rx_stop": "crit",
}


@dataclass
class Finding:
    type: str
    scope: str
    message: str
    parties: dict[str, dict[str, Any]] = field(default_factory=dict)
    level: str | None = None  # None なら種類の既定（configで上書きできる）
    data: dict[str, Any] = field(default_factory=dict)
    once: bool = False
    title: str | None = None

    @property
    def key(self) -> str:
        return f"{self.type}:{self.scope}"


@dataclass
class AlertState:
    key: str
    type: str
    scope: str
    level: str
    title: str
    message: str
    parties: dict[str, dict[str, Any]]
    data: dict[str, Any]
    once: bool
    first_mono: float
    first_epoch: float
    last_true_mono: float
    active: bool = True
    believed_active: bool = False  # 利用者が「発生中」と思っているか（発生を知らせ、解消をまだ知らせていない）
    notified_parties: set[str] = field(default_factory=set)
    last_raise_notify_mono: float | None = None
    resolve_pending_at: float | None = None
    resolved_epoch: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "type": self.type, "scope": self.scope, "level": self.level, "title": self.title,
            "message": self.message, "parties": list(self.parties.values()), "data": self.data,
            "since": self.first_epoch, "active": self.active, "resolved_at": self.resolved_epoch,
        }


@dataclass
class AlertEvent:
    epoch: float
    kind: str  # raised（発生）/ added（当事者の追加）/ cleared（条件が10秒消えた）/ resolved（解消の通知）
    type: str
    key: str
    level: str
    title: str
    message: str
    parties: list[dict[str, Any]]
    notify: bool
    new_parties: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.epoch, "kind": self.kind, "type": self.type, "key": self.key, "level": self.level,
            "title": self.title, "message": self.message, "parties": self.parties, "notify": self.notify,
            "new_parties": self.new_parties, "data": self.data,
        }


class AlertManager:
    def __init__(self, clock: Clock, clear_after: float = 10.0, suppress: float = 30.0,
                 levels: dict[str, str] | None = None, once_store: set[str] | None = None,
                 persist_once: Callable[[set[str]], None] | None = None):
        self.clock = clock
        self.clear_after = clear_after
        self.suppress = suppress
        self.levels = {**DEFAULT_LEVELS, **(levels or {})}
        self.states: dict[str, AlertState] = {}
        self.once_done: set[str] = set(once_store or ())
        self.persist_once = persist_once
        self.on_event: list[Callable[[AlertEvent], None]] = []
        self.lock = threading.Lock()

    def _emit(self, ev: AlertEvent, out: list[AlertEvent]) -> None:
        out.append(ev)

    def evaluate(self, findings: Iterable[Finding]) -> list[AlertEvent]:
        now = self.clock.monotonic()
        epoch = self.clock.time()
        out: list[AlertEvent] = []
        seen: set[str] = set()
        with self.lock:
            for f in findings:
                seen.add(f.key)
                level = f.level or self.levels.get(f.type, "warn")
                title = f.title or TITLES.get(f.type, f.type)
                st = self.states.get(f.key)
                if st is None:
                    st = AlertState(f.key, f.type, f.scope, level, title, f.message, dict(f.parties), dict(f.data),
                                    f.once, now, epoch, now, active=False)
                    self.states[f.key] = st
                st.level, st.title, st.message, st.data, st.once = level, title, f.message, dict(f.data), f.once
                st.last_true_mono = now
                newly = not st.active
                st.parties = dict(f.parties)
                if newly:
                    st.active = True
                    st.first_mono, st.first_epoch = now, epoch
                    st.resolved_epoch = None
                    st.resolve_pending_at = None
                    if st.once:
                        notify = f.key not in self.once_done
                        if notify:
                            self.once_done.add(f.key)
                            if self.persist_once:
                                self.persist_once(set(self.once_done))
                        st.notified_parties = set(st.parties)
                        self._emit(AlertEvent(epoch, "raised", st.type, st.key, level, title, st.message,
                                              list(st.parties.values()), notify, sorted(st.parties), st.data), out)
                        continue
                    if not st.believed_active:
                        st.believed_active = True
                        st.notified_parties = set(st.parties)
                        st.last_raise_notify_mono = now
                        self._emit(AlertEvent(epoch, "raised", st.type, st.key, level, title, st.message,
                                              list(st.parties.values()), True, sorted(st.parties), st.data), out)
                        continue
                    # 解消をまだ知らせていない間に再び成り立った：発生は知らせない
                    self._emit(AlertEvent(epoch, "raised", st.type, st.key, level, title, st.message,
                                          list(st.parties.values()), False, [], st.data), out)
                if st.once:
                    continue
                new = sorted(set(st.parties) - st.notified_parties)
                if new:
                    st.notified_parties |= set(new)
                    st.last_raise_notify_mono = now
                    self._emit(AlertEvent(epoch, "added", st.type, st.key, level, title, st.message,
                                          [st.parties[p] for p in new], True, new, st.data), out)
            for st in self.states.values():
                if st.active and st.key not in seen and now - st.last_true_mono >= self.clear_after:
                    st.active = False
                    st.resolved_epoch = epoch
                    self._emit(AlertEvent(epoch, "cleared", st.type, st.key, st.level, st.title,
                                          st.message, list(st.parties.values()), False), out)
                    if st.once or not st.believed_active:
                        continue
                    st.resolve_pending_at = max(now, (st.last_raise_notify_mono or now) + self.suppress)
                if (not st.active and st.believed_active and st.resolve_pending_at is not None
                        and now >= st.resolve_pending_at):
                    st.believed_active = False
                    st.resolve_pending_at = None
                    st.notified_parties = set()
                    self._emit(AlertEvent(epoch, "resolved", st.type, st.key, st.level, st.title,
                                          "解消：" + st.message, list(st.parties.values()), True), out)
        for ev in out:
            for f in self.on_event:
                f(ev)
        return out

    def active(self) -> list[AlertState]:
        with self.lock:
            a = [s for s in self.states.values() if s.active]
        return sorted(a, key=lambda s: (-LEVEL_RANK.get(s.level, 0), s.first_mono))

    def highest_level(self) -> str | None:
        a = self.active()
        return a[0].level if a else None
