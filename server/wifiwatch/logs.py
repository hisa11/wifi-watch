"""ファイルのログ：events.jsonl（警報と解消、操作のイベント）と channels.csv（10秒ごとの帯域別の占有）。

通信の中身（pcap）は保存しない。時刻はUTC（ISO 8601）。
"""
from __future__ import annotations

import csv
import json
import os
import threading
from pathlib import Path
from typing import Any

from .clock import utc_iso

CHANNEL_FIELDS = ["ts", "freq", "band", "ch", "state", "observed_ago_s", "n_overlap", "n_primary", "n_strong",
                  "max_rssi", "frames_per_s", "reserved", "problem"]


class FileLogs:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.channels_path = self.dir / "channels.csv"
        self.lock = threading.Lock()

    def event(self, ts: float, record: dict[str, Any]) -> None:
        line = json.dumps({"ts": utc_iso(ts), **record}, ensure_ascii=False)
        with self.lock, open(self.events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def channels(self, ts: float, rows: list[dict[str, Any]]) -> None:
        new = not self.channels_path.exists() or os.path.getsize(self.channels_path) == 0
        with self.lock, open(self.channels_path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CHANNEL_FIELDS)
            iso = utc_iso(ts)
            for r in rows:
                w.writerow([iso, r["freq"], r["band"], r["ch"], r["state"], _n(r["observed_ago"]), r["n_overlap"],
                            r["n_primary"], r["n_strong"], _n(r["max_rssi"]), _n(r["rate"]),
                            "|".join(r["reserved"]), int(r["problem"])])


def _n(v: Any) -> str:
    return "" if v is None else str(v)
