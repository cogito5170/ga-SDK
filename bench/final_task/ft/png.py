"""A small deterministic PNG with a digit string (the T1 image). Standard library only."""
import struct
import zlib

_F = {
 "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
 "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11110 00001 00001 01110 00001 00001 11110",
 "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
 "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
 "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
}


def digits_png(code: str, scale: int = 12, pad: int = 2) -> bytes:
    rows = [""] * 7
    for ch in code:
        g = _F[ch].split()
        for r in range(7):
            rows[r] += g[r] + "0"
    bits = [("0" * pad) + r + ("0" * pad) for r in rows]
    bits = ["0" * len(bits[0])] * pad + bits + ["0" * len(bits[0])] * pad
    w, h = len(bits[0]) * scale, len(bits) * scale
    raw = bytearray()
    for r in bits:
        line = bytearray()
        for b in r:
            line += (b"\x00" if b == "1" else b"\xff") * scale
        raw += (b"\x00" + bytes(line)) * scale
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
