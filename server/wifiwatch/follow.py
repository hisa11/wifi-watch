"""自APのchに自chを合わせる（ユーザーの希望、2026-10-15：設定なしで自動で追いかける）。

自APがchを移った（ロボットの再起動でルータがchを選び直した、など）ときに、自ch（自ブロックの受信・警報の基準）を
自APの新しい主ch・幅に合わせる。決め方：
  - 自チームと分類された局のうち、いる局で、いまの自chと同じ帯域のものを見る（2.4/5/6GHzの別の無線は見ない）
  - どれかが いまの自ch・幅 にいれば何もしない
  - 新しいchで「2回聞こえた」（最初に聞こえてから STABLE_S 秒以上あとにもう一度）局だけを根拠にする
  - 根拠の局がみな同じch・幅なら合わせる。別々のchに分かれていれば合わせず、選んでもらう（ambiguous）
  - 決める前に、巡回に入っているその帯域のchをひと回り見直す（ほかの自APを見落としたまま決めない。LIVEで約30秒）
  - 手動スキャン中と、前に変えてから COOLDOWN_S 秒は変えない
  - 画面で自chを手で変えたあとは、そのときの自APの場所を覚えておき、自APがそこから動いたときだけ合わせる
    （手で決めた自chを、すぐ自動で戻さない）
自APの見分け方（MACアドレスかSSID）は classify.py。いるかどうかは analyzer/occupancy.py。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import channels as ch
from .analyzer.core import Analyzer
from .analyzer.occupancy import is_present
from .analyzer.stations import Station
from .scheduler import Scheduler

STABLE_S = 5.0
COOLDOWN_S = 30.0
ROTATION_S = 120.0  # この秒以内に見たchを「巡回に入っている」とみなす


@dataclass
class FollowDecision:
    action: str  # "follow" / "ambiguous"
    freq: int | None = None
    width: int | None = None
    stations: list[dict[str, Any]] = field(default_factory=list)
    choices: list[dict[str, Any]] = field(default_factory=list)


def _since(st: Station) -> float:
    """いまの主ch・幅で聞こえ始めた時刻（UTC）。"""
    return st.moved_at if st.moved_at is not None and st.moved_at > st.first_seen else st.first_seen


def _width_for(st: Station) -> int:
    return st.width if st.width in ch.valid_widths(st.primary_freq) else 20


def _brief(st: Station) -> dict[str, Any]:
    return {"bssid": st.bssid, "ssid": st.ssid, "freq": st.primary_freq, "label": ch.label(st.primary_freq),
            "width": _width_for(st), "rssi": None if st.rssi_avg is None else round(st.rssi_avg)}


class OwnFollower:
    def __init__(self) -> None:
        self.last_change_mono = -1e18
        self.hold: dict[str, tuple[int, int]] = {}  # BSSID → 手で変えたときの（主ch, 幅）
        self._key: Any = None  # 待っている決定
        self._since = 0.0

    @staticmethod
    def _round_done(an: Analyzer, band: str, since: float) -> bool:
        """巡回に入っているその帯域のchを、since のあとにひと回り見たか。"""
        for f in ch.FREQS_BY_BAND[band]:
            last = an.stats.last_observed.get(f)
            if last is None or last < since - ROTATION_S:
                continue  # 巡回に入っていないch（巡回外、QUIETの自ブロック外、受けられないch）
            if last <= since:
                return False
        return True

    def _settled(self, key: Any, an: Analyzer, band: str, now_mono: float) -> bool:
        if key != self._key:
            self._key, self._since = key, now_mono
            return False
        return self._round_done(an, band, self._since)

    def hold_current(self, an: Analyzer) -> None:
        """手で自chを変えたとき：いまの自APの場所を覚える（そこにいる間は合わせない）。"""
        self.hold = {st.bssid: (st.primary_freq, _width_for(st)) for st in an.table.all() if st.kind == "own"}

    def check(self, an: Analyzer, sched: Scheduler, own_width: int, now_mono: float) -> FollowDecision | None:
        d = self._decide(an, sched, own_width, now_mono)
        if d is None:
            self._key = None
            return None
        key = (d.action, d.freq, d.width, [(c["freq"], c["width"]) for c in d.choices])
        if not self._settled(key, an, ch.BY_FREQ[sched.own_freq].band, now_mono):
            return None
        if d.action == "follow" and now_mono - self.last_change_mono < COOLDOWN_S:
            return None
        return d

    def _decide(self, an: Analyzer, sched: Scheduler, own_width: int, now_mono: float) -> FollowDecision | None:
        if sched.manual_running:
            return None
        band = ch.BY_FREQ[sched.own_freq].band
        cands = [st for st in an.table.all()
                 if st.kind == "own" and st.band == band and is_present(st, an, sched, now_mono)]
        for st in cands:  # 手で変えたあと動いた自APは、覚えた場所を忘れる
            if st.bssid in self.hold and self.hold[st.bssid] != (st.primary_freq, _width_for(st)):
                del self.hold[st.bssid]
        cands = [st for st in cands if st.bssid not in self.hold]
        if not cands:
            return None  # 自APが見えない（電源が切れている等）ときは動かさない
        if any(st.primary_freq == sched.own_freq and _width_for(st) == own_width for st in cands):
            return None
        stable = [st for st in cands if st.last_seen - _since(st) >= STABLE_S]
        if len(stable) != len(cands):
            return None  # まだ1回しか聞こえていない自APがある：その場所が分かるまで待つ
        groups: dict[tuple[int, int], list[Station]] = {}
        for st in stable:
            groups.setdefault((st.primary_freq, _width_for(st)), []).append(st)
        if len(groups) > 1:
            choices = [{"freq": f, "width": w, "label": ch.label(f), "stations": [_brief(s) for s in sts]}
                       for (f, w), sts in sorted(groups.items())]
            return FollowDecision("ambiguous", choices=choices)
        (f, w), sts = next(iter(groups.items()))
        sts.sort(key=lambda s: -(s.rssi_avg if s.rssi_avg is not None else -200))
        return FollowDecision("follow", f, w, [_brief(s) for s in sts])
