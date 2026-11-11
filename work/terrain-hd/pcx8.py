"""Minimal 8-bit single-plane PCX codec (the format of the game's texture.pcx).

read(path)  -> (pixels: uint8 array HxW, palette: bytes[768], header: bytes[128])
write(path, pixels, palette, header) -> writes RLE PCX v5 with the given palette
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def read(path):
    data = Path(path).read_bytes()
    if data[0] != 10 or data[3] != 8 or data[65] != 1:
        raise ValueError(f"{path}: not an 8-bit single-plane PCX")
    x0, y0, x1, y1 = (int.from_bytes(data[o:o + 2], "little") for o in (4, 6, 8, 10))
    w, h = x1 - x0 + 1, y1 - y0 + 1
    bpl = int.from_bytes(data[66:68], "little")
    if data[-769] != 12:
        raise ValueError(f"{path}: no 256-colour palette")
    palette = data[-768:]
    out = np.empty(bpl * h, dtype=np.uint8)
    src, n, i = data[128:-769], bpl * h, 0
    j = 0
    while i < n:
        b = src[j]
        j += 1
        if b >= 0xC0:
            run = b & 0x3F
            out[i:i + run] = src[j]
            j += 1
            i += run
        else:
            out[i] = b
            i += 1
    return out.reshape(h, bpl)[:, :w].copy(), palette, data[:128]


def _encode_row(row: bytes) -> bytearray:
    out = bytearray()
    i, n = 0, len(row)
    while i < n:
        b = row[i]
        run = 1
        while i + run < n and run < 63 and row[i + run] == b:
            run += 1
        if run > 1 or b >= 0xC0:
            out += bytes((0xC0 | run, b))
        else:
            out.append(b)
        i += run
    return out


def write(path, pixels: np.ndarray, palette: bytes, header: bytes):
    h, w = pixels.shape
    bpl = w + (w & 1)
    hdr = bytearray(header)
    hdr[4:12] = b"".join(v.to_bytes(2, "little") for v in (0, 0, w - 1, h - 1))
    hdr[66:68] = bpl.to_bytes(2, "little")
    body = bytearray()
    padded = np.zeros((h, bpl), dtype=np.uint8)
    padded[:, :w] = pixels
    for row in padded:
        body += _encode_row(row.tobytes())
    Path(path).write_bytes(bytes(hdr) + bytes(body) + b"\x0c" + bytes(palette))
