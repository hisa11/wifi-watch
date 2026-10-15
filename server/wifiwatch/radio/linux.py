"""実機の無線（Linux、AX210）：iw・ip・modprobe と AF_PACKET の受信。

- 受信専用。送信・注入の操作は持たない。
- subprocess に渡すのは、検証した周波数・幅・IF名と固定の語だけ（shell は使わない）。
- 受信ソケットにはBPFを付け、データフレームはカーネルの中でMACヘッダまでに切る（radio/bpf.py）。
- 特権（CAP_NET_RAW・CAP_NET_ADMIN・モジュールの読み込み）が要るので、radiod の中で使う。
"""
from __future__ import annotations

import logging
import os
import re
import select
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .. import channels as ch
from ..capture import radiotap
from ..clock import Clock, RealClock
from . import bpf
from .base import FrameBatch, Radio, RadioError, RegInfo, SurveyEntry, Tuning, validate_ifname

log = logging.getLogger(__name__)

ETH_P_ALL = 0x0003
MONITOR_FLAGS = ("fcsfail", "control", "otherbss")


@dataclass
class RunResult:
    rc: int
    out: str
    err: str


Runner = Callable[[Sequence[str], float], RunResult]


def subprocess_runner(argv: Sequence[str], timeout: float) -> RunResult:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timeout: {argv[0]}")
    except FileNotFoundError:
        return RunResult(127, "", f"見つかりません: {argv[0]}")
    return RunResult(p.returncode, p.stdout, p.stderr)


def tune_argvs(ifname: str, t: Tuning) -> list[list[str]]:
    """tuneのコマンド（試す順）。iwの版で書き方の受け付けが違うことがあるため候補を持つ。"""
    f, w, c = str(t.freq), t.width, str(t.center)
    base = ["iw", "dev", ifname, "set", "freq", f]
    if w == 20:
        return [base + ["20", f], base + ["HT20"], base + ["NOHT"]]
    if w == 40:
        plus = "HT40+" if t.center > t.freq else "HT40-"
        return [base + ["40", c], base + [plus]]
    return [base + [str(w), c], base + [f"{w}MHz"]]


def parse_survey(out: str) -> list[SurveyEntry]:
    entries: list[SurveyEntry] = []
    cur: dict[str, object] | None = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"frequency:\s+(\d+)(?:\.\d+)? MHz(\s+\[in use\])?", line)
        if m:
            if cur:
                entries.append(SurveyEntry(**cur))  # type: ignore[arg-type]
            cur = {"freq": int(m.group(1)), "in_use": bool(m.group(2)), "noise": None, "active_ms": None, "busy_ms": None}
            continue
        if cur is None:
            continue
        m = re.match(r"noise:\s+(-?\d+) dBm", line)
        if m:
            cur["noise"] = int(m.group(1))
        m = re.match(r"channel active time:\s+(\d+) ms", line)
        if m:
            cur["active_ms"] = int(m.group(1))
        m = re.match(r"channel busy time:\s+(\d+) ms", line)
        if m:
            cur["busy_ms"] = int(m.group(1))
    if cur:
        entries.append(SurveyEntry(**cur))  # type: ignore[arg-type]
    return entries


def parse_reg(out: str, phy: str | None) -> str | None:
    """`iw reg get` から、このphy（無ければglobal）の国コード。"""
    country = None
    section = "global"
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("global"):
            section = "global"
        elif s.startswith("phy#"):
            section = s.split()[0]
        m = re.match(r"country (\w\w):", s)
        if m:
            if phy and section == phy:
                return m.group(1)
            if section == "global" and country is None:
                country = m.group(1)
    return country


def parse_phy_freqs(out: str) -> dict[int, bool]:
    """`iw phy <phy> info` の周波数の行 → {MHz: 使えるか}。"""
    res: dict[int, bool] = {}
    for line in out.splitlines():
        m = re.match(r"\s*\*\s+(\d+)(?:\.\d+)? MHz \[\d+\](.*)$", line)
        if m:
            res[int(m.group(1))] = "disabled" not in m.group(2)
    return res


