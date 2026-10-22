"""局（AP）の表と端末の表。

- 局はBSSIDで1件。6GHzの主chはHE Operationのもの（重複ビーコンで別のchで受けても1件のまま）。
- 受信したchは別に数える（seen_on）。
- IEの解析は重いので、同じ局は parse_interval 秒に1回だけ全部読み、それ以外は強さと時刻だけ更新する。
- 端末はデータフレームのMACヘッダだけから作る（ペイロードは読まない）。
- 局の強さ（rssi_avg）は、直近 RSSI_WINDOW 回の読み取りの中央値。1つだけ外れた値が来ても大きく動かない
  （前は 0.8・0.2 の移動平均で、本番PCで 2.4GHz の局が -72dBm から -11dBm に引っぱられた）。
"""
from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .. import channels as ch
from ..capture import ies

RSSI_WINDOW = 9


@dataclass
class Station:
    bssid: str
    band: str
    primary_freq: int
    width: int = 20
    width_label: str = "20"
    spans: tuple[ch.Span, ...] = ()
    op_source: str = "rx"
    suspect: bool = False
    ssid: str | None = None
    hidden: bool = False
    dup_beacon: bool = False
    standard: str = "legacy"
    security: str = "open"
    akms: list[str] = field(default_factory=list)
    pmf_required: bool | None = None
    country: str | None = None
    bss_color: int | None = None
    wps: dict[str, str] | None = None
    vendor_ouis: list[str] = field(default_factory=list)
    ie_fp: str | None = None
    he_cap_fp: str | None = None
    model_fp: str | None = None
    eht_320_cap: bool = False
    rnr: list[dict[str, Any]] = field(default_factory=list)
    first_seen: float = 0.0  # UTC epoch
    last_seen: float = 0.0
    last_seen_mono: float = 0.0
    rssi: int | None = None
    rssi_avg: float | None = None
    rssi_max: int | None = None
    rssi_recent: deque[int] = field(default_factory=lambda: deque(maxlen=RSSI_WINDOW), repr=False)
    beacons: int = 0
    data_frames: int = 0
    seen_on: dict[int, int] = field(default_factory=dict)
    last_parse_mono: float = -1e18
    visits_at_seen: int = 0  # 最後に聞こえたときの、主chを見に行った回数（ChannelStats.visits）
    # chの移動
    prev_primary_freq: int | None = None
    moved_at: float | None = None
    # 分類（classifyが埋める）
    team: str | None = None
    team_source: str | None = None  # config / tag / ssid
    signature: str | None = None
    kind: str | None = None  # own / team / official_infra / broadcast / unknown / known_ok
    negotiable: bool | None = None
    model_guess: str | None = None
    hints: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)  # RNRで同じ機器と分かった局
    siblings: list[str] = field(default_factory=list)  # BSSIDの並びで同じ機器と分かった局（非公開SSIDの双子など）

    @property
    def freq_label(self) -> str:
        return ch.label(self.primary_freq)

    def overlaps(self, spans: tuple[ch.Span, ...] | list[ch.Span]) -> bool:
        return ch.spans_overlap(self.spans, spans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bssid": self.bssid,
            "ssid": self.ssid,
            "hidden": self.hidden,
            "band": self.band,
            "freq": self.primary_freq,
            "ch": ch.freq_to_number(self.primary_freq),
            "width": self.width,
            "width_label": self.width_label,
            "spans": [[s.lo, s.hi] for s in self.spans],
            "op_source": self.op_source,
            "suspect": self.suspect,
            "dup_beacon": self.dup_beacon,
            "standard": self.standard,
            "security": self.security,
            "pmf_required": self.pmf_required,
            "country": self.country,
            "bss_color": self.bss_color,
            "wps": self.wps,
            "rssi": self.rssi,
            "rssi_avg": None if self.rssi_avg is None else round(self.rssi_avg, 1),
            "rssi_max": self.rssi_max,
            "rssi_recent": list(self.rssi_recent),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "beacons": self.beacons,
            "data_frames": self.data_frames,
            "seen_on": sorted(self.seen_on),
            "team": self.team,
            "team_source": self.team_source,
            "signature": self.signature,
            "kind": self.kind,
            "negotiable": self.negotiable,
            "model_guess": self.model_guess,
            "hints": list(self.hints),
            "linked": list(self.linked),
            "siblings": list(self.siblings),
            "model_fp": self.model_fp,
            "rnr": list(self.rnr),
            "moved_from": self.prev_primary_freq,
            "moved_at": self.moved_at,
        }


@dataclass
class Client:
    mac: str
    bssid: str | None
    first_seen: float
    last_seen: float
    rssi: int | None
    frames: int = 0
    random_mac: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mac": self.mac, "bssid": self.bssid, "first_seen": self.first_seen, "last_seen": self.last_seen,
            "rssi": self.rssi, "frames": self.frames, "random_mac": self.random_mac,
        }


