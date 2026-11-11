// terrainhd.cpp -- drop-in HD terrain textures (GitHub issue #1: "the ability to drop-in their own custom terrain
// packs", like the custom portrait files).
//
// A pack is <game>\terrain\texture_x<k>.pcx for k = 2 and/or 4: TEXTURE.PCX redrawn at k x (2048x1536 / 4096x3072),
// 8-bit, painted in the STOCK file's palette indices. The rasterizer shades by adding an offset to the index
// (Texture_draw_2 0x61B1F0: `add al, ch`), so a pack in another palette layout shades into the wrong colours.
// 4x is the useful maximum: at PRACX's deepest zoom (72, 4K) a tile needs (72+16)/(16*sqrt2) = 3.9 x the texels.
//
// Engine (PRACX 2d0b1438 and the vanilla exe alike; prototype + in-game run in work\terrain-hd on the
// terrain-hd-proto branch):
//  * load_terrain 0x451C50 (called from load_sprites, i.e. every game_init) loads TEXTURE.PCX into the loading
//    canvas 0x798668 with Buffer_load_pcx(name, 0, 10, 236) -- loaded index = file index + 10, 255 -> 9 (the key) --
//    and cuts the terrain Textures out of it: 19 calls of Texture_extract_2 0x619710 (0x451D0B..0x452171), 56x56,
//    TexHeap 0, so each Texture owns a w*h copy whose row stride is its width. Two more 56x56 textures (0x76E8A0,
//    0x76E910; calls 0x4522CA / 0x4522E5) are composited from RAINFALL.PCX through a local buffer.
//  * Every rasterizer steps rows by the texture's own width ([0x9BB4A8] = tex->width), so a 56k x 56k texture
//    draws on the same polygon -- given UVs that span 0..56k-1.
//  * The terrain pass's UVs come from two .data tables -- 0x685CA8 (32 dwords: quarter-tile quads, read @0x4645C4)
//    and 0x686090 (56: triangles @0x463EFF/0x463F14/0x463F35/0x464254/0x464ED2/0x465088, quads @0x463FDE, which the
//    coast pass copies) -- and eight `mov r32, 0x37` that build a 0..55 square (seven in MapWin_gen_terrain_poly,
//    one in DatalinkWin_draw_terraform 0x431180). One law: terrainhd::ScaleUv.
//  * Several draws sample two textures through ONE UV array (the coast pass: a TEXTURE.PCX tile + a RAINFALL
//    composite), so every 56x56 terrain texture moves to level k together. The RAINFALL two get a Scale2x of their
//    own stock pixels. The 60x60 texture 0x78DD80 has its own UV square (0x3B) and stays stock.
//
// Levers, all byte-checked, all-or-nothing, cave-free:
//  * the 21 extract calls -> ExtractRecorder: records each 56x56 Texture, its source rect and its stock pixels. A
//    re-cut (the next game_init) puts the textures back at stock, so the UVs go back to stock with them. The first
//    game_init can run before this module is installed; the textures it cut are then adopted at the main map's
//    first terrain pass (AdoptLoadedTextures: found in .data by shape, placed in TEXTURE.PCX by their pixels).
//  * the 8 calls of MapWin_gen_terrain_poly 0x4632D0 (cdecl, 10 args) -> GenTerrainPolyHook: before the main map's
//    terrain pass, the level for its zoom is installed (terrainhd::LevelFor) -- on the game thread, before any
//    texture is read. Other consoles (the minimap, the base screen) draw with whatever level is installed.
//  * a level is installed by re-cutting every recorded Texture through the engine's own Texture_extract_2 (HD sheet
//    rect x k, or a scratch Buffer holding the upscaled / stock pixels), then rewriting the UV tables and immediates.
//    Any failed extract puts every texture back to stock and switches the feature off for the session.
// Nothing changes without a pack on disk; [UIMod] TerrainHD=0 turns it off with one.

#include "terrainhd.h"
#include "terrainhd_law.h"

#include <windows.h>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

void UimodLog(const char* msg);

