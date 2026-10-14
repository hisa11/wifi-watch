"""無線の共通インタフェース。実機（iw・ip・AF_PACKET）と偽の無線が同じ形を持つ。

受信専用。ここには送信・注入の操作を置かない。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .. import channels as ch

IFNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,15}$")
WIDTHS = (20, 40, 80, 160)  # AX210の受信で使う幅（320MHzは受信できない）


class RadioError(Exception):
    pass


@dataclass(frozen=True)
class Tuning:
    freq: int  # 主20MHzのMHz
    width: int
    center: int  # 中心のMHz

    @property
    def span(self) -> ch.Span:
        return ch.Span.around(self.center, self.width)


def validate_tuning(freq: object, width: object, center: object) -> Tuning:
    """subprocessに渡す前の検査。日本の57chと、標準のブロックだけを通す。"""
    if not ch.is_known_freq(freq):
        raise RadioError(f"使えない周波数です：{freq!r}")
    if not isinstance(width, int) or isinstance(width, bool) or width not in WIDTHS:
        raise RadioError(f"使えない幅です：{width!r}")
    if not isinstance(center, int) or isinstance(center, bool):
        raise RadioError(f"中心周波数が不正です：{center!r}")
    assert isinstance(freq, int)
    if width == 20:
        if center != freq:
            raise RadioError("20MHzでは中心＝主chにしてください")
    else:
        if ch.band_of_freq(freq) == ch.BAND_24:
            raise RadioError("2.4GHzは20MHzだけで受信します")
        ok = [b.center_freq for b in ch.blocks_containing(freq, width)]
        if center not in ok:
            raise RadioError(f"{ch.label(freq)} を含む{width}MHzのブロックではありません：{center}")
    return Tuning(freq, width, center)


def validate_ifname(name: object) -> str:
    if not isinstance(name, str) or not IFNAME_RE.match(name):
        raise RadioError(f"IF名が不正です：{name!r}")
    return name


@dataclass
class FrameBatch:
    tuning: Tuning
    t_start: float  # monotonic。切替が終わって受信を始めた時刻
    t_end: float
    frames: list[bytes] = field(default_factory=list)

    @property
    def receive_s(self) -> float:
        return max(0.0, self.t_end - self.t_start)


@dataclass
class RegInfo:
    country: str | None
    six_ghz_enabled: bool
    six_ghz_freqs: list[int]
    raw: str = ""


@dataclass
class SurveyEntry:
    freq: int
    in_use: bool
    noise: int | None
    active_ms: int | None
    busy_ms: int | None


class Radio(ABC):
    """受信だけを行う無線。tune → capture を繰り返す。"""

    @abstractmethod
    def tune(self, tuning: Tuning) -> None: ...

    @abstractmethod
    def capture(self, duration: float) -> FrameBatch:
        """直前のtuneの設定で duration 秒受信する。受信したchが違うフレームは捨てる。"""

    def survey(self) -> list[SurveyEntry] | None:
        return None

    # 復旧（仕様書「受信停止」）。それぞれ失敗したら RadioError
    @abstractmethod
    def reset_monitor(self) -> None:
        """モニタを再設定する（down → type monitor → up）。"""

    @abstractmethod
    def reload_driver(self) -> None:
        """iwlwifiを読み込み直す。"""

    @abstractmethod
    def managed_scan(self) -> int:
        """管理モードにして1回スキャンし、見えたBSSの数を返す（周りの国情報で6GHzを有効にさせる）。"""

    @abstractmethod
    def regulatory(self) -> RegInfo: ...

    @abstractmethod
    def set_monitor(self) -> None:
        """管理モードからモニタモードに戻す。"""

    def close(self) -> None:
        pass
