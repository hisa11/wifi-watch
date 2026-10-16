"""radiod：無線を扱う特権の小さな補助プロセス（rootで動かす）。

本体（解析・API・中継・通知）はrootで動かさず、Unixソケット越しにここへ頼む。
受ける操作は決まったものだけ（tune・capture・survey・復旧の手順・hello）。値は必ず検査する。
任意のコマンドや送信の操作は受けない。

通信の形（1つのメッセージ）：種類1バイト ＋ 長さ4バイト（ビッグエンディアン）＋ 中身
  1 = 要求（JSON：{"op", "args"}）
  2 = 応答（JSON：{"ok": true, "result"} / {"ok": false, "error"}）
  3 = フレームの束（captureの応答）：t_start(f64) t_end(f64) freq(u16) width(u16) center(u16) 個数(u32)
      ＋ 各フレーム [長さ(u16) ＋ バイト列]

  python -m wifiwatch.radio.radiod --interface wlan0 --socket /run/wifiwatch/radiod.sock --group wifiwatch
"""
from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Any

from .base import FrameBatch, Radio, RadioError, validate_tuning

log = logging.getLogger("radiod")

T_REQ = 1
T_RESP = 2
T_FRAMES = 3
MAX_MSG = 64 * 1024 * 1024
MAX_CAPTURE_S = 2.0
_HDR = struct.Struct(">BI")
_FB = struct.Struct(">ddHHHI")


