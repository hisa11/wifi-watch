"""合成フレームの生成器（テストと偽の無線用）。

radiotap＋802.11の管理・制御・データフレームと、主なIEを組み立てる。実際の通信は作らない。
データフレームの本文は中身のない詰め物で、解析側は読まない。
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .. import channels as ch
from . import dot11 as d

# ---------------------------------------------------------------- radiotap


class _RtPacker:
    def __init__(self, n_words: int):
        self.base = 4 + 4 * n_words
        self.data = bytearray()

    def put(self, align: int, raw: bytes) -> None:
        pos = self.base + len(self.data)
        pad = (-pos) % align
        self.data += b"\x00" * pad + raw


def radiotap(
    freq: int,
    signal: int | None = None,
    chains: list[int] | None = None,
    flags: int = 0,
    tsft: int | None = None,
    he_bw: int | None = None,
    vht_bw: int | None = None,
    vendor_ns: bool = False,
) -> bytes:
    """mac80211と同じ並び：最初の名前空間に合成の強さ、2つ目以降にアンテナごとの強さ。"""
    chains = list(chains or [])
    words: list[int] = []
    w0 = (1 << 1) | (1 << 3) | (1 << 14)
    if tsft is not None:
        w0 |= 1 << 0
    if signal is not None:
        w0 |= 1 << 5
    if vht_bw is not None:
        w0 |= 1 << 21
    if he_bw is not None:
        w0 |= 1 << 23
    if vendor_ns:
        w0 |= (1 << 30) | (1 << 31)
    elif chains:
        w0 |= (1 << 29) | (1 << 31)
    words.append(w0)
    if vendor_ns:
        w_v = 0x1 | (1 << 29) | ((1 << 31) if chains else 0)
        words.append(w_v)
    for i, _ in enumerate(chains):
        w = (1 << 5) | (1 << 11)
        if i < len(chains) - 1:
            w |= (1 << 29) | (1 << 31)
        words.append(w)

    p = _RtPacker(len(words))
    if tsft is not None:
        p.put(8, struct.pack("<Q", tsft))
    p.put(1, bytes([flags]))
    chflags = 0x0100 if freq > 5000 else 0x0080  # 5GHz / 2GHz
    p.put(2, struct.pack("<HH", freq, chflags))
    if signal is not None:
        p.put(1, struct.pack("<b", signal))
    p.put(2, struct.pack("<H", 0))  # RX flags
    if vht_bw is not None:
        code = {20: 0, 40: 1, 80: 4, 160: 11}[vht_bw]
        p.put(2, struct.pack("<HBB4sBBH", 0x0040, 0, code, b"\x00" * 4, 0, 0, 0))
    if he_bw is not None:
        code = {20: 0, 40: 1, 80: 2, 160: 3}[he_bw]
        p.put(2, struct.pack("<6H", 0x4000, 0, 0, 0, code, 0))
    if vendor_ns:
        vendor_data = b"\xAA\xBB\xCC"  # 中身は読まない
        p.put(2, b"\x00\x11\x22" + bytes([0]) + struct.pack("<H", len(vendor_data)))
        p.data += vendor_data
    for i, s in enumerate(chains):
        p.put(1, struct.pack("<b", s))
        p.put(1, bytes([i]))
    length = 4 + 4 * len(words) + len(p.data)
    return struct.pack("<BBH", 0, 0, length) + b"".join(struct.pack("<I", w) for w in words) + bytes(p.data)


# ---------------------------------------------------------------- 802.11


def _mac(m: str | bytes) -> bytes:
    return m if isinstance(m, bytes) else d.mac_bytes(m)


def _fc(ftype: int, subtype: int, flags: int = 0) -> bytes:
    return bytes([(subtype << 4) | (ftype << 2), flags])


def _seq(n: int) -> bytes:
    return struct.pack("<H", (n & 0xFFF) << 4)


def mgmt(subtype: int, a1, a2, a3, body: bytes, seq: int = 0, flags: int = 0) -> bytes:
    return _fc(d.TYPE_MGMT, subtype, flags) + b"\x00\x00" + _mac(a1) + _mac(a2) + _mac(a3) + _seq(seq) + body


def beacon(bssid: str, ies: bytes, seq: int = 0, interval_tu: int = 100, capability: int = 0x0011,
           tsf: int = 0, probe_resp_to: str | None = None) -> bytes:
    body = struct.pack("<QHH", tsf, interval_tu, capability) + ies
    if probe_resp_to:
        return mgmt(d.ST_PROBE_RESP, probe_resp_to, bssid, bssid, body, seq)
    return mgmt(d.ST_BEACON, "ff:ff:ff:ff:ff:ff", bssid, bssid, body, seq)


def deauth(dst: str, src: str, bssid: str, reason: int = 7, disassoc: bool = False,
           protected: bool = False, seq: int = 0) -> bytes:
    st = d.ST_DISASSOC if disassoc else d.ST_DEAUTH
    if protected:
        # PMFでは本文が暗号化される。中身は作らず、それらしい長さの詰め物にする
        return mgmt(st, dst, src, bssid, b"\x00" * 26, seq, flags=d.FLAG_PROTECTED)
    return mgmt(st, dst, src, bssid, struct.pack("<H", reason), seq)


def data(bssid: str, sta: str, to_ap: bool, retry: bool = False, qos: bool = True,
         payload_len: int = 64, seq: int = 0, protected: bool = True) -> bytes:
    flags = (d.FLAG_TODS if to_ap else d.FLAG_FROMDS) | (d.FLAG_RETRY if retry else 0)
    if protected:
        flags |= d.FLAG_PROTECTED
    a1, a2 = (bssid, sta) if to_ap else (sta, bssid)
    subtype = 8 if qos else 0
    hdr = _fc(d.TYPE_DATA, subtype, flags) + b"\x00\x00" + _mac(a1) + _mac(a2) + _mac(bssid) + _seq(seq)
    if qos:
        hdr += b"\x00\x00"
    return hdr + b"\xEE" * payload_len  # 詰め物（読まれない）


def ack(ra: str) -> bytes:
    return _fc(d.TYPE_CTRL, d.ST_ACK) + b"\x00\x00" + _mac(ra)


def rts(ra: str, ta: str) -> bytes:
    return _fc(d.TYPE_CTRL, d.ST_RTS) + b"\x00\x00" + _mac(ra) + _mac(ta)


def with_fcs(frame: bytes, bad: bool = False) -> bytes:
    crc = zlib.crc32(frame) & 0xFFFFFFFF
    if bad:
        crc ^= 0xDEADBEEF
    return frame + struct.pack("<I", crc)


def packet(rt: bytes, frame: bytes) -> bytes:
    return rt + frame


# ---------------------------------------------------------------- IE


def ie(eid: int, body: bytes) -> bytes:
    if len(body) > 255:
        raise ValueError("IEが長すぎます")
    return bytes([eid, len(body)]) + body


def ie_ext(ext: int, body: bytes) -> bytes:
    return ie(255, bytes([ext]) + body)


def ssid_ie(ssid: str | bytes) -> bytes:
    return ie(0, ssid.encode() if isinstance(ssid, str) else ssid)


def rates_ie(band: str) -> bytes:
    return ie(1, bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24]) if band == ch.BAND_24
              else bytes([0x8C, 0x12, 0x98, 0x24, 0xB0, 0x48, 0x60, 0x6C]))


def ds_ie(number: int) -> bytes:
    return ie(3, bytes([number]))


def tim_ie() -> bytes:
    return ie(5, bytes([0, 1, 0, 0]))


def country_ie(code: str = "JP") -> bytes:
    return ie(7, code.encode()[:2] + b"\x04" + bytes([1, 13, 20]))


def ht_cap_ie() -> bytes:
    return ie(45, b"\xEF\x09\x1B" + b"\xFF\xFF" + b"\x00" * 21)


def ht_op_ie(primary: int, sec_offset: int = 0, sta_width_any: bool = False) -> bytes:
    b1 = (sec_offset & 0x03) | (0x04 if sta_width_any else 0)
    return ie(61, bytes([primary, b1]) + b"\x00" * 20)


def vht_cap_ie(supports_160: bool = True) -> bytes:
    info = 0x0F8259B2 | (0x4 if supports_160 else 0)
    return ie(191, struct.pack("<I", info) + b"\xFA\xFF\x00\x00\xFA\xFF\x00\x20")


def vht_op_ie(width: int, ccfs0: int, ccfs1: int = 0) -> bytes:
    return ie(192, bytes([width, ccfs0, ccfs1]) + b"\xFC\xFF")


def he_cap_ie(width_set: int = 0x0C) -> bytes:
    mac = b"\x05\x00\x18\x12\x00\x10"
    phy = bytes([width_set]) + b"\x20\x20\x02\x00\x00\x00\x00\x00\x00\x00"
    return ie_ext(35, mac + phy + b"\xFA\xFF\xFA\xFF")


def he_op_ie(bss_color: int = 1, six: tuple[int, int, int, int, bool] | None = None,
             vht: tuple[int, int, int] | None = None, cohosted: bool = False) -> bytes:
    params = 0x0004  # Default PE Duration=4
    if vht:
        params |= 0x004000
    if cohosted:
        params |= 0x008000
    if six:
        params |= 0x020000
    body = bytes([params & 0xFF, (params >> 8) & 0xFF, (params >> 16) & 0xFF, bss_color & 0x3F]) + b"\xFC\xFF"
    if vht:
        body += bytes(vht)
    if cohosted:
        body += b"\x00"
    if six:
        primary, width_code, c0, c1, dup = six
        ctrl = (width_code & 0x03) | (0x04 if dup else 0)
        body += bytes([primary, ctrl, c0, c1, 0x06])
    return ie_ext(36, body)


def he_6ghz_cap_ie() -> bytes:
    return ie_ext(59, b"\xBD\x06")


def eht_cap_ie(supports_320: bool = True) -> bytes:
    mac = b"\x00\x00"
    # PHY能力の1バイト目のbit1が「6GHzで320MHz」
    phy = bytes([0x02 if supports_320 else 0x00]) + b"\x00" * 8
    return ie_ext(108, mac + phy + b"\x11\x11\x11")


def eht_op_ie(width_code: int | None = None, ccfs0: int = 0, ccfs1: int = 0) -> bytes:
    params = 0x01 if width_code is not None else 0x00
    body = bytes([params]) + b"\x11\x11\x11\x11"
    if width_code is not None:
        body += bytes([width_code & 0x07, ccfs0, ccfs1])
    return ie_ext(106, body)


def rsn_ie(akms: list[int] | None = None, pairwise: list[int] | None = None, group: int = 4,
           mfpr: bool = True, mfpc: bool = True) -> bytes:
    akms = akms or [8]
    pairwise = pairwise or [4]
    oui = b"\x00\x0F\xAC"
    body = struct.pack("<H", 1) + oui + bytes([group])
    body += struct.pack("<H", len(pairwise)) + b"".join(oui + bytes([p]) for p in pairwise)
    body += struct.pack("<H", len(akms)) + b"".join(oui + bytes([a]) for a in akms)
    caps = (0x0040 if mfpr else 0) | (0x0080 if mfpc else 0)
    body += struct.pack("<H", caps)
    return ie(48, body)


def wps_ie(manufacturer: str | None = None, model_name: str | None = None,
           model_number: str | None = None, device_name: str | None = None) -> bytes:
    def tlv(t: int, v: bytes) -> bytes:
        return struct.pack(">HH", t, len(v)) + v

    body = tlv(0x104A, b"\x10") + tlv(0x1044, b"\x02")
    for t, v in ((0x1021, manufacturer), (0x1023, model_name), (0x1024, model_number), (0x1011, device_name)):
        if v is not None:
            body += tlv(t, v.encode())
    return ie(221, b"\x00\x50\xF2\x04" + body)


def vendor_ie(oui: str, vtype: int, payload: bytes = b"") -> bytes:
    return ie(221, d.mac_bytes(oui + ":00:00:00")[:3] + bytes([vtype]) + payload)


@dataclass
class RnrAp:
    bssid: str | None = None
    short_ssid: int | None = None
    params: int | None = None
    psd: int | None = None


def rnr_ie(groups: list[tuple[int, int, list[RnrAp]]], colocated_hdr: bool = False) -> bytes:
    """groups: (運用クラス, ch番号, [RnrAp, …])。同じグループの局は同じ長さにする。"""
    body = b""
    for op_class, number, aps in groups:
        first = aps[0]
        if first.bssid and first.short_ssid is not None:
            ln = 13 if first.psd is not None else (12 if first.params is not None else 11)
        elif first.bssid:
            ln = 9 if first.psd is not None else (8 if first.params is not None else 7)
        elif first.short_ssid is not None:
            ln = 6 if first.params is not None else 5
        else:
            ln = 2 if first.params is not None else 1
        hdr = ((len(aps) - 1) << 4) | (0x08 if colocated_hdr else 0)
        body += bytes([hdr, ln, op_class, number])
        for ap in aps:
            t = b"\xFF"
            if ap.bssid:
                t += d.mac_bytes(ap.bssid)
            if ap.short_ssid is not None:
                t += struct.pack("<I", ap.short_ssid)
            if ap.params is not None:
                t += bytes([ap.params])
            if ap.psd is not None:
                t += struct.pack("<b", ap.psd)
            if len(t) != ln:
                raise ValueError("RNRの同じグループの局は同じ形にしてください")
            body += t
    return ie(201, body)


def short_ssid(ssid: str) -> int:
    return zlib.crc32(ssid.encode()) & 0xFFFFFFFF


# ---------------------------------------------------------------- 局のIEをまとめて作る


def _num(freq: float) -> int:
    n = ch.freq_to_number(int(freq))
    assert n is not None
    return n


def ap_ies(
    freq: int,
    width: int,
    ssid: str,
    *,
    standard: str = "auto",
    dup_beacon: bool = False,
    bss_color: int = 1,
    akms: list[int] | None = None,
    mfpr: bool | None = None,
    wps: dict[str, str] | None = None,
    rnr: bytes | None = None,
    vendor: list[bytes] | None = None,
    hidden: bool = False,
    eht_width_override: tuple[int, int, int] | None = None,
) -> bytes:
    """帯域・主ch・幅から、それらしいビーコンのIE一式を作る。

    standard: "auto"（6GHzはax、幅320はbe）/ "n" / "ac" / "ax" / "be"
    """
    band = ch.band_of_freq(freq)
    if band is None:
        raise ValueError(freq)
    primary = _num(freq)
    if standard == "auto":
        standard = "be" if width == 320 else ("ax" if band == ch.BAND_6 else "ac" if band == ch.BAND_5 else "n")
    if band == ch.BAND_6 and standard in ("n", "ac"):
        raise ValueError("6GHzはax以上")
    if akms is None:
        akms = [8] if band == ch.BAND_6 else [2]
    if mfpr is None:
        mfpr = band == ch.BAND_6

    out = ssid_ie(b"" if hidden else ssid) + rates_ie(band)
    if band == ch.BAND_24:
        out += ds_ie(primary)
    out += tim_ie() + country_ie("JP")
    out += rsn_ie(akms=akms, mfpr=mfpr, mfpc=True)

    if band != ch.BAND_6 and standard in ("n", "ac", "ax", "be"):
        out += ht_cap_ie()
        sec = 0
        if width >= 40:
            if band == ch.BAND_24:
                sec = 1 if primary <= 7 else 3
            else:
                b40 = ch.block_for(freq, 40)
                assert b40 is not None
                sec = 1 if freq < b40.center_freq else 3
        out += ht_op_ie(primary, sec, width >= 40)
    if band == ch.BAND_5 and standard in ("ac", "ax", "be"):
        out += vht_cap_ie(width >= 160)
        if width >= 80:
            b80 = ch.block_for(freq, 80)
            assert b80 is not None
            c0 = _num(b80.center_freq)
            c1 = 0
            if width >= 160:
                b160 = ch.block_for(freq, 160)
                assert b160 is not None
                c1 = _num(b160.center_freq)
            out += vht_op_ie(1, c0, c1)
        else:
            out += vht_op_ie(0, 0, 0)
    if standard in ("ax", "be"):
        out += he_cap_ie()
        six = None
        if band == ch.BAND_6:
            hw = min(width, 160)
            if hw == 20:
                six = (primary, 0, primary, 0, dup_beacon)
            elif hw in (40, 80):
                blk = ch.block_for(freq, hw)
                assert blk is not None
                six = (primary, 1 if hw == 40 else 2, _num(blk.center_freq), 0, dup_beacon)
            else:
                b80 = ch.block_for(freq, 80)
                b160 = ch.block_for(freq, 160)
                assert b80 and b160
                six = (primary, 3, _num(b80.center_freq), _num(b160.center_freq), dup_beacon)
        out += he_op_ie(bss_color, six=six)
        if band == ch.BAND_6:
            out += he_6ghz_cap_ie()
    if standard == "be":
        out += eht_cap_ie(supports_320=band == ch.BAND_6)
        if eht_width_override:
            out += eht_op_ie(*eht_width_override)
        elif width == 320:
            b160 = ch.block_for(freq, 160)
            b320 = ch.block_for(freq, 320)
            assert b160 and b320
            out += eht_op_ie(4, _num(b160.center_freq), _num(b320.center_freq))
        else:
            out += eht_op_ie(None)
    if rnr:
        out += rnr
    if wps:
        out += wps_ie(**wps)
    for v in vendor or []:
        out += v
    return out
