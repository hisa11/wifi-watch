"""処理能力の測定：160MHz受信中を想定した混ぜ方のフレームを、解析が1コアで何フレーム/秒さばけるか。

使い方： .venv/bin/python tools/bench.py [秒ぶん]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifiwatch import channels as ch  # noqa: E402
from wifiwatch.analyzer.core import Analyzer  # noqa: E402
from wifiwatch.capture import build  # noqa: E402
from wifiwatch.clock import FakeClock  # noqa: E402
from wifiwatch.radio import bpf  # noqa: E402
from wifiwatch.radio.base import FrameBatch, validate_tuning  # noqa: E402


def make_frames(n_seconds: int) -> list[bytes]:
    """1秒あたり：ビーコン200（20局×10）、データ2400、ACK2400 ＝ 5000フレーム。"""
    f = ch.parse_channel_ref("6g37")
    out: list[bytes] = []
    beacons = [build.beacon(f"02:00:00:00:{i:02x}:01", build.ap_ies(f, 160, f"AP{i}")) for i in range(20)]
    prog = bpf.truncation_program()
    for s in range(n_seconds):
        for k in range(10):
            for b in beacons:
                out.append(build.radiotap(f, signal=-60, chains=[-63, -64], flags=0x10, tsft=1) + build.with_fcs(b))
        for i in range(2400):
            bss = f"02:00:00:00:{i % 20:02x}:01"
            sta = f"02:aa:00:00:{i % 50:02x}:01"
            d = build.radiotap(f, signal=-55, chains=[-58, -59], flags=0x10, tsft=1, he_bw=160) + build.with_fcs(
                build.data(bss, sta, i % 2 == 0, retry=i % 10 == 0, payload_len=1400))
            out.append(d[:bpf.run(prog, d)])  # 実機と同じく、カーネルで切り詰めた形
            out.append(build.radiotap(f, signal=-55, chains=[-58, -59], flags=0x10, tsft=1) + build.with_fcs(build.ack(sta)))
    return out


def main() -> None:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    frames = make_frames(secs)
    clk = FakeClock()
    a = Analyzer(clk, own_addrs=["02:00:00:00:00:01"])
    tun = validate_tuning(ch.parse_channel_ref("6g37"), 160, 6185)
    t0 = time.perf_counter()
    step = 5000
    for i in range(0, len(frames), step):
        clk.advance(1.0)
        a.process_batch(FrameBatch(tun, clk.monotonic() - 1.0, clk.monotonic(), frames[i:i + step]))
    dt = time.perf_counter() - t0
    print(f"frames={len(frames)} time={dt:.2f}s rate={len(frames) / dt:,.0f} frames/s (1 core) "
          f"python={sys.version.split()[0]}")


if __name__ == "__main__":
    main()
