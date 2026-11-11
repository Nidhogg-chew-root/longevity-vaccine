"""Side-by-side PNG of a sheet region: stock (nearest-neighbour enlarged) | HD level.

usage: py -I preview.py <stock texture.pcx> <texture_x<k>.pcx> <x> <y> <w> <h> <out.png>
x, y, w, h are in STOCK pixels.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pcx8  # noqa: E402

stock, pal, _ = pcx8.read(sys.argv[1])
hd, _, _ = pcx8.read(sys.argv[2])
x, y, w, h = (int(v) for v in sys.argv[3:7])
k = hd.shape[1] // stock.shape[1]
rgb = np.frombuffer(pal, dtype=np.uint8).reshape(256, 3)
left = np.kron(stock[y:y + h, x:x + w], np.ones((k, k), dtype=np.uint8))
right = hd[y * k:(y + h) * k, x * k:(x + w) * k]
gap = np.zeros((left.shape[0], 8), dtype=np.uint8)
Image.fromarray(rgb[np.hstack([left, gap, right])]).save(sys.argv[7])
print(f"{sys.argv[7]}: k={k}")
