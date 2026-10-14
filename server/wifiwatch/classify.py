"""局の分類（誰の、何の機器か／交渉できる相手か）と機種推定。

判定の順（先に決まったものが勝つ）：
  自チーム（BSSID。BSSIDが未設定ならSSID）→ 既知で問題のない局（known_ok_bssids）→ 手動のチーム名タグ
  → configのチーム対応表（BSSID）→ 署名（signatures.json：SSIDのパターン・OUI）→ configのチーム対応表（SSIDのパターン）
  → RNRで紐付いた同じ機器のチーム → 不明
色分け：他高専＝黄（交渉できる）、公式インフラ・放送機材＝赤（交渉できない）。
"""
from __future__ import annotations

import csv
import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .analyzer.stations import Station, StationTable
from .capture.dot11 import is_local_admin
from .config import Config

log = logging.getLogger(__name__)

KINDS = ("own", "known_ok", "team", "official_infra", "broadcast", "unknown")
KIND_LABEL = {
    "own": "自チーム", "known_ok": "既知（問題なし）", "team": "他高専", "official_infra": "公式インフラ",
    "broadcast": "放送機材", "unknown": "不明",
}

# ベンダ固有IEのOUI → チップ・機器の手がかり
VENDOR_IE_HINTS = {
    "00:10:18": "Broadcom", "00:90:4c": "Broadcom(Epigram)", "00:03:7f": "Qualcomm Atheros",
    "8c:fd:f0": "Qualcomm", "00:0c:e7": "MediaTek", "00:0c:43": "Ralink/MediaTek", "00:e0:4c": "Realtek",
    "00:17:f2": "Apple", "00:0b:86": "Aruba", "00:40:96": "Cisco", "00:13:92": "Ruckus", "00:26:86": "Quantenna",
    "f4:f5:e8": "Google", "00:1d:0f": "TP-Link", "00:a0:40": "Apple", "00:16:32": "Samsung",
}

_SSID_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^DIRECT-", re.I), "Wi-Fi Direct"),
    (re.compile(r"^(ESP[_\-]|ESP32|ESP8266|espressif)", re.I), "ESP系マイコン"),
    (re.compile(r"(raspberry|raspi|^rpi)", re.I), "Raspberry Pi"),
    (re.compile(r"(iphone|ipad)", re.I), "スマホのテザリング（iPhone）"),
    (re.compile(r"(androidap|android|galaxy|pixel|xperia|aquos|arrows|oppo|xiaomi|redmi)", re.I),
     "スマホのテザリング（Android）"),
]


@dataclass
class Signature:
    name: str
    ssid: list[str]
    oui: list[str]
    kind: str
    negotiable: bool
    on_6ghz: str  # notify_once / crit / none

    def matches(self, st: Station) -> bool:
        if st.ssid and any(fnmatch.fnmatchcase(st.ssid, p) for p in self.ssid):
            return True
        prefix = st.bssid[:8]
        return any(prefix == o.lower() for o in self.oui)


def load_signatures(path: str | Path) -> list[Signature]:
    p = Path(path)
    if not p.exists():
        log.warning("signatures.json がありません: %s", p)
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for i, s in enumerate(data.get("signatures", [])):
        kind = s.get("kind", "unknown")
        if kind not in ("official_infra", "broadcast", "team", "unknown"):
            raise ValueError(f"signatures[{i}].kind が不正です：{kind}")
        default6 = {"official_infra": "notify_once", "broadcast": "crit"}.get(kind, "none")
        on6 = s.get("on_6ghz", default6)
        if on6 not in ("notify_once", "crit", "none"):
            raise ValueError(f"signatures[{i}].on_6ghz が不正です：{on6}")
        out.append(Signature(s["name"], list(s.get("ssid", [])), [o.lower() for o in s.get("oui", [])], kind,
                             bool(s.get("negotiable", kind == "team")), on6))
    return out


class OuiDb:
    """IEEEの登録（ieee-dataパッケージのCSV）。MA-S → MA-M → MA-L の順で長い方を優先。"""

    def __init__(self, directory: str | Path = "/usr/share/ieee-data"):
        self.tables: list[tuple[int, dict[str, str]]] = []
        d = Path(directory)
        for name, n in (("oui36.csv", 9), ("mam.csv", 7), ("oui.csv", 6)):
            t: dict[str, str] = {}
            p = d / name
            if p.exists():
                with p.open(encoding="utf-8", errors="replace") as f:
                    for row in csv.reader(f):
                        if len(row) >= 3 and row[0].startswith("MA-"):
                            t[row[1].upper()] = row[2].strip()
            self.tables.append((n, t))

    def lookup(self, mac: str) -> str | None:
        h = mac.replace(":", "").upper()
        for n, t in self.tables:
            v = t.get(h[:n])
            if v:
                return v
        return None

    @property
    def loaded(self) -> bool:
        return any(t for _, t in self.tables)