class StationTable:
    def __init__(self, parse_interval: float = 5.0, max_clients: int = 5000, reappear_after: float = 600.0):
        self.parse_interval = parse_interval
        self.reappear_after = reappear_after  # これより長く見ていなかった局が戻ったら「再び現れた」
        self.max_clients = max_clients
        self.stations: dict[str, Station] = {}
        self.clients: dict[str, Client] = {}
        self.lock = threading.RLock()
        self.on_change: list[Any] = []  # f(station, event) event: new / moved / reappeared

    def needs_parse(self, bssid: str, now_mono: float) -> bool:
        st = self.stations.get(bssid)
        return st is None or now_mono - st.last_parse_mono >= self.parse_interval

    def update_beacon(self, bssid: str, rx_freq: int, rssi: int | None, info: ies.BssInfo | None,
                      now: float, now_mono: float) -> Station | None:
        """ビーコン・プローブ応答1つぶん。info が None のときは強さと時刻だけ。"""
        with self.lock:
            st = self.stations.get(bssid)
            if st is None:
                if info is None:
                    return None
                op = ies.operating(info, rx_freq)
                st = Station(bssid, op.band, op.primary_freq, first_seen=now)
                self.stations[bssid] = st
                self._apply_info(st, info, op)
                st.last_parse_mono = now_mono
                event = "new"
            else:
                event = "updated"
                if now - st.last_seen > self.reappear_after:
                    event = "reappeared"
                if info is not None:
                    op = ies.operating(info, rx_freq)
                    if op.primary_freq != st.primary_freq or op.spans != st.spans:
                        st.prev_primary_freq = st.primary_freq
                        st.moved_at = now
                        if event == "updated":
                            event = "moved"
                    self._apply_info(st, info, op)
                    st.last_parse_mono = now_mono
            st.last_seen = now
            st.last_seen_mono = now_mono
            st.beacons += 1
            st.seen_on[rx_freq] = st.seen_on.get(rx_freq, 0) + 1
            if rssi is not None:
                st.rssi = rssi
                st.rssi_recent.append(rssi)
                st.rssi_avg = float(statistics.median(st.rssi_recent))
                st.rssi_max = rssi if st.rssi_max is None else max(st.rssi_max, rssi)
        if event != "updated":
            for f in self.on_change:
                f(st, event)
        return st

    @staticmethod
    def _apply_info(st: Station, info: ies.BssInfo, op: ies.Operating) -> None:
        st.band = op.band
        st.primary_freq = op.primary_freq
        st.width = op.width
        st.width_label = op.width_label
        st.spans = op.spans
        st.op_source = op.source
        st.suspect = op.suspect
        if info.ssid is not None or info.ssid_hidden:
            st.ssid = info.ssid
            st.hidden = info.ssid_hidden
        st.dup_beacon = bool(info.he and info.he.six and info.he.six.dup_beacon)
        st.standard = info.standard
        st.security = info.security
        st.akms = list(info.rsn.akms) if info.rsn else []
        st.pmf_required = info.rsn.mfpr if info.rsn else None
        st.country = info.country
        st.bss_color = info.he.bss_color if info.he else None
        if info.wps:
            st.wps = {k: v for k, v in (("manufacturer", info.wps.manufacturer), ("model_name", info.wps.model_name),
                                        ("model_number", info.wps.model_number), ("device_name", info.wps.device_name)) if v}
        st.vendor_ouis = list(info.vendor_ouis)
        st.ie_fp = info.ie_fp
        st.model_fp = info.model_fp
        st.he_cap_fp = info.he_cap_fp
        st.eht_320_cap = info.eht_320
        st.rnr = [{"freq": r.freq, "bssid": r.bssid, "colocated": r.colocated, "same_ssid": r.same_ssid,
                   "op_class": r.op_class, "channel": r.channel} for r in info.rnr]

    def note_data(self, bssid: str | None, client: str | None, rssi: int | None, now: float,
                  random_mac: bool) -> None:
        with self.lock:
            if bssid is not None:
                st = self.stations.get(bssid)
                if st is not None:
                    st.data_frames += 1
            if client is None:
                return
            c = self.clients.get(client)
            if c is None:
                if len(self.clients) >= self.max_clients:
                    oldest = min(self.clients.values(), key=lambda x: x.last_seen)
                    del self.clients[oldest.mac]
                c = Client(client, bssid, now, now, rssi, random_mac=random_mac)
                self.clients[client] = c
            c.last_seen = now
            c.frames += 1
            if bssid:
                c.bssid = bssid
            if rssi is not None:
                c.rssi = rssi

    def get(self, bssid: str) -> Station | None:
        with self.lock:
            return self.stations.get(bssid)

    def all(self) -> list[Station]:
        with self.lock:
            return list(self.stations.values())

    def prune(self, now: float, max_age: float) -> list[str]:
        """長く見ていない局を表から消す（既定は大会1日ぶん以上）。"""
        with self.lock:
            gone = [b for b, s in self.stations.items() if now - s.last_seen > max_age]
            for b in gone:
                del self.stations[b]
            for m in [m for m, c in self.clients.items() if now - c.last_seen > max_age]:
                del self.clients[m]
            return gone
