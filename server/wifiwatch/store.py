"""SQLiteの保存（PCが正本）。記録とラベル、イベント、設定の小物、通知と中継の待ち行列、処理済みの要求ID。

再起動しても消えないものはすべてここに置く。時刻はUTCのエポック秒。
1つの接続をロックで守って、受信ループ・API・通知・中継のスレッドから使う。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notify_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created REAL NOT NULL,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,
  level TEXT,
  payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL,
  expires_at REAL,
  last_error TEXT,
  sent_at REAL
);
CREATE INDEX IF NOT EXISTS notify_pending ON notify_outbox(sent_at, channel, id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  category TEXT NOT NULL,
  kind TEXT,
  level TEXT,
  key TEXT,
  title TEXT,
  message TEXT,
  origin TEXT,
  data TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  created_at REAL NOT NULL,
  device TEXT,
  origin TEXT,
  bands TEXT NOT NULL DEFAULT '[]',
  location TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  memo TEXT NOT NULL DEFAULT '',
  label_updated_at REAL,
  label_updated_by TEXT,
  body TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS records_created ON records(created_at);
CREATE TABLE IF NOT EXISTS relay_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created REAL NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL,
  sent_at REAL
);
CREATE INDEX IF NOT EXISTS relay_pending ON relay_outbox(sent_at, id);
CREATE TABLE IF NOT EXISTS applied_requests (
  request_id TEXT PRIMARY KEY,
  at REAL NOT NULL,
  result TEXT
);
CREATE TABLE IF NOT EXISTS team_tags (
  bssid TEXT PRIMARY KEY,
  team TEXT NOT NULL,
  at REAL NOT NULL,
  origin TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, tuple(args))

    # ------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self._x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    # ------------------------------------------------------------ 通知の待ち行列
    def notify_enqueue(self, created: float, channel: str, kind: str, level: str | None, payload: dict[str, Any],
                       expires_at: float | None = None) -> int:
        cur = self._x(
            "INSERT INTO notify_outbox(created, channel, kind, level, payload, next_at, expires_at) VALUES(?,?,?,?,?,?,?)",
            (created, channel, kind, level, json.dumps(payload, ensure_ascii=False), created, expires_at))
        return int(cur.lastrowid or 0)

    def notify_pending(self, now: float, limit: int = 50) -> list[sqlite3.Row]:
        with self.lock:
            self.db.execute("DELETE FROM notify_outbox WHERE sent_at IS NULL AND expires_at IS NOT NULL AND expires_at < ?",
                            (now,))
            return list(self.db.execute(
                "SELECT * FROM notify_outbox WHERE sent_at IS NULL ORDER BY channel, id LIMIT ?", (limit,)))

    def notify_sent(self, id_: int, now: float) -> None:
        self._x("UPDATE notify_outbox SET sent_at=?, attempts=attempts+1 WHERE id=?", (now, id_))

    def notify_failed(self, id_: int, next_at: float, error: str) -> None:
        self._x("UPDATE notify_outbox SET attempts=attempts+1, next_at=?, last_error=? WHERE id=?",
                (next_at, error[:300], id_))

    def notify_drop_kind(self, kind: str) -> None:
        """未送の同じ種類（稼働中通知など）を捨てる（新しいものに置き換えるため）。"""
        self._x("DELETE FROM notify_outbox WHERE sent_at IS NULL AND kind=?", (kind,))

    def notify_backlog(self) -> int:
        return int(self._x("SELECT COUNT(*) AS n FROM notify_outbox WHERE sent_at IS NULL").fetchone()["n"])

    def notify_prune_sent(self, before: float) -> None:
        self._x("DELETE FROM notify_outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (before,))

    # ------------------------------------------------------------ イベント
    def add_event(self, ts: float, category: str, kind: str | None = None, level: str | None = None,
                  key: str | None = None, title: str | None = None, message: str | None = None,
                  origin: str | None = None, data: Any = None) -> int:
        cur = self._x(
            "INSERT INTO events(ts, category, kind, level, key, title, message, origin, data) VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, category, kind, level, key, title, message, origin,
             None if data is None else json.dumps(data, ensure_ascii=False)))
        return int(cur.lastrowid or 0)

    def events(self, limit: int = 200, since_id: int = 0, category: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE id > ?"
        args: list[Any] = [since_id]
        if category:
            sql += " AND category = ?"
            args.append(category)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._x(sql, args):
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else None
            out.append(d)
        return out
