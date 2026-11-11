"use strict";
// HD TERRAIN TEXTURES -- Frida PROTOTYPE (writes to the game process). Driven by proto_run.py.
//
// Engine facts (terranx.exe, PRACX 2d0b1438; read from the clean binary):
//  * load_terrain 0x451C50 cuts the map's terrain Textures out of TEXTURE.PCX with Texture_extract_2 0x619710
//    (19 calls 0x451D0B..0x452171: 56x56, TexHeap 0 -> each Texture owns a w*h copy, row stride = w). Two more
//    56x56 textures (0x76E8A0, 0x76E910) are composited from RAINFALL.PCX through a local buffer.
//  * Every rasterizer steps rows by the texture's own width (Texture_draw_2 inner loop: [0x9BB4A8] = tex->width),
//    so a texture cut at 56k x 56k draws fine -- as long as the UVs it is given span 0..56k-1.
//  * The UVs of the terrain pass are NOT derived from the texture. Three sources, all in stock texel units:
//      - two .data tables: 0x685CA8..0x685D28 (4 quarter-tile quads x 4 verts; read at 0x4645C4) and
//        0x686090..0x686170 (12 triangle verts @0x463EFF/0x463F14/0x463F35/0x464254/0x464ED2/0x465088, then
//        16 quad verts @0x463FDE -- the coast pass copies these);
//      - eight `mov r32, 0x37` immediates that build a (0,0)-(55,55) square (seven in MapWin_gen_terrain_poly,
//        one in DatalinkWin_draw_terraform 0x431180).
//    One law for all of them: u' = round(u * (56k - 1) / 55). Because several draws sample two textures with ONE
//    UV array (coast pass: TEXTURE.PCX tile + a RAINFALL texture), every 56x56 texture moves to level k together.
//  * The 60x60 texture 0x78DD80 has its own UV square (0x3B) and is left alone.
//  * Shading = palette index + offset (`add al, ch`): HD art must keep the stock palette layout.
//
// Messages from the host (proto_run.py):
//  scan()                        rpc: every 56x56 Texture in .data: address + stock pixels
//  post {type:'blocks', k, addrs} + data: the k x pixels of each texture, addrs order, (56k)^2 bytes each
//  setLevel(k) / setAuto(on)     rpc: install level k (k=1 restores the stock pixels) at the next
//                                gen_terrain_poly entry, then request a full redraw
//  dump(name)                    rpc: send the main map canvas after the next full terrain pass
//  status()                      rpc

const BASE = Process.mainModule.base;
const at = (va) => BASE.add(va - 0x400000);

const VA = {
    genTerrainPoly: 0x4632D0,   // cdecl (Buffer*, MapWindow*, zoom, faction, a5, flags, x1, y1, x2, y2)
    drawCursor:     0x46AE00,   // cdecl, blink timer -> an idle point on the game thread
    drawMap:        0x46B190,   // cdecl draw_map(int): full redraw
    extract2:       0x619710,   // thiscall Texture_extract_2(Texture*, Buffer*, x, y, w, h, TexHeap*)
    bufferCtor:     0x5D7210,   // thiscall Buffer_ctor(Buffer*)
    bufferResize:   0x5D7670,   // thiscall (Buffer*, w, h, 0, 0) -- as load_terrain sizes its scratch buffers
    mainConsole:    0x9156B0,   // MapWin_g
    dataLo:         0x682000,   // .data
    dataHi:         0x9C21F8,
};
const STOCK_BYTES = { [VA.genTerrainPoly]: "558bec6aff68", [VA.drawCursor]: "558bec51a1b0", [VA.extract2]: "535556578b7c" };
const UV_IMMEDIATES = [0x46485D, 0x4649BB, 0x464AF8, 0x464FD8, 0x465561, 0x465C81, 0x465D43, 0x4311B2];
const UV_TABLES = [[0x685CA8, 32], [0x686090, 56]];                    // [first dword VA, dword count]

const extract2 = new NativeFunction(at(VA.extract2), "int",
    ["pointer", "pointer", "int", "int", "int", "int", "pointer"], "thiscall");
