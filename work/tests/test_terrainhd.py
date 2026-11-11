"""uimod terrainhd_law.h: the laws behind drop-in HD terrain textures (src\\uimod\\src\\terrainhd.cpp), checked against
plain Python statements of the same rules -- and the engine sites terrainhd.cpp byte-checks, in the clean binary."""
from __future__ import annotations

import math
import pathlib
import random
import re
import struct
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import cpp_harness  # noqa: E402

needs_cpp = pytest.mark.skipif(not cpp_harness.have_toolchain(), reason="Visual Studio x86 toolchain not available")


@pytest.fixture(scope="module")
def exe():
    return cpp_harness.build(ROOT / "tests" / "terrainhd_selftest.cpp", "terrainhd_selftest_")


def run(exe, *args):
    return subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------------------------------------------------- UV law

@needs_cpp
@pytest.mark.parametrize("k", [1, 2, 4])
def test_uv_law_is_round_of_the_linear_map(exe, k):
    """Every stock UV (the tables hold 0..55) maps to round(u * (56k-1) / 55): 0 stays 0, 55 is the last HD texel."""
    for u in range(56):
        assert int(run(exe, "uv", u, k)) == round(u * (56 * k - 1) / 55), (u, k)


@needs_cpp
def test_uv_law_endpoints_and_order(exe):
    for k in (1, 2, 4):
        vals = [int(run(exe, "uv", u, k)) for u in range(56)]
        assert vals[0] == 0 and vals[55] == 56 * k - 1 and vals == sorted(vals)
    assert [int(run(exe, "uv", u, 1)) for u in range(56)] == list(range(56)), "level 1 must be the identity"


# ------------------------------------------------------------------------------------------------- level choice

def level_ref(zoom, have):
    if zoom + 16 <= 0:
        return 1
    need = (zoom + 16) / (16 * math.sqrt(2))
    loaded = [k for k in (2, 4, 8) if have & (1 << k)]
    for k in [1] + loaded:
        if k >= need:
            return k
    return loaded[-1] if loaded else 1


@needs_cpp
@pytest.mark.parametrize("have", [0, 1 << 2, 1 << 4, (1 << 2) | (1 << 4), (1 << 2) | (1 << 4) | (1 << 8), 1 << 8])
def test_level_for_zoom_matches_the_sqrt2_law(exe, have):
    for zoom in range(-16, 80, 3):
        assert int(run(exe, "level", zoom, have)) == level_ref(zoom, have), (zoom, have)


@needs_cpp
def test_level_landmarks(exe):
    both = (1 << 2) | (1 << 4)
    assert int(run(exe, "level", 0, both)) == 1        # native zoom: a stock texel per pixel already
    assert int(run(exe, "level", 13, both)) == 2       # the in-game test's zoom (tiles ~101 px)
    assert int(run(exe, "level", 72, both)) == 4       # PRACX's deepest zoom at 4K needs 3.9x
    assert int(run(exe, "level", 72, 1 << 2)) == 2     # only 2x on disk: the best there is
    assert int(run(exe, "level", 72, 0)) == 1          # no pack: stock
    every = both | (1 << 8)
    assert int(run(exe, "level", 72, every)) == 4      # an 8x master is not drawn at PRACX's deepest zoom...
    assert int(run(exe, "level", 80, every)) == 8      # ...only past zoom 74


# ------------------------------------------------------------------------------------------------- 8x master

