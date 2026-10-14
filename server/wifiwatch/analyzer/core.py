"""受信したフレームの束を読んで、局の表・端末の表・chごとの統計・deauthの記録に振り分ける。

- radiotapのchが受信の設定と違うフレーム（切替の前後に紛れたもの）は捨てる。
- FCSが壊れたフレームは数えるだけで、中身は使わない。
- データフレームはMACヘッダだけを見る。管理フレームはビーコン・プローブ応答のIEと、deauth/disassocの宛先と理由だけ。
- 自分のアドレス（自APのBSSID、自端末のMAC）を含むフレームは、活動量とリトライ率から除く。
"""
from __future__ import annotations

import struct
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from ..capture import dot11, ies, radiotap
from ..capture.dot11 import TYPE_CTRL, TYPE_DATA, TYPE_MGMT
from ..clock import Clock
from ..radio.base import FrameBatch
from .stations import StationTable
from .stats import ChannelStats

_MGMT_BSS = (dot11.ST_BEACON, dot11.ST_PROBE_RESP)
_MGMT_KICK = (dot11.ST_DEAUTH, dot11.ST_DISASSOC)
_u16 = struct.Struct("<H").unpack_from


@dataclass(slots=True)
class DeauthEvent:
    mono: float
    epoch: float
    disassoc: bool
    src: str
    dst: str
    bssid: str
    protected: bool
    reason: int | None
    rx_freq: int


@dataclass
class BatchResult:
    frames: int
    accepted: int
    receive_s: float
    beacons: dict[str, int | None]  # この束でビーコン・プローブ応答を受けた局 → 最も強かった値


class Analyzer:
    def __init__(self, clock: Clock, own_addrs: Iterable[str] = (), parse_interval: float = 5.0):
        self.clock = clock
        self.table = StationTable(parse_interval)
        self.stats = ChannelStats()
        self.deauths: deque[DeauthEvent] = deque(maxlen=20000)
        self.own: set[bytes] = set()
        self.set_own_addrs(own_addrs)
        self.counters = {"frames": 0, "radiotap_errors": 0, "header_errors": 0, "wrong_freq": 0, "bad_fcs": 0}
        self.lock = threading.Lock()

    def set_own_addrs(self, addrs: Iterable[str]) -> None:
        self.own = {dot11.mac_bytes(a) for a in addrs}

    def process_batch(self, batch: FrameBatch) -> BatchResult:
        tun = batch.tuning
        key = (tun.freq, tun.width)
        b = self.stats.add_receive(key, batch.receive_s, batch.t_end)
        now = self.clock.time()
        mono = batch.t_end
        accepted = 0
        seen: dict[str, int | None] = {}
        own = self.own
        table = self.table
        stats = self.stats
        c = self.counters
        for raw in batch.frames:
            c["frames"] += 1
            try:
                rx = radiotap.parse(raw)
            except radiotap.RadiotapError:
                c["radiotap_errors"] += 1
                continue
            if rx.freq is not None and rx.freq != tun.freq:
                c["wrong_freq"] += 1
                continue
            if rx.bad_fcs:
                b.bad_fcs += 1
                c["bad_fcs"] += 1
                continue
            # データ・制御フレームはカーネルでヘッダまでに切り詰められている（radio/bpf.py）。
            # FCSを差し引くのは本文を読む管理フレームだけ。
            end = len(raw) - 4 if rx.flags & radiotap.FLAG_FCS_AT_END else len(raw)
            try:
                h = dot11.parse_header(raw, rx.length, len(raw))
            except dot11.Dot11Error:
                c["header_errors"] += 1
                continue
            accepted += 1
            is_own = (h.addr1 in own) or (h.addr2 in own) or (h.addr3 in own) or (h.addr4 is not None and h.addr4 in own)
            ft = h.ftype
            if is_own:
                b.own_frames += 1
            else:
                b.frames += 1
                if ft == TYPE_DATA:
                    b.data += 1
                elif ft == TYPE_MGMT:
                    b.mgmt += 1
                elif ft == TYPE_CTRL:
                    b.ctrl += 1
                if rx.ppdu_bw:
                    b.ppdu_bw[rx.ppdu_bw] = b.ppdu_bw.get(rx.ppdu_bw, 0) + 1
                if ft == TYPE_DATA or (ft == TYPE_MGMT and h.subtype != dot11.ST_BEACON):
                    b.retry_eligible += 1
                    if h.flags & dot11.FLAG_RETRY:
                        b.retries += 1
            if ft == TYPE_MGMT:
                st_type = h.subtype
                if st_type in _MGMT_BSS and not h.flags & dot11.FLAG_PROTECTED:
                    if h.addr3 is None:
                        continue
                    bssid = h.addr3.hex(":")
                    body = rx.length + h.hdr_len + 12
                    info = None
                    if table.needs_parse(bssid, mono) and body <= end:
                        info = ies.parse_bss(raw, body, end)
                    st = table.update_beacon(bssid, tun.freq, rx.signal, info, now, mono)
                    if st is not None:
                        prev = seen.get(bssid)
                        if bssid not in seen or (rx.signal is not None and (prev is None or rx.signal > prev)):
                            seen[bssid] = rx.signal
                        stats.count_beacon_primary(st.primary_freq)
                        if st_type == dot11.ST_BEACON:
                            b.beacons += 1
                elif st_type in _MGMT_KICK:
                    prot = bool(h.flags & dot11.FLAG_PROTECTED)
                    pos = rx.length + h.hdr_len
                    reason = _u16(raw, pos)[0] if not prot and pos + 2 <= end else None
                    self.deauths.append(DeauthEvent(
                        mono, now, st_type == dot11.ST_DISASSOC, h.addr2.hex(":") if h.addr2 else "",
                        h.addr1.hex(":") if h.addr1 else "", h.addr3.hex(":") if h.addr3 else "",
                        prot, reason, tun.freq))
            elif ft == TYPE_DATA:
                bss = h.bssid
                cl = h.client
                table.note_data(bss.hex(":") if bss else None, cl.hex(":") if cl else None, rx.signal, now,
                                bool(cl[0] & 0x02) if cl else False)
        return BatchResult(len(batch.frames), accepted, batch.receive_s, seen)

    def deauths_since(self, mono: float) -> list[DeauthEvent]:
        return [d for d in list(self.deauths) if d.mono >= mono]