const bufferCtor = new NativeFunction(at(VA.bufferCtor), "void", ["pointer"], "thiscall");
const bufferResize = new NativeFunction(at(VA.bufferResize), "int", ["pointer", "int", "int", "int", "int"], "thiscall");
const drawMap = new NativeFunction(at(VA.drawMap), "void", ["int"], "mscdecl");

let textures = [];          // [{tex, stock: NativePointer to 3136 bytes}]
const blocks = {};          // k -> NativePointer, textures.length * (56k)^2 bytes
const tableStock = UV_TABLES.map(([va, n]) => Array.from({ length: n }, (_, i) => at(va + 4 * i).readS32()));
let level = 1;
let pendingLevel = null;
let auto = false;
let redrawPending = false;
let dumpName = null;
let scratch = null, scratchSide = 0;
const zoomSamples = {};
const errors = [];

function hex(p, n) {
    return Array.from(new Uint8Array(p.readByteArray(n)), (b) => b.toString(16).padStart(2, "0")).join("");
}

function checkStock() {
    for (const [va, want] of Object.entries(STOCK_BYTES)) {
        const got = hex(at(Number(va)), 6);
        if (got !== want) throw new Error(`unexpected bytes at ${Number(va).toString(16)}: ${got} (want ${want})`);
    }
    for (const va of UV_IMMEDIATES) {
        const op = at(va).readU8();
        if ((op !== 0xB8 && op !== 0xB9) || at(va + 1).readU32() !== 0x37)
            throw new Error(`UV immediate ${va.toString(16)} is not mov r32, 0x37 (a previous run left a level on?)`);
    }
    for (const t of tableStock) if (t.some((v) => v < 0 || v > 55)) throw new Error("UV table holds non-stock values");
}

function scratchBuffer(side) {
    if (scratch && scratchSide === side) return scratch;
    const buf = Memory.alloc(0x800);
    bufferCtor(buf);
    bufferResize(buf, side, side, 0, 0);
    if (buf.add(0x80).readS32() !== side || Math.abs(buf.add(0x84).readS32()) !== side)
        throw new Error(`scratch buffer resize to ${side} failed`);
    scratch = buf;
    scratchSide = side;
    return buf;
}

const scaleUv = (u, k) => Math.round(u * (56 * k - 1) / 55);

function applyLevel(k) {
    const side = 56 * k;
    const src = k === 1 ? null : blocks[k];
    if (k !== 1 && !src) throw new Error(`no blocks posted for level ${k}`);
    const buf = scratchBuffer(side);
    const pitch = buf.add(0x4A8).readS32();
    let data = buf.add(0x50).readPointer();
    if (data.isNull()) data = buf.add(0x54).readPointer();
    let failed = 0;
    textures.forEach((t, i) => {
        const px = k === 1 ? t.stock : src.add(i * side * side);
        for (let y = 0; y < side; y++) Memory.copy(data.add(y * pitch), px.add(y * side), side);
        if (extract2(t.tex, buf, 0, 0, side, side, ptr(0)) !== 0) failed++;
    });
    if (failed) {
        // a failed extract leaves its Texture EMPTY (it frees the old copy first): put every one back to stock
        if (k !== 1) { applyLevel(1); }
        throw new Error(`level ${k}: ${failed} extracts failed -> restored stock`);
    }
    UV_TABLES.forEach(([va], ti) => tableStock[ti].forEach((u, i) => at(va + 4 * i).writeS32(scaleUv(u, k))));
    for (const va of UV_IMMEDIATES) Memory.patchCode(at(va + 1), 4, (code) => code.writeU32(scaleUv(55, k)));
    level = k;
    send({ type: "applied", level: k, textures: textures.length, uv55: scaleUv(55, k) });
}

// Main-map zoom -> level. A tile is a 56*(zoom+16)/16 px wide diamond showing the 56k-texel square along its
// diagonal (79k texels): 1:1 horizontally needs k >= 0.709 * (zoom+16)/16. Smallest posted level that covers it.
function levelFor(zoom) {
    const need = 0.709 * (zoom + 16) / 16;
    const have = [1, ...Object.keys(blocks).map(Number)].sort((a, b) => a - b);
    return have.find((k) => k >= need) || have[have.length - 1];
}