def send_msg(sock: socket.socket, kind: int, payload: bytes) -> None:
    sock.sendall(_HDR.pack(kind, len(payload)) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(min(1 << 20, n - len(out)))
        if not chunk:
            raise ConnectionError("切断されました")
        out += chunk
    return bytes(out)


def recv_msg(sock: socket.socket) -> tuple[int, bytes]:
    kind, n = _HDR.unpack(_recv_exact(sock, _HDR.size))
    if n > MAX_MSG:
        raise ConnectionError("メッセージが大きすぎます")
    return kind, _recv_exact(sock, n)


def pack_batch(b: FrameBatch) -> bytes:
    parts = [_FB.pack(b.t_start, b.t_end, b.tuning.freq, b.tuning.width, b.tuning.center, len(b.frames))]
    for fr in b.frames:
        fr = fr[:65535]
        parts.append(struct.pack(">H", len(fr)) + fr)
    return b"".join(parts)


def unpack_batch(data: bytes) -> FrameBatch:
    t0, t1, f, w, c, n = _FB.unpack_from(data, 0)
    off = _FB.size
    frames = []
    for _ in range(n):
        (ln,) = struct.unpack_from(">H", data, off)
        off += 2
        frames.append(data[off:off + ln])
        off += ln
    return FrameBatch(validate_tuning(f, w, c), t0, t1, frames)


class RadioServer:
    """1つの相手（本体）だけを受ける。操作は決まったものだけ。"""

    OPS = ("hello", "tune", "capture", "survey", "reset_monitor", "reload_driver", "managed_scan", "regulatory",
           "set_monitor")

    def __init__(self, radio: Radio, path: str, group: str | None = None, allow_uids: list[int] | None = None):
        self.radio = radio
        self.path = path
        self.group = group
        self.allow_uids = allow_uids
        self.srv: socket.socket | None = None
        self.stopping = False

    def bind(self) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)
        try:
            s.bind(self.path)
        finally:
            os.umask(old)
        if self.group:
            gid = grp.getgrnam(self.group).gr_gid
            os.chown(self.path, 0 if os.geteuid() == 0 else -1, gid)
            if os.geteuid() == 0:
                os.chown(p.parent, 0, gid)
                os.chmod(p.parent, 0o750)
        os.chmod(self.path, 0o660)
        s.listen(1)
        self.srv = s

    def _peer_ok(self, conn: socket.socket) -> bool:
        if not self.allow_uids:
            return True
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
        except OSError:
            return False
        return uid in self.allow_uids

    def handle(self, req: dict[str, Any]) -> tuple[int, bytes]:
        op = req.get("op")
        args = req.get("args") or {}
        if op not in self.OPS or not isinstance(args, dict):
            raise RadioError(f"受けない操作です：{op!r}")
        r = self.radio
        if op == "hello":
            return T_RESP, json.dumps({"ok": True, "result": {"pid": os.getpid(),
                                                                "scan_only": bool(getattr(r, "scan_only", False)),
                                                                "unreceivable": list(getattr(r, "unreceivable", []))}}).encode()
        if op == "tune":
            t = validate_tuning(args.get("freq"), args.get("width"), args.get("center"))
            r.tune(t)
            res: Any = None
        elif op == "capture":
            d = args.get("duration")
            if not isinstance(d, (int, float)) or isinstance(d, bool) or not (0 < d <= MAX_CAPTURE_S):
                raise RadioError(f"capture の時間が不正です：{d!r}")
            return T_FRAMES, pack_batch(r.capture(float(d)))
        elif op == "survey":
            s = r.survey()
            res = None if s is None else [e.__dict__ for e in s]
        elif op == "regulatory":
            g = r.regulatory()
            res = {"country": g.country, "six_ghz_enabled": g.six_ghz_enabled, "six_ghz_freqs": g.six_ghz_freqs,
                   "raw": g.raw}
        elif op == "managed_scan":
            res = r.managed_scan()
        else:
            getattr(r, op)()
            res = None
        return T_RESP, json.dumps({"ok": True, "result": res}).encode()

    def serve_conn(self, conn: socket.socket) -> None:
        with conn:
            while not self.stopping:
                try:
                    kind, data = recv_msg(conn)
                except (ConnectionError, OSError):
                    return
                if kind != T_REQ:
                    return
                try:
                    req = json.loads(data.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise RadioError("要求の形が不正です")
                    k, payload = self.handle(req)
                except (RadioError, ValueError, TypeError) as e:
                    k, payload = T_RESP, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode()
                try:
                    send_msg(conn, k, payload)
                except OSError:
                    return

    def serve_forever(self) -> None:
        if self.srv is None:
            self.bind()
        assert self.srv is not None
        while not self.stopping:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                if self.stopping:
                    return
                raise
            if not self._peer_ok(conn):
                log.warning("許していない相手からの接続を閉じました")
                conn.close()
                continue
            self.serve_conn(conn)  # 相手は1つ（本体）だけ

    def stop(self) -> None:
        self.stopping = True
        if self.srv:
            try:
                self.srv.close()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="radiod", description="wifiwatch の無線の補助（受信専用・root）")
    ap.add_argument("--interface", required=True)
    ap.add_argument("--socket", default="/run/wifiwatch/radiod.sock")
    ap.add_argument("--group", default="wifiwatch")
    ap.add_argument("--allow-uid", type=int, action="append", default=None)
    ap.add_argument("--monitor-flag", action="append", default=[], choices=["fcsfail", "control", "otherbss"])
    ap.add_argument("--no-setup", action="store_true", help="起動時にモニタモードへ切り替えない")
    ap.add_argument("--monitor-vif", default=None,
                    help="別のモニタ用IF（例：wwmon0）を作って受ける。IFそのものをモニタにするとchが変わらない無線（rtw89）向け")
    ap.add_argument("--scan-only", action="store_true",
                    help="合否ゲートが不合格のとき：管理モードのパッシブスキャンで動く（ビーコンから分かる機能だけ）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s radiod: %(message)s")
    from .linux import LinuxRadio
    if args.scan_only:
        from .scanonly import ScanOnlyRadio
        radio: LinuxRadio = ScanOnlyRadio(args.interface)
        log.warning("スキャンだけのモードで動きます（活動量・リトライ率・deauth・160MHz受信は使えません）")
    else:
        radio = LinuxRadio(args.interface, monitor_flags=args.monitor_flag, monitor_vif=args.monitor_vif)
    try:
        radio.load_hw_freqs()
        if radio.unreceivable:
            log.warning("この無線にないch：%d（%s）。合わせず、画面では「見ていない（この無線では受けられない）」と出します",
                        len(radio.unreceivable), "6GHzすべて" if all(f > 5925 for f in radio.unreceivable) else "一部")
    except Exception as e:  # 起動は止めない
        log.warning("この無線のchの一覧を読めません：%s", e)
    if not args.no_setup and not args.scan_only:
        # 合否ゲートと同じ順：先に管理モードでスキャンして国情報で6GHzを有効にさせ、それからモニタ
        try:
            n = radio.managed_scan()
            reg = radio.regulatory()
            log.info("管理モードでスキャン：%d局、国=%s、6GHz=%s", n, reg.country, "有効" if reg.six_ghz_enabled else "無効")
        except RadioError as e:
            log.warning("管理モードでのスキャンに失敗：%s", e)
        radio.set_monitor()
    srv = RadioServer(radio, args.socket, args.group, args.allow_uid)
    srv.bind()
    log.info("待ち受け：%s", args.socket)
    try:
        srv.serve_forever()
    finally:
        radio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