class LinuxRadio(Radio):
    def __init__(self, ifname: str, clock: Clock | None = None, runner: Runner = subprocess_runner,
                 monitor_flags: Sequence[str] = (), socket_factory: Callable[[], socket.socket] | None = None,
                 sys_class_net: str = "/sys/class/net"):
        self.ifname = validate_ifname(ifname)
        self.clock = clock or RealClock()
        self.run = runner
        bad = [f for f in monitor_flags if f not in MONITOR_FLAGS]
        if bad:
            raise RadioError(f"monitor_flags が不正です：{bad}")
        self.monitor_flags = list(monitor_flags)
        self.socket_factory = socket_factory or self._open_socket
        self.sys_class_net = Path(sys_class_net)
        self.sock: socket.socket | None = None
        self.tuning: Tuning | None = None
        # この無線（ハードウェア）にあるch。`iw phy <phy> info` に載っていないchは、受けられないので合わせない
        # （6GHz非対応の無線の6GHzなど）。disabled（国設定で使えない）は載っているので、従来どおり合わせて失敗させ、復旧に回す
        self.hw_freqs: set[int] | None = None
        self.unreceivable_tuned = False
        self.tune_style: dict[int, int] = {}  # 幅 → 通った候補の番号
        self.buf = bytearray(8192)

    # ------------------------------------------------------------ 下回り
    def _cmd(self, argv: list[str], timeout: float = 5.0, check: bool = True) -> RunResult:
        r = self.run(argv, timeout)
        if check and r.rc != 0:
            raise RadioError(f"{' '.join(argv)} → {r.rc}: {(r.err or r.out).strip()[:200]}")
        return r

    def _open_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        bpf.attach(s)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.bind((self.ifname, 0))
        s.setblocking(False)
        return s

    def _reopen(self) -> None:
        self._close_sock()
        try:
            self.sock = self.socket_factory()
        except OSError as e:
            raise RadioError(f"受信ソケットを開けません：{e}") from None

    def _close_sock(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _drain(self) -> None:
        if self.sock is None:
            return
        while True:
            try:
                self.sock.recv(1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return

    def phy(self) -> str | None:
        p = self.sys_class_net / self.ifname / "phy80211" / "name"
        try:
            return p.read_text().strip()
        except OSError:
            return None

    def load_hw_freqs(self) -> dict[int, bool]:
        """`iw phy <phy> info` の周波数の一覧（MHz → 使えるか）を読み、この無線にあるchを覚える。"""
        phy = self.phy()
        if not phy or not re.match(r"^phy\d+$", phy):
            return {}
        info = self._cmd(["iw", "phy", phy, "info"], 5.0, check=False)
        freqs = parse_phy_freqs(info.out) if info.rc == 0 else {}
        if freqs:
            self.hw_freqs = set(freqs)
        return freqs

    @property
    def unreceivable(self) -> list[int]:
        """この無線にないch（画面で「見ていない」の理由として出す）。分からなければ空。"""
        if self.hw_freqs is None:
            return []
        return [f for f in ch.ALL_FREQS if f not in self.hw_freqs]

    # ------------------------------------------------------------ 受信
    def tune(self, tuning: Tuning) -> None:
        if self.hw_freqs is not None and tuning.freq not in self.hw_freqs:
            # この無線にないch：合わせない（失敗にもしない）。受信していた秒は0で返す
            self.tuning = tuning
            self.unreceivable_tuned = True
            return
        self.unreceivable_tuned = False
        cands = tune_argvs(self.ifname, tuning)
        order = list(range(len(cands)))
        first = self.tune_style.get(tuning.width)
        if first is not None:
            order.remove(first)
            order.insert(0, first)
        errors = []
        for i in order:
            r = self._cmd(cands[i], 3.0, check=False)
            if r.rc == 0:
                self.tune_style[tuning.width] = i
                self.tuning = tuning
                self._drain()
                return
            errors.append((r.err or r.out).strip()[:120])
        raise RadioError(f"{ch.label(tuning.freq)} {tuning.width}MHzに合わせられません：{' / '.join(errors)}")

    def capture(self, duration: float) -> FrameBatch:
        if self.tuning is None:
            raise RadioError("tuneしていません")
        if self.unreceivable_tuned:
            self.clock.sleep(duration)
            t = self.clock.monotonic()
            return FrameBatch(self.tuning, t, t, [])
        if self.sock is None:
            self._reopen()
        assert self.sock is not None
        t_start = self.clock.monotonic()
        end = t_start + duration
        frames: list[bytes] = []
        want = self.tuning.freq
        sock = self.sock
        buf = self.buf
        while True:
            rem = end - self.clock.monotonic()
            if rem <= 0:
                break
            try:
                r, _, _ = select.select([sock], [], [], rem)
            except InterruptedError:
                continue
            if not r:
                continue
            while True:
                try:
                    n = sock.recv_into(buf)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError as e:
                    raise RadioError(f"受信できません：{e}") from None
                if n <= 0:
                    break
                fr = bytes(buf[:n])
                try:
                    f = radiotap.parse(fr).freq
                except radiotap.RadiotapError:
                    continue
                if f is None or f == want:
                    frames.append(fr)
        return FrameBatch(self.tuning, t_start, self.clock.monotonic(), frames)

    def survey(self) -> list[SurveyEntry] | None:
        r = self._cmd(["iw", "dev", self.ifname, "survey", "dump"], 3.0, check=False)
        if r.rc != 0:
            return None
        return parse_survey(r.out)

    # ------------------------------------------------------------ モードと復旧
    def _link(self, up: bool) -> None:
        self._cmd(["ip", "link", "set", self.ifname, "up" if up else "down"])

    def set_monitor(self) -> None:
        self._close_sock()
        self._link(False)
        self._cmd(["iw", "dev", self.ifname, "set", "type", "monitor"])
        if self.monitor_flags:
            self._cmd(["iw", "dev", self.ifname, "set", "monitor", *self.monitor_flags], check=False)
        self._link(True)
        self.tuning = None
        self._reopen()

    def reset_monitor(self) -> None:
        self.set_monitor()

    def driver(self) -> str | None:
        """このIFのドライバの名前（/sys/class/net/<if>/device/driver の先）。"""
        try:
            return (self.sys_class_net / self.ifname / "device" / "driver").resolve(strict=True).name
        except OSError:
            return None

    def reload_driver(self) -> None:
        # 読み込み直すのは iwlwifi（AX210）だけ。ほかのドライバのIF（開発PCの無線など）には触らない
        drv = self.driver()
        if drv != "iwlwifi":
            raise RadioError(f"{self.ifname} のドライバは {drv or '不明'} のため、読み込み直しません（iwlwifi だけ）")
        self._close_sock()
        self._cmd(["modprobe", "-r", "iwlmvm", "iwlwifi"], 20.0)
        self._cmd(["modprobe", "iwlwifi"], 20.0)
        for _ in range(100):  # IFが戻るまで待つ（最大10秒）
            if (self.sys_class_net / self.ifname).exists():
                return
            time.sleep(0.1)
        raise RadioError(f"読み込み直しのあと {self.ifname} が戻りません（IF名が変わった可能性）")

    def managed_scan(self) -> int:
        """管理モードで1回スキャン（受信のための操作。周りの国情報で6GHzを有効にさせる）。"""
        self._close_sock()
        self._link(False)
        self._cmd(["iw", "dev", self.ifname, "set", "type", "managed"])
        self._link(True)
        # パッシブだけ（受信専用：プローブ要求を送らない。国情報はビーコンから受ける）
        r = self._cmd(["iw", "dev", self.ifname, "scan", "passive"], 30.0, check=False)
        if r.rc != 0:
            time.sleep(2)  # 立ち上がり直後は busy のことがある
            r = self._cmd(["iw", "dev", self.ifname, "scan", "passive"], 30.0, check=False)
        return sum(1 for line in r.out.splitlines() if line.startswith("BSS "))

    def regulatory(self) -> RegInfo:
        phy = self.phy()
        reg = self._cmd(["iw", "reg", "get"], 5.0, check=False)
        country = parse_reg(reg.out, f"phy#{phy[3:]}" if phy and phy.startswith("phy") else None)
        freqs = self.load_hw_freqs()
        six = [f for f in ch.FREQS_BY_BAND[ch.BAND_6] if freqs.get(f)]
        return RegInfo(country, len(six) == len(ch.FREQS_BY_BAND[ch.BAND_6]), six, reg.out[:2000])

    def close(self) -> None:
        self._close_sock()
