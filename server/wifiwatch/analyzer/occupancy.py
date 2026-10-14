"""chごとの占有のまとめ（画面のアナライザーのグラフ、channels.csv、回避先の提案、問題chの強調）。

各chについて「見ているか」（自ch・巡回中・巡回外・手動スキャン中・受信停止中）と、最後に受信した時刻を
必ず添える。局が無いchと見ていないchを区別するため。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .. import channels as ch
from ..config import Config
from ..scheduler import Scheduler
from .core import Analyzer
from .stations import Station


def present_stations(an: Analyzer, sched: Scheduler, now: float) -> list[Station]:
    return [s for s in an.table.all() if now - s.last_seen_mono <= sched.presence_window(s.primary_freq)]


def _mw(dbm: float) -> float:
    return 10 ** (dbm / 10)


@dataclass
class OccupancyContext:
    cfg: Config
    an: Analyzer
    sched: Scheduler
    rx_state: str = "ok"


def channel_rows(ctx: OccupancyContext, now: float, sts: list[Station] | None = None) -> list[dict[str, Any]]:
    cfg, an, sched = ctx.cfg, ctx.an, ctx.sched
    th = cfg.thresholds
    sts = sts if sts is not None else present_stations(an, sched, now)
    states = sched.channel_states()
    res = cfg.reservations()
    reserved_by: dict[int, list[str]] = {}
    for team, fs in res.items():
        for f in fs:
            reserved_by.setdefault(f, []).append(team)
    own_band = set(ch.subchannels(ch.Span.around(*_own_center_width(cfg, sched))))
    rows = []
    for c in ch.ALL_CHANNELS:
        f = c.freq
        span = ch.span20(f)
        ov = [s for s in sts if s.overlaps((span,))]
        rssis = [s.rssi_avg for s in ov if s.rssi_avg is not None]
        strong = sum(1 for r in rssis if r >= th.strong_rssi_dbm)
        st = states[f]
        state = "stopped" if ctx.rx_state == "failed" else st["state"]
        last = an.stats.last_observed.get(f)
        # 活動量：20MHzで受けた値（巡回）と、自chなら自ブロックで受けた値
        rate = None
        rt = an.stats.totals((f, 20), now, 120)
        if f == sched.own_freq and sched.mode != "PRE":
            rt = an.stats.totals((f, sched.block.width), now, 30)
        if rt.receive_s > 0:
            rate = round(rt.frames / rt.receive_s, 1)
        rows.append({
            "freq": f, "band": c.band, "ch": c.number, "psc": c.is_psc, "state": state,
            "revisit_s": st["revisit_s"],
            "observed_ago": None if last is None else round(now - last, 1),
            "n_overlap": len(ov), "n_primary": sum(1 for s in sts if s.primary_freq == f),
            "n_strong": strong, "max_rssi": round(max(rssis)) if rssis else None,
            "rate": rate,
            "reserved": sorted(reserved_by.get(f, [])),
            "own": f == sched.own_freq, "own_band": f in own_band,
            "spare": f in cfg.own.spare, "in_block": f in sched.block.freqs,
            "problem": strong >= th.problem_channel_min_strong,
        })
    return rows


def _own_center_width(cfg: Config, sched: Scheduler) -> tuple[float, float]:
    f = sched.own_freq
    w = cfg.own.width if cfg.own.width in ch.valid_widths(f) else 20
    if w == 20:
        return float(f), 20.0
    b = ch.block_for(f, w)
    return (float(b.center_freq), float(w)) if b else (float(f), 20.0)


def suggestions(ctx: OccupancyContext, now: float, sts: list[Station] | None = None, own_width: int | None = None,
                top: int = 3) -> list[dict[str, Any]]:
    """回避先の候補：自分の帯域幅で、占有の少ないものを上位3つ。ピットから見た参考値。

    点数＝候補に重なる局の強さの和（mW）＋活動量の目安。他チームの予約にかかる候補は後ろに回す。
    しばらく見ていない候補は「未確認」として後ろに回す。
    """
    cfg, an, sched = ctx.cfg, ctx.an, ctx.sched
    sts = sts if sts is not None else present_stations(an, sched, now)
    band = ch.BY_FREQ[sched.own_freq].band
    w = own_width or cfg.own.width
    if w not in ch.valid_widths(sched.own_freq):
        w = 20
    res = cfg.reservations()
    own_team = cfg.own.team
    out = []
    for b in ch.blocks(band, w):
        span = b.span
        freqs = b.freqs
        if not freqs:
            continue
        ov = [s for s in sts if s.kind != "own" and s.overlaps((span,))]
        power = sum(_mw(s.rssi_avg) for s in ov if s.rssi_avg is not None)
        rate = 0.0
        stale = False
        for f in freqs:
            last = an.stats.last_observed.get(f)
            if last is None or now - last > 300:
                stale = True
            t = an.stats.totals((f, 20), now, 300)
            if t.receive_s > 0:
                rate += t.frames / t.receive_s
        others = sorted({team for team, fs in res.items() if team != own_team and any(f in fs for f in freqs)})
        score_db = 10 * math.log10(power) if power > 0 else -120.0
        out.append({
            "label": b.label, "center": b.center_freq, "width": w, "freqs": freqs,
            "primary": freqs[0] if w > 20 else freqs[0],
            "n_overlap": len(ov), "power_dbm": round(score_db, 1), "rate": round(rate, 1),
            "reserved_by": others, "own_reserved": any(f in res.get(own_team, set()) for f in freqs),
            "unverified": stale, "current": sched.own_freq in freqs,
        })
    out.sort(key=lambda c: (bool(c["reserved_by"]), c["unverified"], c["power_dbm"] + 0.05 * c["rate"]))
    picks = [c for c in out if not c["current"]][:top]
    for c in picks:
        why = [f"重なる局{c['n_overlap']}" + (f"（合計{c['power_dbm']:.0f}dBm）" if c["n_overlap"] else "")]
        if c["rate"]:
            why.append(f"活動量 約{c['rate']:.0f}/秒")
        if c["reserved_by"]:
            why.append("予約：" + "、".join(c["reserved_by"]))
        if c["own_reserved"]:
            why.append("自チームの予約内")
        if c["unverified"]:
            why.append("5分以上見ていない")
        c["why"] = "・".join(why)
    return picks
