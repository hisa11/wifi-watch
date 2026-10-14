"""802.11のMACヘッダの解析。

データフレームはMACヘッダまでしか見ない（ペイロードは読まない）。管理フレームだけ本文を返す。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

TYPE_MGMT = 0
TYPE_CTRL = 1
TYPE_DATA = 2
TYPE_EXT = 3

# 管理フレーム
ST_ASSOC_REQ = 0
ST_ASSOC_RESP = 1
ST_REASSOC_REQ = 2
ST_REASSOC_RESP = 3
ST_PROBE_REQ = 4
ST_PROBE_RESP = 5
ST_BEACON = 8
ST_DISASSOC = 10
ST_AUTH = 11
ST_DEAUTH = 12
ST_ACTION = 13

# 制御フレーム（2つ目のアドレスを持つもの）
_CTRL_WITH_TA = {2, 4, 5, 8, 9, 10, 11, 14, 15}  # Trigger, BFRP, NDPA, BAR, BA, PS-Poll, RTS, CF-End, CF-End+Ack
ST_RTS = 11
ST_CTS = 12
ST_ACK = 13

FLAG_TODS = 0x01
FLAG_FROMDS = 0x02
FLAG_RETRY = 0x08
FLAG_PROTECTED = 0x40
FLAG_ORDER = 0x80

BROADCAST = b"\xff" * 6

_u16 = struct.Struct("<H").unpack_from


class Dot11Error(ValueError):
    pass


@dataclass(slots=True)
class Header:
    ftype: int
    subtype: int
    flags: int
    addr1: bytes | None
    addr2: bytes | None
    addr3: bytes | None
    addr4: bytes | None
    hdr_len: int

    @property
    def retry(self) -> bool:
        return bool(self.flags & FLAG_RETRY)

    @property
    def protected(self) -> bool:
        return bool(self.flags & FLAG_PROTECTED)

    @property
    def bssid(self) -> bytes | None:
        """BSSIDが決まる場合だけ返す（WDSと制御フレームは None）。"""
        if self.ftype == TYPE_MGMT:
            return self.addr3
        if self.ftype == TYPE_DATA:
            ds = self.flags & 0x03
            if ds == 0:
                return self.addr3
            if ds == FLAG_TODS:
                return self.addr1
            if ds == FLAG_FROMDS:
                return self.addr2
        return None

    @property
    def client(self) -> bytes | None:
        """データフレームの端末側のアドレス（APでない方）。"""
        if self.ftype != TYPE_DATA:
            return None
        ds = self.flags & 0x03
        if ds == FLAG_TODS:
            return self.addr2
        if ds == FLAG_FROMDS:
            return self.addr1
        return None

    def addresses(self) -> tuple[bytes, ...]:
        return tuple(a for a in (self.addr1, self.addr2, self.addr3, self.addr4) if a is not None)


def parse_header(buf: bytes | bytearray | memoryview, off: int, end: int) -> Header:
    if end - off < 10:
        raise Dot11Error("too short")
    fc0 = buf[off]
    flags = buf[off + 1]
    if fc0 & 0x03:
        raise Dot11Error("protocol version")
    ftype = (fc0 >> 2) & 0x03
    subtype = fc0 >> 4
    a1 = bytes(buf[off + 4:off + 10])
    if ftype == TYPE_CTRL:
        if subtype in _CTRL_WITH_TA and end - off >= 16:
            return Header(ftype, subtype, flags, a1, bytes(buf[off + 10:off + 16]), None, None, 16)
        return Header(ftype, subtype, flags, a1, None, None, None, 10)
    if end - off < 24:
        raise Dot11Error("short header")
    a2 = bytes(buf[off + 10:off + 16])
    a3 = bytes(buf[off + 16:off + 22])
    hl = 24
    a4 = None
    if ftype == TYPE_MGMT:
        if flags & FLAG_ORDER:
            hl += 4
    elif ftype == TYPE_DATA:
        if (flags & 0x03) == 0x03:
            if end - off < 30:
                raise Dot11Error("short wds header")
            a4 = bytes(buf[off + 24:off + 30])
            hl = 30
        if subtype & 0x08:  # QoS
            hl += 2
            if flags & FLAG_ORDER:
                hl += 4
    if hl > end - off:
        raise Dot11Error("header longer than frame")
    return Header(ftype, subtype, flags, a1, a2, a3, a4, hl)


def mac_str(b: bytes | None) -> str | None:
    return b.hex(":") if b is not None else None


def mac_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace(":", "").replace("-", ""))


def is_local_admin(mac: bytes | str) -> bool:
    """ローカル管理（ランダムMACの可能性が高い）か。"""
    first = mac[0] if isinstance(mac, (bytes, bytearray)) else int(mac[:2], 16)
    return bool(first & 0x02)


def is_group(mac: bytes) -> bool:
    return bool(mac[0] & 0x01)
