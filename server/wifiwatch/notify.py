"""通知（仕様書「通知」）。ntfy.sh と Discord Webhook。中継は通さない。

  crit  ntfy（優先度5）とDiscordの両方      warn  ntfy（優先度4）
  解消  ntfy（優先度2）                     稼働中  ntfy（優先度1、起動時と15分ごと）

- 送るものはまずSQLiteの待ち行列に入れる（再起動しても消えない）。回線が切れている間も
  crit と warn は戻るまで無期限に再送する（待つ間隔は最大60秒）。稼働中の通知は15分で捨てる。
- 同じ宛先には入れた順に送る（先頭が送れなければ後ろも待つ）。
- 死活監視（healthchecks.io など）へ1分ごとに生存報告を送る（失敗しても溜めない）。
- crit の発生・追加では、ピットの警報音（USBシリアルに1バイト）も鳴らす（回線が要らない）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Protocol

from .analyzer.alerts import AlertEvent
from .clock import Clock, jst_str
from .config import NotifyConfig
from .store import Store

log = logging.getLogger(__name__)

LEVEL_JP = {"crit": "緊急", "warn": "警告", "info": "情報"}
NTFY_TAGS = {"crit": ["rotating_light"], "warn": ["warning"], "resolved": ["white_check_mark"],
             "heartbeat": ["green_circle"]}
BACKOFF_MAX_S = 60.0
HEARTBEAT_TTL_S = 15 * 60
BUZZ_BYTE = b"C"


class SendError(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Poster(Protocol):
    def post_json(self, url: str, data: dict[str, Any], timeout: float) -> None: ...

    def get(self, url: str, timeout: float) -> None: ...


class HttpxPoster:
    def __init__(self) -> None:
        import httpx
        self._httpx = httpx
        self.client = httpx.Client(follow_redirects=False, headers={"User-Agent": "wifiwatch/0.1"})

    def post_json(self, url: str, data: dict[str, Any], timeout: float) -> None:
        try:
            r = self.client.post(url, content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, timeout=timeout)
        except self._httpx.HTTPError as e:
            raise SendError(f"{type(e).__name__}: {e}") from None
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            raise SendError("429 Too Many Requests", float(ra) if ra and ra.replace(".", "").isdigit() else None)
        if r.status_code >= 300:
            raise SendError(f"HTTP {r.status_code}")

    def get(self, url: str, timeout: float) -> None:
        try:
            r = self.client.get(url, timeout=timeout)
        except self._httpx.HTTPError as e:
            raise SendError(str(e)) from None
        if r.status_code >= 300:
            raise SendError(f"HTTP {r.status_code}")


class Buzzer:
    """USBシリアルの相手（STM32＋ブザー）に1バイト送る。相手が無くても監視は止めない。"""

    def __init__(self, device: str | None, baud: int = 115200):
        self.device = device
        self.baud = baud
        self.last_error: str | None = None

    def beep(self) -> bool:
        if not self.device:
            return False
        try:
            import termios
            fd = os.open(self.device, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                try:
                    attrs = termios.tcgetattr(fd)
                    speed = getattr(termios, f"B{self.baud}", termios.B115200)
                    attrs[0] = 0  # iflag
                    attrs[1] = 0  # oflag
                    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                    attrs[3] = 0  # lflag
                    attrs[4] = attrs[5] = speed
                    termios.tcsetattr(fd, termios.TCSANOW, attrs)
                except termios.error:
                    pass  # 端末でないファイル（テスト）でも書く
                os.write(fd, BUZZ_BYTE)
            finally:
                os.close(fd)
            self.last_error = None
            return True
        except OSError as e:
            self.last_error = str(e)
            log.warning("buzzer: %s", e)
            return False


class Notifier:
    def __init__(self, cfg: NotifyConfig, store: Store, clock: Clock, poster: Poster | None = None,
                 buzzer: Buzzer | None = None, status: Callable[[], dict[str, Any]] | None = None):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.poster = poster or HttpxPoster()
        self.buzzer = buzzer if buzzer is not None else Buzzer(cfg.buzzer_device, cfg.buzzer_baud)
        self.status = status or (lambda: {})
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self._next_heartbeat = -1e18
        self._next_health = -1e18
        self._next_prune = -1e18
        self.last_ok: dict[str, float] = {}
        self.last_error: dict[str, str] = {}
        self.health_ok: bool | None = None

    @property
    def ntfy_url(self) -> str:
        return self.cfg.ntfy_server.rstrip("/") + "/"

    # ------------------------------------------------------------ 入口
    def handle_alert(self, ev: AlertEvent) -> None:
        if not ev.notify:
            return
        now = self.clock.time()
        when = jst_str(ev.epoch)
        if ev.kind == "resolved":
            title = f"解消｜{ev.title}"
            body = f"{ev.message}\n{when} JST"
            self._enqueue_ntfy(now, "resolved", ev.level, title, body, 2, NTFY_TAGS["resolved"])
            return
        lv = LEVEL_JP.get(ev.level, ev.level)
        prefix = "追加｜" if ev.kind == "added" else ""
        title = f"{prefix}{lv}｜{ev.title}"
        detail = ev.message
        if ev.kind == "added" and ev.parties:
            names = "、".join(f"{p.get('ssid') or p.get('bssid')}（{p.get('ch', '')}）" for p in ev.parties[:5])
            detail = f"増えた当事者：{names}\n{ev.message}"
        body = f"{detail}\n{when} JST（ピットから見えたもの）"
        prio = 5 if ev.level == "crit" else 4
        self._enqueue_ntfy(now, ev.kind, ev.level, title, body, prio, NTFY_TAGS.get(ev.level, []))
        if ev.level == "crit":
            if self.cfg.discord_webhook:
                content = f"**🚨 {title}**\n{body}"[:1900]
                self.store.notify_enqueue(now, "discord", ev.kind, ev.level,
                                          {"content": content, "allowed_mentions": {"parse": []}})
            self.buzzer.beep()
        self.wake.set()

    def _enqueue_ntfy(self, now: float, kind: str, level: str | None, title: str, body: str, prio: int,
                      tags: list[str], expires_at: float | None = None) -> None:
        if not self.cfg.ntfy_topic:
            return
        self.store.notify_enqueue(now, "ntfy", kind, level, {
            "topic": self.cfg.ntfy_topic, "title": title, "message": body, "priority": prio, "tags": tags,
        }, expires_at)
        self.wake.set()

    def heartbeat(self) -> None:
        now = self.clock.time()
        st = self.status()
        lines = [f"{jst_str(now)} JST 稼働中"]
        if st:
            lines.append(f"モード {st.get('mode', '?')}・自ch {st.get('own', '?')}・発生中の警報 {st.get('alerts', 0)}件"
                         f"・受信 {st.get('rx', '?')}")
        self.store.notify_drop_kind("heartbeat")
        self._enqueue_ntfy(now, "heartbeat", None, "wifiwatch 稼働中", "\n".join(lines), 1, NTFY_TAGS["heartbeat"],
                           now + HEARTBEAT_TTL_S)

    def ping_health(self) -> None:
        if not self.cfg.healthcheck_url:
            return
        try:
            self.poster.get(self.cfg.healthcheck_url, self.cfg.timeout_s)
            self.health_ok = True
        except SendError as e:
            self.health_ok = False
            log.info("healthcheck ping failed: %s", e)

    # ------------------------------------------------------------ 送る
    def deliver_due(self) -> int:
        """送れるものを送る（1回ぶん）。送れた数を返す。"""
        now = self.clock.time()
        sent = 0
        blocked: set[str] = set()
        for row in self.store.notify_pending(now):
            chn = row["channel"]
            if chn in blocked:
                continue
            if row["next_at"] > now:
                blocked.add(chn)  # 先頭が待ち中なら後ろも待つ（順番を守る）
                continue
            payload = json.loads(row["payload"])
            url = self.ntfy_url if chn == "ntfy" else self.cfg.discord_webhook
            if not url:
                self.store.notify_sent(row["id"], now)
                continue
            try:
                self.poster.post_json(url, payload, self.cfg.timeout_s)
            except SendError as e:
                wait = min(BACKOFF_MAX_S, 2.0 ** min(row["attempts"] + 1, 6))
                if e.retry_after:
                    wait = max(wait, min(e.retry_after, 300.0))
                self.store.notify_failed(row["id"], now + wait, str(e))
                self.last_error[chn] = str(e)
                blocked.add(chn)
                continue
            self.store.notify_sent(row["id"], now)
            self.last_ok[chn] = now
            sent += 1
        return sent

    def tick(self) -> None:
        """定期の仕事（稼働中通知・死活監視・送信）。偽の時計で回すときも使う。"""
        now = self.clock.monotonic()
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + self.cfg.heartbeat_interval_s
            self.heartbeat()
        if now >= self._next_health:
            self._next_health = now + self.cfg.healthcheck_interval_s
            self.ping_health()
        self.deliver_due()
        if now >= self._next_prune:
            self._next_prune = now + 3600
            self.store.notify_prune_sent(self.clock.time() - 7 * 86400)

    # ------------------------------------------------------------ スレッド
    def start(self) -> None:
        def run() -> None:
            while not self.stopping.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("notifier tick failed")
                self.wake.wait(1.0)
                self.wake.clear()

        self.thread = threading.Thread(target=run, name="notifier", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def info(self) -> dict[str, Any]:
        return {"backlog": self.store.notify_backlog(), "last_ok": self.last_ok, "last_error": self.last_error,
                "health_ok": self.health_ok, "buzzer_error": self.buzzer.last_error,
                "ntfy": bool(self.cfg.ntfy_topic), "discord": bool(self.cfg.discord_webhook),
                "healthcheck": bool(self.cfg.healthcheck_url), "buzzer": bool(self.cfg.buzzer_device)}