class Classifier:
    def __init__(self, cfg: Config, signatures: list[Signature], oui: OuiDb | None = None,
                 tags: dict[str, str] | None = None):
        self.cfg = cfg
        self.signatures = signatures
        self.oui = oui
        self.tags: dict[str, str] = dict(tags or {})  # BSSID → チーム名（画面での手動タグ）
        self._own_bssids = set(cfg.own.bssids)
        self._known_ok = set(cfg.known_ok_bssids)
        self._team_bssid = {b: t.name for t in cfg.teams for b in t.bssids}
        self._team_ssid = [(p, t.name) for t in cfg.teams for p in t.ssids]

    def set_tag(self, bssid: str, team: str | None) -> None:
        if team:
            self.tags[bssid] = team
        else:
            self.tags.pop(bssid, None)

    def is_own(self, st: Station) -> bool:
        if self._own_bssids:
            return st.bssid in self._own_bssids
        return bool(st.ssid) and st.ssid in self.cfg.own.ssids

    def signature_of(self, st: Station) -> Signature | None:
        for s in self.signatures:
            if s.matches(st):
                return s
        return None

    def classify(self, st: Station, table: StationTable | None = None) -> None:
        team = None
        source = None
        kind = "unknown"
        negotiable: bool | None = None
        sig = None
        if self.is_own(st):
            kind, team, source, negotiable = "own", self.cfg.own.team, "config", None
        elif st.bssid in self._known_ok:
            kind = "known_ok"
        elif st.bssid in self.tags:
            kind, team, source, negotiable = "team", self.tags[st.bssid], "tag", True
        elif st.bssid in self._team_bssid:
            kind, team, source, negotiable = "team", self._team_bssid[st.bssid], "config", True
        else:
            sig = self.signature_of(st)
            if sig is not None:
                kind, negotiable = sig.kind, sig.negotiable
                if sig.kind == "team":
                    team, source = sig.name, "signature"
            else:
                for pat, name in self._team_ssid:
                    if st.ssid and fnmatch.fnmatchcase(st.ssid, pat):
                        kind, team, source, negotiable = "team", name, "ssid", True
                        break
        if kind == "unknown" and table is not None:
            for other in self._linked(st, table):
                if other.team and other.kind == "team":
                    kind, team, source, negotiable = "team", other.team, "rnr", True
                    break
        st.kind = kind
        st.team = team
        st.team_source = source
        st.negotiable = negotiable
        st.signature = sig.name if sig else None
        st.model_guess, st.hints = self.guess(st, table)

    def _linked(self, st: Station, table: StationTable) -> list[Station]:
        out = []
        for b in st.linked:
            o = table.get(b)
            if o is not None:
                out.append(o)
        return out

    def guess(self, st: Station, table: StationTable | None = None) -> tuple[str | None, list[str]]:
        hints: list[str] = []
        wps = st.wps
        if not wps and table is not None:  # 6GHzはWPSが無い。RNRで紐付いた2.4/5GHz側から借りる
            for o in self._linked(st, table):
                if o.wps:
                    wps = o.wps
                    hints.append(f"RNRで{o.band}GHz側（{o.bssid}）と同じ機器")
                    break
        random_mac = is_local_admin(st.bssid)
        vendor = None if random_mac else (self.oui.lookup(st.bssid) if self.oui else None)
        if random_mac:
            hints.append("ランダムMAC（ローカル管理）")
        elif vendor:
            hints.append(f"OUI: {vendor}")
        ssid_hint = None
        if st.ssid:
            for pat, label in _SSID_HINTS:
                if pat.search(st.ssid):
                    ssid_hint = label
                    hints.append(f"SSIDの傾向: {label}")
                    break
        chips = sorted({VENDOR_IE_HINTS[o] for o in st.vendor_ouis if o in VENDOR_IE_HINTS})
        if chips:
            hints.append("ベンダIE: " + ", ".join(chips))
        hints.append(f"規格: 802.11{st.standard}" if st.standard != "legacy" else "規格: 11a/b/g")
        hints.append(f"暗号: {st.security}" + ("（PMF必須）" if st.pmf_required else ""))
        if st.country:
            hints.append(f"国コード: {st.country}")
        if st.bss_color is not None:
            hints.append(f"BSS Color: {st.bss_color}")
        if st.eht_320_cap:
            hints.append("320MHz対応（EHT）")
        model = None
        if wps:
            parts = [wps.get("manufacturer"), wps.get("model_name"), wps.get("model_number")]
            model = " ".join(p for p in parts if p) or None
        if model is None and ssid_hint:
            model = ssid_hint
        if model is None and vendor:
            model = vendor
        if model is None and chips:
            model = f"{chips[0]}系チップ"
        return model, hints

    def link_rnr(self, table: StationTable, stations: Iterable[Station] | None = None) -> None:
        """RNR（IE201）で、2.4/5/6GHzにまたがる同じ機器を紐付ける。"""
        with table.lock:
            sts = list(stations) if stations is not None else table.all()
            for st in sts:
                for r in st.rnr:
                    b = r.get("bssid")
                    if not b or not (r.get("colocated") or r.get("same_ssid")):
                        continue
                    if b not in st.linked:
                        st.linked.append(b)
                    other = table.get(b)
                    if other is not None and st.bssid not in other.linked:
                        other.linked.append(st.bssid)

    def classify_all(self, table: StationTable) -> None:
        self.link_rnr(table)
        sts = table.all()
        for st in sts:
            self.classify(st, None)
        for st in sts:  # 2回目：RNRで紐付いた局のチーム・WPSを使う
            self.classify(st, table)

    def summary(self, st: Station) -> dict[str, Any]:
        return {"kind": st.kind, "label": KIND_LABEL.get(st.kind or "unknown"), "team": st.team,
                "negotiable": st.negotiable, "signature": st.signature, "model": st.model_guess}
