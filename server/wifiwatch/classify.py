"""局の分類（誰の、何の機器か／交渉できる相手か）と機種推定。

判定の順（先に決まったものが勝つ）：
  自チーム（BSSID。BSSIDが未設定ならSSID）→ 既知で問題のない局（known_ok_bssids）→ 手動のチーム名タグ
  → configのチーム対応表（BSSID）→ 署名（signatures.json：SSIDのパターン・OUI）→ configのチーム対応表（SSIDのパターン）
  → RNRで紐付いた同じ機器・BSSIDの並びで分かる同じ機器（非公開SSIDの双子）のチーム → 不明
色分け：他高専＝黄（交渉できる）、公式インフラ・放送機材＝赤（交渉できない）。

機種推定の順：覚え帳で「この局は◯◯」と覚えた局 → WPS（機器が名乗るメーカー・型番）→ 機種の表のSSID（初期設定の名前）
  → 覚え帳の同じ指紋 → 機種の表のほか（指紋・ベンダIE・OUI）→ OUIのメーカー → ベンダIEのチップ。
指紋（model_fp）は、IEの並びと能力のバイト列から作る（capture/ies.py）。見えた局の情報は外に送らない。
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
from .config import Config, atomic_write_json

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

MODEL_CATEGORIES = ("phone", "mobile_router", "home_router", "wifi_direct", "maker", "video_tx", "other")


@dataclass
class ModelRule:
    """機種の表（models.json）の1行。SSID・指紋・ベンダIE・OUIのどれかが当たれば、その機種とみなす。"""
    label: str
    category: str
    ssid: list[re.Pattern[str]]
    fp: set[str]
    vendor_ie: set[str]
    oui: set[str]

    def match(self, st: Station) -> tuple[str, str] | None:
        """当たれば（ラベル, 何で当たったか）。"""
        if st.model_fp and st.model_fp in self.fp:
            return self.label.replace("{model}", ""), f"指紋 {st.model_fp}"
        if st.ssid:
            for pat in self.ssid:
                m = pat.search(st.ssid)
                if m:
                    model = (m.groupdict().get("model") or "").strip()
                    return self.label.replace("{model}", model), "SSID"
        if self.vendor_ie and any(o in self.vendor_ie for o in st.vendor_ouis):
            return self.label, "ベンダIE"
        if self.oui and st.bssid[:8] in self.oui:
            return self.label, "OUI"
        return None


def load_model_rules(path: str | Path) -> list[ModelRule]:
    p = Path(path)
    if not p.exists() and not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p  # リポジトリの直下（設定ファイルを使わないテストなど）
    if not p.exists():
        log.warning("機種の表がありません: %s", p)
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for i, m in enumerate(data.get("models", [])):
        cat = m.get("category", "other")
        if cat not in MODEL_CATEGORIES:
            raise ValueError(f"models[{i}].category が不正です：{cat}")
        label = str(m.get("label", "")).strip()
        if not label:
            raise ValueError(f"models[{i}].label がありません")
        try:
            pats = [re.compile(x, re.I) for x in m.get("ssid", [])]
        except re.error as e:
            raise ValueError(f"models[{i}].ssid の正規表現が不正です：{e}") from None
        out.append(ModelRule(label, cat, pats, {str(x).lower() for x in m.get("fp", [])},
                             {str(x).lower() for x in m.get("vendor_ie", [])}, {str(x).lower() for x in m.get("oui", [])}))
    return out


FP_RE = re.compile(r"^[0-9a-f]{10}$")


class ModelBook:
    """覚え帳：画面で「この局は◯◯」と覚えた機種。指紋（model_fp）ごとに持ち、同じ指紋の局にも出す。

    data_dir/model_book.json に置く（再起動しても残る）。表（models.json）に移すときは export() の形をそのまま貼る。
    """

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.entries: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            try:
                self.entries = dict(json.loads(self.path.read_text(encoding="utf-8")).get("entries", {}))
            except (OSError, ValueError) as e:
                log.warning("覚え帳を読めません（%s）：%s", self.path, e)

    def get(self, fp: str | None) -> dict[str, Any] | None:
        return self.entries.get(fp) if fp else None

    def set(self, fp: str, label: str | None, bssid: str | None, at: float) -> None:
        if label:
            e = self.entries.get(fp) or {"examples": []}
            e["label"] = label
            e["at"] = at
            if bssid and bssid not in e["examples"]:
                e["examples"] = (e["examples"] + [bssid])[-20:]
            self.entries[fp] = e
        else:
            self.entries.pop(fp, None)
        if self.path:
            atomic_write_json(self.path, {"version": 1, "entries": self.entries})

    def export(self) -> list[dict[str, Any]]:
        """機種の表（models.json）にそのまま貼れる形。"""
        return [{"label": e["label"], "category": "other", "fp": [fp]} for fp, e in sorted(self.entries.items())]




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
                 tags: dict[str, str] | None = None, model_rules: list[ModelRule] | None = None,
                 book: ModelBook | None = None):
        self.cfg = cfg
        self.signatures = signatures
        self.oui = oui
        self.model_rules = model_rules if model_rules is not None else load_model_rules(cfg.models_path())
        self.book = book or ModelBook(None)
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
            for other, how in self._linked(st, table):
                if other.team and other.kind == "team":
                    kind, team, source, negotiable = "team", other.team, how, True
                    break
                if how == "sibling" and other.kind in ("own", "official_infra", "broadcast", "known_ok"):
                    # 非公開SSIDの双子：同じ機器なので同じ扱い（自チームの双子を「所属不明の強い局」にしない）
                    kind, negotiable = other.kind, other.negotiable
                    team, source = other.team, "sibling"
                    sig = self.signature_of(other)
                    break
        st.kind = kind
        st.team = team
        st.team_source = source
        st.negotiable = negotiable
        st.signature = sig.name if sig else None
        st.model_guess, st.hints = self.guess(st, table)

    def _linked(self, st: Station, table: StationTable) -> list[tuple[Station, str]]:
        """同じ機器と分かった局（RNRで紐付いたもの、BSSIDの並びで分かる双子）と、その分かり方。"""
        out = []
        for b in st.linked:
            o = table.get(b)
            if o is not None:
                out.append((o, "rnr"))
        for b in st.siblings:
            o = table.get(b)
            if o is not None and b not in st.linked:
                out.append((o, "sibling"))
        return out

    def guess(self, st: Station, table: StationTable | None = None) -> tuple[str | None, list[str]]:
        hints: list[str] = []
        wps = st.wps
        if not wps and table is not None:  # 6GHzはWPSが無い。RNRで紐付いた2.4/5GHz側（や双子）から借りる
            for o, how in self._linked(st, table):
                if o.wps:
                    wps = o.wps
                    hints.append(f"RNRで{o.band}GHz側（{o.bssid}）と同じ機器" if how == "rnr"
                                 else f"BSSIDの並びで{o.bssid}と同じ機器")
                    break
        random_mac = is_local_admin(st.bssid)
        vendor = None if random_mac else (self.oui.lookup(st.bssid) if self.oui else None)
        if random_mac:
            hints.append("ランダムMAC（ローカル管理）")
        elif vendor:
            hints.append(f"OUI: {vendor}")
        rule_hit = None
        for rule in self.model_rules:
            hit = rule.match(st)
            if hit:
                rule_hit = hit
                hints.append(f"機種の表（{hit[1]}）: {hit[0]}")
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
        if st.model_fp:
            hints.append(f"指紋: {st.model_fp}")
        book = self.book.get(st.model_fp)
        model = None
        if book and st.bssid in book.get("examples", []):
            model = book["label"]  # この局そのものを覚えた
            hints.insert(0, f"覚え帳：この局を「{model}」と覚えています")
        if model is None and wps:
            parts = [wps.get("manufacturer"), wps.get("model_name"), wps.get("model_number")]
            model = " ".join(p for p in parts if p) or None
        # その局のSSIDから分かること（初期設定の名前）は、別の局から指紋で借りた推定より強い
        if model is None and rule_hit and rule_hit[1] == "SSID":
            model = rule_hit[0]
        if model is None and book:
            model = book["label"]
            hints.insert(0, f"覚え帳：同じ指紋（{st.model_fp}）の局を「{model}」と覚えています")
        if model is None and rule_hit:
            model = rule_hit[0]
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

    def link_siblings(self, table: StationTable) -> None:
        """BSSIDの並びで分かる同じ機器（非公開SSIDの双子など）を紐付ける。

        同じ主chで、BSSIDの先頭5オクテットが同じ（1オクテット目はローカル管理のビットを除いて比べる）局どうし。
        例：00:00:5e:00:53:30（名前あり）と :31・:32（非公開）。
        """
        with table.lock:
            groups: dict[tuple[int, str], list[Station]] = {}
            for st in table.all():
                o = st.bssid.split(":")
                if len(o) != 6:
                    continue
                key = (st.primary_freq, f"{int(o[0], 16) & ~0x02:02x}:" + ":".join(o[1:5]))
                groups.setdefault(key, []).append(st)
            for sts in groups.values():
                for st in sts:
                    st.siblings = [o.bssid for o in sts if o is not st] if len(sts) > 1 else []

    def classify_all(self, table: StationTable) -> None:
        self.link_rnr(table)
        self.link_siblings(table)
        sts = table.all()
        for st in sts:
            self.classify(st, None)
        for st in sts:  # 2回目：RNRで紐付いた局のチーム・WPSを使う
            self.classify(st, table)

    def summary(self, st: Station) -> dict[str, Any]:
        return {"kind": st.kind, "label": KIND_LABEL.get(st.kind or "unknown"), "team": st.team,
                "negotiable": st.negotiable, "signature": st.signature, "model": st.model_guess}
