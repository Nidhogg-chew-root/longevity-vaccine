#pragma once

// Drop-in HD terrain textures: <game>\terrain\texture_x2.pcx / _x4.pcx / _x8.pcx (TEXTURE.PCX at 2x / 4x / 8x, stock
// palette; missing levels are cut down from the 8x one) replace the map's terrain textures at the main map's zoom.
// Byte-checked, cave-free, both exes; [UIMod] TerrainHD=0 turns it off. See terrainhd.cpp.
void InstallTerrainHd();
