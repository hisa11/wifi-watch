"""情報要素（IE）の解析と、主ch・帯域幅の決定。

ビーコンとプローブ応答の本文（固定部12バイトの後ろ）を読む。読むのは局の性質を表すIEだけで、
通信の中身には触れない。

主ch・幅の決め方（優先の高い順）：
  幅    EHT Operation（情報あり）→ HE Operationの6GHz情報 → VHT Operation → HT Operation → 20MHz
  主ch  6GHzはHE Operationの6GHz情報の主ch（受信した周波数ではない。重複ビーコンは主ch以外でも届く）。
        2.4GHzはDS Parameter Set、5GHzはHT Operation。無ければ受信した周波数。
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from .. import channels as ch

EID_SSID = 0
EID_RATES = 1
EID_DS = 3
EID_TIM = 5
EID_COUNTRY = 7
EID_HT_CAP = 45
EID_RSN = 48
EID_EXT_RATES = 50
EID_HT_OP = 61
EID_MBSSID = 71
EID_VHT_CAP = 191
EID_VHT_OP = 192
EID_RNR = 201
EID_VENDOR = 221
EID_EXT = 255

EXT_HE_CAP = 35
EXT_HE_OP = 36
EXT_HE_6GHZ_CAP = 59
EXT_EHT_OP = 106
EXT_ML = 107
EXT_EHT_CAP = 108

OUI_MS = "00:50:f2"
OUI_WFA = "50:6f:9a"
OUI_IEEE = "00:0f:ac"

AKM_NAMES = {
    1: "802.1X", 2: "PSK", 3: "FT-802.1X", 4: "FT-PSK", 5: "802.1X-SHA256", 6: "PSK-SHA256",
    8: "SAE", 9: "FT-SAE", 11: "802.1X-SuiteB", 12: "802.1X-SuiteB-192", 13: "FT-802.1X-SHA384",
    14: "FILS-SHA256", 15: "FILS-SHA384", 18: "OWE", 19: "FT-PSK-SHA384", 20: "PSK-SHA384",
    24: "SAE-EXT-KEY", 25: "FT-SAE-EXT-KEY",
}
CIPHER_NAMES = {
    1: "WEP-40", 2: "TKIP", 4: "CCMP-128", 5: "WEP-104", 6: "BIP-CMAC-128", 8: "GCMP-128",
    9: "GCMP-256", 10: "CCMP-256", 11: "BIP-GMAC-128", 12: "BIP-GMAC-256", 13: "BIP-CMAC-256",
}

# 6GHzの運用クラス → 帯域（RNRのchを周波数にするため）
_OPCLASS_BAND = {81: ch.BAND_24, 82: ch.BAND_24, 83: ch.BAND_24, 84: ch.BAND_24}
_OPCLASS_BAND.update({c: ch.BAND_5 for c in range(115, 131)})
_OPCLASS_BAND.update({c: ch.BAND_6 for c in range(131, 138)})

_u16 = struct.Struct("<H").unpack_from
_u16be = struct.Struct(">H").unpack_from
_u32 = struct.Struct("<I").unpack_from


@dataclass(slots=True)
class HtOp:
    primary: int
    sec_offset: int  # 0=なし 1=上 3=下
    sta_width_any: bool


@dataclass(slots=True)
class VhtOp:
    width: int  # 0=20/40 1=80/160/80+80 2=160(旧) 3=80+80(旧)
    ccfs0: int
    ccfs1: int


@dataclass(slots=True)
class SixGhzOp:
    primary: int
    width_code: int  # 0=20 1=40 2=80 3=160/80+80
    dup_beacon: bool
    ccfs0: int
    ccfs1: int
    reg_info: int


@dataclass(slots=True)
class HeOp:
    bss_color: int
    bss_color_disabled: bool
    vht: VhtOp | None
    six: SixGhzOp | None
    er_su_disable: bool


@dataclass(slots=True)
class EhtOp:
    info_present: bool
    width_code: int | None  # 0=20 1=40 2=80 3=160 4=320
    ccfs0: int | None
    ccfs1: int | None
    disabled_subchannels: int | None


@dataclass(slots=True)
class Rsn:
    group: str | None
    pairwise: list[str]
    akms: list[str]
    mfpr: bool
    mfpc: bool


@dataclass(slots=True)
class Wps:
    manufacturer: str | None = None
    model_name: str | None = None
    model_number: str | None = None
    device_name: str | None = None


@dataclass(slots=True)
class RnrEntry:
    op_class: int
    channel: int
    freq: int | None
    bssid: str | None
    short_ssid: int | None
    bss_params: int | None
    colocated: bool
    same_ssid: bool


@dataclass(slots=True)
class BssInfo:
    ssid: str | None = None
    ssid_hidden: bool = False
    ds_channel: int | None = None
    ht: HtOp | None = None
    vht: VhtOp | None = None
    he: HeOp | None = None
    eht: EhtOp | None = None
    has_ht_cap: bool = False
    has_vht_cap: bool = False
    has_he_cap: bool = False
    has_eht_cap: bool = False
    he_phy_widths: int | None = None
    eht_320: bool = False
    he_cap_fp: str | None = None
    rsn: Rsn | None = None
    wpa: bool = False
    wps: Wps | None = None
    country: str | None = None
    rnr: list[RnrEntry] = field(default_factory=list)
    vendor_ouis: list[str] = field(default_factory=list)
    ie_fp: str | None = None
    # 機種の指紋：IEの並び（出たり消えたりするものを除く）と、能力のバイト列（HT/VHT/HE/EHT・拡張能力）。
    # SSID・BSSID・ch・幅の設定には左右されにくい。同じ機種・同じファームウェアなら同じになりやすい
    model_fp: str | None = None
    has_ml: bool = False
    has_mbssid: bool = False

    @property
    def standard(self) -> str:
        if self.has_eht_cap or self.eht is not None:
            return "be"
        if self.has_he_cap or self.he is not None:
            return "ax"
        if self.has_vht_cap or self.vht is not None:
            return "ac"
        if self.has_ht_cap or self.ht is not None:
            return "n"
        return "legacy"

    @property
    def security(self) -> str:
        if self.rsn:
            names = self.rsn.akms
            if any(a.startswith("SAE") for a in names):
                return "WPA3-SAE" + ("/PSK" if any("PSK" in a for a in names) else "")
            if "OWE" in names:
                return "OWE"
            if any("PSK" in a for a in names):
                return "WPA2-PSK"
            if any("802.1X" in a for a in names):
                return "WPA2/3-Enterprise"
            return "RSN"
        if self.wpa:
            return "WPA"
        return "open"


def iter_ies(buf: bytes | memoryview, off: int, end: int):
    """(id, 拡張id or None, 中身の開始, 終わり) を返す。壊れた末尾では止まる。"""
    while off + 2 <= end:
        eid = buf[off]
        ln = buf[off + 1]
        start = off + 2
        stop = start + ln
        if stop > end:
            return
        if eid == EID_EXT and ln >= 1:
            yield eid, buf[start], start + 1, stop
        else:
            yield eid, None, start, stop
        off = stop


def _oui(b: bytes | memoryview, off: int) -> str:
    return bytes(b[off:off + 3]).hex(":")


def _suite(b: bytes | memoryview, off: int, names: dict[int, str]) -> str:
    oui = _oui(b, off)
    t = b[off + 3]
    if oui == OUI_IEEE:
        return names.get(t, f"type{t}")
    return f"{oui}/{t}"


def parse_rsn(b: bytes | memoryview, s: int, e: int) -> Rsn | None:
    try:
        if e - s < 2:
            return None
        off = s + 2  # version
        group = None
        pairwise: list[str] = []
        akms: list[str] = []
        caps = 0
        if off + 4 <= e:
            group = _suite(b, off, CIPHER_NAMES)
            off += 4
        if off + 2 <= e:
            n = _u16(b, off)[0]
            off += 2
            for _ in range(n):
                if off + 4 > e:
                    break
                pairwise.append(_suite(b, off, CIPHER_NAMES))
                off += 4
        if off + 2 <= e:
            n = _u16(b, off)[0]
            off += 2
            for _ in range(n):
                if off + 4 > e:
                    break
                akms.append(_suite(b, off, AKM_NAMES))
                off += 4
        if off + 2 <= e:
            caps = _u16(b, off)[0]
        return Rsn(group, pairwise, akms, bool(caps & 0x0040), bool(caps & 0x0080))
    except (IndexError, struct.error):
        return None


def _wps_text(b: bytes | memoryview, s: int, ln: int) -> str:
    return bytes(b[s:s + ln]).decode("utf-8", "replace").strip("\x00").strip()[:64]


def parse_wps(b: bytes | memoryview, s: int, e: int) -> Wps:
    w = Wps()
    off = s
    while off + 4 <= e:
        t = _u16be(b, off)[0]
        ln = _u16be(b, off + 2)[0]
        v = off + 4
        if v + ln > e:
            break
        if t == 0x1021:
            w.manufacturer = _wps_text(b, v, ln)
        elif t == 0x1023:
            w.model_name = _wps_text(b, v, ln)
        elif t == 0x1024:
            w.model_number = _wps_text(b, v, ln)
        elif t == 0x1011:
            w.device_name = _wps_text(b, v, ln)
        off = v + ln
    return w


def parse_he_op(b: bytes | memoryview, s: int, e: int) -> HeOp | None:
    if e - s < 6:
        return None
    params = b[s] | (b[s + 1] << 8) | (b[s + 2] << 16)
    color_info = b[s + 3]
    off = s + 6
    vht = None
    six = None
    if params & 0x004000:  # VHT Operation Information Present
        if off + 3 > e:
            return None
        vht = VhtOp(b[off], b[off + 1], b[off + 2])
        off += 3
    if params & 0x008000:  # Co-Hosted BSS
        off += 1
    if params & 0x020000:  # 6 GHz Operation Information Present
        if off + 5 > e:
            return None
        ctrl = b[off + 1]
        six = SixGhzOp(b[off], ctrl & 0x03, bool(ctrl & 0x04), b[off + 2], b[off + 3], (ctrl >> 3) & 0x07)
    return HeOp(color_info & 0x3F, bool(color_info & 0x80), vht, six, bool(params & 0x010000))


def parse_eht_op(b: bytes | memoryview, s: int, e: int) -> EhtOp | None:
    if e - s < 5:
        return None
    params = b[s]
    off = s + 5
    if params & 0x01 and off + 3 <= e:
        ctrl, c0, c1 = b[off], b[off + 1], b[off + 2]
        off += 3
        dis = None
        if params & 0x02 and off + 2 <= e:
            dis = _u16(b, off)[0]
        return EhtOp(True, ctrl & 0x07, c0, c1, dis)
    return EhtOp(False, None, None, None, None)


def parse_rnr(b: bytes | memoryview, s: int, e: int) -> list[RnrEntry]:
    out: list[RnrEntry] = []
    off = s
    while off + 4 <= e:
        hdr = b[off]
        info_len = b[off + 1]
        op_class = b[off + 2]
        chan = b[off + 3]
        count = (hdr >> 4) + 1
        hdr_coloc = bool(hdr & 0x08)
        off += 4
        band = _OPCLASS_BAND.get(op_class)
        freq = ch.number_to_freq(band, chan) if band else None
        for _ in range(count):
            if info_len == 0 or off + info_len > e:
                return out
            t = off
            bssid = None
            short_ssid = None
            params = None
            L = info_len
            if L >= 7:
                bssid = bytes(b[t + 1:t + 7]).hex(":")
                if L >= 11:
                    short_ssid = _u32(b, t + 7)[0]
                    if L >= 12:
                        params = b[t + 11]
                elif L >= 8:
                    params = b[t + 7]
            elif L >= 5:
                short_ssid = _u32(b, t + 1)[0]
                if L >= 6:
                    params = b[t + 5]
            elif L >= 2:
                params = b[t + 1]
            coloc = hdr_coloc or bool(params is not None and params & 0x40)
            same = bool(params is not None and params & 0x02)
            out.append(RnrEntry(op_class, chan, freq, bssid, short_ssid, params, coloc, same))
            off += info_len
    return out


# 機種の指紋から外すIE：TIM（プローブ応答には無い）、チャネル切替の告知、Quiet、BSS Load（付けたり外したりする機器がある）
_FP_SKIP = {EID_TIM, 37, 40, 60, 11}
# 能力として指紋に入れるIE（中身のバイト列ごと）
_FP_CAPS = {EID_HT_CAP, EID_VHT_CAP, 127}
_FP_CAPS_EXT = {EXT_HE_CAP, EXT_EHT_CAP, EXT_HE_6GHZ_CAP}


def parse_bss(buf: bytes | memoryview, off: int, end: int) -> BssInfo:
    """ビーコン・プローブ応答のIE部分（固定部の後ろ）を読む。"""
    info = BssInfo()
    toks: list[str] = []
    fp = hashlib.sha1()
    for eid, ext, s, e in iter_ies(buf, off, end):
        if eid == EID_EXT:
            tok = f"255.{ext}"
        elif eid == EID_VENDOR and e - s >= 4:
            tok = f"221.{_oui(buf, s)}.{buf[s + 3]}"
        else:
            tok = str(eid)
        toks.append(tok)
        if eid not in _FP_SKIP:
            fp.update(tok.encode() + b",")
            if eid in _FP_CAPS or (eid == EID_EXT and ext in _FP_CAPS_EXT):
                body = bytearray(buf[s:e])
                if eid == EID_HT_CAP and len(body) >= 1:
                    body[0] &= ~0x0E & 0xFF  # 40MHzの可否とSMパワーセーブは設定で変わるので除く
                fp.update(bytes(body) + b";")
        if eid == EID_SSID:
            raw = bytes(buf[s:e])
            if not raw or raw.count(0) == len(raw):
                info.ssid_hidden = True
                info.ssid = None
            else:
                info.ssid = raw.decode("utf-8", "replace")
        elif eid == EID_DS and e - s >= 1:
            info.ds_channel = buf[s]
        elif eid == EID_HT_OP and e - s >= 2:
            b1 = buf[s + 1]
            info.ht = HtOp(buf[s], b1 & 0x03, bool(b1 & 0x04))
        elif eid == EID_HT_CAP:
            info.has_ht_cap = True
        elif eid == EID_VHT_CAP:
            info.has_vht_cap = True
        elif eid == EID_VHT_OP and e - s >= 3:
            info.vht = VhtOp(buf[s], buf[s + 1], buf[s + 2])
        elif eid == EID_RSN:
            info.rsn = parse_rsn(buf, s, e)
        elif eid == EID_COUNTRY and e - s >= 2:
            info.country = bytes(buf[s:s + 2]).decode("ascii", "replace")
        elif eid == EID_RNR:
            info.rnr.extend(parse_rnr(buf, s, e))
        elif eid == EID_MBSSID:
            info.has_mbssid = True
        elif eid == EID_VENDOR and e - s >= 4:
            oui = _oui(buf, s)
            vtype = buf[s + 3]
            if oui not in info.vendor_ouis:
                info.vendor_ouis.append(oui)
            if oui == OUI_MS and vtype == 4:
                info.wps = parse_wps(buf, s + 4, e)
            elif oui == OUI_MS and vtype == 1:
                info.wpa = True
        elif eid == EID_EXT:
            if ext == EXT_HE_CAP:
                info.has_he_cap = True
                if e - s >= 17:
                    info.he_phy_widths = buf[s + 6] >> 1 & 0x7F
                info.he_cap_fp = hashlib.sha1(bytes(buf[s:min(e, s + 17)])).hexdigest()[:10]
            elif ext == EXT_HE_OP:
                info.he = parse_he_op(buf, s, e)
            elif ext == EXT_EHT_CAP:
                info.has_eht_cap = True
                if e - s >= 3:
                    info.eht_320 = bool(buf[s + 2] & 0x02)
            elif ext == EXT_EHT_OP:
                info.eht = parse_eht_op(buf, s, e)
            elif ext == EXT_ML:
                info.has_ml = True
    info.ie_fp = hashlib.sha1(",".join(toks).encode()).hexdigest()[:10]
    info.model_fp = fp.hexdigest()[:10]
    return info


# ---------------------------------------------------------------- 主chと幅


@dataclass(frozen=True, slots=True)
class Operating:
    band: str
    primary_freq: int
    width: int  # 20/40/80/160/320（80+80は160として数える）
    width_label: str  # "20" … "320"、"80+80"
    spans: tuple[ch.Span, ...]
    source: str  # どのIEで決めたか（eht/he6/vht/ht/ds/rx）
    suspect: bool = False  # IEが主chと矛盾したので20MHzとみなした

    @property
    def center_freq(self) -> float:
        return sum(s.center for s in self.spans) / len(self.spans)


def _vht_spans(band: str, w: int, c0: int, c1: int) -> tuple[int, str, tuple[ch.Span, ...]] | None:
    f0 = ch.number_to_freq(band, c0)
    if w == 1:
        if c1 == 0:
            return 80, "80", (ch.Span.around(f0, 80),)
        f1 = ch.number_to_freq(band, c1)
        if abs(c1 - c0) == 8:
            return 160, "160", (ch.Span.around(f1, 160),)
        if abs(c1 - c0) > 16:
            return 160, "80+80", (ch.Span.around(f0, 80), ch.Span.around(f1, 80))
        return 80, "80", (ch.Span.around(f0, 80),)
    if w == 2:
        return 160, "160", (ch.Span.around(f0, 160),)
    if w == 3 and c1:
        f1 = ch.number_to_freq(band, c1)
        return 160, "80+80", (ch.Span.around(f0, 80), ch.Span.around(f1, 80))
    return None


def operating(info: BssInfo, rx_freq: int) -> Operating:
    band = ch.band_of_freq(rx_freq) or ch.BAND_6
    # 主ch
    primary = None
    psrc = "rx"
    if band == ch.BAND_6 and info.he and info.he.six:
        primary = ch.number_to_freq(band, info.he.six.primary)
        psrc = "he6"
    elif band == ch.BAND_24 and info.ds_channel:
        primary = ch.number_to_freq(band, info.ds_channel)
        psrc = "ds"
    elif info.ht and info.ht.primary:
        primary = ch.number_to_freq(band, info.ht.primary)
        psrc = "ht"
    elif info.ds_channel:
        primary = ch.number_to_freq(band, info.ds_channel)
        psrc = "ds"
    if primary is None or ch.band_of_freq(primary) != band:
        primary = rx_freq
        psrc = "rx"

    res: tuple[int, str, tuple[ch.Span, ...]] | None = None
    src = psrc
    eht = info.eht
    if eht and eht.info_present and eht.width_code is not None and band != ch.BAND_24:
        wc = eht.width_code
        widths = {0: 20, 1: 40, 2: 80, 3: 160, 4: 320}
        if wc in widths:
            w = widths[wc]
            c = eht.ccfs1 if w >= 160 else eht.ccfs0
            if w == 20:
                res = (20, "20", (ch.span20(primary),))
            elif c:
                res = (w, str(w), (ch.Span.around(ch.number_to_freq(band, c), w),))
            src = "eht"
    if res is None and band == ch.BAND_6 and info.he and info.he.six:
        s6 = info.he.six
        src = "he6"
        if s6.width_code == 0:
            res = (20, "20", (ch.span20(primary),))
        elif s6.width_code in (1, 2):
            w = 40 if s6.width_code == 1 else 80
            res = (w, str(w), (ch.Span.around(ch.number_to_freq(band, s6.ccfs0), w),))
        else:
            if s6.ccfs1 and abs(s6.ccfs1 - s6.ccfs0) == 8:
                res = (160, "160", (ch.Span.around(ch.number_to_freq(band, s6.ccfs1), 160),))
            elif s6.ccfs1 and abs(s6.ccfs1 - s6.ccfs0) > 16:
                res = (160, "80+80", (ch.Span.around(ch.number_to_freq(band, s6.ccfs0), 80),
                                      ch.Span.around(ch.number_to_freq(band, s6.ccfs1), 80)))
            else:
                f0 = ch.number_to_freq(band, s6.ccfs0)
                b160 = ch.block_for(primary, 160)
                if b160 and b160.center_freq == f0:
                    res = (160, "160", (ch.Span.around(f0, 160),))
                else:
                    res = (80, "80", (ch.Span.around(f0, 80),))
    if res is None and band == ch.BAND_5:
        vht = info.vht or (info.he.vht if info.he else None)
        if vht and vht.width:
            res = _vht_spans(band, vht.width, vht.ccfs0, vht.ccfs1)
            if res:
                src = "vht"
    if res is None and info.ht and info.ht.sta_width_any and info.ht.sec_offset in (1, 3):
        center = primary + 10 if info.ht.sec_offset == 1 else primary - 10
        res = (40, "40", (ch.Span.around(center, 40),))
        src = "ht"
    if res is None:
        res = (20, "20", (ch.span20(primary),))

    width, wlabel, spans = res
    suspect = False
    if not any(s.lo <= primary - 10 and primary + 10 <= s.hi for s in spans):
        width, wlabel, spans = 20, "20", (ch.span20(primary),)
        suspect = True
    return Operating(band, primary, width, wlabel, spans, src, suspect)
