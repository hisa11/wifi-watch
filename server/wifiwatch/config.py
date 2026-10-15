"""config.json の読み込みと検証、画面やアプリから変えた設定（overrides.json）の保存。

間違いは「どこが・なぜ」を日本語で示して止める。秘密（トークン、トピック、Webhook、
死活監視のURL）は config.json にだけ置き、コミットしない（config.example.json だけコミット）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import channels as ch

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
IFNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,15}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("config.json に誤りがあります:\n" + "\n".join(f"  - {p}" for p in problems))


def _mac(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{v!r} はMACアドレスとして読めません")
    s = v.strip().lower().replace("-", ":")
    if not MAC_RE.match(s):
        raise ValueError(f"「{v}」はMACアドレスとして読めません（例：aa:bb:cc:dd:ee:ff）")
    return s


def _freq(v: object) -> int:
    return ch.parse_channel_ref(v)


Mac = Annotated[str, BeforeValidator(_mac)]
Freq = Annotated[int, BeforeValidator(_freq)]
Width = Literal[20, 40, 80, 160, 320]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OwnConfig(_Strict):
    team: str = Field(min_length=1)
    ssids: list[str] = []
    bssids: list[Mac] = []
    station_macs: list[Mac] = []
    channel: Freq
    width: Width = 20
    spare: list[Freq] = []
    pmf_required: bool | None = None  # None: 6GHzなら必須とみなす

    @model_validator(mode="after")
    def _width_fits(self) -> "OwnConfig":
        if self.width not in ch.valid_widths(self.channel):
            raise ValueError(
                f"own.width={self.width} は {ch.label(self.channel)} では使えません"
                f"（使える幅：{', '.join(map(str, ch.valid_widths(self.channel)))}）"
            )
        return self


class TeamConfig(_Strict):
    name: str = Field(min_length=1)
    channels: list[Freq] = []
    spare: list[Freq] = []
    bssids: list[Mac] = []
    ssids: list[str] = []  # globパターン（例："KOSEN-A*"）


class NotifyConfig(_Strict):
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    discord_webhook: str | None = None
    healthcheck_url: str | None = None
    healthcheck_interval_s: float = 60
    heartbeat_interval_s: float = 900
    buzzer_device: str | None = None
    buzzer_baud: int = 115200
    timeout_s: float = 10

    @field_validator("ntfy_topic")
    @classmethod
    def _topic(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not TOPIC_RE.match(v):
            raise ValueError("ntfy_topic は英数字と _ - だけ、64文字以内にしてください")
        if len(v) < 16:
            raise ValueError("ntfy_topic は推測されにくい16文字以上にしてください（公開トピックのため）")
        return v

    @field_validator("ntfy_server", "discord_webhook", "healthcheck_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v is None or v.startswith("https://") or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", v):
            return v  # http はテスト用のループバックだけ
        raise ValueError(f"「{v}」は https:// で始めてください")


class RelayConfig(_Strict):
    url: str | None = None
    pc_token: str | None = None
    idle_interval_s: float = 10
    fg_interval_s: float = 2
    timeout_s: float = 10

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.rstrip("/")
        if v.startswith("https://"):
            return v
        if re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", v):
            return v  # ローカルの中継（開発用）
        raise ValueError("relay.url は https:// で始めてください（開発用の http://127.0.0.1:ポート は可）")

    @model_validator(mode="after")
    def _token(self) -> "RelayConfig":
        if self.url and not self.pc_token:
            raise ValueError("relay.url を書くときは relay.pc_token も書いてください")
        if self.fg_interval_s < 1 or self.idle_interval_s < self.fg_interval_s:
            raise ValueError("relay の間隔は fg_interval_s ≥ 1 かつ idle_interval_s ≥ fg_interval_s にしてください")
        return self


class ScheduleConfig(_Strict):
    """巡回の時間配分（すべて暫定。仕様書「受信の仕方」）。"""

    cycle_s: float = 10.0
    own_s: float = 7.0
    slot_s: float = 0.25
    switch_s: float = 0.03  # 1回の切替にかかると仮定する時間（推定）
    live_in_block_slots: int = 4
    live_six_slots: int = 4
    live_five_slots: int = 3
    live_two4_slots: int = 1
    live_groups: int = 4  # 巡回の12枠を何回に分けて自ブロックの受信の間に挟むか
    quiet_in_block_s: float = 1.0  # 10秒あたり
    liveness_period_s: float = 30.0  # QUIETと2.4/5GHzを両方外したときの生存確認の間隔
    liveness_dwell_s: float = 0.25
    coldstart_dwell_s: float = 0.2
    min_switch_interval_s: float = 0.2

    @model_validator(mode="after")
    def _check(self) -> "ScheduleConfig":
        slots = self.live_in_block_slots + self.live_six_slots + self.live_five_slots + self.live_two4_slots
        if self.own_s + slots * self.slot_s > self.cycle_s + 1e-9:
            raise ValueError("schedule: own_s と巡回の枠の合計が cycle_s を超えています")
        for name in ("slot_s", "coldstart_dwell_s", "liveness_dwell_s"):
            if getattr(self, name) < self.min_switch_interval_s:
                raise ValueError(f"schedule.{name} は {self.min_switch_interval_s}秒以上にしてください（切替は200ms未満の間隔にしない）")
        if self.switch_s >= self.slot_s:
            raise ValueError("schedule.switch_s は slot_s より短くしてください")
        return self


class ThresholdConfig(_Strict):
    """警報のしきい値（すべて暫定。PREモードの下見で較正する）。"""

    strong_rssi_dbm: float = -70
    problem_channel_min_strong: int = 3
    unknown_margin_db: float = 20
    unknown_min_stations: int = 5
    unknown_abs_dbm: float = -45
    wide_width_mhz: int = 160
    activity_ratio: float = 3.0
    activity_min_rate: float = 100.0
    activity_baseline_s: float = 600
    activity_min_baseline_buckets: int = 3
    retry_rate: float = 0.30
    retry_min_frames: int = 50
    retry_window_s: float = 10
    deauth_count: int = 10
    deauth_window_s: float = 3
    newcomer_lookback_s: float = 600
    newcomer_hold_s: float = 600
    clear_after_s: float = 10
    renotify_suppress_s: float = 30
    liveness_empty_cycles: int = 2
    levels: dict[str, Literal["warn", "crit"]] = {}


class ApiConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8080

    @field_validator("host")
    @classmethod
    def _loopback(cls, v: str) -> str:
        if v != "127.0.0.1":
            raise ValueError("api.host は 127.0.0.1 だけにしてください（PCは外からの接続を受けない）")
        return v


class RadioConfig(_Strict):
    kind: Literal["fake", "linux", "radiod", "scan_only"] = "fake"
    radiod_socket: str = "/run/wifiwatch/radiod.sock"
    monitor_flags: list[Literal["fcsfail", "control", "otherbss"]] = []  # linux のとき（radiod は起動の引数で渡す）
    fake_scenario: str | None = None  # 偽の無線のシナリオ（JSONファイル）。無ければ組み込みの見本


class Config(_Strict):
    interface: str = "wlan0"
    own: OwnConfig
    teams: list[TeamConfig] = []
    known_ok_bssids: list[Mac] = []
    locations: list[str] = ["ピット", "フィールド北", "フィールド南", "客席東", "客席西"]
    notify: NotifyConfig = NotifyConfig()
    relay: RelayConfig = RelayConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    thresholds: ThresholdConfig = ThresholdConfig()
    api: ApiConfig = ApiConfig()
    radio: RadioConfig = RadioConfig()
    data_dir: str = "data"
    signatures: str = "signatures.json"
    # 機種の表（初期設定のSSID・指紋など）。省略すると signatures と同じフォルダの models.json
    models: str | None = None
    device_name: str = "pit-pc"

    def models_path(self) -> str:
        return self.models or str(Path(self.signatures).parent / "models.json")

    @field_validator("interface")
    @classmethod
    def _ifname(cls, v: str) -> str:
        if not IFNAME_RE.match(v):
            raise ValueError(f"interface「{v}」はIF名として使えません（英数字と _ . - の15文字以内）")
        return v

    @model_validator(mode="after")
    def _teams(self) -> "Config":
        names = [t.name for t in self.teams]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"teams の名前が重複しています：{', '.join(sorted(dup))}")
        if self.own.team in names:
            raise ValueError(f"own.team「{self.own.team}」は teams に入れないでください（自チームは own に書く）")
        return self

    # 便利な派生値 --------------------------------------------------------
    def reservations(self) -> dict[str, set[int]]:
        """チーム名 → 予約ch（割当chと予備ch、MHz）。自チームも含む。"""
        out = {t.name: set(t.channels) | set(t.spare) for t in self.teams}
        out[self.own.team] = {self.own.channel, *self.own.spare}
        return out


def _format_loc(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for p in loc:
        if isinstance(p, int):
            parts[-1:] = [f"{parts[-1]}[{p}]"] if parts else [f"[{p}]"]
        else:
            parts.append(str(p))
    return ".".join(parts)


def _translate(err: dict[str, Any]) -> str:
    loc = _format_loc(tuple(x for x in err.get("loc", ()) if x not in ("function-after[_width_fits(), OwnConfig]",)))
    t = err.get("type", "")
    msg = err.get("msg", "")
    if msg.startswith("Value error, "):
        msg = msg[len("Value error, "):]
    elif t == "missing":
        msg = "必須の項目がありません"
    elif t == "extra_forbidden":
        msg = "知らない項目です（綴りを確認してください）"
    elif t == "literal_error":
        msg = f"使えない値です（{msg}）"
    elif t.endswith("_type") or t.endswith("_parsing"):
        msg = f"型が違います（{msg}）"
    return f"{loc}: {msg}" if loc else msg


def parse_config(data: Any) -> Config:
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError([_translate(x) for x in e.errors()]) from None


def load_config(path: str | os.PathLike[str]) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError([f"{p} がありません（config.example.json を写して書き換えてください）"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError([f"{p} のJSONが読めません：{e.lineno}行{e.colno}列 {e.msg}"]) from None
    cfg = parse_config(data)
    # 相対パスは config.json の場所から
    base = p.resolve().parent
    if not os.path.isabs(cfg.data_dir):
        cfg.data_dir = str(base / cfg.data_dir)
    if not os.path.isabs(cfg.signatures):
        cfg.signatures = str(base / cfg.signatures)
    if cfg.models and not os.path.isabs(cfg.models):
        cfg.models = str(base / cfg.models)
    if cfg.radio.fake_scenario and not os.path.isabs(cfg.radio.fake_scenario):
        cfg.radio.fake_scenario = str(base / cfg.radio.fake_scenario)
    return cfg


def atomic_write_json(path: str | os.PathLike[str], data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Overrides:
    """画面やアプリから変えた設定。再起動しても残す（data_dir/overrides.json）。

    持つもの：巡回から外した帯域とch、モード、自分のch（再起動なしで変更したもの）。
    """

    DEFAULTS: dict[str, Any] = {
        "excluded_bands": [],
        "excluded_freqs": [],
        "mode": "LIVE",
        "own_channel": None,
        "own_width": None,
    }

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = dict(self.DEFAULTS)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                for k in self.DEFAULTS:
                    if k in loaded:
                        self._data[k] = loaded[k]
            except (json.JSONDecodeError, OSError):
                # 壊れていたら既定に戻す（監視を止めないことを優先）。壊れたファイルは残す
                self.path.rename(self.path.with_suffix(".broken.json"))
        self._sanitize()

    def _sanitize(self) -> None:
        d = self._data
        d["excluded_bands"] = sorted({b for b in d.get("excluded_bands") or [] if b in ch.BANDS})
        d["excluded_freqs"] = sorted({f for f in d.get("excluded_freqs") or [] if ch.is_known_freq(f)})
        if d.get("mode") not in ("PRE", "LIVE", "QUIET"):
            d["mode"] = "LIVE"
        if d.get("own_channel") is not None and not ch.is_known_freq(d["own_channel"]):
            d["own_channel"] = None
            d["own_width"] = None

    def get(self, key: str) -> Any:
        with self._lock:
            v = self._data[key]
            return list(v) if isinstance(v, list) else v

    def update(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                if k not in self.DEFAULTS:
                    raise KeyError(k)
                self._data[k] = v
            self._sanitize()
            atomic_write_json(self.path, self._data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))
