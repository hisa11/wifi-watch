"""偽の無線。シナリオの局が合成フレームを出し、受信の設定に応じて「聞こえる」ものだけを返す。

受信のモデル（仕様書「受信の仕方」に合わせた仮定）：
- 受信機は主20MHz（tuneのfreq）でパケットを検出する。
- 20MHzのビーコンは局の主chでだけ聞こえる。重複ビーコンの局は、局の帯域内のどの20MHzでも聞こえる。
- データは局の全幅で送る。受信機の主20MHzがその範囲にあり、範囲が受信の幅に収まるときだけ聞こえる
  （AX210は320MHzを受信できないので、320MHzの局のデータは聞こえない）。
- ACKは局の帯域に重複して送る（受信機の主20MHzが局の帯域内なら聞こえる）。
- 強さは2本のアンテナに分け、radiotapには強い方（ドライバの値）とアンテナごとの値を入れる。
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import channels as ch
from ..capture import build
from ..clock import Clock
from .base import FrameBatch, Radio, RadioError, RegInfo, SurveyEntry, Tuning


@dataclass
class SimStation:
    bssid: str
    ssid: str
    freq: int
    width: int = 20
    rssi: float = -60.0
    standard: str = "auto"
    dup_beacon: bool = False
    beacon_interval: float = 0.1024
    data_rate: float = 0.0  # データフレーム/秒（ACKは別に同数）
    retry_ratio: float = 0.0
    clients: list[str] = field(default_factory=list)
    present: list[list[float | None]] = field(default_factory=lambda: [[0.0, None]])  # シナリオ開始からの秒
    deauth: list[list[Any]] = field(default_factory=list)  # [開始秒, 個数, 宛先MAC]
    ap: dict[str, Any] = field(default_factory=dict)  # build.ap_ies への追加の引数

    _ies: bytes | None = field(default=None, repr=False)
    _phase: float = field(default=0.0, repr=False)

    def ies(self) -> bytes:
        if self._ies is None:
            self._ies = build.ap_ies(self.freq, self.width, self.ssid, standard=self.standard,
                                     dup_beacon=self.dup_beacon, **self.ap)
        return self._ies

    def span(self) -> ch.Span:
        if self.width == 20:
            return ch.span20(self.freq)
        b = ch.block_for(self.freq, self.width)
        if b is None:
            raise ValueError(f"{self.ssid}: {ch.label(self.freq)} に{self.width}MHzのブロックがありません")
        return b.span

    def is_present(self, t: float) -> bool:
        for a, b in self.present:
            if (a or 0.0) <= t and (b is None or t < b):
                return True
        return False


@dataclass
class Scenario:
    stations: list[SimStation]
    seed: int = 1
    noise_db: float = 2.0
    sensitivity_dbm: float = -91.0
    bad_fcs_ratio: float = 0.0

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Scenario":
        sts = []
        for s in data.get("stations", []):
            s = dict(s)
            s["freq"] = ch.parse_channel_ref(s["freq"])
            sts.append(SimStation(**s))
        return Scenario(sts, **{k: v for k, v in data.items() if k != "stations"})

    @staticmethod
    def load(path: str | Path) -> "Scenario":
        return Scenario.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class FakeRadio(Radio):
    def __init__(self, clock: Clock, scenario: Scenario, switch_s: float = 0.03):
        self.clock = clock
        self.scenario = scenario
        self.switch_s = switch_s
        self.t0 = clock.monotonic()
        self.rng = random.Random(scenario.seed)
        for i, s in enumerate(scenario.stations):
            s._phase = self.rng.uniform(0, s.beacon_interval)
            _ = s.span()  # 不正なシナリオは最初に止める
        self.tuning: Tuning | None = None
        self.tune_log: list[tuple[float, Tuning]] = []
        self.monitor = True
        # 故障の注入
        self.stalled = False
        self.stall_fix: str = "monitor"  # monitor / reload / never
        self.six_enabled = True
        self.reload_disables_six = False  # 読み込み直し後、管理モードでスキャンするまで6GHzが無効
        self.calls: list[str] = []
        self.seq = 0

    # ------------------------------------------------------------ 受信
    def tune(self, tuning: Tuning) -> None:
        if not self.monitor:
            raise RadioError("モニタモードではありません")
        if ch.band_of_freq(tuning.freq) == ch.BAND_6 and not self.six_enabled:
            raise RadioError("6GHzが無効です（disabled）")
        self.clock.sleep(self.switch_s)
        self.tuning = tuning
        self.tune_log.append((self.clock.monotonic(), tuning))

    def capture(self, duration: float) -> FrameBatch:
        if self.tuning is None:
            raise RadioError("tuneしていません")
        t_start = self.clock.monotonic()
        self.clock.sleep(duration)
        t_end = self.clock.monotonic()
        batch = FrameBatch(self.tuning, t_start, t_end)
        if self.stalled or not self.monitor:
            return batch
        a = t_start - self.t0
        b = t_end - self.t0
        for st in self.scenario.stations:
            self._gen_station(st, a, b, batch.frames)
        return batch

    def _signal(self, base: float) -> tuple[int, list[int]] | None:
        s = base + self.rng.gauss(0, self.scenario.noise_db)
        if s < self.scenario.sensitivity_dbm:
            return None
        c = [round(s - 3 + self.rng.gauss(0, 1.0)), round(s - 3 + self.rng.gauss(0, 1.0))]
        c = [max(-120, min(-10, x)) for x in c]
        return max(c), c

    def _emit(self, out: list[bytes], base: float, t: float, frame: bytes) -> None:
        sig = self._signal(base)
        if sig is None:
            return
        drv, chains = sig
        assert self.tuning is not None
        bad = self.scenario.bad_fcs_ratio > 0 and self.rng.random() < self.scenario.bad_fcs_ratio
        rt = build.radiotap(self.tuning.freq, signal=drv, chains=chains,
                            flags=0x10 | (0x40 if bad else 0), tsft=int(t * 1e6))
        out.append(rt + build.with_fcs(frame, bad=bad))

    def _gen_station(self, st: SimStation, a: float, b: float, out: list[bytes]) -> None:
        if not st.is_present(a) and not st.is_present(b):
            return
        tun = self.tuning
        assert tun is not None
        rx_span = tun.span
        P = tun.freq
        span = st.span()
        # ビーコン
        hears_beacon = P == st.freq or (st.dup_beacon and span.contains_freq(P) and ch.band_of_freq(P) == ch.BAND_6)
        if hears_beacon:
            k0 = math.ceil((a - st._phase) / st.beacon_interval)
            k1 = math.ceil((b - st._phase) / st.beacon_interval)
            for k in range(max(0, k0), k1):
                t = st._phase + k * st.beacon_interval
                if not st.is_present(t):
                    continue
                self.seq += 1
                frame = build.beacon(st.bssid, st.ies(), seq=self.seq, tsf=int(t * 1e6))
                self._emit(out, st.rssi, t, frame)
        # データとACK
        if st.data_rate > 0 and span.contains_freq(P):
            n = self._count(st.data_rate * (b - a))
            data_ok = span.lo >= rx_span.lo and span.hi <= rx_span.hi and st.width <= 160
            clients = st.clients or ["02:aa:00:00:00:01"]
            for i in range(n):
                t = a + (b - a) * (i + 0.5) / max(n, 1)
                if not st.is_present(t):
                    continue
                sta = clients[i % len(clients)]
                to_ap = i % 2 == 0
                retry = self.rng.random() < st.retry_ratio
                self.seq += 1
                if data_ok:
                    frame = build.data(st.bssid, sta, to_ap, retry=retry, seq=self.seq)
                    self._emit(out, st.rssi, t, frame)
                self._emit(out, st.rssi, t, build.ack(sta if to_ap else st.bssid))
        # deauthの洪水（20MHz、主chだけ）
        for start, count, dst in st.deauth:
            if P != st.freq:
                continue
            for j in range(int(count)):
                t = float(start) + j * 0.05
                if a <= t < b:
                    self.seq += 1
                    frame = build.deauth(dst, st.bssid, st.bssid, protected=False, seq=self.seq)
                    self._emit(out, st.rssi, t, frame)

    def _count(self, expected: float) -> int:
        base = int(expected)
        return base + (1 if self.rng.random() < expected - base else 0)

    # ------------------------------------------------------------ 調査と復旧
    def survey(self) -> list[SurveyEntry] | None:
        if self.tuning is None:
            return None
        return [SurveyEntry(self.tuning.freq, True, -95, 1000, 100)]

    def reset_monitor(self) -> None:
        self.calls.append("reset_monitor")
        self.monitor = True
        if self.stalled and self.stall_fix == "monitor":
            self.stalled = False

    def reload_driver(self) -> None:
        self.calls.append("reload_driver")
        self.monitor = False
        if self.reload_disables_six:
            self.six_enabled = False
        if self.stalled and self.stall_fix == "reload":
            self.stalled = False

    def managed_scan(self) -> int:
        self.calls.append("managed_scan")
        self.monitor = False
        self.six_enabled = True  # 周りの国情報で6GHzが有効になる（LAR）
        return len(self.scenario.stations)

    def regulatory(self) -> RegInfo:
        self.calls.append("regulatory")
        six = list(ch.FREQS_BY_BAND[ch.BAND_6]) if self.six_enabled else []
        return RegInfo("JP", self.six_enabled, six, "country JP: DFS-JP (fake)")

    def set_monitor(self) -> None:
        self.calls.append("set_monitor")
        self.monitor = True


def demo_scenario() -> Scenario:
    """開発と画面確認用の見本（会場らしい局の並び）。config.example.json と対応する。"""
    st: list[SimStation] = [
        # 自チーム（6GHz ch37）
        SimStation("02:00:00:00:00:01", "OUR-ROBOT-6G", ch.parse_channel_ref("6g37"), 20, -48,
                   data_rate=150, clients=["02:00:00:00:00:02"]),
        # 他チーム（予約どおり）
        SimStation("02:a0:00:00:00:01", "KOSEN-A-6G", ch.parse_channel_ref("6g5"), 20, -62, data_rate=80),
        SimStation("02:b0:00:00:00:01", "KOSEN-B-6G", ch.parse_channel_ref("6g53"), 40, -66, data_rate=60),
        # 予約外のchに出ているA高専の2台目（10分後に出る）
        SimStation("02:a0:00:00:00:02", "KOSEN-A-SPARE", ch.parse_channel_ref("6g41"), 20, -58,
                   present=[[600.0, None]], data_rate=40),
        # 所属不明の広帯域（初期設定のまま320MHzのWi-Fi 7ルータ、主ch49）
        SimStation("00:1d:73:11:22:33", "Buffalo-6G-7A3F", ch.parse_channel_ref("6g49"), 320, -55,
                   dup_beacon=True, data_rate=30, ap={"wps": {"manufacturer": "BUFFALO INC.", "model_name": "WXR18000BE10P"}}),
        # 自ブロック内の別chの局
        SimStation("02:c0:00:00:00:01", "KOSEN-C-6G", ch.parse_channel_ref("6g45"), 20, -70, data_rate=20),
        # PSC
        SimStation("02:d0:00:00:00:01", "DIRECT-ab-Pixel", ch.parse_channel_ref("6g21"), 20, -75),
        # 5GHz：公式のFree Wi-Fi（過去の全国大会：W56のch100〜128に1台ずつ、20MHz、-60〜-73dBm）とスタッフ用（ch140）
        *[SimStation(f"02:52:46:00:01:{i:02x}", "ROBOCON_Free_Wi-Fi", ch.parse_channel_ref(f"5g{n}"), 20,
                     (-61, -73, -73, -72, -73, -67, -61, -60)[i], data_rate=30)
          for i, n in enumerate((100, 104, 108, 112, 116, 120, 124, 128))],
        SimStation("02:52:46:00:03:01", "ROBOCON_Staff_W", ch.parse_channel_ref("5g140"), 20, -54, data_rate=20),
        SimStation("02:52:46:00:03:02", "ROBOCON_Staff_W", ch.parse_channel_ref("5g140"), 20, -64, data_rate=10),
        # 5GHzに残ったチームのAPと、同じ機器の非公開SSID（全国大会の not-togetoge-5g と *hidden*）
        SimStation("02:4e:54:00:00:10", "not-togetoge-5g", ch.parse_channel_ref("5g136"), 20, -35, data_rate=60),
        SimStation("02:4e:54:00:00:11", "not-togetoge-5g", ch.parse_channel_ref("5g136"), 20, -36, ap={"hidden": True}),
        # 観客のスマホのテザリング（80MHz、主ch132）
        SimStation("02:58:50:00:00:01", "xperia5", ch.parse_channel_ref("5g132"), 80, -84, data_rate=5),
        # 5GHz：CineEye（桁違いに強い）
        SimStation("02:6c:5a:00:00:01", "CineEye2-1234", ch.parse_channel_ref("5g44"), 40, -31,
                   standard="ac", data_rate=400),
        # 5GHz：2.4/5GHzで6GHzをRNRで知らせるルータ（Buffalo の5GHz側）
        SimStation("00:1d:73:11:22:32", "Buffalo-5G-7A3F", ch.parse_channel_ref("5g36"), 80, -57,
                   standard="ax", data_rate=10,
                   ap={"wps": {"manufacturer": "BUFFALO INC.", "model_name": "WXR18000BE10P"},
                       "rnr": build.rnr_ie([(137, 49, [build.RnrAp("00:1d:73:11:22:33", build.short_ssid("Buffalo-6G-7A3F"), 0x42)])])}),
        # 2.4GHz
        SimStation("b8:27:eb:00:00:01", "raspberrypi", ch.parse_channel_ref("2g1"), 20, -68, standard="n", data_rate=5),
        SimStation("24:6f:28:00:00:01", "ESP_1A2B3C", ch.parse_channel_ref("2g6"), 20, -72, standard="n"),
        SimStation("02:52:46:00:02:01", "ROBOCON_Free_Wi-Fi", ch.parse_channel_ref("2g11"), 20, -64, standard="n", data_rate=20),
        # 2.4GHz：観客のスマホ・モバイルルータ・家庭用ルータ（地区大会の様子。初期設定のSSIDが多い）
        *[SimStation(b, ssid, ch.parse_channel_ref(f"2g{n}"), w, r, standard="n", ap=ap)
          for b, ssid, n, w, r, ap in (
              ("02:3a:9d:8a:2a:47", "aterm-8a2a47-g", 1, 20, -59, {}),
              ("02:3a:9d:8a:2a:48", "aterm-8a2a47-w", 1, 20, -60, {}),  # 同じ機器の2つ目のSSID（BSSIDが並ぶ）
              ("02:e0:4c:53:3f:f9", "elecom-533ff9", 1, 20, -63, {}),
              ("02:a1:b2:36:98:01", "Pixel_3698", 1, 20, -68, {}),
              ("02:a1:b2:00:07:01", "hidden", 7, 20, -61, {"hidden": True}),
              ("02:f5:03:77:30:01", "FS030W_P377301", 6, 20, -60, {}),
              ("02:a1:b2:00:09:01", "AI-WIFI", 9, 20, -60, {}),
              ("02:a1:b2:00:0b:01", "Galaxy Z Fold6 5734", 11, 20, -70, {}),
              ("02:a1:b2:00:0b:02", "Xperia 10 VII", 11, 20, -73, {}),
              ("02:a1:b2:00:06:01", "susumuのiPhone", 6, 20, -74, {"vendor": [build.vendor_ie("00:17:f2", 0x0a, b"\x00\x01\x03\x01\x00\x00")]}),
              ("02:a1:b2:00:06:02", "AiR-WiFi_0ZQUF7", 6, 20, -79, {}),
          )],
    ]
    return Scenario(st, seed=7)
