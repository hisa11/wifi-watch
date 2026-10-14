"""日本で使えるch（MHz）の表と、帯域・ブロック・重なりの計算。

chは周波数（MHz）で持つ。ch番号は帯域をまたいで衝突するため（2.4GHzのch1は2412MHz、
6GHzのch1は5955MHz）、番号は表示と設定の読み込みにだけ使う。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BAND_24 = "2.4"
BAND_5 = "5"
BAND_6 = "6"
BANDS = (BAND_24, BAND_5, BAND_6)
BAND_LABEL = {BAND_24: "2.4GHz", BAND_5: "5GHz", BAND_6: "6GHz"}

# 日本で使えるch。5GHzはW52/W53/W56（ch144を含む）の20ch、6GHzは5925〜6425MHzの24ch。
NUMBERS = {
    BAND_24: tuple(range(1, 14)),
    BAND_5: (36, 40, 44, 48, 52, 56, 60, 64,
             100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144),
    BAND_6: tuple(range(1, 94, 4)),
}
PSC_6 = (5, 21, 37, 53, 69, 85)

# 5GHzで束ねられるブロック（中心ch）。日本ではch149以上が無いので、132〜144は80MHzまで。
_BLOCK_CENTERS_5 = {
    40: (38, 46, 54, 62, 102, 110, 118, 126, 134, 142),
    80: (42, 58, 106, 122, 138),
    160: (50, 114),
}
# 6GHz（日本の24ch）。320MHzは中心ch31（ch1〜61）と中心ch63（ch33〜93）の2本で、互いに重なる。
_BLOCK_CENTERS_6 = {
    40: (3, 11, 19, 27, 35, 43, 51, 59, 67, 75, 83, 91),
    80: (7, 23, 39, 55, 71, 87),
    160: (15, 47, 79),
    320: (31, 63),
}


def number_to_freq(band: str, number: int) -> int:
    if band == BAND_24:
        return 2484 if number == 14 else 2407 + 5 * number
    if band == BAND_5:
        return 5000 + 5 * number
    if band == BAND_6:
        return 5935 if number == 2 else 5950 + 5 * number
    raise ValueError(f"unknown band {band!r}")


def band_of_freq(freq: float) -> str | None:
    """周波数がどの帯域か（日本で使えないchでも帯域は返す）。"""
    if 2400 <= freq <= 2500:
        return BAND_24
    if 5150 <= freq <= 5925:
        return BAND_5
    if 5925 < freq <= 7125:
        return BAND_6
    return None


def freq_to_number(freq: int) -> int | None:
    band = band_of_freq(freq)
    if band == BAND_24:
        return 14 if freq == 2484 else (freq - 2407) // 5
    if band == BAND_5:
        return (freq - 5000) // 5
    if band == BAND_6:
        return 2 if freq == 5935 else (freq - 5950) // 5
    return None


def number_in_band_to_freq(band: str, number: int) -> int:
    """IEに書かれたch番号（中心chを含む）を周波数にする。番号の範囲は検査しない。"""
    return number_to_freq(band, number)


@dataclass(frozen=True)
class Channel:
    freq: int
    band: str
    number: int

    @property
    def label(self) -> str:
        return f"{BAND_LABEL[self.band]} ch{self.number}"

    @property
    def short(self) -> str:
        return f"{'2' if self.band == BAND_24 else self.band}g{self.number}"

    @property
    def is_psc(self) -> bool:
        return self.band == BAND_6 and self.number in PSC_6


ALL_CHANNELS: tuple[Channel, ...] = tuple(
    Channel(number_to_freq(b, n), b, n) for b in BANDS for n in NUMBERS[b]
)
BY_FREQ: dict[int, Channel] = {c.freq: c for c in ALL_CHANNELS}
FREQS_BY_BAND: dict[str, tuple[int, ...]] = {
    b: tuple(c.freq for c in ALL_CHANNELS if c.band == b) for b in BANDS
}
ALL_FREQS: tuple[int, ...] = tuple(BY_FREQ)
assert len(ALL_FREQS) == 57


def channel(freq: int) -> Channel:
    try:
        return BY_FREQ[freq]
    except KeyError:
        raise ValueError(f"{freq}MHz は日本で使えるchではありません") from None


def is_known_freq(freq: object) -> bool:
    return isinstance(freq, int) and not isinstance(freq, bool) and freq in BY_FREQ


def label(freq: int) -> str:
    c = BY_FREQ.get(freq)
    if c:
        return c.label
    n = freq_to_number(freq)
    b = band_of_freq(freq)
    return f"{BAND_LABEL[b]} ch{n}" if b and n is not None else f"{freq}MHz"


_REF_RE = re.compile(r"^\s*(2\.4|2|5|6)\s*(?:g|ghz)?\s*[:/ \-]?\s*(?:ch)?\s*(\d{1,3})\s*$", re.I)


def parse_channel_ref(value: object) -> int:
    """設定に書かれたchを周波数（MHz）にする。

    受け付ける形：整数のMHz（6135）、文字列 "6g37"・"6GHz ch37"・"6:37"・"5g36"・"2.4g1"・"2g1"。
    日本で使えないchは ValueError。
    """
    if isinstance(value, bool):
        raise ValueError(f"{value!r} はchとして読めません")
    if isinstance(value, int):
        if value in BY_FREQ:
            return value
        raise ValueError(f"{value}MHz は日本で使えるchではありません")
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_channel_ref(int(s))
        m = _REF_RE.match(s)
        if m:
            band = {"2": BAND_24, "2.4": BAND_24, "5": BAND_5, "6": BAND_6}[m.group(1)]
            num = int(m.group(2))
            if num not in NUMBERS[band]:
                hint = {
                    BAND_24: "1〜13",
                    BAND_5: "36〜64と100〜144の4おき",
                    BAND_6: "1〜93の4おき（1, 5, 9, …）",
                }[band]
                raise ValueError(f"「{value}」は使えないchです（{BAND_LABEL[band]}は{hint}）")
            return number_to_freq(band, num)
    raise ValueError(f"「{value}」はchとして読めません（例：\"6g37\"、\"5g36\"、6135）")


# ---------------------------------------------------------------- 幅と範囲


@dataclass(frozen=True)
class Span:
    """占有する周波数の範囲（両端のMHz）。"""

    lo: float
    hi: float

    @staticmethod
    def around(center: float, width: float) -> "Span":
        return Span(center - width / 2, center + width / 2)

    @property
    def width(self) -> float:
        return self.hi - self.lo

    @property
    def center(self) -> float:
        return (self.lo + self.hi) / 2

    def overlaps(self, other: "Span") -> bool:
        return self.lo < other.hi and other.lo < self.hi

    def contains_freq(self, freq: float) -> bool:
        return self.lo < freq < self.hi


def span20(freq: int) -> Span:
    return Span.around(freq, 20)


def subchannels(span: Span) -> list[int]:
    """範囲にかかる日本の20MHz chの周波数（中心が範囲内のもの）。"""
    return [f for f in ALL_FREQS if span.lo < f < span.hi]


def spans_overlap(a: list[Span] | tuple[Span, ...], b: list[Span] | tuple[Span, ...]) -> bool:
    return any(x.overlaps(y) for x in a for y in b)


@dataclass(frozen=True)
class Block:
    """束ねたchのブロック（受信の設定と、回避先の候補に使う）。"""

    band: str
    width: int
    center_freq: int

    @property
    def span(self) -> Span:
        return Span.around(self.center_freq, self.width)

    @property
    def freqs(self) -> list[int]:
        return subchannels(self.span)

    @property
    def label(self) -> str:
        fs = self.freqs
        if not fs:
            return f"{self.width}MHz@{self.center_freq}"
        a, b = BY_FREQ[fs[0]].number, BY_FREQ[fs[-1]].number
        if a == b:
            return f"{BAND_LABEL[self.band]} ch{a}"
        return f"{BAND_LABEL[self.band]} ch{a}〜{b}（{self.width}MHz）"


def blocks(band: str, width: int) -> list[Block]:
    """帯域と幅ごとの、日本で使えるブロックの一覧。"""
    if width == 20:
        return [Block(band, 20, f) for f in FREQS_BY_BAND[band]]
    table = {BAND_5: _BLOCK_CENTERS_5, BAND_6: _BLOCK_CENTERS_6}.get(band, {})
    return [Block(band, width, number_to_freq(band, c)) for c in table.get(width, ())]


def blocks_containing(freq: int, width: int) -> list[Block]:
    band = band_of_freq(freq)
    if band is None:
        return []
    return [b for b in blocks(band, width) if b.span.contains_freq(freq)]


def block_for(freq: int, width: int) -> Block | None:
    """主chを含む幅widthのブロック（320MHzで2本候補があるときは低い方）。"""
    bs = blocks_containing(freq, width)
    return bs[0] if bs else None


def own_block(freq: int, max_width: int = 160) -> Block:
    """自分のchを含む、受信に使うブロック（160MHzがあればそれ、無ければ80→40→20）。"""
    for w in (160, 80, 40):
        if w <= max_width:
            b = block_for(freq, w)
            if b:
                return b
    return Block(band_of_freq(freq) or BAND_6, 20, freq)


def valid_widths(freq: int) -> list[int]:
    band = band_of_freq(freq)
    out = [20]
    for w in (40, 80, 160, 320):
        if block_for(freq, w) is not None:
            out.append(w)
    if band == BAND_24:
        out = [20, 40]
    return out
