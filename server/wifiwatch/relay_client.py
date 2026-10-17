"""中継への問い合わせ（PCから外向きだけ。PCは外からの接続を受けない）。

- 間隔：アプリが前面にいないとき10秒、いるとき2秒（設定で変えられる）。
- 起動直後は版数なしで問い合わせ、受信箱を全部受け取る（落ちる前の取りこぼしを拾う）。
- 受け取った指示は、ローカルAPIと同じ controller の関数で実行する（中継専用の実行経路は作らない）。
  指示の期限（60秒）は中継の時計どうしで比べ、過ぎていれば実行しない。
- 保存できたものだけ「保存した」（ack）を次の問い合わせで返す。保存できなかったら版数を忘れて次も全部受け取る。
- アプリが前面にいる間だけ、状態の写し（8KB以下）を同じ要求で置く。
- 新しい記録と、PCで変えたラベルは記録の写しとして置く。つながらない間は溜めて（SQLite）、つながったら送る。
- 受信と解析の処理を止めないよう、別スレッドで動く。中継が止まっても監視と通知は止まらない。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from .clock import Clock
from .config import RelayConfig
from .controller import ControlError, Controller
from .store import Store

log = logging.getLogger(__name__)

COMMAND_TTL_MS = 60_000


class RelayError(Exception):
    pass


class RelayClient:
    def __init__(self, cfg: RelayConfig, ctl: Controller, store: Store, clock: Clock,
                 transport: httpx.BaseTransport | None = None):
        assert cfg.url and cfg.pc_token
        self.cfg = cfg
        self.ctl = ctl
        self.store = store
        self.clock = clock
        self.client = httpx.Client(base_url=cfg.url, timeout=cfg.timeout_s, transport=transport,
                                   headers={"Authorization": f"Bearer {cfg.pc_token}",
                                            "User-Agent": "wifiwatch-pc/0.1"})
        self.version: str | None = None  # 起動直後は版数なし＝受信箱を全部受け取る
        self.fg = False
        self.view_served: float | None = None  # 最後に一覧を置いた「頼まれた時刻」（同じ頼みには1回だけ置く）
        self.acks: list[str] = []
        self.ok = False
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self._publish()
        ctl.on_record.append(lambda rec: self.wake.set())

    # ------------------------------------------------------------ 通信
    def _req(self, method: str, path: str, body: Any) -> dict[str, Any]:
        try:
            r = self.client.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise RelayError(f"{type(e).__name__}: {e}") from None
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except Exception:
                msg = ""
            raise RelayError(f"HTTP {r.status_code} {msg}")
        return r.json()

    def _publish(self) -> None:
        self.ctl.relay_info = {
            "configured": True, "ok": self.ok, "fg": self.fg, "last_ok": self.last_ok, "last_error": self.last_error,
            "backlog": self.store.relay_backlog(), "version": self.version,
        }

    def _fail(self, e: Exception) -> None:
        self.ok = False
        self.failures += 1
        self.last_error = str(e)[:200]
        self._publish()
        log.info("relay: %s", e)

    # ------------------------------------------------------------ 1回ぶん
    def sync_once(self) -> bool:
        acks = self.acks[:200]
        body: dict[str, Any] = {"version": self.version, "acks": acks}
        if self.fg:
            body["state"] = self.ctl.compact_state()
        try:
            data = self._req("POST", "/api/pc/sync", body)
        except RelayError as e:
            self._fail(e)
            return False
        self.acks = self.acks[len(acks):]
        self.ok = True
        self.failures = 0
        self.last_ok = self.clock.time()
        self.last_error = None
        was_fg = self.fg
        self.fg = bool(data.get("fg"))
        inbox = data.get("inbox")
        if inbox is not None:
            all_ok = True
            for item in inbox:
                try:
                    self._apply(item, float(data["now"]))
                    self.acks.append(item["field"])
                except Exception:
                    log.exception("relay: 受け取ったものを保存できませんでした: %s", item.get("field"))
                    all_ok = False
            self.version = data["version"] if all_ok else None
        else:
            self.version = data["version"]
        self._publish()
        if self.fg and not was_fg:
            self.wake.set()  # 前面になったらすぐ状態の写しを置く
        vr = data.get("view_req")
        if isinstance(vr, (int, float)) and vr != self.view_served:
            self.push_view(float(vr))
        return True

    def push_view(self, asked: float) -> bool:
        """アプリが「PCが今見ている局」を頼んだ：今の一覧を1回だけ置く（指示ではないので、PCの動きは変えない）。"""
        try:
            self._req("PUT", "/api/pc/view", {"for": asked, "view": self.ctl.pc_view()})
        except RelayError as e:
            self._fail(e)  # 頼みが残っていれば次の問い合わせでもう一度置く
            return False
        self.view_served = asked
        return True

    def _apply(self, item: dict[str, Any], relay_now: float) -> None:
        kind = item.get("kind")
        rid = item.get("request_id")
        now = self.clock.time()
        if kind == "command":
            age = relay_now - float(item.get("accepted_at", 0))
            if age > COMMAND_TTL_MS:
                self.ctl.command_status[rid] = {"state": "expired", "at": now, "type": item.get("type")}
                return  # 受け取った時点で期限を過ぎていれば実行しない（受け取ったことは返す）
            prev = self.ctl.command_status.get(rid)
            if prev is None:
                # 中継が受け付けてからPCが受け取るまでの秒（中継の時計どうし。実機の確認で使う）
                delay = round(age / 1000.0, 1)
                self.ctl.command_status[rid] = {"state": "received", "at": now, "type": item.get("type"), "delay_s": delay}
                self.ctl.log_event("relay", "command", "info", rid, "アプリの指示を受け取った",
                                   f"{item.get('type')}・中継で受け付けてから{delay}秒", "app", {"delay_s": delay})
            p = item.get("params") or {}
            try:
                if item.get("type") == "scan":
                    res = self.ctl.manual_scan(p.get("bands"), p.get("freqs"), bool(p.get("confirm")), "app", rid)
                    if not (prev and prev.get("state") == "done"):
                        self.ctl.command_status[rid] = {"state": "running", "at": now, "type": "scan", "scan": res.get("id"),
                                                        "total": res.get("total")}
                elif item.get("type") == "bands":
                    self.ctl.set_bands(p.get("excluded_bands", []), p.get("excluded_freqs", []), "app", rid)
                    self.ctl.command_status[rid] = {"state": "done", "at": now, "type": "bands"}
                else:
                    raise ControlError(400, "unsupported", "受けない種類の指示です")
            except ControlError as e:
                # 断ったことも結果。アプリに見せ、受信箱からは消す（同じものを何度も試さない）
                self.ctl.command_status[rid] = {"state": "rejected", "at": now, "type": item.get("type"),
                                                "error": e.code, "message": e.message}
            return
        if kind == "label":
            try:
                self.ctl.update_label(str(item.get("record_id")), item.get("label") or {}, "app",
                                      float(item.get("edited_at", 0)), rid)
            except ControlError as e:
                if e.status == 404:
                    return  # 記録が無い（消えた）ものは捨てる
                raise
            return
        if kind == "walk":
            try:
                self.ctl.ingest_record(item.get("record"), "app")
            except ControlError as e:
                log.warning("relay: 歩測の記録を受け付けません: %s", e.message)
            return
        log.warning("relay: 知らない種類 %s", kind)

    def push_once(self, limit: int = 10) -> int:
        """記録の写しを置く（溜まっているものから順に）。"""
        now = self.clock.time()
        sent = 0
        for row in self.store.relay_pending(now, limit):
            rec = self.store.get_record(row["ref"]) if row["kind"] == "record" else None
            if rec is None:
                self.store.relay_sent(row["id"], now)
                continue
            rec = dict(rec)
            rec.pop("updated_at", None)
            try:
                self._req("PUT", f"/api/pc/records/{row['ref']}", {"record": rec})
            except RelayError as e:
                self.store.relay_failed(row["id"], now + min(60.0, 2.0 ** min(row["attempts"] + 1, 6)))
                self._fail(e)
                break
            self.store.relay_sent(row["id"], now)
            sent += 1
        self._publish()
        return sent

    # ------------------------------------------------------------ スレッド
    def interval(self) -> float:
        return self.cfg.fg_interval_s if self.fg else self.cfg.idle_interval_s

    def start(self) -> None:
        def run() -> None:
            while not self.stopping.is_set():
                try:
                    self.sync_once()
                    if self.ok:
                        self.push_once()
                except Exception:
                    log.exception("relay loop")
                wait = self.interval() if self.ok else min(self.cfg.idle_interval_s, 2.0 * (1 + min(self.failures, 4)))
                self.wake.wait(wait)
                self.wake.clear()

        self.thread = threading.Thread(target=run, name="relay", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.client.close()
