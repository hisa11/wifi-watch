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

    # ------------------------------------------------------------ 記録とラベル
    @staticmethod
    def _label_of(row: sqlite3.Row) -> dict[str, Any]:
        return {"location": row["location"], "tags": json.loads(row["tags"] or "[]"), "memo": row["memo"] or "",
                "updated_at": row["label_updated_at"], "updated_by": row["label_updated_by"]}

    def upsert_record(self, rec: dict[str, Any], now: float) -> bool:
        """記録を入れる（同じIDなら本文を置き換える）。ラベルは編集時刻の新しい方を残す。新規ならTrue。"""
        lab = rec.get("label") or {}
        with self.lock:
            old = self.db.execute("SELECT * FROM records WHERE id=?", (rec["id"],)).fetchone()
            if old is not None:
                ol = self._label_of(old)
                if (ol["updated_at"] or 0) >= (lab.get("updated_at") or 0):
                    lab = ol
            body = {k: v for k, v in rec.items() if k != "label"}
            self.db.execute(
                "INSERT INTO records(id, kind, created_at, device, origin, bands, location, tags, memo, label_updated_at,"
                " label_updated_by, body, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, created_at=excluded.created_at,"
                " device=excluded.device, origin=excluded.origin, bands=excluded.bands, location=excluded.location,"
                " tags=excluded.tags, memo=excluded.memo, label_updated_at=excluded.label_updated_at,"
                " label_updated_by=excluded.label_updated_by, body=excluded.body, updated_at=excluded.updated_at",
                (rec["id"], rec["kind"], rec["created_at"], rec.get("device"), rec.get("origin"),
                 json.dumps(rec.get("bands") or []), lab.get("location"),
                 json.dumps(lab.get("tags") or [], ensure_ascii=False), lab.get("memo") or "",
                 lab.get("updated_at"), lab.get("updated_by"), json.dumps(body, ensure_ascii=False), now))
            return old is None

    def get_record(self, id_: str) -> dict[str, Any] | None:
        r = self._x("SELECT * FROM records WHERE id=?", (id_,)).fetchone()
        if r is None:
            return None
        body = json.loads(r["body"])
        body["label"] = self._label_of(r)
        body["updated_at"] = r["updated_at"]
        return body

    def list_records(self, kind: str | None = None, since: float | None = None, until: float | None = None,
                     location: str | None = None, band: str | None = None, tag: str | None = None,
                     limit: int = 200, updated_after: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if since is not None:
            sql += " AND created_at>=?"
            args.append(since)
        if until is not None:
            sql += " AND created_at<?"
            args.append(until)
        if location:
            sql += " AND location=?"
            args.append(location)
        if updated_after is not None:
            sql += " AND updated_at>?"
            args.append(updated_after)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 5000)))
        out = []
        for r in self._x(sql, args):
            bands = json.loads(r["bands"] or "[]")
            if band and band not in bands:
                continue
            lab = self._label_of(r)
            if tag and tag not in lab["tags"]:
                continue
            body = json.loads(r["body"])
            out.append({
                "id": r["id"], "kind": r["kind"], "created_at": r["created_at"], "device": r["device"],
                "origin": r["origin"], "bands": bands, "label": lab, "updated_at": r["updated_at"],
                "n_stations": len(body.get("stations") or []), "n_channels": len(body.get("channels") or []),
            })
        return out

    def update_label(self, id_: str, label: dict[str, Any], edited_at: float, by: str, now: float) -> tuple[bool, dict[str, Any] | None]:
        """編集時刻の新しい方を残す。適用したらTrue。"""
        with self.lock:
            r = self.db.execute("SELECT * FROM records WHERE id=?", (id_,)).fetchone()
            if r is None:
                return False, None
            if r["label_updated_at"] is not None and r["label_updated_at"] >= edited_at:
                return False, self.get_record(id_)
            self.db.execute(
                "UPDATE records SET location=?, tags=?, memo=?, label_updated_at=?, label_updated_by=?, updated_at=? WHERE id=?",
                (label.get("location"), json.dumps(label.get("tags") or [], ensure_ascii=False), label.get("memo") or "",
                 edited_at, by, now, id_))
        return True, self.get_record(id_)

    def all_records_full(self, ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if ids is None:
            rows = list(self._x("SELECT id FROM records ORDER BY created_at"))
            ids = [r["id"] for r in rows]
        return [r for r in (self.get_record(i) for i in ids) if r is not None]

    # ------------------------------------------------------------ 要求IDの冪等
    def request_result(self, request_id: str) -> Any:
        r = self._x("SELECT result FROM applied_requests WHERE request_id=?", (request_id,)).fetchone()
        return None if r is None else json.loads(r["result"] or "null")

    def request_seen(self, request_id: str) -> bool:
        return self._x("SELECT 1 FROM applied_requests WHERE request_id=?", (request_id,)).fetchone() is not None

    def request_save(self, request_id: str, result: Any, at: float) -> None:
        self._x("INSERT OR REPLACE INTO applied_requests(request_id, at, result) VALUES(?,?,?)",
                (request_id, at, json.dumps(result, ensure_ascii=False)))

    def request_prune(self, before: float) -> None:
        self._x("DELETE FROM applied_requests WHERE at < ?", (before,))

    # ------------------------------------------------------------ 手動のチーム名タグ
    def set_tag(self, bssid: str, team: str | None, at: float, origin: str) -> None:
        if team:
            self._x("INSERT OR REPLACE INTO team_tags(bssid, team, at, origin) VALUES(?,?,?,?)", (bssid, team, at, origin))
        else:
            self._x("DELETE FROM team_tags WHERE bssid=?", (bssid,))

    def tags(self) -> dict[str, str]:
        return {r["bssid"]: r["team"] for r in self._x("SELECT bssid, team FROM team_tags")}

    # ------------------------------------------------------------ 中継に送る待ち行列
    def relay_enqueue(self, kind: str, ref: str, now: float) -> None:
        with self.lock:
            self.db.execute("DELETE FROM relay_outbox WHERE sent_at IS NULL AND kind=? AND ref=?", (kind, ref))
            self.db.execute("INSERT INTO relay_outbox(created, kind, ref, next_at) VALUES(?,?,?,?)", (now, kind, ref, now))

    def relay_pending(self, now: float, limit: int = 20) -> list[sqlite3.Row]:
        return list(self._x("SELECT * FROM relay_outbox WHERE sent_at IS NULL AND next_at<=? ORDER BY id LIMIT ?",
                            (now, limit)))

    def relay_backlog(self) -> int:
        return int(self._x("SELECT COUNT(*) AS n FROM relay_outbox WHERE sent_at IS NULL").fetchone()["n"])

    def relay_sent(self, id_: int, now: float) -> None:
        self._x("UPDATE relay_outbox SET sent_at=?, attempts=attempts+1 WHERE id=?", (now, id_))

    def relay_failed(self, id_: int, next_at: float) -> None:
        self._x("UPDATE relay_outbox SET attempts=attempts+1, next_at=? WHERE id=?", (next_at, id_))
