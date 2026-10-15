"""仕様書「検知と警報」の表の判定。1秒ごとに呼ばれ、成り立っている条件（Finding）を作る。

警報はすべて「ピットから見えたもの」で判定する。強さ（RSSI）は新規参入では参考の表示だけで、
判定に使うのは「正体不明の強力局」だけ。
"""
from __future__ import annotations

import logging
from statistics import median
from typing import Any, Callable

from .. import channels as ch
from ..classify import Classifier, Signature
from ..clock import Clock
from ..config import Config
from ..scheduler import Scheduler
from .alerts import AlertEvent, AlertManager, Finding
from .core import Analyzer
from .occupancy import is_present
from .stations import Station

log = logging.getLogger(__name__)


def party(st: Station) -> dict[str, Any]:
    return {
        "id": st.bssid, "bssid": st.bssid, "ssid": st.ssid, "hidden": st.hidden, "band": st.band,
        "freq": st.primary_freq, "ch": ch.label(st.primary_freq), "width": st.width, "width_label": st.width_label,
        "rssi": None if st.rssi_avg is None else round(st.rssi_avg), "team": st.team, "kind": st.kind,
        "model": st.model_guess,
    }


def name(st: Station) -> str:
    return st.ssid if st.ssid else "（非公開SSID）"


def _with_more(first: str, n: int) -> str:
    return first if n <= 1 else f"{first} ほか{n - 1}局"


