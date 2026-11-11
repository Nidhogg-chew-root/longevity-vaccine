#pragma once

// Drop-in HD terrain textures: <game>\terrain\texture_x2.pcx / texture_x4.pcx (TEXTURE.PCX at 2x / 4x, stock
// palette) replace the map's terrain textures at the main map's zoom. Byte-checked, cave-free, both exes;
// [UIMod] TerrainHD=0 turns it off. See terrainhd.cpp.
void InstallTerrainHd();
