"""nl80211（generic netlink）でスキャン結果を読む最小の実装。IEを生のまま受け取る。

合否ゲートが不合格（6GHzでモニタモードにできない）ときの代わり（管理モードのパッシブスキャン）で使う。
読むだけ（NL80211_CMD_GET_SCAN のダンプ）。スキャンの開始は `iw dev <if> scan` に任せる。
"""
from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass

NETLINK_GENERIC = 16
GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2
NLM_F_REQUEST = 0x01
NLM_F_DUMP = 0x300
NLMSG_ERROR = 2
NLMSG_DONE = 3
NL80211_CMD_GET_SCAN = 32
NL80211_ATTR_IFINDEX = 3
NL80211_ATTR_BSS = 47
NL80211_BSS_BSSID = 1
NL80211_BSS_FREQUENCY = 2
NL80211_BSS_INFORMATION_ELEMENTS = 6
NL80211_BSS_SIGNAL_MBM = 7
NL80211_BSS_SEEN_MS_AGO = 10
NL80211_BSS_BEACON_IES = 11
NLA_F_NESTED = 0x8000

_NLH = struct.Struct("=IHHII")
_GENL = struct.Struct("=BBH")
_NLA = struct.Struct("=HH")


class NetlinkError(Exception):
    pass


@dataclass
class ScanBss:
    bssid: str
    freq: int
    signal_dbm: float | None
    ies: bytes
    seen_ms_ago: int | None


def _align(n: int) -> int:
    return (n + 3) & ~3


def attrs(data: bytes, off: int = 0, end: int | None = None) -> dict[int, bytes]:
    end = len(data) if end is None else end
    out: dict[int, bytes] = {}
    while off + 4 <= end:
        ln, typ = _NLA.unpack_from(data, off)
        if ln < 4 or off + ln > end:
            break
        out[typ & ~NLA_F_NESTED & 0x3FFF] = data[off + 4:off + ln]
        off += _align(ln)
    return out


def nla(typ: int, payload: bytes) -> bytes:
    ln = 4 + len(payload)
    return _NLA.pack(ln, typ) + payload + b"\x00" * (_align(ln) - ln)


def msg(typ: int, flags: int, seq: int, cmd: int, payload: bytes, version: int = 1) -> bytes:
    body = _GENL.pack(cmd, version, 0) + payload
    return _NLH.pack(_NLH.size + len(body), typ, flags, seq, 0) + body


def parse_messages(data: bytes) -> list[tuple[int, bytes]]:
    """(種類, genlの中身＝コマンド4バイトの後ろ) の並び。エラーは例外。"""
    out = []
    off = 0
    while off + _NLH.size <= len(data):
        ln, typ, _flags, _seq, _pid = _NLH.unpack_from(data, off)
        if ln < _NLH.size or off + ln > len(data):
            break
        body = data[off + _NLH.size:off + ln]
        if typ == NLMSG_ERROR:
            (err,) = struct.unpack_from("=i", body, 0)
            if err != 0:
                raise NetlinkError(f"netlink error {err}")
        elif typ == NLMSG_DONE:
            out.append((typ, b""))
        else:
            out.append((typ, body[_GENL.size:]))
        off += _align(ln)
    return out


def bss_from_attrs(a: dict[int, bytes]) -> ScanBss | None:
    bss = a.get(NL80211_ATTR_BSS)
    if bss is None:
        return None
    b = attrs(bss)
    if NL80211_BSS_BSSID not in b or NL80211_BSS_FREQUENCY not in b:
        return None
    bssid = b[NL80211_BSS_BSSID][:6].hex(":")
    (freq,) = struct.unpack("=I", b[NL80211_BSS_FREQUENCY][:4])
    sig = None
    if NL80211_BSS_SIGNAL_MBM in b:
        (mbm,) = struct.unpack("=i", b[NL80211_BSS_SIGNAL_MBM][:4])
        sig = mbm / 100.0
    seen = None
    if NL80211_BSS_SEEN_MS_AGO in b:
        (seen,) = struct.unpack("=I", b[NL80211_BSS_SEEN_MS_AGO][:4])
    ies = b.get(NL80211_BSS_INFORMATION_ELEMENTS) or b.get(NL80211_BSS_BEACON_IES) or b""
    return ScanBss(bssid, freq, sig, ies, seen)


class Nl80211:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
        self.sock.bind((0, 0))
        self.seq = 1
        self.family = self._family("nl80211")

    def _request(self, data: bytes) -> list[tuple[int, bytes]]:
        self.sock.send(data)
        out: list[tuple[int, bytes]] = []
        while True:
            chunk = self.sock.recv(1 << 20)
            msgs = parse_messages(chunk)
            for t, body in msgs:
                if t == NLMSG_DONE:
                    return out
                out.append((t, body))
            if not msgs or not (struct.unpack_from("=H", chunk, 6)[0] & 0x2):  # NLM_F_MULTI でなければ1回で終わり
                return out

    def _family(self, name: str) -> int:
        self.seq += 1
        req = msg(GENL_ID_CTRL, NLM_F_REQUEST, self.seq, CTRL_CMD_GETFAMILY,
                  nla(CTRL_ATTR_FAMILY_NAME, name.encode() + b"\x00"))
        for _t, body in self._request(req):
            a = attrs(body)
            if CTRL_ATTR_FAMILY_ID in a:
                return struct.unpack("=H", a[CTRL_ATTR_FAMILY_ID][:2])[0]
        raise NetlinkError("nl80211 が見つかりません")

    def scan_dump(self, ifname: str) -> list[ScanBss]:
        idx = socket.if_nametoindex(ifname)
        self.seq += 1
        req = msg(self.family, NLM_F_REQUEST | NLM_F_DUMP, self.seq, NL80211_CMD_GET_SCAN,
                  nla(NL80211_ATTR_IFINDEX, struct.pack("=I", idx)))
        out = []
        for _t, body in self._request(req):
            b = bss_from_attrs(attrs(body))
            if b is not None:
                out.append(b)
        return out

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def available() -> bool:
    return hasattr(socket, "AF_NETLINK") and os.name == "posix"
