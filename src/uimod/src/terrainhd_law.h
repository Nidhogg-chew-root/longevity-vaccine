// HD terrain textures: the pure laws (no Windows, no engine), shared by terrainhd.cpp and its self-test
// (work\tests\terrainhd_selftest.cpp). See terrainhd.cpp for the engine side.
#pragma once
#include <cstddef>
#include <cstdint>

namespace terrainhd {

// The stock terrain texture is 56x56 texels; a level-k sheet carries every tile at 56k x 56k.
constexpr int kTile = 56;
constexpr int kStockSheetW = 1024, kStockSheetH = 768;   // TEXTURE.PCX
constexpr int kLevels[] = { 2, 4 };                       // offered HD levels; 1 = stock

// The engine's terrain UVs are in stock texel units, 0..55 (two .data tables + eight `mov r32, 0x37`).
// Level k spans the same polygon over 0..56k-1: u' = round(u * (56k - 1) / 55). 0 -> 0, 55 -> 56k-1, monotonic.
inline int ScaleUv(int u, int k)
{
    return (u * (kTile * k - 1) * 2 + 55) / (55 * 2);
}

// The main map's zoom -> the level that keeps a tile at least 1 texel per pixel across. A tile is a
// 56*(zoom+16)/16 px wide diamond showing the 56k-texel square along its diagonal (56k*sqrt2 texels), so
// k >= 56*(zoom+16)/16 / (56*sqrt2) = (zoom+16) / (16*sqrt2). `have` is a bit mask of the loaded levels (bit k);
// the smallest loaded level that covers it, else the largest loaded, else 1 (stock).
inline int LevelFor(int zoom, unsigned have)
{
    // k*16*sqrt2 >= zoom+16, in integers: (k*16)^2 * 2 >= (zoom+16)^2 -- for zoom+16 > 0; anything smaller is stock
    if (zoom + 16 <= 0) return 1;
    const long long need2 = static_cast<long long>(zoom + 16) * (zoom + 16);
    if (1LL * 16 * 16 * 2 >= need2) return 1;                // stock already covers it
    int best = 1;
    for (int k : kLevels) {
        if (!(have & (1u << k))) continue;
        best = k;
        if (1LL * (k * 16) * (k * 16) * 2 >= need2) return k;
    }
    return best;
}

// A level-k sheet must be exactly k x the stock sheet.
inline bool SheetSizeOk(int w, int h, int k)
{
    return w == kStockSheetW * k && (h == kStockSheetH * k || h == -kStockSheetH * k);   // Buffer h is stored negative
}

// Scale2x (EPX) of an n x n index image into 2n x 2n. Copies only neighbouring palette INDICES -- never blends --
// so the shading bands the rasterizer adds to the index stay intact. Edges clamp. dst must not alias src.
inline void Scale2x(const uint8_t* src, int n, uint8_t* dst)
{
    auto at = [&](int x, int y) {
        x = x < 0 ? 0 : (x >= n ? n - 1 : x);
        y = y < 0 ? 0 : (y >= n ? n - 1 : y);
        return src[y * n + x];
    };
    const int m = 2 * n;
    for (int y = 0; y < n; ++y) {
        for (int x = 0; x < n; ++x) {
            const uint8_t e = at(x, y), b = at(x, y - 1), h = at(x, y + 1), d = at(x - 1, y), f = at(x + 1, y);
            const bool ok = b != h && d != f;
            dst[(2 * y) * m + 2 * x]         = ok && d == b ? d : e;
            dst[(2 * y) * m + 2 * x + 1]     = ok && b == f ? f : e;
            dst[(2 * y + 1) * m + 2 * x]     = ok && d == h ? d : e;
            dst[(2 * y + 1) * m + 2 * x + 1] = ok && h == f ? f : e;
        }
    }
}

} // namespace terrainhd