class Detector:
    def __init__(self, cfg: Config, clock: Clock, analyzer: Analyzer, scheduler: Scheduler,
                 classifier: Classifier, alerts: AlertManager, signatures: list[Signature],
                 rx_status: Callable[[], dict[str, Any]] | None = None):
        self.cfg = cfg
        self.th = cfg.thresholds
        self.clock = clock
        self.an = analyzer
        self.sched = scheduler
        self.cls = classifier
        self.alerts = alerts
        self.sigs = {s.name: s for s in signatures}
        self.rx_status = rx_status or (lambda: {"state": "ok"})
        self.own_width = cfg.own.width
        self.own_spare = list(cfg.own.spare)
        self.newcomers: dict[str, float] = {}  # BSSID → 新規参入と判定した時刻（monotonic）
        self._candidates: dict[str, str] = {}  # BSSID → new / reappeared / moved
        self._obs_snapshot: dict[int, float] = {}
        self._prev_overlap: dict[str, bool] = {}
        self.newcomer_min_observed_s = 0.15
        analyzer.table.on_change.append(self._on_station)

    # ------------------------------------------------------------ 自分の帯域
    def own_spans(self) -> tuple[ch.Span, ...]:
        f = self.sched.own_freq
        w = self.own_width if self.own_width in ch.valid_widths(f) else 20
        if w == 20:
            return (ch.span20(f),)
        b = ch.block_for(f, w)
        return (b.span,) if b else (ch.span20(f),)

    def set_own(self, freq: int, width: int | None = None) -> None:
        if width is not None:
            self.own_width = width
        self.newcomers.clear()
        self._prev_overlap.clear()

    def own_addrs(self) -> set[str]:
        return set(self.cfg.own.bssids) | set(self.cfg.own.station_macs)

    def own_pmf_required(self) -> bool:
        for b in self.cfg.own.bssids:
            st = self.an.table.get(b)
            if st is not None and st.pmf_required is not None:
                return st.pmf_required
        if self.cfg.own.pmf_required is not None:
            return self.cfg.own.pmf_required
        return ch.BY_FREQ[self.sched.own_freq].band == ch.BAND_6  # 6GHzはPMF必須

    # ------------------------------------------------------------ 局の出入り
    def _on_station(self, st: Station, event: str) -> None:
        if event == "new":
            # 起動直後に見えた局は「新規」ではない：そのchを一度でも見たことがあってから現れた局だけ
            # （受信したch＝重複ビーコンや160MHz受信で見えたchも含めて判断する）
            seen = max(self._obs_snapshot.get(f, 0.0) for f in (st.primary_freq, *st.seen_on))
            if seen < self.newcomer_min_observed_s:
                return
        self._candidates[st.bssid] = event

    def present(self, st: Station, now: float) -> bool:
        return is_present(st, self.an, self.sched, now)

    # ------------------------------------------------------------ 判定
    def evaluate(self) -> list[AlertEvent]:
        now = self.clock.monotonic()
        table = self.an.table
        self.cls.classify_all(table)
        sts = [s for s in table.all() if self.present(s, now)]
        findings: list[Finding] = []
        findings += self._assignment(sts)
        findings += self._newcomer(sts, now)
        findings += self._unknown_strong(sts)
        findings += self._signatures(sts)
        findings += self._activity_and_retry(now)
        findings += self._ssid_clone(sts)
        findings += self._deauth(now)
        findings += self._rx_stop()
        self._obs_snapshot = dict(self.an.stats.observed_total)
        return self.alerts.evaluate(findings)

    def _assignment(self, sts: list[Station]) -> list[Finding]:
        res = self.cfg.reservations()
        own_team = self.cfg.own.team
        off: dict[str, dict[str, dict]] = {}
        enc: dict[str, dict[str, dict]] = {}
        enc_detail: dict[str, set[str]] = {}
        for st in sts:
            if st.kind != "team" or not st.team:
                continue
            mine = res.get(st.team)
            if mine and st.primary_freq not in mine:
                off.setdefault(st.team, {})[st.bssid] = party(st)
            hit = set()
            for other, chans in res.items():
                if other == st.team:
                    continue
                for f in chans:
                    if any(s.overlaps(ch.span20(f)) for s in st.spans):
                        hit.add(other)
            if hit:
                p = party(st)
                p["overlaps_teams"] = sorted(hit)
                enc.setdefault(st.team, {})[st.bssid] = p
                enc_detail.setdefault(st.team, set()).update(hit)
        out = []
        for team, ps in off.items():
            first = next(iter(ps.values()))
            allowed = ", ".join(ch.label(f) for f in sorted(res.get(team, [])))
            msg = _with_more(f"{team}：{first['ssid'] or '（非公開）'} が {first['ch']} に出ています（予約：{allowed}）", len(ps))
            out.append(Finding("offchannel", team, msg, ps))
        for team, ps in enc.items():
            first = next(iter(ps.values()))
            victims = "、".join(("自チーム" if t == own_team else t) for t in sorted(enc_detail[team]))
            msg = _with_more(f"{team}：{first['ssid'] or '（非公開）'} の{first['width_label']}MHz（主{first['ch']}）が {victims} の予約chにかかっています", len(ps))
            out.append(Finding("encroach", team, msg, ps))
        return out

    def _newcomer(self, sts: list[Station], now: float) -> list[Finding]:
        own = self.own_spans()
        table = self.an.table
        cands, self._candidates = self._candidates, {}
        for bssid, event in cands.items():
            st = table.get(bssid)
            if st is None or st.kind in ("own", "known_ok"):
                continue
            ov = st.overlaps(own)
            if not ov:
                continue
            if event == "moved" and self._prev_overlap.get(bssid, False):
                continue  # もともと重なっていた局が少し動いただけ
            self.newcomers.setdefault(bssid, now)
        present = {s.bssid: s for s in sts}
        parties = {}
        for bssid, since in list(self.newcomers.items()):
            st = present.get(bssid)
            if st is None or not st.overlaps(own) or now - since > self.th.newcomer_hold_s or st.kind in ("own", "known_ok"):
                del self.newcomers[bssid]
                continue
            parties[bssid] = party(st)
        for s in sts:
            self._prev_overlap[s.bssid] = s.overlaps(own)
        if not parties:
            return []
        first = next(iter(parties.values()))
        who = first["team"] or "所属不明"
        model = f"・{first['model']}" if first["model"] else ""
        rssi = f" {first['rssi']}dBm" if first["rssi"] is not None else ""
        msg = _with_more(f"自分の帯域に新しい局：{first['ssid'] or '（非公開）'}（{who}{model}）{first['ch']} {first['width_label']}MHz{rssi}", len(parties))
        return [Finding("newcomer", "own", msg, parties)]

    def _bands_of_interest(self) -> set[str]:
        out = {ch.BAND_6, ch.BY_FREQ[self.sched.own_freq].band}
        out |= {ch.BY_FREQ[f].band for f in self.own_spare}
        return out

    def _unknown_strong(self, sts: list[Station]) -> list[Finding]:
        out = []
        for band in sorted(self._bands_of_interest()):
            in_band = [s for s in sts if s.band == band and s.kind != "own" and s.rssi_avg is not None]
            parties = {}
            for st in in_band:
                if st.kind != "unknown":
                    continue
                reasons = []
                if st.width >= self.th.wide_width_mhz:
                    reasons.append(f"広帯域{st.width_label}MHz")
                others = [o.rssi_avg for o in in_band if o.bssid != st.bssid]
                if len(others) >= self.th.unknown_min_stations:
                    med = median(others)
                    if st.rssi_avg >= med + self.th.unknown_margin_db:
                        reasons.append(f"中央値{med:.0f}dBmより{st.rssi_avg - med:.0f}dB強い")
                elif st.rssi_avg >= self.th.unknown_abs_dbm:
                    reasons.append(f"{st.rssi_avg:.0f}dBm（{self.th.unknown_abs_dbm:.0f}dBm以上）")
                if reasons:
                    p = party(st)
                    p["reasons"] = reasons
                    parties[st.bssid] = p
            if parties:
                first = next(iter(parties.values()))
                msg = _with_more(f"所属不明の局：{first['ssid'] or '（非公開）'} {first['ch']}（{'、'.join(first['reasons'])}）", len(parties))
                out.append(Finding("unknown_strong", band, msg, parties))
        return out

    def _signatures(self, sts: list[Station]) -> list[Finding]:
        out = []
        by_sig: dict[str, dict[str, dict]] = {}
        for st in sts:
            if st.band != ch.BAND_6 or not st.signature:
                continue
            sig = self.sigs.get(st.signature)
            if sig is None or sig.on_6ghz == "none":
                continue
            by_sig.setdefault(sig.name, {})[st.bssid] = party(st)
        for name_, ps in by_sig.items():
            sig = self.sigs[name_]
            first = next(iter(ps.values()))
            msg = _with_more(f"{name_}が6GHzに出現：{first['ssid'] or '（非公開）'} {first['ch']} {first['width_label']}MHz", len(ps))
            if sig.on_6ghz == "crit":
                out.append(Finding("sig6_crit", name_, msg, ps, title=f"{name_}の6GHz出現"))
            else:
                out.append(Finding("sig6_once", name_, msg, ps, once=True, title=f"{name_}の6GHz出現"))
        return out

    def own_key(self) -> tuple[int, int]:
        if self.sched.mode == "PRE":
            return (self.sched.own_freq, 20)
        return (self.sched.own_freq, self.sched.block.width)

    def _activity_and_retry(self, now: float) -> list[Finding]:
        out = []
        key = self.own_key()
        st = self.an.stats
        cur = st.totals(key, now, 10)
        if cur.receive_s >= 2.0:
            rate = cur.frames / cur.receive_s
            base = st.baseline(key, now, self.th.activity_baseline_s, self.th.activity_min_baseline_buckets)
            if base is not None and rate >= self.th.activity_min_rate and rate >= self.th.activity_ratio * base:
                ratio = rate / base if base > 0 else float("inf")
                rtxt = f"{ratio:.1f}倍" if base > 0 else "静かな状態から"
                msg = f"自ch（{ch.label(key[0])}）の活動量が急増：{rate:.0f}フレーム/秒（基準 {base:.0f}/秒、{rtxt}）"
                out.append(Finding("activity", str(key[0]), msg, {},
                                   data={"rate": round(rate, 1), "baseline": round(base, 1), "width": key[1]}))
        win = st.totals(key, now, self.th.retry_window_s)
        if win.retry_eligible >= self.th.retry_min_frames:
            r = win.retries / win.retry_eligible
            if r >= self.th.retry_rate:
                msg = f"自ch（{ch.label(key[0])}）のリトライ率 {r * 100:.0f}%（{win.retry_eligible}フレーム中）"
                out.append(Finding("retry", str(key[0]), msg, {}, data={"rate": round(r, 3), "frames": win.retry_eligible}))
        return out

    def _ssid_clone(self, sts: list[Station]) -> list[Finding]:
        own_b = set(self.cfg.own.bssids)
        if not own_b or not self.cfg.own.ssids:
            return []
        known_ok = set(self.cfg.known_ok_bssids)
        out = []
        for ssid in self.cfg.own.ssids:
            ps = {s.bssid: party(s) for s in sts if s.ssid == ssid and s.bssid not in own_b and s.bssid not in known_ok}
            if ps:
                first = next(iter(ps.values()))
                msg = _with_more(f"自分と同じSSID「{ssid}」の別AP：{first['bssid']} {first['ch']}", len(ps))
                out.append(Finding("ssid_clone", ssid, msg, ps))
        return out

    def _deauth(self, now: float) -> list[Finding]:
        mine = self.own_addrs()
        if not mine:
            return []
        evs = [d for d in self.an.deauths_since(now - self.th.deauth_window_s)
               if d.src in mine or d.dst in mine or d.bssid in mine]
        if len(evs) < self.th.deauth_count:
            return []
        pmf = self.own_pmf_required()
        srcs: dict[str, dict] = {}
        for d in evs:
            srcs.setdefault(d.src, {"id": d.src, "bssid": d.src, "ssid": None, "ch": ch.label(d.rx_freq),
                                    "count": 0, "protected": 0})
            srcs[d.src]["count"] += 1
            srcs[d.src]["protected"] += int(d.protected)
        level = "warn" if pmf else "crit"
        note = "自APはPMF必須（偽のdeauthは効かない見込み）" if pmf else "自APはPMF必須ではない（切断されうる）"
        msg = f"自分宛てのdeauth/disassocが{self.th.deauth_window_s:.0f}秒で{len(evs)}個（{note}）"
        return [Finding("deauth", "own", msg, srcs, level=self.cfg.thresholds.levels.get("deauth", level),
                        data={"count": len(evs), "pmf_required": pmf})]

    def _rx_stop(self) -> list[Finding]:
        s = self.rx_status()
        if s.get("state") != "failed":
            return []
        return [Finding("rx_stop", "radio", f"受信停止：{s.get('reason', '2.4/5GHzでフレームを受信できません')}", {},
                        data=s)]
