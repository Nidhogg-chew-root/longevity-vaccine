"""Drive terrain_hd_proto.js in a RUNNING game (start it with thinker.exe and load a save first).

usage: py -I proto_run.py <game dir> <out dir> step [step ...]
steps:  level:<k>   every 56x56 terrain texture at k x (1 = stock), then a full redraw
        auto        choose k from the main map's zoom
        dump:<name> save the main map canvas after the next full terrain pass -> <out>\\<name>.png
        wait:<s>    sleep
        status      print the script's state
HD art: <game>\\terrain_hd\\texture_x<k>.pcx (see make_test_sheets.py). Textures that are not cut from
TEXTURE.PCX (the RAINFALL composites) get a Scale2x of their own stock pixels at the same k. The game is put
back to level 1 before the script unloads.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import frida
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pcx8  # noqa: E402
from make_test_sheets import scale2x  # noqa: E402


def match_rects(textures, sheet: np.ndarray):
    """Source rect of each 56x56 texture in the stock sheet, or None. Returns (rects, report)."""
    rows = [sheet[y].tobytes() for y in range(sheet.shape[0])]
    rects, ambiguous = [], []
    for t in textures:
        px = np.frombuffer(bytes.fromhex(t["hex"]), dtype=np.uint8).reshape(56, 56)
        first = px[0].tobytes()
        hits = []
        for y in range(sheet.shape[0] - 55):
            x = rows[y].find(first)
            while x >= 0:
                if x + 56 <= sheet.shape[1] and (sheet[y:y + 56, x:x + 56] == px).all():
                    hits.append((x, y))
                x = rows[y].find(first, x + 1)
        if len(hits) > 1:
            ambiguous.append((hex(t["addr"]), hits[:4]))   # identical pixels: any of them is the same tile
        rects.append(hits[0] if hits else None)
    n = sum(r is not None for r in rects)
    return rects, {"textures": len(textures), "in_texture_pcx": n, "other": len(textures) - n,
                   "other_addrs": [hex(t["addr"]) for t, r in zip(textures, rects) if r is None],
                   "ambiguous": ambiguous}


def build_blocks(textures, rects, hd: np.ndarray, k: int) -> bytes:
    side = 56 * k
    out = bytearray()
    for t, r in zip(textures, rects):
        if r is not None:
            x, y = r
            block = hd[y * k:y * k + side, x * k:x * k + side]
        else:
            block = np.frombuffer(bytes.fromhex(t["hex"]), dtype=np.uint8).reshape(56, 56)
            while block.shape[0] < side:
                block = scale2x(block)
        assert block.shape == (side, side)
        out += np.ascontiguousarray(block).tobytes()
    return bytes(out)


def main():
    game, out = Path(sys.argv[1]), Path(sys.argv[2])
    steps = sys.argv[3:]
    out.mkdir(parents=True, exist_ok=True)
    sheet, stock_pal, _ = pcx8.read(game / "texture.pcx")
    hd_sheets = {}
    for p in sorted((game / "terrain_hd").glob("texture_x*.pcx")):
        k = int(p.stem.split("_x")[1])
        px, pal, _ = pcx8.read(p)
        if px.shape != (sheet.shape[0] * k, sheet.shape[1] * k) or pal != stock_pal:
            sys.exit(f"{p}: must be exactly {k}x the stock sheet with the stock palette")
        hd_sheets[k] = px

    session = frida.attach("terranx.exe")
    script = session.create_script((HERE / "terrain_hd_proto.js").read_text(encoding="utf-8"))
    dumps, done = {}, set()

    def on_message(msg, data):
        if msg["type"] == "error":
            print("SCRIPT ERROR:", msg.get("stack") or msg)
            return
        p = msg["payload"]
        if p["type"] == "dump":
            dumps[p["name"]] = (p, data)
        elif p["type"] == "palette":
            meta, raw = dumps.pop(p["name"])
            w, h, pitch = meta["w"], meta["h"], meta["pitch"]
            idx = np.frombuffer(raw, dtype=np.uint8).reshape(h, pitch)[:, :w]
            quad = np.frombuffer(data, dtype=np.uint8).reshape(256, 4)
            rgb = quad[:, 2::-1] if quad[:, :3].any() else np.frombuffer(stock_pal, np.uint8).reshape(256, 3)
            dst = out / f"{p['name']}.png"
            Image.fromarray(np.ascontiguousarray(rgb[idx])).save(dst)
            print(f"dump {dst} ({w}x{h}, level {meta['level']})")
            done.add(p["name"])
        else:
            print(json.dumps(p))

    def wait(pred, secs=20):
        deadline = time.time() + secs
        while time.time() < deadline and not pred():
            time.sleep(0.2)
        return pred()

    script.on("message", on_message)
    script.load()
    api = script.exports_sync
    try:
        print("check:", api.check())
        textures = api.scan()
        rects, report = match_rects(textures, sheet)
        print("scan:", json.dumps(report))
        if report["in_texture_pcx"] == 0:
            sys.exit("no texture matched TEXTURE.PCX -- the in-memory indices differ from the file; stopping")
        for k, hd in hd_sheets.items():
            script.post({"type": "blocks", "k": k, "addrs": [t["addr"] for t in textures]},
                        build_blocks(textures, rects, hd, k))
        wait(lambda: len(api.status()["levels"]) == len(hd_sheets))
        print("levels posted:", api.status()["levels"])

        for step in steps:
            cmd, _, arg = step.partition(":")
            if cmd == "level":
                api.set_level(int(arg))
                ok = wait(lambda: api.status()["level"] == int(arg) and api.status()["pendingLevel"] is None)
                print(step, "->", "ok" if ok else f"not applied (no map redraw?) {api.status()['errors'][-1:]}")
            elif cmd == "auto":
                api.set_auto(True)
            elif cmd == "dump":
                api.dump(arg)
                if not wait(lambda: arg in done):
                    print(step, "-> timed out (no full map redraw seen)")
            elif cmd == "wait":
                time.sleep(float(arg))
            elif cmd == "status":
                print(json.dumps(api.status(), indent=1))
    finally:
        if api.status()["level"] != 1:
            api.set_level(1)
            wait(lambda: api.status()["level"] == 1)
        script.unload()
        session.detach()


if __name__ == "__main__":
    main()
