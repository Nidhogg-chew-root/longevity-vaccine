"""Build prototype HD terrain sheets from the game's own texture.pcx with Scale2x (EPX).

Scale2x only ever copies a neighbour's palette INDEX, so the result keeps the stock palette and the shading
bands the texture rasterizer relies on (it shades by adding an offset to the index: Texture_draw_2's
`add al, ch`). It sharpens diagonal edges and never blurs -- a stand-in for real HD art, good enough to
prove the engine path end to end.

usage: py -I make_test_sheets.py <game dir> <out dir> [levels, default: 2 4]
writes <out dir>\texture_x<k>.pcx for each level k (a power of two). 4 is the useful maximum: at PRACX's
deepest zoom (72, at 4K) a tile needs 0.709 * (72+16)/16 = 3.9 x the stock texels (terrain_hd_proto.js).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pcx8  # noqa: E402


def scale2x(e: np.ndarray) -> np.ndarray:
    p = np.pad(e, 1, mode="edge")
    b, h = p[:-2, 1:-1], p[2:, 1:-1]          # up, down
    d, f = p[1:-1, :-2], p[1:-1, 2:]          # left, right
    ok = (b != h) & (d != f)
    out = np.empty((e.shape[0] * 2, e.shape[1] * 2), dtype=e.dtype)
    out[0::2, 0::2] = np.where(ok & (d == b), d, e)
    out[0::2, 1::2] = np.where(ok & (b == f), f, e)
    out[1::2, 0::2] = np.where(ok & (d == h), d, e)
    out[1::2, 1::2] = np.where(ok & (h == f), f, e)
    return out


def main():
    game, out = Path(sys.argv[1]), Path(sys.argv[2])
    levels = sorted({int(a) for a in sys.argv[3:]} or {2, 4})
    if any(k & (k - 1) or k < 2 for k in levels):
        sys.exit("levels must be powers of two >= 2")
    pix, pal, hdr = pcx8.read(game / "texture.pcx")
    out.mkdir(parents=True, exist_ok=True)
    cur, k = pix, 1
    for want in levels:
        while k < want:
            cur, k = scale2x(cur), k * 2
        dst = out / f"texture_x{k}.pcx"
        pcx8.write(dst, cur, pal, hdr)
        back, pal2, _ = pcx8.read(dst)
        assert back.shape == (pix.shape[0] * k, pix.shape[1] * k) and pal2 == pal and (back == cur).all()
        print(f"{dst}: {back.shape[1]}x{back.shape[0]}, {dst.stat().st_size // 1024} KB, palette identical")


if __name__ == "__main__":
    main()
