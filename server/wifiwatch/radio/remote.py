"""RemoteRadio：radiod（特権の補助）にUnixソケット越しで頼む無線。本体はrootで動かさない。"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any

from ..clock import Clock, RealClock
from .base import FrameBatch, Radio, RadioError, RegInfo, SurveyEntry, Tuning
from .radiod import T_FRAMES, T_REQ, T_RESP, recv_msg, send_msg, unpack_batch


class RemoteRadio(Radio):
    def __init__(self, path: str, clock: Clock | None = None, timeout: float = 60.0):
        self.path = path
        self.clock = clock or RealClock()
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.unreceivable: list[int] = []
        self.hello_at: float | None = None

    def _connect(self) -> socket.socket:
        if self.sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(self.path)
            except OSError as e:
                s.close()
                raise RadioError(f"radiod に届きません（{self.path}）：{e}") from None
            self.sock = s
        return self.sock

    def _call(self, op: str, **args: Any) -> tuple[int, bytes]:
        with self.lock:
            for attempt in (1, 2):
                s = self._connect()
                try:
                    send_msg(s, T_REQ, json.dumps({"op": op, "args": args}).encode())
                    return recv_msg(s)
                except (OSError, ConnectionError) as e:
                    self.close()
                    if attempt == 2:
                        raise RadioError(f"radiod との通信が切れました：{e}") from None
        raise RadioError("unreachable")

    def _json(self, op: str, **args: Any) -> Any:
        kind, data = self._call(op, **args)
        if kind != T_RESP:
            raise RadioError("radiod の応答の形が違います")
        res = json.loads(data.decode("utf-8"))
        if not res.get("ok"):
            raise RadioError(res.get("error", "radiod の失敗"))
        return res.get("result")

    def hello(self) -> dict[str, Any]:
        r = self._json("hello")
        self.scan_only = bool(r.get("scan_only"))
        self.unreceivable = [int(f) for f in r.get("unreceivable") or []]
        self.hello_at = self.clock.monotonic()
        return r

    def tune(self, tuning: Tuning) -> None:
        # radiod があとから起動したときや、受けられるchが変わったとき（国情報）のために、ときどき聞き直す
        if self.hello_at is None or self.clock.monotonic() - self.hello_at > 60:
            self.hello()
        self._json("tune", freq=tuning.freq, width=tuning.width, center=tuning.center)

    def capture(self, duration: float) -> FrameBatch:
        kind, data = self._call("capture", duration=duration)
        if kind == T_FRAMES:
            return unpack_batch(data)
        res = json.loads(data.decode("utf-8"))
        raise RadioError(res.get("error", "capture に失敗"))

    def survey(self) -> list[SurveyEntry] | None:
        r = self._json("survey")
        return None if r is None else [SurveyEntry(**e) for e in r]

    def reset_monitor(self) -> None:
        self._json("reset_monitor")

    def reload_driver(self) -> None:
        self._json("reload_driver")

    def managed_scan(self) -> int:
        return int(self._json("managed_scan"))

    def regulatory(self) -> RegInfo:
        r = self._json("regulatory")
        return RegInfo(r["country"], r["six_ghz_enabled"], r["six_ghz_freqs"], r.get("raw", ""))

    def set_monitor(self) -> None:
        self._json("set_monitor")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
