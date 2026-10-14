"""PREモードの下見のまとめ（仕様書「約5分でしきい値を較正し、全帯域を一度は見る」）。

しきい値を自動で書き換えることはしない。観測した分布と「今のしきい値だと何局・どのchが当たるか」、
それに目安の値を出し、人がconfigを直す判断に使う。相対的なしきい値（活動量の基準＝過去10分の中央値、
強さ＝6GHz内の中央値との差）は、もともと観測から自動で決まる。
"""
from __future__ import annotations

from statistics import median, quantiles
from typing import Any

from . import channels as ch
from .analyzer.core import Analyzer
from .config import ThresholdConfig
from .scheduler import Scheduler


def _pct(vals: list[float], p: int) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    q = quantiles(sorted(vals), n=100, method="inclusive")
    return round(q[min(98, max(0, p - 1))], 1)


def calibrate(analyzer: Analyzer, scheduler: Scheduler, th: ThresholdConfig, now_mono: float,
              window_s: float = 600.0) -> dict[str, Any]:
    sts = [s for s in analyzer.table.all() if now_mono - s.last_seen_mono <= window_s and s.rssi_avg is not None]
    bands: dict[str, Any] = {}
    for b in ch.BANDS:
        rs = [s.rssi_avg for s in sts if s.band == b]
        bands[b] = {
            "stations": len(rs),
            "rssi_p50": _pct(rs, 50), "rssi_p90": _pct(rs, 90), "rssi_max": round(max(rs), 1) if rs else None,
            "strong": sum(1 for r in rs if r >= th.strong_rssi_dbm),
            "wide": sum(1 for s in sts if s.band == b and s.width >= th.wide_width_mhz),
        }
    six = [s.rssi_avg for s in sts if s.band == ch.BAND_6]
    rates = {}
    for f in ch.FREQS_BY_BAND[ch.BAND_6]:
        t = analyzer.stats.totals((f, 20), now_mono, window_s)
        if t.receive_s > 0:
            rates[f] = round(t.frames / t.receive_s, 1)
    problem = []
    for f in ch.ALL_FREQS:
        span = ch.span20(f)
        n = sum(1 for s in sts if s.rssi_avg >= th.strong_rssi_dbm and s.overlaps((span,)))
        if n >= th.problem_channel_min_strong:
            problem.append({"freq": f, "ch": ch.label(f), "strong": n})
    own_rate = rates.get(scheduler.own_freq)
    hints = []
    if len(six) >= th.unknown_min_stations:
        hints.append(f"6GHzの局の強さの中央値は {median(six):.0f}dBm。中央値＋{th.unknown_margin_db:.0f}dB"
                     f"（{median(six) + th.unknown_margin_db:.0f}dBm）以上の所属不明の局で警報が出ます。")
    else:
        hints.append(f"6GHzの局が{len(six)}局（5未満）なので、所属不明の局は {th.unknown_abs_dbm:.0f}dBm 以上で警報が出ます。")
    if own_rate is not None:
        hints.append(f"自chの活動量は約{own_rate:.0f}フレーム/秒（20MHzで受けた値）。急増の警報は基準の"
                     f"{th.activity_ratio:.0f}倍かつ{th.activity_min_rate:.0f}/秒以上で出ます。")
    if problem:
        hints.append(f"強い局（{th.strong_rssi_dbm:.0f}dBm以上）が{th.problem_channel_min_strong}つ以上重なるch："
                     + "、".join(p["ch"] for p in problem))
    covered = sorted({f for s in sts for f in s.seen_on})
    return {
        "bands": bands,
        "six_rates": {str(k): v for k, v in rates.items()},
        "problem_channels": problem,
        "own_rate": own_rate,
        "rounds": scheduler.pre_rounds,
        "channels_seen_with_stations": len(covered),
        "hints": hints,
        "thresholds": {"strong_rssi_dbm": th.strong_rssi_dbm, "unknown_abs_dbm": th.unknown_abs_dbm,
                       "unknown_margin_db": th.unknown_margin_db, "activity_ratio": th.activity_ratio,
                       "activity_min_rate": th.activity_min_rate},
    }