def downscale_ref(src: np.ndarray, f: int) -> np.ndarray:
    """Each f x f block -> its most frequent index; a tie goes to the index met first in the block (row-major)."""
    h, w = src.shape
    out = np.empty((h // f, w // f), dtype=np.uint8)
    for by in range(h // f):
        for bx in range(w // f):
            block = src[by * f:(by + 1) * f, bx * f:(bx + 1) * f].ravel().tolist()
            counts = {}
            for v in block:
                counts[v] = counts.get(v, 0) + 1
            out[by, bx] = max(dict.fromkeys(block), key=lambda v: counts[v])   # max keeps the first of equals
    return out


@needs_cpp
@pytest.mark.parametrize("f", [2, 4])
@pytest.mark.parametrize("seed", [1, 2])
def test_downscale_is_block_mode_first_met_wins(exe, f, seed):
    rng = random.Random(seed)
    src = np.array([[rng.choice([10, 11, 47, 61, 9]) for _ in range(48)] for _ in range(32)], dtype=np.uint8)
    got = np.frombuffer(bytes.fromhex(run(exe, "down", 48, 32, f, src.tobytes().hex())), dtype=np.uint8)
    assert (got.reshape(32 // f, 48 // f) == downscale_ref(src, f)).all()
    assert set(np.unique(got)) <= set(np.unique(src)), "a downscale must never invent an index"


@needs_cpp
def test_downscale_undoes_a_nearest_upscale(exe):
    """An 8x master that is just the stock art blown up comes back to exactly the stock art."""
    rng = random.Random(7)
    small = np.array([[rng.randrange(10, 83) for _ in range(12)] for _ in range(6)], dtype=np.uint8)
    big = np.kron(small, np.ones((4, 4), dtype=np.uint8))
    got = np.frombuffer(bytes.fromhex(run(exe, "down", 48, 24, 4, big.tobytes().hex())), dtype=np.uint8)
    assert (got.reshape(6, 12) == small).all()


# ------------------------------------------------------------------------------------------------- sheets

@needs_cpp
def test_sheet_size_is_exactly_k_times_stock(exe):
    assert run(exe, "sheet", 2048, 1536, 2) == "1"
    assert run(exe, "sheet", 4096, -3072, 4) == "1"     # Buffer heights are stored negative
    for w, h, k in [(2048, 1536, 4), (2047, 1536, 2), (1024, 768, 2), (4096, 3071, 4)]:
        assert run(exe, "sheet", w, h, k) == "0"


# ------------------------------------------------------------------------------------------------- Scale2x

def scale2x_ref(e: np.ndarray) -> np.ndarray:
    p = np.pad(e, 1, mode="edge")
    b, h, d, f = p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]
    ok = (b != h) & (d != f)
    out = np.empty((e.shape[0] * 2, e.shape[1] * 2), dtype=e.dtype)
    out[0::2, 0::2] = np.where(ok & (d == b), d, e)
    out[0::2, 1::2] = np.where(ok & (b == f), f, e)
    out[1::2, 0::2] = np.where(ok & (d == h), d, e)
    out[1::2, 1::2] = np.where(ok & (h == f), f, e)
    return out


@needs_cpp
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_scale2x_matches_epx_and_only_copies_indices(exe, seed):
    rng = random.Random(seed)
    src = np.array([[rng.choice([10, 11, 47, 61]) for _ in range(56)] for _ in range(56)], dtype=np.uint8)
    got = np.frombuffer(bytes.fromhex(run(exe, "scale2x", 56, src.tobytes().hex())), dtype=np.uint8).reshape(112, 112)
    assert (got == scale2x_ref(src)).all()
    assert set(np.unique(got)) <= set(np.unique(src)), "Scale2x must never invent an index (shading bands)"


# ------------------------------------------------------------------------------------------------- engine sites

def _clean_exe():
    """The unpatched PRACX binary the tests compare against (work\\game-path.txt), if this checkout has one."""
    cfg = ROOT / "game-path.txt"
    if not cfg.is_file():
        return None
    game = pathlib.Path(cfg.read_text(encoding="utf-8").strip())
    for name in ("terranx.exe.bak", "terranx_PRACX.exe", "terranx.exe"):
        if (game / name).is_file():
            return game / name
    return None


def _sites_from_source():
    src = (ROOT.parent / "src" / "uimod" / "src" / "terrainhd.cpp").read_text(encoding="utf-8")

    def array(name):
        body = src.split(f"{name}[] = {{", 1)[1].split("};", 1)[0]
        code = "\n".join(line.split("//")[0] for line in body.splitlines())
        return [int(v, 16) for v in re.findall(r"0x[0-9A-Fa-f]+", code)]
    return array("kExtractSites"), array("kGenSites"), array("kUvImmediates")


def test_the_source_lists_every_site_once():
    extract, gen, imm = _sites_from_source()
    assert len(extract) == 21 and len(set(extract)) == 21
    assert len(gen) == 8 and len(set(gen)) == 8
    assert len(imm) == 8 and len(set(imm)) == 8


@pytest.mark.skipif(_clean_exe() is None, reason="no game binary configured (work\\game-path.txt)")
def test_every_site_is_stock_in_the_binary():
    pefile = pytest.importorskip("pefile")
    pe = pefile.PE(str(_clean_exe()), fast_load=True)
    img, base = pe.get_memory_mapped_image(), pe.OPTIONAL_HEADER.ImageBase

    def target(va):
        o = va - base
        assert img[o] == 0xE8, f"{va:#x} is not a call"
        return va + 5 + struct.unpack_from("<i", img, o + 1)[0]

    extract, gen, imm = _sites_from_source()
    assert all(target(s) == 0x619710 for s in extract)
    assert all(target(s) == 0x4632D0 for s in gen)
    for s in imm:
        o = s - base
        assert img[o] in (0xB8, 0xB9) and struct.unpack_from("<I", img, o + 1)[0] == 0x37, hex(s)
    for va, n in ((0x685CA8, 32), (0x686090, 56)):
        assert all(0 <= v <= 55 for v in struct.unpack_from(f"<{n}i", img, va - base)), hex(va)
