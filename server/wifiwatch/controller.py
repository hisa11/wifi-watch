"""ローカルAPIと中継の指示が共通で呼ぶ内部の関数（中継専用の実行経路は作らない）。

- 指示の値はここでも検査する（中継でも検査するが、PC側が最後の砦）。
- 要求IDつきの指示は冪等：同じIDが二度来たら、前の結果を返して実行しない。
- 変更はイベントとして残す（いつ・どこから・何を）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import threading
from typing import Any, Callable

from . import channels as ch
from .analyzer.alerts import AlertEvent
from .analyzer.occupancy import OccupancyContext, channel_rows, is_present, missed_visits, present_stations, suggestions
from .analyzer.stations import StationTable
from .capture import ies
from .classify import FP_RE
from .follow import FollowDecision, OwnFollower
from .clock import Clock, jst_str, utc_iso
from .config import OWN_AP_MAX, Config, normalize_mac, valid_ssid
from .logs import FileLogs, write_own
from .monitor import Monitor
from .scheduler import Busy, ManualScan, NeedsConfirm, SchedulerError
from .store import Store

log = logging.getLogger(__name__)

ORIGINS = ("desktop", "app")
MAX_LOCATION = 40
MAX_TAGS = 10
MAX_TAG_LEN = 24
MAX_MEMO = 1000
BSSID_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _own_ap_text(a: dict[str, Any]) -> str:
    if a["match"] == "bssid":
        return "MACアドレス " + "、".join(a["bssids"]) + (f"（SSID {'、'.join(a['ssids'])} はなりすましの見張りに使う）" if a["ssids"] else "")
    if a["match"] == "ssid":
        return "SSID " + "、".join(a["ssids"])
    return "未設定"


class ControlError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


def _origin(o: str) -> str:
    if o not in ORIGINS:
        raise ControlError(400, "bad_origin", f"origin が不正です：{o}")
    return o


# 自chを自APに合わせたことを、画面とアプリに出しておく時間
FOLLOW_SHOW_S = 1800.0


class Controller:
    def __init__(self, cfg: Config, mon: Monitor, store: Store, logs: FileLogs, clock: Clock,
                 notifier: Any = None):
        self.cfg = cfg
        self.mon = mon
        self.store = store
        self.logs = logs
        self.clock = clock
        self.notifier = notifier
        self.started_at = clock.time()
        self.relay_info: dict[str, Any] = {"configured": bool(cfg.relay.url)}
        self.command_status: dict[str, dict[str, Any]] = {}  # 中継から来た指示の受け取り・実行中・結果（状態の写しに載せる）
        self.on_record: list[Callable[[dict[str, Any]], None]] = []
        self.lock = threading.RLock()
        # 再起動しても残すもの
        for b, t in store.tags().items():
            mon.classifier.set_tag(b, t)
        mon.alerts.once_done |= set(store.kv_get("alert_once", []))
        mon.alerts.persist_once = lambda s: store.kv_set("alert_once", sorted(s))
        mon.on_alert_event.append(self._on_alert)
        mon.engine.on_scan_record.append(self._on_scan_record)
        mon.liveness.on_change.append(self._on_liveness)
        mon.on_calibration.append(self._on_calibration)
        mon.engine.add_ticker(10.0, self._write_channels)
        if notifier is not None:
            mon.on_alert_event.append(notifier.handle_alert)
        # 自APのchに自chを合わせる
        self.follower = OwnFollower()
        self.own_followed: dict[str, Any] | None = None
        self.own_follow_pending: dict[str, Any] | None = None
        mon.engine.add_ticker(2.0, self._follow_tick)

    # ------------------------------------------------------------ イベント
    def log_event(self, category: str, kind: str | None = None, level: str | None = None, key: str | None = None,
                  title: str | None = None, message: str | None = None, origin: str | None = None,
                  data: Any = None) -> None:
        ts = self.clock.time()
        self.store.add_event(ts, category, kind, level, key, title, message, origin, data)
        self.logs.event(ts, {"category": category, "kind": kind, "level": level, "key": key, "title": title,
                             "message": message, "origin": origin, "data": data})

    def _on_alert(self, ev: AlertEvent) -> None:
        if ev.kind == "raised" and not ev.notify and not ev.new_parties:
            return  # 解消を知らせる前の再発生は記録しない（cleared と対で残っている）
        self.log_event("alert", ev.kind, ev.level, ev.key, ev.title, ev.message, None,
                       {"parties": [p.get("id") for p in ev.parties], "notify": ev.notify, "type": ev.type})

    def _on_liveness(self, note: dict[str, Any]) -> None:
        what = note.get("what")
        msgs = {"reset_monitor": "モニタを再設定", "reload_driver": "iwlwifiを読み込み直し（管理モードでスキャン→国設定の確認→モニタ）",
                "recovered": "受信が戻った", "failed": "復旧できない", "reset_monitor_failed": "モニタの再設定に失敗"}
        level = "crit" if what == "failed" else ("info" if what == "recovered" else "warn")
        detail = note.get("error") or note.get("reason") or note.get("why")
        msg = msgs.get(str(what), str(what)) + (f"：{detail}" if detail else "")
        self.log_event("radio", what, level, "liveness", "受信の見張り", msg[:300], None, note)

    def _on_calibration(self, cal: dict[str, Any]) -> None:
        self.store.kv_set("calibration", cal)
        self.log_event("calibration", "pre", "info", None, "PREの下見のまとめ", "／".join(cal.get("hints", [])), None,
                       {"bands": cal.get("bands")})

    def _on_scan_record(self, rec: dict[str, Any]) -> None:
        now = self.clock.time()
        self.store.upsert_record(rec, now)
        self.store.relay_enqueue("record", rec["id"], now)
        n = len(rec.get("stations") or [])
        self.log_event("scan", "done", "info", rec["id"], "手動スキャン完了",
                       f"{len(rec['settings']['freqs'])}chを見て{n}局", rec.get("origin"),
                       {"record": rec["id"], "request_id": rec.get("request_id")})
        rid = rec.get("request_id")
        if rid:
            self.command_status[rid] = {"state": "done", "record": rec["id"], "at": now}
        for f in self.on_record:
            f(rec)

    def _write_channels(self, now_mono: float) -> None:
        ts = self.clock.time()
        self.logs.channels(ts, channel_rows(self._occ(), now_mono))
        own = []
        for b in self.cfg.own.bssids:
            st = self.mon.analyzer.table.get(b)
            if st is not None:
                own.append({"bssid": b, "ssid": st.ssid, "freq": st.primary_freq, "rssi": st.rssi,
                            "rssi_avg": None if st.rssi_avg is None else round(st.rssi_avg, 1),
                            "seen_ago_s": round(now_mono - st.last_seen_mono, 1)})
        if own:
            write_own(self.logs, ts, own)

    def _occ(self) -> OccupancyContext:
        return OccupancyContext(self.cfg, self.mon.analyzer, self.mon.scheduler, self.mon.liveness.state)

    # ------------------------------------------------------------ 冪等
    def _idempotent(self, request_id: str | None, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if request_id is not None:
            if not isinstance(request_id, str) or not (1 <= len(request_id) <= 80) or not re.match(r"^[A-Za-z0-9_\-]+$", request_id):
                raise ControlError(400, "bad_request_id", "request_id が不正です")
            with self.lock:
                if self.store.request_seen(request_id):
                    prev = self.store.request_result(request_id)
                    return {**(prev or {}), "duplicate": True}
                res = fn()
                self.store.request_save(request_id, res, self.clock.time())
                return res
        return fn()

    # ------------------------------------------------------------ 手動スキャン
    def manual_scan(self, bands: list[str] | None = None, freqs: list[int] | None = None, confirm: bool = False,
                    origin: str = "desktop", request_id: str | None = None) -> dict[str, Any]:
        _origin(origin)
        if bands is not None and (not isinstance(bands, list) or any(b not in ch.BANDS for b in bands)):
            raise ControlError(400, "bad_bands", "帯域は 2.4 / 5 / 6 のどれかです")
        if freqs is not None and (not isinstance(freqs, list) or any(not ch.is_known_freq(f) for f in freqs)):
            raise ControlError(400, "bad_freqs", "chが不正です（日本で使える57chのMHz）")
        if freqs is not None and len(freqs) > 57:
            raise ControlError(400, "bad_freqs", "chが多すぎます")

        def run() -> dict[str, Any]:
            try:
                m = self.mon.scheduler.request_manual(freqs, origin, bool(confirm), request_id, bands)
            except Busy as e:
                raise ControlError(409, "busy", str(e)) from None
            except NeedsConfirm as e:
                raise ControlError(409, "needs_confirm", str(e)) from None
            except SchedulerError as e:
                raise ControlError(400, "invalid", str(e)) from None
            self.log_event("scan", "start", "info", m.id, "手動スキャン開始",
                           f"{len(m.freqs)}ch・約{m.remaining_s:.0f}秒（{'アプリ' if origin == 'app' else '画面'}から）", origin,
                           {"freqs": m.freqs, "request_id": request_id, "mode": self.mon.scheduler.mode})
            return {"id": m.id, "total": len(m.freqs), "seconds": m.remaining_s, "state": m.state}

        return self._idempotent(request_id, run)

    def scan_status(self, scan_id: str) -> dict[str, Any]:
        st = self.mon.scheduler.manual_status()
        if st and st["id"] == scan_id:
            if st["state"] == "done":
                st["record"] = f"scan-{scan_id}"
            return st
        rec = self.store.get_record(f"scan-{scan_id}")
        if rec:
            return {"id": scan_id, "state": "done", "record": rec["id"], "total": len(rec["settings"]["freqs"]),
                    "done": len(rec["settings"]["freqs"]), "remaining_s": 0}
        raise ControlError(404, "not_found", "そのスキャンはありません")

    # ------------------------------------------------------------ 巡回の帯域ON/OFF
    def bands(self) -> dict[str, Any]:
        s = self.mon.scheduler
        block = set(s.block.freqs)
        out: dict[str, Any] = {}
        for b in ch.BANDS:
            out[b] = {
                "label": ch.BAND_LABEL[b], "excluded": b in s.excluded_bands,
                "channels": [{"freq": f, "ch": ch.BY_FREQ[f].number, "excluded": s.is_excluded(f),
                              "locked": f in block, "own": f == s.own_freq} for f in ch.FREQS_BY_BAND[b]],
            }
        return {"bands": out, "excluded_bands": sorted(s.excluded_bands), "excluded_freqs": sorted(s.excluded_freqs),
                "own_block": sorted(block), "allocation": s.allocation() if s.mode != "PRE" else {}}

    def set_bands(self, excluded_bands: Any, excluded_freqs: Any, origin: str = "desktop",
                  request_id: str | None = None) -> dict[str, Any]:
        _origin(origin)
        if not isinstance(excluded_bands, list) or not all(isinstance(b, str) for b in excluded_bands):
            raise ControlError(400, "bad_bands", "excluded_bands は帯域の並びです")
        if not isinstance(excluded_freqs, list) or not all(isinstance(f, int) and not isinstance(f, bool) for f in excluded_freqs):
            raise ControlError(400, "bad_freqs", "excluded_freqs はMHzの並びです")
        if len(excluded_freqs) > 57:
            raise ControlError(400, "bad_freqs", "chが多すぎます")

        def run() -> dict[str, Any]:
            s = self.mon.scheduler
            try:
                bset, fset = s.validate_exclusions(excluded_bands, excluded_freqs)
            except SchedulerError as e:
                raise ControlError(400, "invalid", str(e)) from None
            before = (sorted(s.excluded_bands), sorted(s.excluded_freqs))
            s.set_exclusions(bset, fset)
            self.mon.overrides.update(excluded_bands=sorted(bset), excluded_freqs=sorted(fset))
            after = (sorted(bset), sorted(fset))
            if before != after:
                msg = "外した帯域：" + ("、".join(ch.BAND_LABEL[b] for b in after[0]) or "なし")
                if after[1]:
                    msg += "／外したch：" + "、".join(ch.label(f) for f in after[1][:12]) + ("…" if len(after[1]) > 12 else "")
                self.log_event("bands", "set", "info", None, "巡回の帯域ON/OFF", msg, origin,
                               {"before": before, "after": after, "request_id": request_id})
            return {"excluded_bands": after[0], "excluded_freqs": after[1]}

        return self._idempotent(request_id, run)

    # ------------------------------------------------------------ モード・自ch・タグ（画面だけ）
    def set_mode(self, mode: str, origin: str = "desktop") -> dict[str, Any]:
        if origin != "desktop":
            raise ControlError(403, "desktop_only", "モードの切替はデスクトップの画面だけです")
        s = self.mon.scheduler
        before = s.mode
        try:
            s.set_mode(mode)
        except SchedulerError as e:
            raise ControlError(400, "invalid", str(e)) from None
        self.mon.overrides.update(mode=mode)
        if before != mode:
            self.log_event("mode", "set", "info", None, "モードの切替", f"{before} → {mode}", origin)
        return {"mode": mode}

    def set_own(self, freq: Any, width: Any, origin: str = "desktop", why: str | None = None) -> dict[str, Any]:
        if origin not in ("desktop", "auto"):
            raise ControlError(403, "desktop_only", "自chの変更はデスクトップの画面だけです")
        try:
            f = ch.parse_channel_ref(freq)
        except ValueError as e:
            raise ControlError(400, "bad_channel", str(e)) from None
        if width not in ch.valid_widths(f):
            raise ControlError(400, "bad_width", f"{ch.label(f)} で使える幅は {ch.valid_widths(f)} です")
        before = (self.cfg.own.channel, self.cfg.own.width)
        self.cfg.own.channel = f
        self.cfg.own.width = width
        s = self.mon.scheduler
        s.set_own(f)
        self.mon.detector.set_own(f, width)
        self.mon.overrides.update(own_channel=f, own_width=width,
                                  excluded_freqs=sorted(s.excluded_freqs))
        self.log_event("own", "set" if origin == "desktop" else "follow", "info", None,
                       "自chの変更" if origin == "desktop" else "自chを自APに合わせた",
                       f"{ch.label(before[0])} {before[1]}MHz → {ch.label(f)} {width}MHz" + (f"（{why}）" if why else ""), origin)
        if origin == "desktop":
            self.follower.hold_current(self.mon.analyzer)  # 手で決めた自chを、自APが動くまで自動で変えない
            self.own_follow_pending = None
        return {"own": f, "width": width, "block": s.block.label}

    # ------------------------------------------------------------ 自APのchに自chを合わせる（follow.py）
    def set_own_follow(self, enabled: Any, origin: str = "desktop") -> dict[str, Any]:
        if origin != "desktop":
            raise ControlError(403, "desktop_only", "自動で合わせるかどうかはデスクトップの画面だけで変えられます")
        if not isinstance(enabled, bool):
            raise ControlError(400, "bad_value", "enabled は true か false です")
        self.mon.overrides.update(own_follow=enabled)
        if not enabled:
            self.own_follow_pending = None
        self.log_event("own", "follow_setting", "info", None, "自APのchに自動で合わせる", "オン" if enabled else "オフ", origin)
        return self.follow_info()

    def follow_info(self) -> dict[str, Any]:
        last = self.own_followed
        recent = last if last and self.clock.time() - last["at"] < FOLLOW_SHOW_S else None
        return {"enabled": bool(self.mon.overrides.get("own_follow")), "last": recent, "pending": self.own_follow_pending}

    def _follow_tick(self, now: float) -> None:
        if not self.mon.overrides.get("own_follow"):
            return
        mon = self.mon
        d: FollowDecision | None = self.follower.check(mon.analyzer, mon.scheduler, self.cfg.own.width, now)
        if d is None:
            self.own_follow_pending = None
            return
        if d.action == "ambiguous":
            key = [(c["freq"], c["width"]) for c in d.choices]
            if self.own_follow_pending is None or self.own_follow_pending["key"] != key:
                self.own_follow_pending = {"key": key, "choices": d.choices, "since": self.clock.time()}
                text = "、".join(f"{c['label']}・{c['width']}MHz（{'、'.join(s['ssid'] or s['bssid'] for s in c['stations'])}）"
                                for c in d.choices)
                self.log_event("own", "follow_ambiguous", "warn", None, "自APが別々のchにいます",
                               f"{text}。どれに自chを合わせるか、PCの画面で選んでください", "auto")
                if self.notifier:
                    self.notifier.notify_info("自APが別々のchにいます",
                                              f"{text}\nどれに自chを合わせるか、PCの画面で選んでください", "warn")
            return
        assert d.freq is not None and d.width is not None
        before = (self.cfg.own.channel, self.cfg.own.width)
        st = d.stations[0]
        name = st["ssid"] or st["bssid"]
        reserved = d.freq in self.mon.own_reserved
        why = f"自AP {name} が {ch.label(d.freq)} に移った" + ("" if reserved else "・予約外のch")
        self.set_own(d.freq, d.width, "auto", why)
        self.follower.last_change_mono = now
        self.own_follow_pending = None
        self.own_followed = {"at": self.clock.time(), "from": before[0], "from_label": ch.label(before[0]),
                             "from_width": before[1], "to": d.freq, "to_label": ch.label(d.freq), "to_width": d.width,
                             "ap": name, "bssid": st["bssid"], "reserved": reserved,
                             "reserved_labels": [ch.label(f) for f in sorted(self.mon.own_reserved)]}
        if self.notifier:
            title = "自chを自APに合わせました" + ("" if reserved else "（予約外のch）")
            body = (f"自AP {name} が {ch.label(d.freq)}・{d.width}MHz に移ったので、自chを "
                    f"{ch.label(before[0])}・{before[1]}MHz から合わせました。")
            if not reserved:
                body += f"\n移った先は自チームの予約ch（{'、'.join(self.own_followed['reserved_labels'])}）ではありません。"
            self.notifier.notify_info(title, body, "info" if reserved else "warn")

    def tag(self, bssid: Any, team: Any, origin: str = "desktop") -> dict[str, Any]:
        if not isinstance(bssid, str) or not BSSID_RE.match(bssid):
            raise ControlError(400, "bad_bssid", "BSSIDが不正です")
        if team is not None and (not isinstance(team, str) or not (1 <= len(team.strip()) <= 40)):
            raise ControlError(400, "bad_team", "チーム名は1〜40文字です")
        team = team.strip() if isinstance(team, str) else None
        self.store.set_tag(bssid, team, self.clock.time(), origin)
        self.mon.classifier.set_tag(bssid, team)
        self.mon.classifier.classify_all(self.mon.analyzer.table)
        self.log_event("tag", "set" if team else "clear", "info", bssid, "チーム名のタグ",
                       f"{bssid} → {team or '（外した）'}", origin)
        return {"bssid": bssid, "team": team}

    # ------------------------------------------------------------ 自チームのAP（デスクトップの画面だけ）
    def set_own_ap(self, bssids: Any, ssids: Any, origin: str = "desktop") -> dict[str, Any]:
        """自チームのAPを、MACアドレス（BSSID）かSSIDで登録する。

        MACアドレスが1つでもあれば、そのAPだけが自チーム（同じSSIDの別の局は「SSIDのなりすまし」の警報）。
        SSIDだけなら、その名前の局はすべて自チーム（ルータを替えても追従する）。
        """
        if origin != "desktop":
            raise ControlError(403, "desktop_only", "自チームのAPはデスクトップの画面だけで変えられます")
        if not isinstance(bssids, list) or not isinstance(ssids, list):
            raise ControlError(400, "bad_own_ap", "bssids と ssids はリストで送ってください")
        if len(bssids) > OWN_AP_MAX or len(ssids) > OWN_AP_MAX:
            raise ControlError(400, "bad_own_ap", f"MACアドレスとSSIDは、それぞれ{OWN_AP_MAX}個までです")
        try:
            bs = list(dict.fromkeys(normalize_mac(b) for b in bssids))
        except ValueError as e:
            raise ControlError(400, "bad_bssid", str(e)) from None
        if any(not valid_ssid(x) for x in ssids):
            raise ControlError(400, "bad_ssid", "SSIDは1〜32バイトです")
        ss = list(dict.fromkeys(ssids))
        before = self.own_ap()
        own = self.cfg.own
        own.bssids, own.ssids = bs, ss
        self.mon.overrides.update(own_bssids=bs, own_ssids=ss)
        self.mon.classifier.set_own_bssids(bs)
        self.mon.analyzer.set_own_addrs(bs + list(own.station_macs))
        self.mon.classifier.classify_all(self.mon.analyzer.table)
        after = self.own_ap()
        self.log_event("own", "ap", "info", None, "自チームのAP",
                       f"{_own_ap_text(before)} → {_own_ap_text(after)}", origin)
        return after

    def own_ap(self) -> dict[str, Any]:
        own = self.cfg.own
        match = "bssid" if own.bssids else ("ssid" if own.ssids else "none")
        return {"bssids": list(own.bssids), "ssids": list(own.ssids), "match": match}

    # ------------------------------------------------------------ 機種の覚え帳（デスクトップの画面だけ）
    def remember_model(self, fp: Any, label: Any, bssid: Any = None, origin: str = "desktop") -> dict[str, Any]:
        """指紋（model_fp）に機種の名前を覚える。label が空なら忘れる。同じ指紋の局すべてに出る。"""
        if origin != "desktop":
            raise ControlError(403, "desktop_only", "機種の覚え帳はデスクトップの画面だけで変えられます")
        if not isinstance(fp, str) or not FP_RE.match(fp):
            raise ControlError(400, "bad_fp", "指紋が不正です（10桁の16進）")
        if label is not None and (not isinstance(label, str) or not (1 <= len(label.strip()) <= 60)):
            raise ControlError(400, "bad_label", "機種の名前は1〜60文字です")
        if bssid is not None and (not isinstance(bssid, str) or not BSSID_RE.match(bssid)):
            raise ControlError(400, "bad_bssid", "BSSIDが不正です")
        label = label.strip() if isinstance(label, str) and label.strip() else None
        self.mon.classifier.book.set(fp, label, bssid, self.clock.time())
        self.mon.classifier.classify_all(self.mon.analyzer.table)
        self.log_event("model", "set" if label else "clear", "info", fp, "機種の覚え帳",
                       f"指紋 {fp} → {label or '（忘れた）'}", origin)
        return {"fp": fp, "label": label}

    def model_book(self) -> dict[str, Any]:
        c = self.mon.classifier
        sts = self.mon.analyzer.table.all()
        entries = []
        for fp, e in sorted(c.book.entries.items(), key=lambda kv: -kv[1].get("at", 0)):
            entries.append({"fp": fp, "label": e.get("label"), "at": e.get("at"), "examples": e.get("examples", []),
                            "now": sum(1 for s in sts if s.model_fp == fp)})
        return {"entries": entries, "export": c.book.export(), "rules": len(c.model_rules)}

    # ------------------------------------------------------------ 記録とラベル
    @staticmethod
    def clean_label(label: Any) -> dict[str, Any]:
        if not isinstance(label, dict):
            raise ControlError(400, "bad_label", "ラベルが不正です")
        loc = label.get("location")
        if loc is not None and (not isinstance(loc, str) or len(loc) > MAX_LOCATION):
            raise ControlError(400, "bad_label", f"場所は{MAX_LOCATION}文字以内です")
        tags = label.get("tags", [])
        if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(
                not isinstance(t, str) or not (1 <= len(t) <= MAX_TAG_LEN) for t in tags):
            raise ControlError(400, "bad_label", f"タグは{MAX_TAGS}個まで、1つ{MAX_TAG_LEN}文字までです")
        memo = label.get("memo", "")
        if not isinstance(memo, str) or len(memo) > MAX_MEMO:
            raise ControlError(400, "bad_label", f"メモは{MAX_MEMO}文字以内です")
        return {"location": (loc.strip() or None) if isinstance(loc, str) else None,
                "tags": list(dict.fromkeys(t.strip() for t in tags if t.strip())), "memo": memo}

    def list_records(self, **filters: Any) -> list[dict[str, Any]]:
        return self.store.list_records(**filters)

    def get_record(self, id_: str) -> dict[str, Any]:
        r = self.store.get_record(id_)
        if r is None:
            raise ControlError(404, "not_found", "その記録はありません")
        return r

    def update_label(self, id_: str, label: Any, origin: str = "desktop", edited_at: float | None = None,
                     request_id: str | None = None) -> dict[str, Any]:
        _origin(origin)
        clean = self.clean_label(label)
        now = self.clock.time()
        if edited_at is None:
            edited_at = now
        if not isinstance(edited_at, (int, float)) or isinstance(edited_at, bool) or not math.isfinite(edited_at):
            raise ControlError(400, "bad_time", "編集時刻が不正です")

        def run() -> dict[str, Any]:
            applied, rec = self.store.update_label(id_, clean, float(edited_at), origin, now)
            if rec is None:
                raise ControlError(404, "not_found", "その記録はありません")
            if applied:
                self.store.relay_enqueue("record", id_, now)
                self.log_event("label", "set", "info", id_, "ラベルの変更",
                               f"{id_}：場所={clean['location'] or '—'} タグ={','.join(clean['tags']) or '—'}", origin)
            return {"applied": applied, "label": rec["label"]}

        return self._idempotent(request_id, run)

    def ingest_record(self, rec: Any, origin: str = "app") -> dict[str, Any]:
        """アプリの歩測の記録を取り込む（同じIDなら置き換え＝冪等）。"""
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not re.match(r"^[A-Za-z0-9_\-]{1,64}$", rec["id"]):
            raise ControlError(400, "bad_record", "記録のIDが不正です")
        if rec.get("kind") != "walk":
            raise ControlError(400, "bad_record", "アプリから受けるのは歩測の記録だけです")
        if not isinstance(rec.get("created_at"), (int, float)):
            raise ControlError(400, "bad_record", "created_at が不正です")
        sts = rec.get("stations")
        if not isinstance(sts, list) or len(sts) > 2000:
            raise ControlError(400, "bad_record", "stations が不正です")
        label = self.clean_label(rec.get("label") or {})
        label["updated_at"] = (rec.get("label") or {}).get("updated_at")
        label["updated_by"] = "app"
        bands = sorted({b for b in (ch.band_of_freq(s.get("freq", 0)) for s in sts if isinstance(s, dict)) if b})
        body = {k: rec[k] for k in ("id", "kind", "created_at", "device", "settings", "stations", "note") if k in rec}
        body.update({"origin": origin, "bands": bands, "label": label})
        now = self.clock.time()
        new = self.store.upsert_record(body, now)
        if new:
            self.store.relay_enqueue("record", body["id"], now)
            self.log_event("walk", "ingest", "info", body["id"], "スマホの結果を受け取った",
                           f"{len(sts)}局・場所={label['location'] or '—'}", origin)
        return {"id": body["id"], "new": new}

    def export(self, fmt: str, **filters: Any) -> tuple[str, bytes, str]:
        metas = self.store.list_records(limit=5000, **filters)
        recs = self.store.all_records_full([m["id"] for m in metas])
        stamp = jst_str(self.clock.time()).replace("/", "").replace(":", "").replace(" ", "-")
        if fmt == "json":
            return ("application/json", json.dumps(recs, ensure_ascii=False, indent=1).encode("utf-8"),
                    f"wifiwatch-records-{stamp}.json")
        if fmt != "csv":
            raise ControlError(400, "bad_format", "format は json か csv です")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["record_id", "kind", "created_at_utc", "created_at_jst", "device", "origin", "location", "tags",
                    "memo", "bssid", "ssid", "band", "freq", "ch", "width", "rssi", "standard", "security", "model",
                    "team", "station_kind"])
        for r in recs:
            lab = r.get("label") or {}
            base = [r["id"], r["kind"], utc_iso(r["created_at"]), jst_str(r["created_at"]), r.get("device") or "",
                    r.get("origin") or "", lab.get("location") or "", "|".join(lab.get("tags") or []), lab.get("memo") or ""]
            sts = r.get("stations") or []
            if not sts:
                w.writerow(base + [""] * 12)
            for s in sts:
                f = s.get("freq")
                w.writerow(base + [s.get("bssid", ""), s.get("ssid") or "", ch.band_of_freq(f) if f else "", f or "",
                                   s.get("ch") or (ch.freq_to_number(f) if f else ""), s.get("width") or "",
                                   s.get("rssi") if s.get("rssi") is not None else "", s.get("standard") or "",
                                   s.get("security") or "", s.get("model_guess") or s.get("model") or "",
                                   s.get("team") or "", s.get("kind") or ""])
        return ("text/csv; charset=utf-8", ("﻿" + buf.getvalue()).encode("utf-8"), f"wifiwatch-records-{stamp}.csv")

    # ------------------------------------------------------------ 状態（画面・アプリ）
    def _alerting(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for a in self.mon.alerts.active():
            for pid in a.parties:
                out.setdefault(pid, []).append(a.type)
        return out

    def stations(self, include_gone_s: float = 600.0) -> list[dict[str, Any]]:
        now = self.clock.monotonic()
        s = self.mon.scheduler
        alerting = self._alerting()
        out = []
        for st in self.mon.analyzer.table.all():
            age = now - st.last_seen_mono
            win = s.presence_window(st.primary_freq)
            if age > win + include_gone_s:
                continue
            out.append({
                "bssid": st.bssid, "ssid": st.ssid, "hidden": st.hidden, "band": st.band, "freq": st.primary_freq,
                "ch": ch.freq_to_number(st.primary_freq), "width": st.width, "width_label": st.width_label,
                "spans": [[x.lo, x.hi] for x in st.spans], "rssi": None if st.rssi_avg is None else round(st.rssi_avg),
                "kind": st.kind or "unknown", "team": st.team, "model": st.model_guess, "signature": st.signature,
                "model_fp": st.model_fp,
                "standard": st.standard, "security": st.security, "age_s": round(age, 1),
                "present": is_present(st, self.mon.analyzer, s, now), "missed": missed_visits(st, self.mon.analyzer),
                "window_s": round(win), "alerts": alerting.get(st.bssid, []),
                "new": st.bssid in self.mon.detector.newcomers, "dup_beacon": st.dup_beacon,
                "first_seen": st.first_seen, "last_seen": st.last_seen,
            })
        return out

    def station_detail(self, bssid: str) -> dict[str, Any]:
        st = self.mon.analyzer.table.get(bssid)
        if st is None:
            raise ControlError(404, "not_found", "その局はありません")
        d = st.to_dict()
        d["alerts"] = [a.to_dict() for a in self.mon.alerts.active() if bssid in a.parties]
        d["age_s"] = round(self.clock.monotonic() - st.last_seen_mono, 1)
        d["window_s"] = round(self.mon.scheduler.presence_window(st.primary_freq))
        d["missed"] = missed_visits(st, self.mon.analyzer)
        d["present"] = is_present(st, self.mon.analyzer, self.mon.scheduler, self.clock.monotonic())
        d["clients"] = [c.to_dict() for c in list(self.mon.analyzer.table.clients.values()) if c.bssid == bssid][:50]
        book = self.mon.classifier.book.get(st.model_fp)
        d["model_book"] = book.get("label") if book else None
        d["same_fp"] = sum(1 for o in self.mon.analyzer.table.all() if st.model_fp and o.model_fp == st.model_fp)
        return d

    def record_for_screen(self, id_: str) -> dict[str, Any]:
        """画面で記録を開くとき：スマホから届いた記録の局に、PCの解析での分類（自チーム・他高専など）を付ける
        （グラフの色と記号をPCで受けた局と同じにする）。記録そのもの（正本）は変えない。"""
        r = dict(self.get_record(id_))
        if r.get("kind") == "walk" and any(isinstance(x, dict) and x.get("ies_hex") for x in r.get("stations") or []):
            got = {d["bssid"]: d for d in self.record_stations(id_, [x["bssid"] for x in r["stations"] if isinstance(x, dict) and x.get("bssid")])}
            sts = []
            for x in r["stations"]:
                d = got.get(str(x.get("bssid", "")).lower()) if isinstance(x, dict) else None
                if d is not None and d.get("has_ies"):
                    x = {**x, "kind": d.get("kind"), "team": d.get("team"), "model_guess": d.get("model_guess"),
                         "signature": d.get("signature"), "security": d.get("security"), "hidden": d.get("hidden"),
                         "spans": d.get("spans") or x.get("spans"), "width_label": d.get("width_label") or x.get("width_label")}
                sts.append(x)
            r["stations"] = sts
        return r

    def record_stations(self, rid: str, bssids: list[str]) -> list[dict[str, Any]]:
        """記録の中の局の詳細（画面の右の「局の詳細」に出す）。スマホから届いた記録は局ごとのIEを持っているので、
        PCが受けた局と同じ解析（IEの読み取り・RNRと並びでの紐付け・分類・機種推定）にかける。IEの無い記録
        （PCの手動スキャン）は、記録に書いてある分類をそのまま出す。強さは記録を取ったときの値。"""
        rec = self.store.get_record(rid)
        if rec is None:
            raise ControlError(404, "not_found", "その記録はありません")
        want = [normalize_mac(b) for b in bssids[:50]]
        sts = [x for x in rec.get("stations") or [] if isinstance(x, dict) and isinstance(x.get("bssid"), str)]
        table = StationTable()
        at = float(rec.get("created_at") or 0)
        for x in sts:  # 記録の全部の局を入れる（RNR・BSSIDの並びの紐付けに、ほかの局も要る）
            try:
                raw = bytes.fromhex(x.get("ies_hex") or "")
                freq = int(x.get("freq") or 0)
            except (TypeError, ValueError):
                continue
            if raw and ch.band_of_freq(freq):
                table.update_beacon(x["bssid"].lower(), freq, x.get("rssi"), ies.parse_bss(raw, 0, len(raw)), at, 0.0)
        self.mon.classifier.classify_all(table)
        live = self.mon.analyzer.table
        lab = rec.get("label") or {}
        out = []
        for b in want:
            x = next((q for q in sts if q["bssid"].lower() == b), None)
            if x is None:
                continue
            st = table.get(b)
            if st is not None:
                d = st.to_dict()
            else:  # IEの無い記録：書いてあるものだけ
                f = int(x.get("primary_freq") or x.get("freq") or 0)
                w = int(x.get("width") or 20)
                d = {"bssid": b, "ssid": x.get("ssid"), "hidden": bool(x.get("hidden")), "band": x.get("band") or ch.band_of_freq(f),
                     "freq": f, "ch": ch.freq_to_number(f), "width": w, "width_label": str(x.get("width_label") or w),
                     "spans": x.get("spans") or [[f - w / 2, f + w / 2]], "standard": x.get("standard"), "security": x.get("security"),
                     "pmf_required": False, "kind": x.get("kind") or "unknown", "team": x.get("team"), "team_source": None,
                     "signature": x.get("signature"), "model_guess": x.get("model_guess") or x.get("model"), "model_fp": None,
                     "hints": [], "linked": [], "siblings": [], "rnr": [], "dup_beacon": bool(x.get("dup_beacon")), "suspect": False}
            book = self.mon.classifier.book.get(d.get("model_fp")) if d.get("model_fp") else None
            d.update({"rssi": x.get("rssi"), "rssi_avg": None, "rssi_max": None, "rssi_samples": x.get("rssi_samples"),
                      "alerts": [], "clients": [], "model_book": book.get("label") if book else None,
                      "has_ies": st is not None, "live": live.get(b) is not None,
                      "record": {"id": rec.get("id"), "kind": rec.get("kind"), "created_at": rec.get("created_at"),
                                 "device": rec.get("device"), "origin": rec.get("origin"), "location": lab.get("location")}})
            out.append(d)
        out.sort(key=lambda d: -(d["rssi"] if isinstance(d["rssi"], (int, float)) else -200))
        return out

    def clients(self) -> list[dict[str, Any]]:
        now = self.clock.time()
        rows = [c.to_dict() for c in list(self.mon.analyzer.table.clients.values()) if now - c.last_seen < 900]
        rows.sort(key=lambda c: -c["last_seen"])
        return rows[:500]

    def own_series(self) -> list[dict[str, Any]]:
        """自chの活動量とリトライ率（10秒ごと、直近10分）。"""
        s = self.mon.scheduler
        key = self.mon.detector.own_key()
        now_m = self.clock.monotonic()
        now = self.clock.time()
        out = []
        for b in self.mon.analyzer.stats.window(key, now_m, 600):
            out.append({"t": round(now - (now_m - b.start), 1), "receive_s": round(b.receive_s, 2),
                        "rate": round(b.frames / b.receive_s, 1) if b.receive_s > 0.2 else None,
                        "retry": round(b.retries / b.retry_eligible, 3) if b.retry_eligible >= 10 else None})
        return out

    def state(self) -> dict[str, Any]:
        now_m = self.clock.monotonic()
        now = self.clock.time()
        mon = self.mon
        s = mon.scheduler
        occ = self._occ()
        sts = present_stations(mon.analyzer, s, now_m)
        alerts = [a.to_dict() for a in mon.alerts.active()]
        counts = {"crit": sum(1 for a in alerts if a["level"] == "crit"),
                  "warn": sum(1 for a in alerts if a["level"] == "warn")}
        series = self.own_series()
        recent = [x for x in series if x["rate"] is not None][-3:]
        st = s.status()
        return {
            "now": now, "device": self.cfg.device_name, "started_at": self.started_at,
            "mode": s.mode, "mode_elapsed_s": st["mode_elapsed_s"], "pre_rounds": s.pre_rounds,
            "own": {
                "team": self.cfg.own.team, "freq": s.own_freq, "label": ch.label(s.own_freq), "width": self.cfg.own.width,
                "block": st["own_block"], "spare": [{"freq": f, "label": ch.label(f)} for f in self.cfg.own.spare],
                "ratio": st["own_ratio"], "series": series,
                "rate": recent[-1]["rate"] if recent else None,
                "retry": next((x["retry"] for x in reversed(series) if x["retry"] is not None), None),
                "bssids": list(self.cfg.own.bssids),
                "ap": self.own_ap(),
                "follow": self.follow_info(),
                "reserved": [{"freq": f, "label": ch.label(f)} for f in sorted(self.mon.own_reserved)],
            },
            "rx": mon.liveness.status(),
            "alerts": alerts, "alert_counts": counts,
            "manual": s.manual_status(),
            "allocation": st["allocation"], "excluded_bands": st["excluded_bands"], "excluded_freqs": st["excluded_freqs"],
            "channels": channel_rows(occ, now_m, sts),
            "stations": self.stations(),
            "suggestions": suggestions(occ, now_m, sts),
            "calibration": mon.calibration or self.store.kv_get("calibration"),
            "notify": self.notifier.info() if self.notifier else None,
            "relay": self.relay_info,
            "teams": [{"name": t.name, "channels": sorted(set(t.channels) | set(t.spare))} for t in self.cfg.teams],
            "locations": list(self.cfg.locations),
            "thresholds": {"strong_rssi_dbm": self.cfg.thresholds.strong_rssi_dbm,
                           "problem_min": self.cfg.thresholds.problem_channel_min_strong},
            "counters": dict(mon.analyzer.counters),
            "radio": {"kind": self.cfg.radio.kind, "scan_only": bool(getattr(mon.radio, "scan_only", False)),
                      "unreceivable": list(getattr(mon.radio, "unreceivable", None) or []),
                      "disabled": list(getattr(mon.radio, "disabled_freqs", None) or [])},
            "clients": sum(1 for c in list(mon.analyzer.table.clients.values()) if now - c.last_seen < 900),
        }

    # ------------------------------------------------------------ アプリが頼んだときだけ置く、PCが今見ている局
    def pc_view(self, limit: int = 200_000) -> dict[str, Any]:
        """PCの画面のアナライザーと同じ中身（局ごとの主ch・帯域・強さ・分類・いるか）と、chの状態（見ていないchの区別）。
        アプリが頼んだときに1回だけ中継に置く（常には配らない）。大きすぎるときは、いない局→弱い局の順に落とす。"""
        sts = self.stations(include_gone_s=120.0)
        sts.sort(key=lambda q: (not q["present"], -(q["rssi"] if q["rssi"] is not None else -200)))
        rows = [{"b": q["bssid"], "s": q["ssid"], "h": q["hidden"], "f": q["freq"], "w": q["width_label"],
                 "sp": q["spans"], "r": q["rssi"], "k": q["kind"], "t": q["team"], "m": q["model"], "x": q["standard"],
                 "p": q["present"], "a": bool(q["alerts"]), "ag": round(q["age_s"])} for q in sts]
        out: dict[str, Any] = {"v": 1, "at": round(self.clock.time(), 1), "state": self.compact_state(),
                               "strong": self.cfg.thresholds.strong_rssi_dbm, "stations": rows,
                               "scan_only": bool(getattr(self.mon.radio, "scan_only", False))}
        while rows and len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > limit:
            gone = [i for i, q in enumerate(rows) if not q["p"]]
            del rows[gone[-1] if gone else len(rows) - 1]
        out["n_total"] = len(sts)
        return out

    # ------------------------------------------------------------ 中継に置く状態の写し（8KB以下）
    STATE_CODES = {"own": "o", "in_block": "i", "patrol": "p", "excluded": "x", "paused": "z", "scanning": "s",
                   "scan_target": "t", "stopped": "!"}

    def compact_state(self, limit: int = 7600) -> dict[str, Any]:
        now_m = self.clock.monotonic()
        mon = self.mon
        s = mon.scheduler
        st = s.status()
        alerts = mon.alerts.active()
        rows = channel_rows(self._occ(), now_m)
        chans = []
        for r in rows:
            code = self.STATE_CODES.get(r["state"], "p")
            if r["observed_ago"] is None and code in ("p", "i"):
                code = "u"  # まだ一度も見ていない
            chans.append([r["freq"], code, r["n_overlap"], r["max_rssi"], r["n_strong"]])
        cmds = dict(list(self.command_status.items())[-20:])
        out: dict[str, Any] = {
            "v": 1, "at": round(self.clock.time(), 1), "device": self.cfg.device_name,
            "mode": s.mode,
            "own": {"freq": s.own_freq, "label": ch.label(s.own_freq), "width": self.cfg.own.width,
                    "ratio": st["own_ratio"], "block": st["own_block"]["label"], "block_freqs": st["own_block"]["freqs"]},
            "rx": {"state": mon.liveness.state, "reason": mon.liveness.reason[:120]},
            "counts": {"crit": sum(1 for a in alerts if a.level == "crit"), "warn": sum(1 for a in alerts if a.level == "warn")},
            "alerts": [{"level": a.level, "type": a.type, "title": a.title, "message": a.message[:160],
                        "since": round(a.first_epoch)} for a in alerts[:8]],
            "manual": s.manual_status(),
            "bands": {"excluded_bands": sorted(s.excluded_bands), "excluded_freqs": sorted(s.excluded_freqs)},
            "commands": cmds,
            "channels": chans,
        }
        fi = self.follow_info()
        if fi["last"]:  # 自chを自APに合わせた（アプリで知らせる）。あるときだけ付ける
            last = fi["last"]
            out["own"]["followed"] = {"at": round(last["at"]), "from": last["from_label"], "to": last["to_label"],
                                      "ap": last["ap"][:32], "reserved": last["reserved"]}
        if fi["pending"]:
            out["own"]["follow_pending"] = [f"{c['label']}・{c['width']}MHz" for c in fi["pending"]["choices"]][:4]
        unrx = list(getattr(mon.radio, "unreceivable", None) or [])
        if unrx:  # この無線では受けられないch（6GHz非対応の無線など）。アプリで「見ていない」の理由に出す
            out["bands"]["unreceivable"] = unrx
        if out["manual"]:
            out["manual"] = {k: out["manual"][k] for k in ("id", "state", "origin", "request_id", "done", "total", "remaining_s")}
        size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        if size > limit:
            for a in out["alerts"]:
                a["message"] = a["message"][:60]
            out["alerts"] = out["alerts"][:4]
            size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        if size > limit:
            out.pop("channels")
        return out
