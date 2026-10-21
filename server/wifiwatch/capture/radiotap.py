"""radiotapヘッダの解析（自前の軽い実装）。

- presentの拡張（bit31）、radiotap名前空間の付け直し（bit29）、ベンダ名前空間（bit30）を扱う。
- 各フィールドはヘッダ先頭からの自然な整列（align）に合わせて読む。
- 強さ（RSSI）は2本を合成した値を使う。アンテナごとの値（2つ目以降の名前空間）があれば
  電力の和で合成し、無ければドライバが最初の名前空間に入れた値を使う。
  0dBm以上・-127dBm以下はありえない値（受けられないほど強い・値なし）なので、強さ不明にする
  （driver_signal・chains には書かれたままの値を残す。調べる道具で見るため）。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

# bit → (align, size)。Linuxの radiotap iterator と同じ表。
_FIELDS: dict[int, tuple[int, int]] = {
    0: (8, 8),    # TSFT
    1: (1, 1),    # Flags
    2: (1, 1),    # Rate
    3: (2, 4),    # Channel
    4: (2, 2),    # FHSS
    5: (1, 1),    # dBm antenna signal
    6: (1, 1),    # dBm antenna noise
    7: (2, 2),    # Lock quality
    8: (2, 2),    # TX attenuation
    9: (2, 2),    # dB TX attenuation
    10: (1, 1),   # dBm TX power
    11: (1, 1),   # Antenna
    12: (1, 1),   # dB antenna signal
    13: (1, 1),   # dB antenna noise
    14: (2, 2),   # RX flags
    15: (2, 2),   # TX flags
    16: (1, 1),   # RTS retries
    17: (1, 1),   # data retries
    18: (4, 8),   # XChannel
    19: (1, 3),   # MCS
    20: (4, 8),   # A-MPDU status
    21: (2, 12),  # VHT
    22: (8, 12),  # timestamp
    23: (2, 12),  # HE
    24: (2, 12),  # HE-MU
    25: (2, 6),   # HE-MU-other-user
    26: (1, 1),   # 0-length PSDU
    27: (2, 4),   # L-SIG
}
BIT_TLV = 28
BIT_RADIOTAP_NS = 29
BIT_VENDOR_NS = 30
BIT_EXT = 31

FLAG_FCS_AT_END = 0x10
FLAG_BAD_FCS = 0x40

_VHT_BW = {0: 20, 1: 40, 2: 40, 3: 40}
_VHT_BW.update({i: 80 for i in range(4, 11)})
_VHT_BW.update({i: 160 for i in range(11, 26)})
_HE_BW = {0: 20, 1: 40, 2: 80, 3: 160}

_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_u64 = struct.Struct("<Q").unpack_from
_s8 = struct.Struct("<b").unpack_from


class RadiotapError(ValueError):
    pass


@dataclass(slots=True)
class Rx:
    length: int
    freq: int | None = None
    signal: int | None = None
    driver_signal: int | None = None
    chains: tuple[tuple[int, int], ...] = ()
    flags: int = 0
    ppdu_bw: int | None = None
    tsft: int | None = None

    @property
    def fcs_at_end(self) -> bool:
        return bool(self.flags & FLAG_FCS_AT_END)

    @property
    def bad_fcs(self) -> bool:
        return bool(self.flags & FLAG_BAD_FCS)


def combine_dbm(values: list[int]) -> int | None:
    vals = [v for v in values if -127 < v < 0]
    if not vals:
        return None
    return round(10 * math.log10(sum(10 ** (v / 10) for v in vals)))


def parse(buf: bytes | bytearray | memoryview) -> Rx:
    n = len(buf)
    if n < 8:
        raise RadiotapError("too short")
    if buf[0] != 0:
        raise RadiotapError("version")
    length = _u16(buf, 2)[0]
    if length < 8 or length > n:
        raise RadiotapError("length")

    words: list[int] = []
    off = 4
    while True:
        if off + 4 > length:
            raise RadiotapError("present words")
        w = _u32(buf, off)[0]
        off += 4
        words.append(w)
        if not w & 0x80000000:
            break

    rx = Rx(length)
    arg = off
    in_vendor = False
    vendor_end: int | None = None
    bit_base = 0
    ns_index = 0
    ns_signal: int | None = None
    ns_antenna: int | None = None
    chains: list[tuple[int, int]] = []
    done = False

    def close_ns() -> None:
        nonlocal ns_signal, ns_antenna
        if ns_signal is not None:
            if ns_index == 0 and ns_antenna is None:
                rx.driver_signal = ns_signal
            else:
                chains.append((ns_antenna if ns_antenna is not None else len(chains), ns_signal))
        ns_signal = None
        ns_antenna = None

    for w in words:
        if done:
            break
        if in_vendor:
            if vendor_end is not None:
                arg = vendor_end
                vendor_end = None
        else:
            bits = w & 0x1FFFFFFF
            bit = 0
            while bits:
                if bits & 1:
                    fb = bit_base + bit
                    if fb == BIT_TLV:
                        done = True  # 残りはTLV。必要なものはもう読めている
                        break
                    spec = _FIELDS.get(fb)
                    if spec is None:
                        done = True  # 知らないフィールドの先は位置が分からない
                        break
                    align, size = spec
                    arg = (arg + align - 1) & ~(align - 1)
                    if arg + size > length:
                        raise RadiotapError("field overflow")
                    if fb == 1:
                        rx.flags = buf[arg]
                    elif fb == 3:
                        rx.freq = _u16(buf, arg)[0]
                    elif fb == 5:
                        ns_signal = _s8(buf, arg)[0]
                    elif fb == 11:
                        ns_antenna = buf[arg]
                    elif fb == 0:
                        rx.tsft = _u64(buf, arg)[0]
                    elif fb == 18:
                        if rx.freq is None:
                            rx.freq = _u16(buf, arg + 4)[0]
                    elif fb == 19:
                        known, flags = buf[arg], buf[arg + 1]
                        if known & 0x01:
                            rx.ppdu_bw = 40 if (flags & 0x03) == 1 else 20
                    elif fb == 21:
                        known = _u16(buf, arg)[0]
                        if known & 0x0040:
                            rx.ppdu_bw = _VHT_BW.get(buf[arg + 3], rx.ppdu_bw)
                    elif fb == 23:
                        data1 = _u16(buf, arg)[0]
                        data5 = _u16(buf, arg + 8)[0]
                        if data1 & 0x4000:
                            bw = _HE_BW.get(data5 & 0x0F)
                            if bw:
                                rx.ppdu_bw = bw
                    arg += size
                bits >>= 1
                bit += 1
            if not done and w & (1 << BIT_VENDOR_NS):
                arg = (arg + 1) & ~1
                if arg + 6 > length:
                    raise RadiotapError("vendor ns")
                skip = _u16(buf, arg + 4)[0]
                arg += 6
                vendor_end = arg + skip
        if w & (1 << BIT_RADIOTAP_NS):
            if not in_vendor:
                close_ns()
            in_vendor = False
            bit_base = 0
            ns_index += 1
        elif w & (1 << BIT_VENDOR_NS):
            if not in_vendor:
                close_ns()
            in_vendor = True
            bit_base = 0
        else:
            bit_base += 32
    if not in_vendor:
        close_ns()

    rx.chains = tuple(chains)
    combined = combine_dbm([s for _, s in chains]) if chains else None
    drv = rx.driver_signal
    rx.signal = combined if combined is not None else (drv if drv is not None and -127 < drv < 0 else None)
    return rx