Interceptor.attach(at(VA.genTerrainPoly), {
    onEnter(args) {
        this.main = args[1].equals(at(VA.mainConsole));
        this.canvas = args[0];
        const zoom = args[2].toInt32();
        const key = `${this.main ? "main" : args[1]}:${zoom}`;
        zoomSamples[key] = (zoomSamples[key] || 0) + 1;
        if (this.main && auto && pendingLevel === null) {
            const want = levelFor(zoom);
            if (want !== level) pendingLevel = want;
        }
        if (pendingLevel !== null) {
            const k = pendingLevel;
            pendingLevel = null;
            try { applyLevel(k); redrawPending = true; } catch (e) { errors.push(String(e)); send({ type: "error-msg", error: String(e) }); }
        }
        this.full = this.main && (args[8].toInt32() - args[6].toInt32()) > 6;   // the window, not a cursor radius
    },
    onLeave() {
        if (!this.full || dumpName === null || redrawPending || pendingLevel !== null) return;
        const b = this.canvas;
        const w = b.add(0x80).readS32(), h = Math.abs(b.add(0x84).readS32()), pitch = b.add(0x4A8).readS32();
        let data = b.add(0x50).readPointer();
        if (data.isNull()) data = b.add(0x54).readPointer();
        if (data.isNull()) { errors.push("canvas has no CPU pixels"); dumpName = null; return; }
        send({ type: "dump", name: dumpName, w, h, pitch, level }, data.readByteArray(pitch * h));
        send({ type: "palette", name: dumpName }, b.add(0xA4).readByteArray(1024));
        dumpName = null;
    },
});

Interceptor.attach(at(VA.drawCursor), {
    onLeave() {
        if (!redrawPending) return;
        redrawPending = false;
        drawMap(1);
    },
});

function onBlocks(msg, data) {
    const k = msg.k, side = 56 * k;
    if (msg.addrs.length !== textures.length || msg.addrs.some((a, i) => !at(a).equals(textures[i].tex)) ||
        data.byteLength !== textures.length * side * side) {
        errors.push(`blocks for level ${k} do not match the scan`);
    } else {
        const mem = Memory.alloc(data.byteLength);
        mem.writeByteArray(data);
        blocks[k] = mem;
        send({ type: "blocks-ok", k });
    }
    recv("blocks", onBlocks);
}
recv("blocks", onBlocks);

rpc.exports = {
    check() { checkStock(); return "ok"; },
    scan() {
        const out = [];
        const n = VA.dataHi - VA.dataLo;
        const view = new DataView(at(VA.dataLo).readByteArray(n));
        textures = [];
        for (let off = 0; off + 0x70 <= n; off += 4) {
            if (view.getInt32(off + 4, true) !== 56 || view.getInt32(off + 8, true) !== 56) continue;
            if (view.getInt32(off + 0x14, true) !== 55 || view.getInt32(off + 0x1C, true) !== 55 ||
                view.getInt32(off + 0x20, true) !== 55 || view.getInt32(off + 0x6C, true) !== 0) continue;
            const data = ptr(view.getUint32(off, true));
            if (data.isNull()) continue;
            const stock = Memory.alloc(56 * 56);
            Memory.copy(stock, data, 56 * 56);
            textures.push({ tex: at(VA.dataLo + off), stock });
            out.push({ addr: VA.dataLo + off, hex: hex(stock, 56 * 56) });
        }
        return out;
    },
    setLevel(k) { auto = false; pendingLevel = k; redrawPending = true; return k; },
    setAuto(on) { auto = !!on; redrawPending = true; return auto; },
    redraw() { redrawPending = true; },
    dump(name) { dumpName = name; redrawPending = true; },
    status() {
        return { level, auto, pendingLevel, redrawPending, dumpPending: dumpName, textures: textures.length,
                 levels: Object.keys(blocks), zoomSamples, errors };
    },
};
