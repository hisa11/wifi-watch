"""無線が RF-kill（無線の電源オフ）で止められているか。/sys を読むだけ（root 不要、何も変えない）。

画面のWi-Fiのスイッチ・機内モード・ノートPCのキー（Fn）で止められると、IFを上げられず受信が止まる
（`ip link set ... up` が「Operation not possible due to RF-kill」で失敗する）。受信停止の理由として、
復旧の手順を回す代わりに、これを出す。
"""
from __future__ import annotations

from pathlib import Path

from .base import validate_ifname


def state(ifname: str, sys_class_net: str = "/sys/class/net") -> str | None:
    """"hard"（スイッチ・キーなどハードウェアで）/ "soft"（画面・機内モードなどソフトで）/ None（止められていない・分からない）。"""
    try:
        validate_ifname(ifname)
    except Exception:
        return None
    phy = Path(sys_class_net) / ifname / "phy80211"
    soft = hard = False
    try:
        for d in phy.glob("rfkill*"):
            hard |= (d / "hard").read_text().strip() == "1"
            soft |= (d / "soft").read_text().strip() == "1"
    except OSError:
        return None
    return "hard" if hard else ("soft" if soft else None)


def describe(st: str) -> str:
    how = "ハードウェア（スイッチ・キー）" if st == "hard" else "ソフト（画面のWi-Fiのスイッチ・機内モード）"
    return (f"無線が RF-kill で止められています（{how}）。Wi-Fi をオンに戻してください"
            "（モニタモードの間は、画面のWi-Fiの切り替えや機内モードを押さない）")
