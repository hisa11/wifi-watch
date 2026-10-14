"""手動スキャンの結果を集めて「記録」にする。

記録に入れるのはビーコンとヘッダから分かることだけ（通信の中身は入れない）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import channels as ch
from .analyzer.stations import StationTable
from .scheduler import ManualScan


@dataclass
class _ChanAcc:
    receive_s: float = 0.0
    frames: int = 0
    accepted: int = 0
    beacons: dict[str, int | None] = field(default_factory=dict)


class ScanCollector:
    def __init__(self) -> None:
        self._acc: dict[str, dict[int, _ChanAcc]] = {}

    def add(self, scan_id: str, freq: int, receive_s: float, frames: int, accepted: int,
            beacons: dict[str, int | None]) -> None:
        acc = self._acc.setdefault(scan_id, {}).setdefault(freq, _ChanAcc())
        acc.receive_s += receive_s
        acc.frames += frames
        acc.accepted += accepted
        for b, r in beacons.items():
            prev = acc.beacons.get(b)
            if b not in acc.beacons or (r is not None and (prev is None or r > prev)):
                acc.beacons[b] = r

    def finish(self, scan: ManualScan, table: StationTable, device: str, mode_before: str,
               classify: Any = None) -> dict[str, Any]:
        per = self._acc.pop(scan.id, {})
        best: dict[str, int | None] = {}
        for acc in per.values():
            for b, r in acc.beacons.items():
                if b not in best or (r is not None and (best[b] is None or r > best[b])):  # type: ignore[operator]
                    best[b] = r
        stations = []
        for bssid, rssi in best.items():
            st = table.get(bssid)
            if st is None:
                continue
            if classify is not None:
                classify(st)
            stations.append({
                "bssid": st.bssid, "ssid": st.ssid, "hidden": st.hidden, "band": st.band,
                "freq": st.primary_freq, "ch": ch.freq_to_number(st.primary_freq),
                "width": st.width, "width_label": st.width_label,
                "spans": [[s.lo, s.hi] for s in st.spans], "rssi": rssi, "standard": st.standard,
                "security": st.security, "model_guess": st.model_guess, "team": st.team, "kind": st.kind,
                "signature": st.signature, "dup_beacon": st.dup_beacon,
            })
        stations.sort(key=lambda s: (s["freq"], -(s["rssi"] if s["rssi"] is not None else -999)))
        chans = []
        for f in scan.freqs:
            acc = per.get(f, _ChanAcc())
            span = ch.span20(f)
            overl = [s for s in stations if any(ch.Span(a, b).overlaps(span) for a, b in s["spans"])]
            rssis = [s["rssi"] for s in overl if s["rssi"] is not None]
            chans.append({
                "freq": f, "band": ch.BY_FREQ[f].band, "ch": ch.BY_FREQ[f].number,
                "receive_s": round(acc.receive_s, 3), "frames": acc.accepted,
                "rate": round(acc.accepted / acc.receive_s, 1) if acc.receive_s > 0 else None,
                "bss_primary": sum(1 for s in stations if s["freq"] == f),
                "bss_overlap": len(overl), "max_rssi": max(rssis) if rssis else None,
            })
        bands = sorted({ch.BY_FREQ[f].band for f in scan.freqs})
        return {
            "id": f"scan-{scan.id}",
            "kind": "pc_scan",
            "created_at": scan.finished_at or scan.requested_at,
            "device": device,
            "origin": scan.origin,
            "request_id": scan.request_id,
            "settings": {"freqs": list(scan.freqs), "slot_s": scan.slot_s, "width": 20, "mode_before": mode_before,
                         "requested_at": scan.requested_at},
            "bands": bands,
            "stations": stations,
            "channels": chans,
            "label": {"location": None, "tags": [], "memo": "", "updated_at": None, "updated_by": None},
        }
