---
name: project-smac-terrain-hd
description: Drop-in HD terrain textures (issue #1) — how the engine cuts and samples terrain Textures, the three UV sources, the load remap, and the uimod module terrainhd.cpp
metadata:
  type: project
---
**What:** `<game>\terrain\texture_x2.pcx` / `texture_x4.pcx` (TEXTURE.PCX at 2x / 4x, stock palette indices) replace
the map's terrain textures at the main map's zoom. Module: `src/uimod/src/terrainhd.cpp`, laws in `terrainhd_law.h`
(tests: `work/tests/test_terrainhd.py`). Contributed on a fork for issue #1; a Frida prototype of the same lever is
on the `terrain-hd-proto` branch (`work/terrain-hd/`).

**Engine facts** (PRACX 2d0b1438; identical in the vanilla exe):
- `load_terrain` 0x451C50 (from `load_sprites` 0x455EB0, from `game_init` 0x58F2F0 — five callers, so it reruns)
  loads TEXTURE.PCX with `Buffer_load_pcx(name, 0, 10, 236)`: **loaded index = file index + 10, 255 -> 9** (the key).
  Probed against known rects in a running game; any tool that compares file pixels with in-memory textures must
  apply it.
- 19 `Texture_extract_2` 0x619710 calls cut ~160 56x56 Textures (TexHeap 0: each owns a w*h copy, stride = w);
  two more 56x56 (0x76E8A0, 0x76E910) are composited from RAINFALL.PCX through a local Buffer.
- `Texture_extract_2` fills the texture's corner/midpoint UV fields from w,h, and every rasterizer steps rows by the
  texture's own width (`[0x9BB4A8] = tex->width`) — **but the terrain pass does not use those fields.** Its UVs come
  from: the .data tables 0x685CA8 (32 dwords) and 0x686090 (56), and eight `mov r32, 0x37` (seven in
  `MapWin_gen_terrain_poly` 0x4632D0, one in `DatalinkWin_draw_terraform` 0x431180). Missing the tables shows only
  the top-left 1/k of every ground tile.
- One UV array can feed two textures in one draw (the coast pass: a TEXTURE.PCX tile + a RAINFALL composite), so
  every 56x56 terrain texture must move to level k **together**; the composites get a Scale2x of their own pixels.
- Shading = palette index + offset (`Texture_draw_2`: `add al, ch`): HD art must keep the stock palette layout.
  Scale2x (index-copying, never blending) keeps it and gives crisp edges.
- 60x60 0x78DD80 (RAINFALL) has its own UV square (0x3B) — left stock.

**Laws:** `u' = round(u * (56k - 1) / 55)` for every UV source. Level for the main map's zoom: smallest loaded k with
`k * 16 * sqrt2 >= zoom + 16` (stock when zoom <= 6); 4x is the useful maximum (PRACX's zoom 72 at 4K needs 3.9x).

**Timing gotcha (in-game, 2026-11-11):** in the first run of the module the textures were already cut when it was
installed and nothing was recorded, so it never switched. The module now also adopts already-loaded textures at the
main map's first terrain pass (found in .data by shape, placed in TEXTURE.PCX by pixels, the sheet loaded with the
same remap). The second run took the recorder path (no "adopted" log line) — the first run's ordering is not yet
explained, so both paths stay.

**Verified in game** (PRACX + Thinker 5.5, 2560x1440): levels 1/2/4 switch with zoom, 162 textures, renders match
stock geometry at 4x; the thin diagonal tile-edge lines are the stock game's own.

**Open:** a Preferences row (only the `[UIMod] TerrainHD=0` ini switch today); real HD art (the tests used Scale2x
sheets made from the stock file); the terrain overlay SPRITES (rocks, forests' sprite parts, farms) are separate
sprites, not these textures, and stay stock.
