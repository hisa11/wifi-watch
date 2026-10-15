"""エミュレータのアプリを、画面の文字で探して押す小さな道具（通しの確認用）。

  python tools/adbui.py dump                 # 見えている文字の一覧
  python tools/adbui.py tap "スキャン"        # その文字の要素を押す（最初に見つかったもの）
  python tools/adbui.py type "ピット"         # 文字を入れる（ASCII以外はADBKeyboardが無いと入らない）
  python tools/adbui.py shot out.png          # 画面を撮る
"""
from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ADB = "adb"


def sh(*args: str, binary: bool = False):
    r = subprocess.run([ADB, *args], capture_output=True, timeout=60)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def nodes():
    sh("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    xml = sh("exec-out", "cat", "/sdcard/ui.xml")
    root = ET.fromstring(xml[xml.index("<"):])
    out = []
    for n in root.iter("node"):
        t = n.get("text") or n.get("content-desc") or ""
        b = re.findall(r"\d+", n.get("bounds", ""))
        if t and len(b) == 4:
            x1, y1, x2, y2 = map(int, b)
            out.append((t, (x1 + x2) // 2, (y1 + y2) // 2, n.get("clickable") == "true"))
    return out


def tap(text: str, index: int = 0) -> bool:
    hits = [n for n in nodes() if text in n[0]]
    if len(hits) <= index:
        print(f"見つからない：{text}", file=sys.stderr)
        return False
    _, x, y, _ = hits[index]
    sh("shell", "input", "tap", str(x), str(y))
    return True


def main() -> int:
    cmd = sys.argv[1]
    if cmd == "dump":
        for t, x, y, c in nodes():
            print(f"{'*' if c else ' '} ({x},{y}) {t}")
    elif cmd == "tap":
        return 0 if tap(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0) else 1
    elif cmd == "type":
        sh("shell", "input", "text", sys.argv[2].replace(" ", "%s"))
    elif cmd == "shot":
        open(sys.argv[2], "wb").write(sh("exec-out", "screencap", "-p", binary=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