namespace {

constexpr uintptr_t kVA_Extract2       = 0x619710;   // thiscall (Texture*, Buffer* src, x, y, w, h, TexHeap*) RET 0x18
constexpr uintptr_t kVA_GenTerrainPoly = 0x4632D0;   // cdecl (Buffer*, MapWindow*, zoom, faction, 6 more)
constexpr uintptr_t kVA_BufferCtor     = 0x5D7210;   // thiscall (Buffer*)
constexpr uintptr_t kVA_BufferResize   = 0x5D7670;   // thiscall (Buffer*, w, h, 0, 0) RET 0x10 -- as load_terrain sizes its own
constexpr uintptr_t kVA_LoadPcx        = 0x5D7DE0;   // thiscall (Buffer*, name, pal, a4, a5) RET 0x10; RESIZES self
constexpr uintptr_t kVA_LoadingCanvas  = 0x798668;   // the Buffer TEXTURE.PCX is loaded into
constexpr uintptr_t kVA_MainConsole    = 0x9156B0;   // MapWin_g

const uint32_t kExtractSites[] = {
    0x451D0B, 0x451D27, 0x451D50, 0x451D8F, 0x451DB6, 0x451DE0, 0x451E24, 0x451E86, 0x451EBF, 0x451EE9,
    0x451F32, 0x451F76, 0x451FBF, 0x452000, 0x452030, 0x452081, 0x4520E1, 0x452128, 0x452171,   // TEXTURE.PCX
    0x4522CA, 0x4522E5,                                                                         // RAINFALL composites
};
const uint32_t kGenSites[] = { 0x40F35F, 0x468C73, 0x46927C, 0x469308, 0x469E47, 0x47130C, 0x4B6B9C, 0x4BDF5C };
const uint32_t kUvImmediates[] = { 0x46485D, 0x4649BB, 0x464AF8, 0x464FD8, 0x465561, 0x465C81, 0x465D43, 0x4311B2 };
struct UvTable { uint32_t va; int count; };
const UvTable kUvTables[] = { { 0x685CA8, 32 }, { 0x686090, 56 } };

// Buffer fields (Thinker engine_win.h; Buffer is 0x588 bytes)
constexpr int kBufData = 0x50, kBufBits = 0x54, kBufW = 0x80, kBufH = 0x84, kBufPitch = 0x4A8, kBufSize = 0x588;
constexpr int kTex = terrainhd::kTile;

struct Texture { uint8_t* data; int w; int h; };       // the first three of 0x70 bytes

struct Entry {
    Texture* tex;
    int x, y;
    bool sheet;                                        // cut from TEXTURE.PCX (else a RAINFALL composite)
    uint8_t stock[kTex * kTex];
};

typedef int (__thiscall* FExtract2)(Texture*, void*, int, int, int, int, void*);
typedef int (__cdecl* FGen)(void*, void*, int, int, int, int, int, int, int, int);
typedef void (__thiscall* FBufferCtor)(void*);
typedef int (__thiscall* FBufferResize)(void*, int, int, int, int);
typedef int (__thiscall* FLoadPcx)(void*, const char*, void*, int, int);

FExtract2 g_extract = nullptr;
FGen g_gen = nullptr;
std::vector<Entry> g_entries;
int g_level = 1;                                       // installed level; 1 = stock
unsigned g_have = 0;                                   // bit k: texture_x<k>.pcx is on disk with the right size
unsigned g_loaded = 0;                                 // bit k: that sheet is loaded into g_sheet[k]
bool g_dead = false;                                   // a failed install: stock for the rest of the session
alignas(16) uint8_t g_sheet[5][kBufSize + 0x80];       // index 2 and 4: the HD sheets; 1: the stock sheet (adoption)
alignas(16) uint8_t g_scratch[kBufSize + 0x80];
bool g_scratchReady = false;
int32_t g_uvStock[2][56];
char g_dir[MAX_PATH];                                  // game folder, with trailing backslash

BYTE* At(uintptr_t va) { return reinterpret_cast<BYTE*>(GetModuleHandleW(nullptr)) + (va - 0x400000); }

void Log(const char* fmt, ...)
{
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf_s(buf, sizeof buf, _TRUNCATE, fmt, ap);
    va_end(ap);
    UimodLog(buf);
}

bool CallsTo(uint32_t site, uintptr_t target)
{
    const BYTE* p = At(site);
    if (p[0] != 0xE8) return false;
    int32_t rel;
    memcpy(&rel, p + 1, 4);
    return reinterpret_cast<uintptr_t>(p) + 5 + rel == reinterpret_cast<uintptr_t>(At(target));
}

void Retarget(uint32_t site, const void* fn)
{
    BYTE* p = At(site);
    const int32_t rel = static_cast<int32_t>(reinterpret_cast<uintptr_t>(fn) - (reinterpret_cast<uintptr_t>(p) + 5));
    DWORD old;
    VirtualProtect(p + 1, 4, PAGE_EXECUTE_READWRITE, &old);
    memcpy(p + 1, &rel, 4);
    VirtualProtect(p + 1, 4, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
}

void WriteUv(int k)
{
    for (int t = 0; t < 2; ++t) {
        int32_t* p = reinterpret_cast<int32_t*>(At(kUvTables[t].va));
        DWORD old;
        VirtualProtect(p, kUvTables[t].count * 4, PAGE_READWRITE, &old);
        for (int i = 0; i < kUvTables[t].count; ++i) p[i] = terrainhd::ScaleUv(g_uvStock[t][i], k);
        VirtualProtect(p, kUvTables[t].count * 4, old, &old);
    }
    const uint32_t imm = static_cast<uint32_t>(terrainhd::ScaleUv(55, k));
    for (uint32_t site : kUvImmediates) {
        BYTE* p = At(site);
        DWORD old;
        VirtualProtect(p + 1, 4, PAGE_EXECUTE_READWRITE, &old);
        memcpy(p + 1, &imm, 4);
        VirtualProtect(p + 1, 4, old, &old);
        FlushInstructionCache(GetCurrentProcess(), p, 5);
    }
}

// --- Buffers ----------------------------------------------------------------------------------------------------

uint8_t* Pixels(void* buf)
{
    uint8_t* d = *reinterpret_cast<uint8_t**>(static_cast<uint8_t*>(buf) + kBufData);
    return d ? d : *reinterpret_cast<uint8_t**>(static_cast<uint8_t*>(buf) + kBufBits);
}

int Field(void* buf, int off) { return *reinterpret_cast<int*>(static_cast<uint8_t*>(buf) + off); }

// The scratch Buffer at side x side holding `px` (row stride `side`). False if the engine would not size it.
bool FillScratch(const uint8_t* px, int side)
{
    if (!g_scratchReady) {
        reinterpret_cast<FBufferCtor>(At(kVA_BufferCtor))(g_scratch);
        g_scratchReady = true;
    }
    if (Field(g_scratch, kBufW) != side || (Field(g_scratch, kBufH) != side && Field(g_scratch, kBufH) != -side))
        reinterpret_cast<FBufferResize>(At(kVA_BufferResize))(g_scratch, side, side, 0, 0);
    uint8_t* d = Pixels(g_scratch);
    const int pitch = Field(g_scratch, kBufPitch);
    if (!d || Field(g_scratch, kBufW) != side || pitch < side) return false;
    for (int y = 0; y < side; ++y) memcpy(d + y * pitch, px + y * side, side);
    return true;
}

bool SheetPath(int k, char* out, size_t n)
{
    return _snprintf_s(out, n, _TRUNCATE, "%sterrain\\texture_x%d.pcx", g_dir, k) > 0;
}

// The PCX header's size, read before the engine loads anything (a wrong file is refused without an allocation).
bool PcxSize(const char* path, int* w, int* h)
{
    FILE* f = nullptr;
    if (fopen_s(&f, path, "rb") != 0 || !f) return false;
    uint8_t hdr[128];
    const bool ok = fread(hdr, 1, sizeof hdr, f) == sizeof hdr;
    fclose(f);
    if (!ok || hdr[0] != 10 || hdr[3] != 8 || hdr[65] != 1) return false;   // 8-bit, one plane
    const int x0 = hdr[4] | hdr[5] << 8, y0 = hdr[6] | hdr[7] << 8, x1 = hdr[8] | hdr[9] << 8, y1 = hdr[10] | hdr[11] << 8;
    *w = x1 - x0 + 1;
    *h = y1 - y0 + 1;
    return true;
}

bool LoadSheet(int k)
{
    if (g_loaded & (1u << k)) return true;
    char path[MAX_PATH];
    if (!SheetPath(k, path, sizeof path)) return false;
    reinterpret_cast<FBufferCtor>(At(kVA_BufferCtor))(g_sheet[k]);
    reinterpret_cast<FLoadPcx>(At(kVA_LoadPcx))(g_sheet[k], path, nullptr, 10, 0xEC);   // load_terrain's own arguments
    if (!terrainhd::SheetSizeOk(Field(g_sheet[k], kBufW), Field(g_sheet[k], kBufH), k) || !Pixels(g_sheet[k])) {
        Log("[terrainhd] texture_x%d.pcx did not load as %dx%d -> level %d off\n", k,
            terrainhd::kStockSheetW * k, terrainhd::kStockSheetH * k, k);
        g_have &= ~(1u << k);
        return false;
    }
    g_loaded |= 1u << k;
    return true;
}

// --- levels -----------------------------------------------------------------------------------------------------

bool CutAll(int k)
{
    const int side = kTex * k;
    std::vector<uint8_t> a, b;
    for (Entry& e : g_entries) {
        int rc;
        if (k == 1) {
            rc = FillScratch(e.stock, kTex) ? g_extract(e.tex, g_scratch, 0, 0, kTex, kTex, nullptr) : -1;
        } else if (e.sheet) {
            rc = g_extract(e.tex, g_sheet[k], e.x * k, e.y * k, side, side, nullptr);
        } else {
            a.assign(e.stock, e.stock + kTex * kTex);
            for (int n = kTex; n < side; n *= 2) {
                b.resize(4 * n * n);
                terrainhd::Scale2x(a.data(), n, b.data());
                a.swap(b);
            }
            rc = FillScratch(a.data(), side) ? g_extract(e.tex, g_scratch, 0, 0, side, side, nullptr) : -1;
        }
        if (rc != 0) {
            Log("[terrainhd] level %d: extract of texture %p failed (%d)\n", k, static_cast<void*>(e.tex), rc);
            return false;
        }
    }
    return true;
}

void SetLevel(int k)
{
    if (k != 1 && !LoadSheet(k)) return;
    if (!CutAll(k)) {
        // a failed extract has already freed that texture's copy: every one back to stock, then stay there
        const bool back = k != 1 && CutAll(1);
        WriteUv(1);
        g_level = 1;
        g_dead = true;
        Log(back ? "[terrainhd] stock textures restored; HD terrain off for this session\n"
                 : "[terrainhd] could not restore stock textures; HD terrain off for this session\n");
        return;
    }
    WriteUv(k);
    if (g_level != k) Log("[terrainhd] level %d (%u textures)\n", k, static_cast<unsigned>(g_entries.size()));
    g_level = k;
}

// --- hooks ------------------------------------------------------------------------------------------------------

// Stands in for Texture_extract_2 at load_terrain's 21 sites: same contract (thiscall, RET 0x18).
int __fastcall ExtractRecorder(Texture* tex, void* /*edx*/, void* src, int x, int y, int w, int h, void* heap)
{
    const int rc = g_extract(tex, src, x, y, w, h, heap);
    if (rc != 0 || w != kTex || h != kTex || tex->w != kTex || tex->h != kTex || !tex->data) return rc;
    if (g_level != 1) {                       // a new load_terrain: the textures are being cut at stock again
        WriteUv(1);
        g_level = 1;
    }
    Entry* e = nullptr;
    for (Entry& it : g_entries) if (it.tex == tex) { e = &it; break; }
    if (!e) {
        g_entries.emplace_back();
        e = &g_entries.back();
        e->tex = tex;
    }
    e->x = x;
    e->y = y;
    e->sheet = src == At(kVA_LoadingCanvas);
    memcpy(e->stock, tex->data, sizeof e->stock);
    return rc;
}

// The textures load_terrain cut BEFORE this module was installed (the first game_init can run before uimod's Init, as
// it did in the first in-game run of this module): found in .data by their Texture shape, and each 56x56 copy located
// in TEXTURE.PCX loaded the way load_terrain loads it, so the indices compare as they are. One that is in no rect of
// the sheet is a RAINFALL composite. Once, at the main map's first terrain pass.
bool g_adoptTried = false;

bool LooksLikeTerrainTexture(const uint8_t* p)
{
    const int* f = reinterpret_cast<const int*>(p);
    return f[0] != 0 && f[1] == kTex && f[2] == kTex && f[0x14 / 4] == kTex - 1 && f[0x1C / 4] == kTex - 1 &&
           f[0x20 / 4] == kTex - 1 && f[0x6C / 4] == 0;
}

void AdoptLoadedTextures()
{
    g_adoptTried = true;
    char path[MAX_PATH];
    if (_snprintf_s(path, sizeof path, _TRUNCATE, "%stexture.pcx", g_dir) <= 0) return;
    reinterpret_cast<FBufferCtor>(At(kVA_BufferCtor))(g_sheet[1]);
    reinterpret_cast<FLoadPcx>(At(kVA_LoadPcx))(g_sheet[1], path, nullptr, 10, 0xEC);   // load_terrain's arguments
    const uint8_t* sheet = Pixels(g_sheet[1]);
    const int sw = Field(g_sheet[1], kBufW), sh = Field(g_sheet[1], kBufH) < 0 ? -Field(g_sheet[1], kBufH) : Field(g_sheet[1], kBufH);
    const int pitch = Field(g_sheet[1], kBufPitch);
    if (!sheet || sw != terrainhd::kStockSheetW || sh != terrainhd::kStockSheetH) {
        UimodLog("[terrainhd] texture.pcx did not load at 1024x768 -> HD terrain off\n");
        g_dead = true;
        return;
    }
    // .data of the exe (the Texture objects are statics there)
    const BYTE* base = At(0x400000);
    const IMAGE_NT_HEADERS* nt = reinterpret_cast<const IMAGE_NT_HEADERS*>(base + reinterpret_cast<const IMAGE_DOS_HEADER*>(base)->e_lfanew);
    const IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
    int matched = 0;
    for (int s = 0; s < nt->FileHeader.NumberOfSections; ++s, ++sec) {
        if (memcmp(sec->Name, ".data", 6) != 0) continue;
        const uint8_t* lo = base + sec->VirtualAddress;
        const uint8_t* hi = lo + sec->Misc.VirtualSize - 0x70;
        for (const uint8_t* p = lo; p <= hi; p += 4) {
            if (!LooksLikeTerrainTexture(p)) continue;
            Texture* tex = reinterpret_cast<Texture*>(const_cast<uint8_t*>(p));
            Entry e{};
            e.tex = tex;
            memcpy(e.stock, tex->data, sizeof e.stock);
            for (int y = 0; y + kTex <= sh && !e.sheet; ++y) {
                const uint8_t* row = sheet + y * pitch;
                for (int x = 0; x + kTex <= sw; ++x) {
                    if (row[x] != e.stock[0] || memcmp(row + x, e.stock, kTex) != 0) continue;
                    int r = 1;
                    while (r < kTex && memcmp(sheet + (y + r) * pitch + x, e.stock + r * kTex, kTex) == 0) ++r;
                    if (r == kTex) { e.sheet = true; e.x = x; e.y = y; break; }
                }
            }
            matched += e.sheet;
            g_entries.push_back(e);
        }
    }
    Log("[terrainhd] adopted %u loaded terrain textures (%d placed in TEXTURE.PCX)\n",
        static_cast<unsigned>(g_entries.size()), matched);
    if (matched == 0) {
        g_entries.clear();
        g_dead = true;
        UimodLog("[terrainhd] no texture matched TEXTURE.PCX -> HD terrain off\n");
    }
}

int __cdecl GenTerrainPolyHook(void* buf, void* win, int zoom, int a4, int a5, int a6, int a7, int a8, int a9, int a10)
{
    if (!g_dead && g_have && win == At(kVA_MainConsole) && g_entries.empty() && !g_adoptTried) AdoptLoadedTextures();
    if (!g_dead && g_have && !g_entries.empty() && win == At(kVA_MainConsole)) {
        const int want = terrainhd::LevelFor(zoom, g_have);
        if (want != g_level) SetLevel(want);
    }
    return g_gen(buf, win, zoom, a4, a5, a6, a7, a8, a9, a10);
}

bool GameDir(char* out, size_t n)
{
    const DWORD len = GetModuleFileNameA(nullptr, out, static_cast<DWORD>(n));
    if (len == 0 || len >= n) return false;
    char* slash = strrchr(out, '\\');
    if (!slash) return false;
    slash[1] = 0;
    return true;
}

} // namespace

void InstallTerrainHd()
{
    if (!GameDir(g_dir, sizeof g_dir)) return;
    char ini[MAX_PATH];
    if (_snprintf_s(ini, sizeof ini, _TRUNCATE, "%sAlpha Centauri.ini", g_dir) > 0 &&
        GetPrivateProfileIntA("UIMod", "TerrainHD", 1, ini) == 0) {
        UimodLog("[terrainhd] off ([UIMod] TerrainHD=0)\n");
        return;
    }
    for (int k : terrainhd::kLevels) {
        char path[MAX_PATH];
        int w = 0, h = 0;
        if (!SheetPath(k, path, sizeof path) || GetFileAttributesA(path) == INVALID_FILE_ATTRIBUTES) continue;
        if (PcxSize(path, &w, &h) && terrainhd::SheetSizeOk(w, h, k)) {
            g_have |= 1u << k;
        } else {
            Log("[terrainhd] terrain\\texture_x%d.pcx is %dx%d, not an 8-bit %dx%d PCX -> ignored\n", k, w, h,
                terrainhd::kStockSheetW * k, terrainhd::kStockSheetH * k);
        }
    }
    if (!g_have) return;                      // no pack: nothing patched

    for (uint32_t s : kExtractSites) if (!CallsTo(s, kVA_Extract2)) {
        Log("[terrainhd] unexpected bytes at extract call %X -> HD terrain NOT installed\n", s);
        return;
    }
    for (uint32_t s : kGenSites) if (!CallsTo(s, kVA_GenTerrainPoly)) {
        Log("[terrainhd] unexpected bytes at gen_terrain_poly call %X -> HD terrain NOT installed\n", s);
        return;
    }
    for (uint32_t s : kUvImmediates) {
        const BYTE* p = At(s);
        uint32_t imm;
        memcpy(&imm, p + 1, 4);
        if ((p[0] != 0xB8 && p[0] != 0xB9) || imm != 0x37) {
            Log("[terrainhd] unexpected bytes at UV immediate %X -> HD terrain NOT installed\n", s);
            return;
        }
    }
    for (int t = 0; t < 2; ++t) {
        memcpy(g_uvStock[t], At(kUvTables[t].va), kUvTables[t].count * 4);
        for (int i = 0; i < kUvTables[t].count; ++i) if (g_uvStock[t][i] < 0 || g_uvStock[t][i] > 55) {
            Log("[terrainhd] UV table %X holds non-stock values -> HD terrain NOT installed\n", kUvTables[t].va);
            return;
        }
    }

    g_extract = reinterpret_cast<FExtract2>(At(kVA_Extract2));
    g_gen = reinterpret_cast<FGen>(At(kVA_GenTerrainPoly));
    g_entries.reserve(192);
    for (uint32_t s : kExtractSites) Retarget(s, reinterpret_cast<const void*>(&ExtractRecorder));
    for (uint32_t s : kGenSites) Retarget(s, reinterpret_cast<const void*>(&GenTerrainPolyHook));
    Log("[terrainhd] installed: levels%s%s from terrain\\ (21 extract + 8 terrain-pass sites)\n",
        (g_have & 4) ? " 2x" : "", (g_have & 16) ? " 4x" : "");
}
