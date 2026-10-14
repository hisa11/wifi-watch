"""受信ソケットに付けるclassic BPF。カーネルの中でフレームを切り詰め、ペイロードをユーザ空間に渡さない。

残す長さ（radiotapの後ろ）：
- データフレーム        MACヘッダの長さちょうど（24、4アドレスなら30、QoSで+2、QoSかつOrderで+4）
- 制御フレーム          16バイトまで（RA・TAまで）
- ビーコン・プローブ応答 全部（IEを読むため）
- その他の管理フレーム  32バイトまで（ヘッダ＋HTC＋deauth/disassocの理由コード）
- 拡張（type 3）        32バイトまで

テスト用に、使う命令だけの小さな解釈器を持つ（`run`）。
"""
from __future__ import annotations

import ctypes
import socket
from dataclasses import dataclass

# 命令（linux/filter.h）
LD_B_ABS = 0x30
LD_B_IND = 0x50
LD_IMM = 0x00
LD_MEM = 0x60
ST = 0x02
ALU_LSH_K = 0x64
ALU_OR_X = 0x4C
ALU_AND_K = 0x54
ALU_ADD_K = 0x04
ALU_ADD_X = 0x0C
TAX = 0x07
TXA = 0x87
JEQ_K = 0x15
JA = 0x05
RET_K = 0x06
RET_A = 0x16

SO_ATTACH_FILTER = 26
FULL = 0x40000


@dataclass
class Insn:
    code: int
    k: int = 0
    jt: str | int = 0  # ラベル名か相対の数
    jf: str | int = 0
    label: str | None = None


def _assemble(prog: list[Insn]) -> list[tuple[int, int, int, int]]:
    pos = {i.label: n for n, i in enumerate(prog) if i.label}
    out = []
    for n, i in enumerate(prog):
        def rel(t: str | int) -> int:
            if isinstance(t, int):
                return t
            d = pos[t] - n - 1
            if not 0 <= d <= 255:
                raise ValueError("jump out of range")
            return d
        if i.code == JA and isinstance(i.k, str):
            k = pos[i.k] - n - 1
        else:
            k = i.k
        out.append((i.code, rel(i.jt), rel(i.jf), k & 0xFFFFFFFF))
    return out


def truncation_program() -> list[tuple[int, int, int, int]]:
    p = [
        # X = radiotapの長さ（リトルエンディアンのu16）
        Insn(LD_B_ABS, 3), Insn(ALU_LSH_K, 8), Insn(TAX), Insn(LD_B_ABS, 2), Insn(ALU_OR_X), Insn(TAX),
        Insn(LD_B_IND, 0), Insn(ST, 0),            # M0 = FCの1バイト目
        Insn(ALU_AND_K, 0x0C),
        Insn(JEQ_K, 0x08, 0, "nondata"),
        # データ：ヘッダ長を計算
        Insn(LD_B_IND, 1), Insn(ALU_AND_K, 0x03),
        Insn(JEQ_K, 0x03, 0, "base24"),
        Insn(LD_IMM, 30), Insn(JA, "store_base"),
        Insn(LD_IMM, 24, label="base24"),
        Insn(ST, 1, label="store_base"),           # M1 = 24 or 30
        Insn(LD_MEM, 0), Insn(ALU_AND_K, 0x80),
        Insn(JEQ_K, 0x00, "data_ret", 0),          # QoSでなければそのまま
        Insn(LD_MEM, 1), Insn(ALU_ADD_K, 2), Insn(ST, 1),
        Insn(LD_B_IND, 1), Insn(ALU_AND_K, 0x80),
        Insn(JEQ_K, 0x00, "data_ret", 0),          # Order（+HTC）でなければそのまま
        Insn(LD_MEM, 1), Insn(ALU_ADD_K, 4), Insn(ST, 1),
        Insn(LD_MEM, 1, label="data_ret"), Insn(ALU_ADD_X), Insn(RET_A),
        # データ以外
        Insn(LD_MEM, 0, label="nondata"), Insn(ALU_AND_K, 0x0C),
        Insn(JEQ_K, 0x04, 0, "notctrl"),
        Insn(TXA), Insn(ALU_ADD_K, 16), Insn(RET_A),
        Insn(JEQ_K, 0x00, 0, "short", label="notctrl"),
        Insn(LD_MEM, 0), Insn(ALU_AND_K, 0xF0),
        Insn(JEQ_K, 0x80, "full", 0),              # ビーコン
        Insn(JEQ_K, 0x50, "full", "short"),        # プローブ応答
        Insn(RET_K, FULL, label="full"),
        Insn(TXA, label="short"), Insn(ALU_ADD_K, 32), Insn(RET_A),
    ]
    return _assemble(p)


def run(prog: list[tuple[int, int, int, int]], pkt: bytes) -> int:
    """解釈器（テスト用）。戻り値は残すバイト数（パケット長で頭打ち）。"""
    A = X = 0
    M = [0] * 16
    pc = 0
    n = len(pkt)
    while True:
        code, jt, jf, k = prog[pc]
        pc += 1
        if code == LD_B_ABS:
            if k >= n:
                return 0
            A = pkt[k]
        elif code == LD_B_IND:
            if X + k >= n:
                return 0
            A = pkt[X + k]
        elif code == LD_IMM:
            A = k
        elif code == LD_MEM:
            A = M[k]
        elif code == ST:
            M[k] = A
        elif code == ALU_LSH_K:
            A = (A << k) & 0xFFFFFFFF
        elif code == ALU_OR_X:
            A |= X
        elif code == ALU_AND_K:
            A &= k
        elif code == ALU_ADD_K:
            A = (A + k) & 0xFFFFFFFF
        elif code == ALU_ADD_X:
            A = (A + X) & 0xFFFFFFFF
        elif code == TAX:
            X = A
        elif code == TXA:
            A = X
        elif code == JEQ_K:
            pc += jt if A == k else jf
        elif code == JA:
            pc += k
        elif code == RET_K:
            return min(k, n)
        elif code == RET_A:
            return min(A, n)
        else:
            raise ValueError(f"unknown opcode {code:#x}")


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def attach(sock: socket.socket, prog: list[tuple[int, int, int, int]] | None = None) -> None:
    """AF_PACKETのソケットに付ける（カーネルがフィルタを写すので、配列は呼び出しの間だけ生きていればよい）。"""
    prog = prog or truncation_program()
    arr = (_SockFilter * len(prog))(*[_SockFilter(c, jt, jf, k) for c, jt, jf, k in prog])
    fprog = _SockFprog(len(prog), ctypes.cast(arr, ctypes.POINTER(_SockFilter)))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, bytes(fprog))
