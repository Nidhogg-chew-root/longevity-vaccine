# Longevity Vaccine

**Alpha Centauri, made for modern PCs. Get it at [longevitymods.com](https://longevitymods.com/).**

Longevity Vaccine is a free mod for *Sid Meier's Alpha Centauri: Alien Crossfire*.

- **The interface scales to your screen, up to 4K.** Every menu, popup and line of text fits your resolution and keeps the original look.
- **Fixes the Windows 11 new-game freeze** and other crashes.
- **Fullscreen, borderless or windowed.** Switch from the in-game preferences tab.
- **Movies play inside the game.** Optional HD remastered cinematics.
- **Optional HD leader portraits** by Turovetsky ([YouTube](https://www.youtube.com/@Turovetsky), [X](https://x.com/turovetsky)), or drop in your own portrait images.
- **Smooth map scrolling** at high resolutions.
- **Drop-in HD terrain:** put `texture_x2.pcx` and/or `texture_x4.pcx` (the game's `texture.pcx` redrawn at 2x / 4x, same palette) in a `terrain` folder in the game folder, and the map uses them as you zoom in.
- **Fixes gameplay bugs.** The AI can no longer see cloaked units, etc.
- **Modern-style mouse controls** (optional)
- **Self-updater** (optional)
- **Multiplayer supported**

See the [full changelog](work/CHANGELOG.md).

## Install

You need *Sid Meier's Alpha Centauri Planetary Pack* from GOG, on Windows 7+.

1. Download the zip from [longevitymods.com](https://longevitymods.com/) or the [latest release](../../releases/latest).
2. Extract the whole zip.
3. Run `INSTALL.bat`.
4. Play with `terranx_PRACX.exe` or the "SMACX Longevity" shortcut. The GOG Galaxy shortcut does NOT work.

**Thinker Mod 5.5** is supported. Install into your Thinker folder and start `thinker.exe` as usual. The installer sets the folder up for you. It keeps your original `terranx.exe` and puts it back when you uninstall.

Other mods and the base game without Alien Crossfire are not supported.

**To uninstall**, run `INSTALL.bat --uninstall` from a command prompt.

## Report a bug

[Open an issue](../../issues). Say what you were doing, your resolution, and whether you use Thinker. Attach `uimod.log` from the game folder. For install or update problems, also attach the `LongevityMods\logs` folder.

---

## For developers

This is the source the mod is built from. Each `v<version>` tag points at the exact source of that release. `SOURCE-MANIFEST.json` lists every file with its SHA-256.

### Layout

| Path | What |
|------|------|
| `src/uimod/` | The in-game DLL in C++: rendering hooks, display modes, movie player, update checker, preferences. |
| `work/patches/patches.yaml` | The binary patches to `terranx.exe`, grouped by screen or feature. |
| `work/scripts/` | The patcher, the installer, the thunk assemblers, the scaling laws, packaging and reverse-engineering tools. |
| `work/tests/` | The regression suite. |
| `work/symbols/terranx.h` | The symbol map of the game binary. |
| `work/video-upscale/` | The HD Cinematics pipeline. |
| `work/scripts/PROBES.md` | Which Frida scripts only observe and which write to the game. Read the writers. Don't run them. |
| `docs/skills/` | How the scaling work is done. |
| `docs/notes/` | Engineering notes and history. A `[[name]]` link means `docs/notes/name.md`. Start at `docs/notes/README.md`. |
| `work/CHANGELOG.md` | The player changelog. |

### Scaling rules

Every scaled value is `vanilla × g/16`. `g` is the runtime scale from the screen size, and 16 is native 1024×768.

1. Every change must look right and not crash at any resolution. At `g <= 16` the game behaves exactly like vanilla.
2. Scale uniformly. Never stretch the 4:3 layout on one axis. The only exception is a sprite drawn over a background the game already stretches.
3. Measure vanilla values from the unpatched game. Never eyeball them.
4. `work/scripts/scaling_laws.py` is the source of truth. Hooks and thunks are ports of it, and a test fails if they disagree.
5. Regenerate dither and scanlines at device density. Never resample them.
6. Measure the engine. Don't model it.
7. Never hardcode the game path or exe name. `work/scripts/config.py` resolves both.

### Build and test

- Some launchers, Frida probes and the Ghidra-side analysis scripts (the `work/scripts/*.py` that set `PROJECT_LOCATION`, plus a few assembler output paths) still hardcode the maintainer's checkout folder `D:\SmacxUi`. Edit the constant at the top of the file to point at yours. The patcher, the installer, the DLL build and the test suite do not depend on it.
- Install Python 3.12 as a full install at `tools/python`, not a venv. Then run `pip install capstone keystone-engine unicorn pefile pyyaml pillow numpy scipy pytest pytest-xdist frida-tools pyghidra==3.1.0`. The pyghidra version must match Ghidra's.
- Copy `work/game-path.txt.example` to `work/game-path.txt` and point it at a PRACX GOG install. The patcher expects MD5 `2d0b1438` and keeps a `.bak`. If the PRACX executable there is not named `terranx.exe`, put its name in `work/game-exe.txt` (or set `SMACX_EXE_NAME`); without it the installer tests read the wrong `.bak` and fail instead of skipping.
- Optional: Ghidra 12.1 at `tools/ghidra_12.1_PUBLIC` and JDK 21 at `tools/jdk-21.0.11+10` for decompiling.
- Optional: Node.js for the JavaScript scaling tests, and PowerShell 7 for `work/run-<WxH>.bat`.
- Once per clone, run `git config core.hooksPath .githooks`.

Build the DLL with `src/uimod/build.bat Release`. It expects Visual Studio 2022 Community, Win32. For another edition, edit `build.bat` and `tools/run-build-ffmpeg-slim.bat`, and set `VCVARS32` for the C++ test harness.

The DLL links a slim ffmpeg that you build yourself:

1. Put the FFmpeg 8.1 source at `tools/ffmpeg-8.1` and MSYS2 at `tools/msys64`. Put `nasm` on PATH.
2. Run `tools/run-build-ffmpeg-slim.bat`. It writes `tools/ffmpeg-slim`.
3. Copy `include` to `src/uimod/third_party/ffmpeg/include`. Copy `lib/*` and `bin/*.lib` to `src/uimod/third_party/ffmpeg/lib`. Copy the five DLLs in `bin` to `src/uimod/third_party/ffmpeg/bin`.

Run the tests:

```
cd work\tests
py -m pytest -q              # whole suite, about 2 min on 64 workers
py -m pytest -k scaling -q   # scaling-law sweep only
```

The tests never launch the game or touch a real install.

### Build the installer

```
py work\scripts\trace_imports.py
py work\scripts\build_runtime.py --verify
py work\scripts\fetch_python38.py           # Windows 7 and 8 interpreter; needs the Windows SDK's UCRT redistributable folder (Windows Kits\10\Redist\10.0.*\ucrt), see THIRD_PARTY.md
py work\scripts\release.py --out dist\release --sign-key <your key>
```

The installer's end-to-end tests skip until `work/build/runtime` exists. An installer accepts only manifests signed by the key in `work/scripts/manifest.py`, so players' installs trust the project key alone.

### What the installer changes

It writes to the game folder: the patched exe with a `.bak`, `uimod.dll`, the ffmpeg DLLs and a `LongevityMods` folder. It adds a desktop shortcut if you say yes. If the game folder isn't writable, it caches downloads in `%LOCALAPPDATA%\LongevityVaccine`. Every download is HTTPS and checked against a signed manifest. The DLL's only network traffic is the multiplayer home-network fix (`src/uimod/src/lanjoin.cpp`): during TCP/IP multiplayer it asks your router over UPnP on the local network and test-connects to the other PC. Single player never loads Winsock, and `DirectPlayLanFix=0` under `[UIMod]` turns the fix off.

### Not in this repo

- Release publishing tools. `release.py` and `sign_manifest.py` are here. No key is.
- Most analysis scratch and game disassembly dumps. Tests that need them skip.
- Thinker and PRACX source under `refs/`. Clone them from their own repositories if you need them.
- The HD portrait images (Turovetsky's pack; its terms are in `THIRD_PARTY.md`). The pipeline that packs them is here.
- The private working notes that `docs/notes/README.md` describes as withheld; the engineering notes are all here.

### Contributing

Issues and pull requests are welcome. Releases are built from a private tree, so a merged change reaches players in the next release. Please follow the scaling rules.

## Licence

Copyright (C) 2026 Longevity Mods. Free software under the GNU GPL, version 3 or later, with no warranty. See `LICENSE`. Third-party components are listed in `THIRD_PARTY.md`.

Longevity Vaccine is not affiliated with Firaxis Games, 2K, Take-Two Interactive, Electronic Arts or GOG. You need a copy of the game. Thanks to the PRACX and Thinker projects.
