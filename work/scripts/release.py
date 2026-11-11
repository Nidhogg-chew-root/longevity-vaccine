"""Build the Longevity Vaccine release artifacts: staged modules, deterministic zips, manifest.

Nothing in this repo built a distributable before this script. It produces, under `dist\\release`:

    <version>\\LongevityVaccine_<version>.zip   the mod + the installer + its Python runtime
    <version>\\manifest.json                    what the updater fetches; signed with --sign-key
    modules\\LV-movies-hd-<pack version>.zip     the 61 upscaled cinematics
    modules\\LV-portraits-hd-<pack version>.zip  (when the pack exists)

DESIGN NOTES

* Packs are versioned INDEPENDENTLY of the product (version.py MODULE_VERSIONS) and live under
  `modules\\` named for their own version, so a product release reuses them unchanged and a
  pack-only release (`--modules movies-hd`) reuses the product archive. The manifest always
  lists every module that has an artifact. Changing a pack's CONTENT means bumping its version:
  an archive name is immutable once published, and an unbumped pack is reused, not rebuilt.

* Archive layout is FLAT and is the one and only payload layout: a `scripts` folder for the
  code and its launcher, `patches` beside it, `CHANGELOG.txt` at the root. A checkout keeps the
  same files under `work`, because that folder also holds tests, screenshots and notes and is
  not a payload -- the mapping below is the only place the two are related, and nothing in the field
  ever sees the checkout shape.

* Zips are DETERMINISTIC: entries sorted, timestamps fixed, no host metadata. The same inputs
  must produce the same sha256, or the manifest hash changes on every rebuild and nobody can
  tell a real change from a repack.

* The manifest reads its target table straight out of patches.yaml, so the shipped
  base/patched MD5s cannot drift from what the patcher actually applies.

* It REFUSES to package when the L1 gate fails, and refuses when the bundled Python still
  carries developer-only packages (frida, pytest, numpy, ...). The latter is the single biggest
  release-payload defect and it is invisible in a working tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import version as ver  # noqa: E402

_REPO = _SCRIPTS.parent.parent
_PATCHES = _REPO / "work" / "patches" / "patches.yaml"
_TESTS = _REPO / "work" / "tests"
_PY = _REPO / "tools" / "python" / "python.exe"

#: A fixed timestamp for every zip entry. 1980-01-01 is the earliest the zip format allows.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Packages that must NEVER ship. The installer needs exactly pyyaml + pefile; everything else
#: in tools\python\Lib\site-packages is developer tooling (Ghidra bridge, Frida, test harness,
#: image/pixel maths) and accounts for most of the 428 MB payload.
DEV_ONLY = {
    "PIL", "pillow", "capstone", "keystone", "frida", "frida_tools", "jpype", "_jpype",
    "numpy", "pyghidra", "pytest", "_pytest", "pygments", "prompt_toolkit", "wcwidth",
    "websockets", "colorama", "iniconfig", "pluggy", "packaging", "py.py", "org.jpype.jar",
    "xdist", "execnet", "pytest_xdist",
}

#: What goes in each module, as (source, archive path) pairs. `source` may be a file or a dir.
#: Archive paths are repo-relative so an extracted archive is a working tree for install.py.
MODULE_SPEC = {
    "base": {
        "required": True,
        "files": [
            ("work/run-install.bat", "scripts/run-install.bat"),
            # Linux / macOS entry point: finds the Wine prefix and runs INSTALL.bat under it. Executable, LF.
            ("work/install.sh", "INSTALL.sh"),
            # The installer's own runtime. Everything install.py imports at run time has to be
            # here: shipping install.py without ed25519_verify/manifest would leave an installed
            # copy unable to check a signature, i.e. unable to update at all.
            ("work/scripts/install.py", "scripts/install.py"),
            ("work/scripts/apply_patches.py", "scripts/apply_patches.py"),
            ("work/scripts/config.py", "scripts/config.py"),
            ("work/scripts/version.py", "scripts/version.py"),
            ("work/scripts/installer_exit.py", "scripts/installer_exit.py"),
            ("work/scripts/ed25519_verify.py", "scripts/ed25519_verify.py"),
            ("work/scripts/manifest.py", "scripts/manifest.py"),
            ("work/scripts/fetch.py", "scripts/fetch.py"),
            ("work/scripts/preflight.py", "scripts/preflight.py"),
            ("work/scripts/transact.py", "scripts/transact.py"),
            ("work/scripts/receipt.py", "scripts/receipt.py"),
            ("work/scripts/bootstrap.py", "scripts/bootstrap.py"),
            # The Datalinks text patch (helpx.txt / conceptsx.txt), applied by install.py on every run.
            ("work/scripts/patch_helptext.py", "scripts/patch_helptext.py"),
            # Public roots our hosts chain to, loaded by fetch.py beside the Windows store: a Windows 7 whose
            # store never met them cannot fetch anything otherwise (make_roots_pem.py).
            ("work/scripts/roots.pem", "scripts/roots.pem"),
            # NOT shipped, deliberately: sign_manifest.py (the private-key machinery),
            # release.py and hash_hd_pack.py (build tooling). test_release asserts their absence.
            ("work/patches/patches.yaml", "patches/patches.yaml"),
            ("work/patches/patches_prax.yaml", "patches/patches_prax.yaml"),
            ("work/CHANGELOG.md", "CHANGELOG.txt"),   # .txt: opens in Notepad on a stock Windows; .md opens in nothing
            ("src/uimod/build/Release/uimod.dll", "src/uimod/build/Release/uimod.dll"),
            # The update prompt's popup picture (200x120 8-bit PCX in the game's popup-art
            # format; generated by work/scripts/art/make_longevity_sm.py). Deployed beside the
            # exe by deploy_runtime; the DLL shows a text-only popup when it is absent.
            ("work/art/longevity_sm.pcx", "art/longevity_sm.pcx"),
            # The player's guide to replacement portraits; deploy_runtime places it in <game>\portraits\.
            ("work/art/portraits-README.txt", "portraits/LONGEVITY_PORTRAITS_README.txt"),
        ],
        "globs": [
            ("src/uimod/third_party/ffmpeg/bin", "*.dll",
             "src/uimod/third_party/ffmpeg/bin"),
        ],
        # NOT the raw tools/python tree. The shipped interpreter is the CURATED subset
        # staged by build_runtime.py -- see collect() -- which is 18 MB rather than 428.
        "trees": [],
    },
    "movies-hd": {
        "required": False,
        "files": [],
        # The provenance sidecar ships with the pack: it carries per-clip sha256 + size, which is
        # what lets the installer skip unchanged clips instead of recopying 1.3 GB every run.
        # Generate it with work\scripts\hash_hd_pack.py.
        "globs": [("dist/movies-hd", "*.mp4", "movies/hd"),
                  ("dist/movies-hd", "pack-manifest.json", "movies/hd")],
        "trees": [],
    },
    "portraits-hd": {
        "required": False,
        "files": [],
        # Built by work\scripts\art\build_portraits_hd.py.  It lands in portraits\hd ONLY: the player's own images
        # live in portraits\ (and fill in whatever the pack lacks), so the pack can never overwrite one.  Exactly the 14 leaders + the
        # manifest + the art's provenance/credit (_check_portrait_pack refuses anything else).
        "globs": [("dist/portraits-hd", "*.png", "portraits/hd"),
                  ("dist/portraits-hd", "pack-manifest.json", "portraits/hd"),
                  ("dist/portraits-hd", "PROVENANCE.txt", "portraits/hd")],
        "trees": [],
    },
}

#: Extracted at the archive root so a player double-clicks one obvious thing. Kept CRLF, and it
#: guards its own target -- a shim that silently does nothing when the payload is incomplete
#: would defeat the installer's exit guarantee before Python is ever reached.
_ROOT_LAUNCHER = (
    "@echo off\r\n"
    "REM Longevity Vaccine - installer entry point. Extract the whole archive, then run this.\r\n"
    "setlocal\r\n"
    'set "INNER=%~dp0scripts\\run-install.bat"\r\n'
    'if not exist "%INNER%" (\r\n'
    "  echo.\r\n"
    "  echo  ============================================================\r\n"
    "  echo     INSTALLATION FAILED\r\n"
    "  echo  ============================================================\r\n"
    "  echo  This archive is incomplete - scripts\\run-install.bat is missing.\r\n"
    "  echo  Nothing on disk was changed.\r\n"
    "  echo.\r\n"
    "  echo  What to do: extract the WHOLE archive before running INSTALL.bat.\r\n"
    "  echo.\r\n"
    "  pause\r\n"
    "  endlocal ^& exit /b 9\r\n"
    ")\r\n"
    'call "%INNER%" %*\r\n'
    "endlocal & exit /b %ERRORLEVEL%\r\n"
)


#: The file name a player actually downloads, per module.
#:
#: `base` IS the product, so it carries the product's name rather than an internal module id --
#: "base-0.1.0.zip" in a downloads folder tells the person who saved it nothing. The optional
#: packs keep their module id, which is what they are.
#:
#: One source of truth: the manifest URL, the staged file and every test all derive from
#: archive_name(). Nothing may spell a release file name out by hand -- except the website
#: (not in the public source), which can't import it; its own tests pin it to this table.
ARCHIVE_NAME = {
    "base": "LongevityVaccine_{version}.zip",
}
#: Optional packs carry an "LV-" prefix so they are identifiable in a downloads folder or
#: a releases list without being mistaken for the product itself -- "movies-hd-0.1.0.zip"
#: sitting next to somebody else's mod files says nothing about where it came from.
DEFAULT_ARCHIVE_NAME = "LV-{module}-{version}.zip"


def archive_name(module: str, version: str) -> str:
    return ARCHIVE_NAME.get(module, DEFAULT_ARCHIVE_NAME).format(
        module=module, version=version)


def sha256_file(p: Path, _buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(_buf), b""):
            h.update(chunk)
    return h.hexdigest()


#: Where build_runtime.py stages the shipped interpreter.
RUNTIME_DIR = _REPO / "work" / "build" / "runtime"
#: The Windows 7 / 8 interpreter (CPython 3.8.10 + app-local UCRT + the pure-Python pefile/yaml), staged by
#: the same build_runtime.py run and shipped as tools/python38 beside tools/python. The SAME scripts run on
#: it, so it is verified (probe + the whole installer flow under 3.8) and stale-checked exactly like 3.12.
RUNTIME38_DIR = _REPO / "work" / "build" / "runtime38"
#: (staged dir, where it lands in the payload) for both interpreters: everything that walks "the runtime"
#: walks this.
RUNTIMES = ((RUNTIME_DIR, "tools/python"), (RUNTIME38_DIR, "tools/python38"))


def runtime_root() -> Path:
    """The staged 3.12 runtime's root, or an explained refusal. Requires BOTH interpreters (a release
    without tools/python38 leaves every Windows 7 / 8 player with an installer that cannot start);
    callers that pack walk RUNTIMES, not the returned root.

    Deliberately NOT built on demand here: staging is driven by a measured import trace, and a
    release that silently regenerated that trace from whatever happened to be installed would be
    exactly the unreviewable step this whole approach exists to avoid. Build it explicitly, look
    at what changed, then package.
    """
    for root, dst in RUNTIMES:
        if not (root / "python.exe").is_file():
            raise FileNotFoundError(
                f"no staged runtime at {root} ({dst}). Build it first:\n"
                f"    py work\\scripts\\fetch_python38.py          (once per clone: tools\\python38)\n"
                f"    py work\\scripts\\trace_imports.py --out work\\build\\import-trace.json\n"
                f"    py work\\scripts\\build_runtime.py --out work\\build\\runtime --verify")
    problem = runtime_stale()
    if problem:
        print(f"  WARNING: {problem}\n"
              "  If an import moved, the trimmed interpreter may be missing a module. Re-run\n"
              "  trace_imports.py + build_runtime.py --verify to be sure.")
    return RUNTIME_DIR


def runtime_stale(root: Path = None) -> str:
    """Why the staged runtime cannot be trusted as-is, or "" when it can. A warning here in a
    developer's run; a REFUSAL in release_all.py (--skip-runtime, dist_stale). With no `root`,
    BOTH staged interpreters are checked and the first problem names its runtime."""
    if root is None:
        for r, dst in RUNTIMES:
            problem = runtime_stale(r)
            if problem:
                return f"{dst}: {problem}"
        return ""
    root = Path(root)
    # Only the SHIPPED .py files matter: "could an import have moved?" is the question, and a
    # .bat, a .yaml or release.py itself (which never runs on a player's machine) cannot move
    # one. A warning that fires when nothing is wrong is one people learn to ignore.
    newest, newest_name = 0.0, None
    for src, _dst in MODULE_SPEC["base"]["files"]:
        f = _REPO / src
        if f.suffix == ".py" and f.is_file() and f.stat().st_mtime > newest:
            newest, newest_name = f.stat().st_mtime, src
    # The build STAMP, not a file mtime: build_runtime stages with copy2, which preserves the
    # source timestamps, so python.exe carries tools\python's date.
    try:
        staged = float((root / ".staged-at").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return "the staged runtime carries no .staged-at stamp"
    if newest > staged:
        return f"{newest_name} changed after the runtime was staged"
    # build_runtime --verify writes .verified-at AFTER the probes pass; a runtime that was
    # staged and then failed verification has the stamp but not the marker.
    try:
        verified = float((root / ".verified-at").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return "the staged runtime was never verified (run build_runtime.py --verify)"
    if verified < staged:
        return "the staged runtime was re-staged after its last verification"
    return ""


def _check_portrait_pack(d: Path) -> None:
    """The portraits-hd folder is exactly what build_portraits_hd.py writes, unchanged since: the 14 leader images,
    pack-manifest.json and PROVENANCE.txt (the artist's credit), every image matching its manifest sha256/size.
    A stray file (Thumbs.db, a test image) or a hand-edited image would otherwise ship under the signed manifest."""
    import json
    sys.path.insert(0, str(_REPO / "work" / "scripts" / "art"))
    import build_portraits_hd as bph
    want = {f"{n}.png" for n in bph.LEADERS.values()} | {bph.MANIFEST, bph.PROVENANCE}
    have = {p.name for p in d.iterdir() if p.is_file()}
    if have != want:
        extra, missing = sorted(have - want), sorted(want - have)
        raise ValueError(f"portraits-hd: {d} is not the built pack"
                         + (f"; unexpected {extra}" if extra else "") + (f"; missing {missing}" if missing else "")
                         + " (rebuild it with work\\scripts\\art\\build_portraits_hd.py)")
    doc = json.loads((d / bph.MANIFEST).read_text(encoding="utf-8"))
    files = {f["name"]: f for f in doc.get("files", [])}
    if set(files) != {f"{n}.png" for n in bph.LEADERS.values()}:
        raise ValueError(f"portraits-hd: {bph.MANIFEST} does not list exactly the 14 leaders")
    for name, f in files.items():
        p = d / name
        if p.stat().st_size != f.get("size") or bph.sha256_file(p) != f.get("sha256"):
            raise ValueError(f"portraits-hd: {name} does not match {bph.MANIFEST} (edited after the build?)")
    _check_portrait_provenance((d / bph.PROVENANCE).read_bytes(), bph.PROVENANCE)


#: The tracked provenance every shipped pack must carry, word for word: the artist's credit and the licence as the
#: artist confirmed it (2026-11-06). A pack built from an older text (the provisional licence) never ships.
_PORTRAIT_PROVENANCE = _REPO / "work" / "art" / "portraits-hd-PROVENANCE.txt"


def _check_portrait_provenance(data: bytes, name: str) -> None:
    text = data.decode("utf-8", errors="replace")
    for must in ("Turovetsky", "https://www.youtube.com/@Turovetsky", "https://x.com/turovetsky"):
        if must not in text:
            raise ValueError(f"portraits-hd: {name} lacks the artist's required credit ({must})")
    want = _PORTRAIT_PROVENANCE.read_text(encoding="utf-8")
    if text.replace("\r\n", "\n") != want.replace("\r\n", "\n"):
        raise ValueError(f"portraits-hd: {name} is not work\\art\\portraits-hd-PROVENANCE.txt (built from an older "
                         "text? rebuild the pack: work\\scripts\\art\\build_portraits_hd.py --provenance "
                         "work\\art\\portraits-hd-PROVENANCE.txt)")


def _check_reused_portrait_pack(archive: Path, mver: str) -> None:
    """An existing LV-portraits-hd-<v>.zip is reused unbuilt.  If dist\\portraits-hd was rebuilt since (another crop,
    another image, an edited PROVENANCE) the old zip would silently ship under the signed manifest: refuse instead,
    member by member (CRC32 + size), unless the folder is absent (a machine without the pack source).  Whatever
    the folder holds, the archive's own PROVENANCE.txt must be the current tracked one (credit + confirmed licence)."""
    import zlib
    with zipfile.ZipFile(archive) as z:
        members = {Path(i.filename).name: (i.CRC, i.file_size) for i in z.infolist() if not i.is_dir()}
        prov = next((i for i in z.infolist() if Path(i.filename).name == "PROVENANCE.txt"), None)
        if prov is None:
            raise SystemExit(f"REFUSING to reuse {archive.name}: it carries no PROVENANCE.txt (the artist's credit)")
        try:
            _check_portrait_provenance(z.read(prov), f"{archive.name}:PROVENANCE.txt")
        except ValueError as e:
            raise SystemExit(f"REFUSING to reuse {archive.name}: {e}. Bump MODULE_VERSIONS['portraits-hd'] in "
                             f"version.py (a published archive name is immutable).")
    d = _REPO / "dist" / "portraits-hd"
    if not d.is_dir():
        return
    if "pack-manifest.json" not in members:
        return      # not an archive built from a pack folder (every real one carries the manifest): nothing to compare
    current = {}
    for p in sorted(d.iterdir()):
        if p.is_file():
            current[p.name] = (zlib.crc32(p.read_bytes()) & 0xFFFFFFFF, p.stat().st_size)
    if members != current:
        changed = sorted(set(members) ^ set(current) | {n for n in members if n in current and members[n] != current[n]})
        raise SystemExit(f"REFUSING to reuse {archive.name}: dist\\portraits-hd changed since v{mver} was built "
                         f"({', '.join(changed[:6])}{'...' if len(changed) > 6 else ''}). Bump MODULE_VERSIONS"
                         f"['portraits-hd'] in version.py (a published archive name is immutable).")


def collect(module: str, include_python: bool) -> list[tuple[Path, str]]:
    """Resolve a module spec into concrete (source file, archive path) pairs."""
    spec = MODULE_SPEC[module]
    out: list[tuple[Path, str]] = []

    for src, dst in spec["files"]:
        p = _REPO / src
        if not p.is_file():
            raise FileNotFoundError(f"{module}: missing required file {p}")
        out.append((p, dst))

    for src_dir, pattern, dst_dir in spec["globs"]:
        d = _REPO / src_dir
        if not d.is_dir():
            if spec["required"]:
                raise FileNotFoundError(f"{module}: missing required dir {d}")
            continue
        for p in sorted(d.glob(pattern)):
            if p.is_file():
                out.append((p, f"{dst_dir}/{p.name}"))

    if module == "portraits-hd" and out:
        _check_portrait_pack(_REPO / "dist" / "portraits-hd")

    for src_dir, dst_dir in spec["trees"]:
        d = _REPO / src_dir
        if not d.is_dir():
            raise FileNotFoundError(f"{module}: missing required tree {d}")
        for p in sorted(d.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append((p, f"{dst_dir}/{p.relative_to(d).as_posix()}"))

    if include_python and module == "base":
        # The SHIPPED interpreter is a curated subset, not the developer's tree.
        #
        # Shipping tools/python wholesale meant a 428 MB base module -- ~96% of it developer
        # tooling (frida, numpy, PIL, pip, pytest, capstone, ...) and parts of CPython the
        # installer never touches -- and, because the installer is resident, the player paid for
        # it twice. It also forced the DEVELOPER to uninstall their own tools before they could
        # cut a release, which is a hostile thing to require and an easy thing to forget.
        runtime_root()                     # both staged, or an explained refusal
        for runtime, dst_dir in RUNTIMES:  # tools/python (3.12) and tools/python38 (Windows 7 / 8)
            for p in sorted(runtime.rglob("*")):
                # .staged-at and .verified-at are BUILD stamps that change every time the runtime is
                # staged or verified, so packing them would make every rebuild produce a different
                # sha256 and destroy the reproducible-zip guarantee this module is built around.
                if p.is_file() and "__pycache__" not in p.parts \
                        and p.name not in (".staged-at", ".verified-at"):
                    out.append((p, f"{dst_dir}/{p.relative_to(runtime).as_posix()}"))

    if module == "base":
        out.append((None, "INSTALL.bat"))   # synthesised, see write_zip
    return out


#: Archive entries that must extract executable on Linux / macOS (fixed bits, so the zip stays reproducible).
EXECUTABLE_ENTRIES = {"INSTALL.sh"}


def write_zip(entries: list[tuple[Path, str]], dest: Path) -> None:
    """Write a byte-for-byte reproducible zip.

    Sorted entries + a fixed timestamp + fixed external attrs: the same inputs must always hash the
    same, or every rebuild looks like a new release.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    # Level 9, not 6: ~45 KB smaller for nothing but build time, and the level is
    # deterministic so the reproducible-zip guarantee is unaffected. DEFLATE specifically --
    # the base archive is extracted by a human with Windows Explorer, which cannot read LZMA
    # or bzip2 zip entries and would fail or silently skip them.
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, arc in sorted(entries, key=lambda e: e[1]):
            info = zipfile.ZipInfo(arc, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            if arc in EXECUTABLE_ENTRIES:
                # unzip and macOS Archive Utility apply the mode bits only from a Unix-made entry
                info.external_attr = 0o100755 << 16
                info.create_system = 3
            if src is None:
                z.writestr(info, _ROOT_LAUNCHER.encode("ascii"))
            else:
                z.writestr(info, src.read_bytes())
    tmp.replace(dest)


def check_dev_only_packages(root: Path = None) -> list[str]:
    """Developer packages must never ship. Returns the offenders found.

    Checks the STAGED runtime by default, not the developer's interpreter. Pointing this at
    tools/python made the check a demand that the developer uninstall frida, numpy, pytest and
    the rest before cutting a release -- which is both hostile and beside the point, since what
    matters is only what ends up in the archive.
    """
    if root is None:                     # both staged interpreters ship: check both
        found = set()
        for r, _dst in RUNTIMES:
            found.update(check_dev_only_packages(r))
        return sorted(found)
    sp = Path(root) / "Lib" / "site-packages"
    if not sp.is_dir():
        return []
    found = set()
    for entry in sp.iterdir():
        name = entry.name
        if name.endswith(".dist-info") or name == "__pycache__":
            continue
        stem = name.split("-")[0]
        for dev in DEV_ONLY:
            if stem == dev or name == dev or stem.lower() == dev.lower():
                found.add(name)
    return sorted(found)


def run_gate() -> None:
    """Refuse to package unless the L1 locks and the new unit suites pass.

    .githooks\\pre-commit runs `-k scaling` only, which does NOT match test_apply_md5.py or
    test_patch_integrity.py -- so a patches.yaml change that moves the build MD5 can be committed
    with the lock unchecked. A release must not inherit that gap.
    """
    # The installer/updater suites too: a release_all.py run gates on the WHOLE suite and passes
    # --skip-gate here, but a direct release.py run used to package with none of the update
    # machinery tested at all.
    files = ["test_patch_integrity.py", "test_apply_md5.py", "test_targets.py",
             "test_version_sync.py", "test_installer_exit.py", "test_manifest.py",
             "test_bootstrap.py", "test_check.py", "test_preflight.py"]
    print("--- release gate ---")
    cmd = [str(_PY), "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"]
    r = subprocess.run(cmd, cwd=str(_TESTS))
    if r.returncode != 0:
        raise SystemExit("release gate FAILED -- refusing to package. Fix the tests above.")
    print("  gate passed\n")


#: Where archives are published (`--base-url` / `--pack-url` defaults). The website (not in the
#: public source) hard-codes this prefix for its download button, so moving it means changing
#: the site too; the website's own tests fail until they agree.
BASE_URL = "https://github.com/BoltActionBalrog/longevity-vaccine/releases/download/v{version}"
PACK_URL = "https://github.com/BoltActionBalrog/longevity-vaccine/releases/download/{module}-v{version}"


def module_url(name: str, mver: str, base_url: str, pack_url: str) -> str:
    """Where a module's archive is published. `base` lives under the product's release tag; each
    pack version is its OWN release tag, uploaded once and immutable across product releases."""
    tmpl = base_url if name == "base" else pack_url
    return f"{tmpl.format(module=name, version=mver).rstrip('/')}/{archive_name(name, mver)}"


def build_manifest(version: str, modules: dict, base_url: str,
                   pack_url: str = None) -> dict:
    """`modules` is name -> {version, sha256, size} for EVERY module that has an artifact --
    built now or reused from a previous build. A manifest that omitted a module the player has
    installed would read as "dropped"; one that omitted `base` is rejected by the preflight."""
    import yaml
    # The C loader: same 9x on the same 1.78 MB file (see apply_patches._yaml_load).
    import apply_patches as _ap
    spec = _ap._yaml_load(_PATCHES.read_text(encoding="utf-8"))
    targets = {}
    for tid, t in (spec.get("targets") or {}).items():
        if not t.get("launch") or t.get("pending") or not t.get("base_md5"):
            continue
        entry = {"base_md5": t["base_md5"], "patched_md5": t.get("patched_md5")}
        if t.get("companion"):
            entry["companion"] = {
                k: {"base_md5": v.get("base_md5"), "patched_md5": v.get("patched_md5")}
                for k, v in t["companion"].items()}
        targets[tid] = entry

    return {
        "schema": 1,
        "protocol": 1,
        "product": "longevity-vaccine",
        "latest": version,
        # NOT `version`: that made every existing install "too old" on every release.
        "min_upgradable_from": ver.MIN_UPGRADABLE_FROM,
        "manual_install_required": False,
        "manual_install_reason": None,
        "unsupported_versions": [],
        "unsupported_targets": [],
        "targets": targets,
        "min_free_bytes": 3 * 1024 ** 3,
        "modules": {
            name: {
                "version": info["version"],
                "required": MODULE_SPEC[name]["required"],
                "url": module_url(name, info["version"], base_url, pack_url or base_url),
                "sha256": info["sha256"],
                "size": info["size"],
            }
            for name, info in modules.items()
        },
        "yanked": [],
        "notes_url": "https://longevitymods.com/vaccine/changelog",
    }


def main() -> int:
    apr = argparse.ArgumentParser(description="Build the Longevity Vaccine release artifacts.")
    apr.add_argument("--version", default=ver.VERSION,
                     help=f"release version (default: version.py = {ver.VERSION})")
    apr.add_argument("--out", default=str(_REPO / "dist" / "release"))
    apr.add_argument("--modules", default="base",
                     help="Comma-separated modules to BUILD (default: base). A module not named "
                          "is reused from its existing archive when there is one, so `--modules "
                          "movies-hd` is a pack-only release and a bare run is a product release "
                          "that carries the packs along unchanged. Pack versions come from "
                          "version.py MODULE_VERSIONS; bump one there to release it.")
    apr.add_argument("--rebuild", action="store_true",
                     help="build the named modules even when their archive already exists -- "
                          "only for an UNPUBLISHED version; a published pack must get a new "
                          "version in version.py instead, its archive name is immutable")
    apr.add_argument("--no-python", action="store_true",
                     help="omit tools/python from the base module (fast dev builds only -- the "
                          "result is NOT installable)")
    apr.add_argument("--allow-untrimmed", action="store_true",
                     help="package even though developer-only packages are present")
    apr.add_argument("--skip-gate", action="store_true", help="skip the test gate (not for a real release)")
    apr.add_argument("--sign-key", default=None, metavar="PATH",
                     help="sign the manifest with this Ed25519 key (see sign_manifest.py "
                          "--keygen). Without it the manifest is UNSIGNED and unpublishable.")
    apr.add_argument("--base-url", default=BASE_URL,
                     help="where the product archive is published ({version} = product)")
    apr.add_argument("--pack-url", default=PACK_URL,
                     help="where a pack archive is published ({module}, {version} = the PACK's "
                          "version): one release tag per pack version, immutable")
    args = apr.parse_args()

    if not ver.is_valid(args.version):
        raise SystemExit(f"--version {args.version!r} is not MAJOR.MINOR.PATCH")
    if args.version != ver.VERSION:
        raise SystemExit(
            f"--version {args.version} disagrees with work/scripts/version.py ({ver.VERSION}). "
            "Bump version.py (and src/uimod/src/version.h) instead of overriding here.")

    for name, mver in ver.MODULE_VERSIONS.items():
        if not ver.is_valid(mver):
            raise SystemExit(f"version.py MODULE_VERSIONS[{name!r}] = {mver!r} is not "
                             "MAJOR.MINOR.PATCH")
    if not ver.is_valid(ver.MIN_UPGRADABLE_FROM):
        raise SystemExit("version.py MIN_UPGRADABLE_FROM is not MAJOR.MINOR.PATCH")
    packs = set(MODULE_SPEC) - {"base"}
    if set(ver.MODULE_VERSIONS) != packs:
        raise SystemExit(f"version.py MODULE_VERSIONS names {sorted(ver.MODULE_VERSIONS)} but "
                         f"release.py MODULE_SPEC packages {sorted(packs)}; make them agree")
    if args.no_python and args.sign_key:
        # Allowed (the signing tests need it) but never publishable: the .no-python marker
        # written beside the archive makes release_all.verify_output and publish.read_release
        # refuse it.
        print("WARNING: signing a --no-python build. It is NOT installable and cannot be "
              "published; the archive is marked so publish.py refuses it.\n")

    include_python = not args.no_python
    offenders = check_dev_only_packages()
    if offenders and include_python:
        where = ", ".join(str(r) for r, _d in RUNTIMES if check_dev_only_packages(r))
        msg = (f"developer-only packages are in the STAGED runtime ({where}):\n  "
               + ", ".join(offenders)
               + "\nThe shipped set is pyyaml + pefile only. Re-stage with build_runtime.py.")
        if not args.allow_untrimmed:
            raise SystemExit("REFUSING to package: " + msg)
        print("WARNING: " + msg + "\n")

    if not args.skip_gate:
        run_gate()

    out_dir = Path(args.out) / args.version          # the product archive + its manifest
    pack_dir = Path(args.out) / "modules"            # pack archives, named for THEIR version,
    out_dir.mkdir(parents=True, exist_ok=True)       # so they survive across product releases
    wanted = [m.strip() for m in args.modules.split(",") if m.strip()]
    for name in wanted:
        if name not in MODULE_SPEC:
            raise SystemExit(f"unknown module {name!r}")

    # Every module with an artifact goes in the manifest: the ones named are (re)built, the rest
    # are reused from the archive a previous run left. Reuse is safe because the zips are
    # deterministic -- a rebuild of unchanged inputs is byte-identical -- and it is what makes a
    # pack-only release possible without re-packing the product, or vice versa.
    built: dict[str, dict] = {}
    for name in MODULE_SPEC:
        mver = ver.module_version(name)
        dest = (out_dir if name == "base" else pack_dir) / archive_name(name, mver)
        if name in wanted and (args.rebuild or not dest.is_file() or name == "base"):
            try:
                entries = collect(name, include_python)
            except FileNotFoundError as e:
                if MODULE_SPEC[name]["required"]:
                    raise SystemExit(f"cannot build a release: {e}")
                print(f"  skipped {name}: {e}")
                continue
            payload = [e for e in entries if e[0] is not None or e[1] == "INSTALL.bat"]
            if not payload:
                print(f"  skipped {name}: no files found")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"  packing {name} {mver}: {len(payload)} files -> {dest.name}")
            write_zip(entries, dest)
            how = "built"
            # A --no-python base is NOT installable. Mark it, so a later pack-only run cannot
            # silently reuse it into a signed manifest.
            marker = dest.with_name(dest.name + ".no-python")
            if name == "base" and not include_python:
                marker.write_text("built with --no-python; not installable\n", encoding="ascii")
            else:
                marker.unlink(missing_ok=True)
        elif dest.is_file():
            marker = dest.with_name(dest.name + ".no-python")
            if name == "base" and marker.is_file() and include_python:
                raise SystemExit(
                    f"REFUSING to reuse {dest.name}: it was built with --no-python and is not "
                    "installable. Rebuild it (--modules base) before cutting a pack release.")
            how = "reused"
            if name == "portraits-hd":
                _check_reused_portrait_pack(dest, mver)
            if name in wanted:
                print(f"  {name} {mver} is already built; not rebuilt (pass --rebuild to force "
                      f"-- only for a version that has NOT been published)")
        else:
            if name != "base":
                print(f"  WARNING: {name} v{mver} has no archive and was not built; it is LEFT "
                      f"OUT of this manifest (add --modules {name} to build it). Players who "
                      f"have it installed will read that as 'dropped'.")
            continue
        built[name] = {"version": mver, "path": str(dest), "how": how,
                       "sha256": sha256_file(dest), "size": dest.stat().st_size}
        print(f"  {name:<14} {mver:<8} {how:<8} {built[name]['size']:>14,} bytes  "
              f"sha256 {built[name]['sha256'][:16]}...")

    if "base" not in built:
        raise SystemExit("the base module is required and was not built (name it in --modules, "
                         f"or build it first so {out_dir} holds its archive)")

    manifest = build_manifest(args.version, built, args.base_url, args.pack_url)
    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    signed = False
    if args.sign_key:
        import sign_manifest as sm
        import manifest as mfmod
        seed = sm.load_seed(Path(args.sign_key))
        pk_hex = sm.public_key(seed).hex()
        # Signing with a key the shipped client does not trust yields a manifest EVERY install
        # rejects -- a release that looks fine here and is inert in the field. Catch it now.
        # Compare BYTES, not strings: verify_detached() is case-insensitive and strips
        # whitespace, so an uppercase or newline-terminated PUBLIC_KEY_HEX would have made this
        # refuse the correct key -- a fail-closed bug that blocks a legitimate release.
        def _same_key(a: str, b: str) -> bool:
            try:
                return bytes.fromhex(a.strip()) == bytes.fromhex(b.strip())
            except (ValueError, AttributeError, TypeError):
                return False
        if mfmod.PUBLIC_KEY_HEX and not _same_key(mfmod.PUBLIC_KEY_HEX, pk_hex):
            raise SystemExit(
                f"REFUSING to sign: --sign-key is\n    {pk_hex}\n"
                f"but manifest.PUBLIC_KEY_HEX (what shipped clients trust) is\n"
                f"    {mfmod.PUBLIC_KEY_HEX}\n"
                "Nothing would accept this manifest. Use the release key, or -- only for the "
                "very first release -- update PUBLIC_KEY_HEX to match.")
        if not mfmod.PUBLIC_KEY_HEX:
            print(f"WARNING: manifest.PUBLIC_KEY_HEX is empty, so no client trusts anything yet.\n"
                  f"         Set it to {pk_hex} before cutting a real release.\n")
        sm.sign_file(seed, mpath)
        # Prove the pair the installer will actually read verifies, through the same code path.
        try:
            mfmod.load_verified_files(mpath, public_key_hex=pk_hex)
        except mfmod.ManifestError as e:
            raise SystemExit(f"the signed manifest does not verify: {e}")
        signed = True

    print(f"\n=== release {args.version} staged in {out_dir} ===")
    for name, info in built.items():
        print(f"  {name:<14} {info['version']:<8} {info['how']:<8} {info['size']:>14,} bytes")
    print(f"  manifest.json  ({len(manifest['targets'])} target(s), "
          f"{len(manifest['modules'])} module(s))")
    if signed:
        print("  manifest.json.sig  (verified against its own public key)")
        print("\nNEXT: confirm manifest.PUBLIC_KEY_HEX is this key, then publish.")
    else:
        print("\nNEXT: sign it -- `sign_manifest.py --key <key> --manifest "
              f"{mpath}`. It is UNSIGNED and not publishable as-is.")
    if args.no_python:
        print(f"NOTE: --no-python was used; {archive_name('base', args.version)} is "
              "NOT installable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
