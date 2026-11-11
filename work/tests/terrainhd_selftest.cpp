// Self-test harness for terrainhd_law.h (tests\test_terrainhd.py builds and runs it).
//   terrainhd_selftest uv <u> <k>            -> ScaleUv(u, k)
//   terrainhd_selftest level <zoom> <mask>   -> LevelFor(zoom, mask)
//   terrainhd_selftest sheet <w> <h> <k>     -> 1/0 SheetSizeOk
//   terrainhd_selftest scale2x <n> <hex>     -> hex of the 2n x 2n result
//   terrainhd_selftest down <w> <h> <f> <hex> -> hex of the (w/f) x (h/f) DownscaleMode result
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "../../src/uimod/src/terrainhd_law.h"

int main(int argc, char** argv)
{
    if (argc < 3) return 2;
    const char* mode = argv[1];
    if (std::strcmp(mode, "uv") == 0 && argc == 4) {
        std::printf("%d", terrainhd::ScaleUv(std::atoi(argv[2]), std::atoi(argv[3])));
        return 0;
    }
    if (std::strcmp(mode, "level") == 0 && argc == 4) {
        std::printf("%d", terrainhd::LevelFor(std::atoi(argv[2]), static_cast<unsigned>(std::strtoul(argv[3], nullptr, 0))));
        return 0;
    }
    if (std::strcmp(mode, "sheet") == 0 && argc == 5) {
        std::printf("%d", terrainhd::SheetSizeOk(std::atoi(argv[2]), std::atoi(argv[3]), std::atoi(argv[4])) ? 1 : 0);
        return 0;
    }
    if (std::strcmp(mode, "scale2x") == 0 && argc == 4) {
        const int n = std::atoi(argv[2]);
        const char* hex = argv[3];
        if (static_cast<int>(std::strlen(hex)) != 2 * n * n) return 4;
        std::vector<uint8_t> src(n * n), dst(4 * n * n);
        for (int i = 0; i < n * n; ++i) {
            char b[3] = { hex[2 * i], hex[2 * i + 1], 0 };
            src[i] = static_cast<uint8_t>(std::strtoul(b, nullptr, 16));
        }
        terrainhd::Scale2x(src.data(), n, dst.data());
        for (uint8_t v : dst) std::printf("%02x", v);
        return 0;
    }
    if (std::strcmp(mode, "down") == 0 && argc == 6) {
        const int w = std::atoi(argv[2]), h = std::atoi(argv[3]), f = std::atoi(argv[4]);
        const char* hex = argv[5];
        if (static_cast<int>(std::strlen(hex)) != 2 * w * h) return 4;
        std::vector<uint8_t> src(w * h), dst((w / f) * (h / f));
        for (int i = 0; i < w * h; ++i) {
            char b[3] = { hex[2 * i], hex[2 * i + 1], 0 };
            src[i] = static_cast<uint8_t>(std::strtoul(b, nullptr, 16));
        }
        terrainhd::DownscaleMode(src.data(), w, h, w, f, dst.data(), w / f);
        for (uint8_t v : dst) std::printf("%02x", v);
        return 0;
    }
    return 3;
}
