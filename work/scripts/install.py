"""Longevity Vaccine — SMACX UI-scaling + cinematics installer (end-user CLI wizard).

One command that takes a stock GOG install to the finished mod:
  1. detect the target exe(s) by MD5 and apply the right patch set
     (PRACX  -> full UI-scaling set;  pristine -> the uimod DLL loader),
  2. deploy uimod.dll + the slim ffmpeg DLLs beside the exe,
  3. optionally install the AI-upscaled cinematic pack and flip the runtime
     setting [UIMod] UpscaledVideos.

Re-runnable: an already-patched exe is detected and left alone (use --force to
restore from its .bak and re-patch). Reuses apply_patches.py (patch/restore/MD5)
and config.py (game-dir resolution) — no duplicated logic.

  run-install.bat                  # interactive (prompts for AI cinematics)
  run-install.bat --ai on          # non-interactive: install the HD pack
  run-install.bat --ai off         # non-interactive: original .wve (live upscale)
  run-install.bat --force          # restore + re-patch even if already patched
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import shutil
import contextlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent       # ...\work\scripts
sys.path.insert(0, str(_SCRIPTS))
#: installer_exit is stdlib-only, so importing it here cannot fail for the reasons below. It must
#: be available before anything else, because it owns the exit guarantee.
import installer_exit as ix  # noqa: E402

#: config / apply_patches / version are imported INSIDE main() by _load_modules(), not here.
#: apply_patches needs pyyaml and pefile, and a module-scope ImportError would escape run_guarded
#: entirely -- the process would exit 1 with no verdict, no disk statement and no wait, which is
#: precisely the silent exit the guarantee forbids. That is not hypothetical: the payload-trim
#: task (ACTION_ITEMS "TRIM THE INSTALLER PAYLOAD") drops the bundled interpreter to the embeddable
#: distribution, and the first mistrimmed package would land here.
config = None
ap = None
ver = None


def _load_modules() -> None:
    """Import the third-party-dependent modules from inside the guard."""
    global config, ap, ver
    try:
        import config as _config
        import apply_patches as _apply_patches
        import version as _version
    except Exception as e:  # noqa: BLE001 - any import failure is the same user-facing problem
        raise ix.InstallerError(
            "The installer could not start: part of its own package is missing or unreadable.",
            detail=f"  {type(e).__name__}: {e}",
            changed=False,
            remedy="download the installer again and extract the WHOLE archive before running "
                   "it -- running it from inside the zip is the usual cause.")
    config, ap, ver = _config, _apply_patches, _version

def payload_root() -> Path:
    """Where the installer's own files live.

    The shipped payload is FLAT (`<root>/scripts/install.py`); a checkout is not
    (`<root>/work/scripts/install.py`). So the root is found by looking for the bundled
    interpreter rather than by counting `.parent` hops, which would be right for exactly one of
    the two. LONGEVITY_PAYLOAD_ROOT overrides it, which is what lets a test drive a relocated
    payload without a repo checkout.
    """
    env = os.environ.get("LONGEVITY_PAYLOAD_ROOT")
    if env:
        return Path(env)
    import config as _cfg
    return _cfg.payload_root_from(_SCRIPTS)


def find_payload(*relatives: str):
    """First existing candidate under the payload root, or None.

    Several inputs sit at different places in the repo than in a release archive -- the HD pack is
    `dist\\movies-hd` in a checkout but `movies\\hd` once staged -- so the caller lists both rather
    than the installer assuming a checkout.
    """
    root = payload_root()
    for rel in relatives:
        p = root / rel
        if p.exists():
            return p
    return None


_REPO = _SCRIPTS.parent.parent                   # kept for the repo-only dev paths below


def uimod_dll() -> Path:
    return find_payload("src/uimod/build/Release/uimod.dll", "uimod.dll") \
        or payload_root() / "src/uimod/build/Release/uimod.dll"


#: Files deploy_runtime() places beside the exe besides uimod.dll + the ffmpeg DLLs, and
#: uninstall removes: the update prompt's popup picture (release.py MODULE_SPEC "base").
POPUP_ART_NAME = "longevity_sm.pcx"


def popup_art() -> Path:
    return find_payload("art/" + POPUP_ART_NAME, "work/art/" + POPUP_ART_NAME) \
        or payload_root() / "art" / POPUP_ART_NAME


def ffmpeg_bin() -> Path:
    return find_payload("src/uimod/third_party/ffmpeg/bin", "ffmpeg") \
        or payload_root() / "src/uimod/third_party/ffmpeg/bin"


def default_pack() -> Path:
    return find_payload("movies/hd", "dist/movies-hd") or payload_root() / "dist/movies-hd"


def default_portrait_pack() -> Path:
    return find_payload("portraits/hd", "dist/portraits-hd") or payload_root() / "dist/portraits-hd"


#: The player's guide to replacement portraits (release.py MODULE_SPEC "base"), placed in <game>\portraits\.
PORTRAITS_README = "LONGEVITY_PORTRAITS_README.txt"


def portraits_readme() -> Path:
    return find_payload("portraits/" + PORTRAITS_README, "work/art/portraits-README.txt") \
        or payload_root() / "portraits" / PORTRAITS_README


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


#: Cached target table. load_targets() re-parses the 1.77 MB patches.yaml (~2 s), and resolution
#: happens once per candidate exe, so an uncached call would stall the run for seconds at a time.
_TARGETS = None


def _targets() -> dict:
    global _TARGETS
    if _TARGETS is None:
        try:
            _TARGETS = ap.load_targets(ap.DEFAULT_PATCHES)
        except ValueError as e:
            # A malformed or unreadable shipped patches.yaml is a packaging fault, not a bug in
            # the installer -- reporting it through the crash handler would tell the player the
            # opposite.
            raise ix.InstallerError(
                "The installer's patch data could not be read.",
                detail=f"  {e}",
                changed=False,
                remedy="download the installer again and extract the WHOLE archive.")
    return _TARGETS


def _exe_names() -> list:
    """Executable names worth looking for: the configured one plus every shipping target's.

    No hardcoded names (CLAUDE.md's opening invariant) -- and a base that is not a shipping
    target contributes no candidate.
    """
    names = [config.exe_name()]
    names += [t.exe for t in _targets().values() if t.launch and t.exe]
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def _md5_safe(p: Path, changed=False) -> str:
    """Hash a file, turning a read failure into an explained abort rather than a crash.

    `changed` is NOT decorative. This is called from sites that run after the exe has already
    been patched (the companion pass, the uninstall loop), and a hardcoded False there told the
    player "Nothing on disk was changed" about a half-finished install. Every caller past the
    first write passes True or None.
    """
    try:
        return _md5(p)
    except OSError as e:
        raise ix.InstallerError(
            f"Could not read {p.name}.",
            detail=f"  {type(e).__name__}: {e}",
            changed=changed,
            remedy="close Alpha Centauri if it is running, then re-run the installer.")


def _overlay_of(spec):
    """The folder-detected overlay (PRACX + Thinker) of a plain target: a launch target with
    requires_files on the same base. None when the table has none."""
    return next((t for t in _targets().values()
                 if t.launch and t.requires_files and t.base_md5 == spec.base_md5 and t.id != spec.id), None)


# ---- setting a folder up for Thinker ----------------------------------------------------------
# Thinker's launcher starts a file named terranx.exe and nothing else, and on a stock GOG
# Planetary Pack that name is the ORIGINAL game (no PRACX) while the PRACX build is
# terranx_PRACX.exe. A player who drops Thinker in such a folder runs Thinker on the original game
# and the mod never loads. The installer sets the folder up itself: the original terranx.exe is
# kept under a clear name (never deleted), a copy of the clean PRACX build takes its place, and
# the normal pass then patches it as the PRACX + Thinker configuration. A record file says what
# was done, so --uninstall puts the original back and a later run neither redoes it nor backs
# the original up a second time. Nothing is asked (user 2026-11-06): the swap is reversible. Every later step first reads the folder as it IS (GOG's "verify
# files" may have put the original back, Thinker may be gone, the player may have renamed
# things): it acts on the record only while the set-up is still in effect.
#
# terranx_PRACX.exe is LEFT CLEAN in a folder set up this way (put back to clean when an earlier
# plain install had patched it). Thinker players start thinker.exe; what else starts the game --
# GOG's "SMAX PRACX" entry and shortcut start terranx_PRACX.exe, its main Play button the original
# SMAC terran.exe -- is said in the closing note. An update refreshes only the receipt's exe, so a
# second patched copy would go stale beside a newer uimod.dll; a clean one is always safe to start.

#: The record of a set-up folder, beside the receipt. JSON: exe, backup (null when there was no
#: original), original_md5, source.
THINKER_SETUP_NAME = "longevity-thinker-setup.json"
#: What the original terranx.exe is kept as (a hash suffix is added if the name is taken by
#: something else).
THINKER_ORIGINAL_STEM = "_original"
_SETUP_TMP = ".longevity-tmp"


def _thinker_overlay():
    """The launcher overlay target (PRACX + Thinker): exe, launcher, requires_files. None if the
    table has none."""
    return next((t for t in _targets().values()
                 if t.launch and t.requires_files and t.exe and getattr(t, "launcher", None)), None)


def _read_setup(game_dir: Path) -> dict:
    import json
    try:
        doc = json.loads((Path(game_dir) / THINKER_SETUP_NAME).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _write_setup(game_dir: Path, doc: dict) -> None:
    import json
    p = Path(game_dir) / THINKER_SETUP_NAME
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _pracx_source(game_dir: Path, ov) -> Path:
    """A clean PRACX build in the folder under another name than the overlay's exe (the live file,
    or the .bak of one we patched), else None."""
    for n in _exe_names():
        if n.lower() == ov.exe.lower():
            continue
        for probe in (game_dir / n, game_dir / (n + ".bak")):
            try:
                if probe.is_file() and _md5(probe) == ov.base_md5:
                    return probe
            except OSError:
                continue
    return None


def _stock_original_hashes() -> set:
    """The hashes of the ORIGINAL game's exe under the overlay's name: the table's non-launch
    entries for that file name (the un-PRACX'd GOG terranx.exe)."""
    ov = _thinker_overlay()
    return {t.base_md5 for t in _targets().values()
            if not t.launch and t.exe and ov is not None and t.exe.lower() == ov.exe.lower()
            and t.base_md5}


def _our_builds(ov) -> set:
    """Every build this project made from the PRACX base (not the base itself): this release's
    and every earlier release's (known_patched), for every configuration of that base."""
    return ap.builds_on_base(_targets(), ov.base_md5)


def _pracx_family(ov) -> set:
    """Every hash that means "already a PRACX build": the clean base and each build we make from it."""
    return _our_builds(ov) | {ov.base_md5}


def _md5_or_none(p: Path):
    try:
        return _md5(p) if p.is_file() else None
    except OSError:
        return None


def _thinker_here(game_dir: Path, ov) -> bool:
    return all((Path(game_dir) / f).is_file() for f in ov.requires_files)


def _ours_in_setup(game_dir: Path, ov) -> bool:
    """In a folder the installer set up, is the exe Thinker starts a PRACX build of OURS -- the
    clean copy, or any release's build the table records (patched_md5 + known_patched)? A build
    the table does NOT record (a dev build, or a release whose hash was never added) is still
    recognised the way the uninstall trusts a backup: the record is there, the exe is not the
    recorded original, and its .bak is the clean PRACX base the set-up put there."""
    game_dir = Path(game_dir)
    rec = _read_setup(game_dir)
    if not rec:
        return False
    live = game_dir / (rec.get("exe") or ov.exe)
    md5 = _md5_or_none(live)
    if md5 is None or (rec.get("original_md5") and md5 == rec["original_md5"]) \
            or md5 in _stock_original_hashes():
        return False                      # the original (recorded, or GOG's stock one) is back
    if md5 in _pracx_family(ov):
        return True
    return _md5_or_none(live.with_suffix(live.suffix + ".bak")) == ov.base_md5


def _setup_in_effect(game_dir: Path) -> bool:
    """The installer set this folder up for Thinker, Thinker is still here, and the exe Thinker
    starts is still a PRACX build of ours (not the original put back by GOG or by hand)."""
    ov = _thinker_overlay()
    return (ov is not None and thinker_setup_folder(game_dir) and _thinker_here(game_dir, ov)
            and _ours_in_setup(game_dir, ov))


def _backup_name(game_dir: Path, ov, md5: str) -> Path:
    stem, suf = os.path.splitext(ov.exe)
    plain = game_dir / f"{stem}{THINKER_ORIGINAL_STEM}{suf}"
    try:
        if not plain.exists() or _md5(plain) == md5:
            return plain
    except OSError:
        pass
    return game_dir / f"{stem}{THINKER_ORIGINAL_STEM}-{md5[:8]}{suf}"


def _plain_name(src: Path) -> str:
    return src.name[:-4] if src.name.lower().endswith(".bak") else src.name


def plan_thinker_setup(game_dir: Path, args):
    """Decide what the install does about Thinker. Asks nothing (user 2026-11-06) and writes
    nothing (the install applies the plan just before it patches, after every question). The plan,
    or None:

    * {"action": "setup", ...}: thinker.dll beside a PRACX build that Thinker's launcher would never
      start: the stock original, one GOG put back, none at all, or one the installer does not
      recognise (kept under a hashed name) -- all set up, console or not. terranx.exe.bak follows
      the backup rule (`_backup_kind`): GOG's original is accepted (`gog_bak`), a build of ours is
      dropped (`drop_bak`); any other stops the whole install.
    * {"action": "aside", ...}: already PRACX, only a spare terranx.exe.bak to move or drop.
    * {"action": "release"}: a folder the installer set up whose set-up is no longer in effect and
      will not be redone -- Thinker removed from the folder: the
      original goes back and the record goes, so the plain install that follows never swaps it out.

    Raises the red refusal (nothing written) for a foreign terranx.exe.bak, and for a set-up folder
    whose terranx.exe is neither the file the set-up put there nor the original it kept: a Thinker
    player asked for Thinker, and the plain build is not that (user 2026-11-06).
    """
    game_dir = Path(game_dir)
    ov = _thinker_overlay()
    if ov is None:
        return None
    record = _read_setup(game_dir)
    thinker_here = _thinker_here(game_dir, ov)
    if not thinker_here and not record:
        return None
    live = game_dir / ov.exe
    # terranx.exe.bak FIRST, whatever terranx.exe is: the patch pass writes its own backup under
    # that name only when the name is free, and otherwise keeps what is there -- so a foreign .bak
    # beside a clean PRACX terranx.exe would be patched over and leave a build with no backup the
    # installer can trust (the next run calls it unknown; --uninstall cannot restore it). A .bak that
    # is GOG's original byte for byte is a spare copy of a file the set-up keeps anyway: accepted
    # without a word (apply removes the duplicate, or sets a non-duplicate aside, named in the
    # record). Anything else is a file nobody can vouch for: the whole install stops, before
    # anything is written (user 2026-11-05). Checked before the Thinker-removed shape too: the
    # revert can leave terranx.exe a clean PRACX copy that the pass then patches.
    # This is the backup rule (`_backup_kind`) applied to the exe Thinker starts, with the set-up's
    # record: a build of ours under that name is dropped (reproducible; the pass writes a fresh one).
    bak = live.with_suffix(live.suffix + ".bak")
    bak_md5 = _md5_or_none(bak)
    gog_bak = drop_bak = None
    if bak_md5 is not None:
        kind = _backup_kind(bak_md5, ov.base_md5)
        if kind == "gog":
            gog_bak = bak
        elif kind == "ours":
            drop_bak = bak
        elif kind == "foreign":
            raise _foreign_backup(bak)
    if not thinker_here:
        ix.say("\n--- Thinker ---")
        ix.say("  Thinker is no longer in this folder, which the installer had set up for it: the installer undoes that set-up (see below), and the mod is installed for PRACX without Thinker.")
        return {"action": "release", "game_dir": game_dir}   # a GOG/ours .bak: plan_backups, after the revert
    md5 = _md5_or_none(live)
    if md5 in _pracx_family(ov) or _ours_in_setup(game_dir, ov):
        if md5 != ov.base_md5:
            # A build of ours whose backup is GOG's copy or another build of ours: nothing the pass
            # can patch (no clean base anywhere under this name), so the spare is left where it is
            # -- moving or dropping it would be a write before the "nothing to patch" error.
            gog_bak = drop_bak = None
        if gog_bak is not None or drop_bak is not None:
            # Already PRACX, with a spare under the name the patch pass needs: GOG's copy set
            # aside, a build of ours dropped (apply), nothing else to set up.
            return {"action": "aside", "game_dir": game_dir, "ov": ov, "gog_bak": gog_bak,
                    "drop_bak": drop_bak, "record": record}
        return None                       # already PRACX: clean, or one of ours (any release)
    restored_by_gog = bool(record) and md5 is not None and (
        md5 == record.get("original_md5") or md5 in _stock_original_hashes())
    if record and md5 is not None and not restored_by_gog:
        # The folder was set up, and its terranx.exe is now neither ours nor the recorded original
        # (with no clean-base .bak beside it, or _ours_in_setup would have said so). Setting up
        # over it would replace the record's pointer to the real original, and installing the
        # plain build beside it would not be what a Thinker player asked for: the whole install
        # stops, before anything is written (user 2026-11-06), whether or not there is a PRACX
        # copy to set up from. --uninstall leaves this terranx.exe too, so the remedy is the file.
        raise _replaced_exe(game_dir, ov.exe, record)
    src = _pracx_source(game_dir, ov)
    if src is None:
        return None                       # no PRACX build here to set up from
    backup = None
    # For the one line only: GOG's own exe (or none). A recorded original GOG did NOT write (the
    # player's own build, copied back by hand) is restored_by_gog for the flow, not "vanilla".
    vanilla = md5 is None or md5 in _stock_original_hashes()
    if md5 is not None:
        if not vanilla and not restored_by_gog and bak_md5 == ov.base_md5:
            return None                   # ours under another build: patch_target re-patches it
        backup = _backup_name(game_dir, ov, md5)
    # No question (user 2026-11-06): Thinker starts terranx.exe and nothing else, the swap is the
    # one way to play Thinker with the mod, and it is reversible (the original is kept; --uninstall
    # puts it back) -- so it simply happens, a terranx.exe the installer does not recognise
    # included (kept under a hashed _original name). apply says what it did, in one line.
    return {"action": "setup", "game_dir": game_dir, "ov": ov, "live": live, "src": src,
            "backup": backup, "md5": md5, "record": record, "gog_bak": gog_bak, "drop_bak": drop_bak,
            "vanilla": vanilla, "keep_record": _keeps_record(game_dir, record, md5)}


#: The refusal for a <exe>.bak that is none of: the clean base, GOG's original, a build of ours.
#: The user's wording (2026-11-05), shown in red; the install stops before any write.
FOREIGN_BACKUP_TEXT = (
    "Your game installation folder has an unrecognized {name} file. This should only happen if you "
    "edited that file by hand. Longevity Vaccine cannot install over it. To install, verify that you "
    "do not need {name} and delete it, then re-run this installer. Your game was not modified.")

#: The refusal for a Thinker folder the installer set up whose terranx.exe is now neither the file
#: the set-up put there nor the original it kept (user 2026-11-06: a Thinker player asked for
#: Thinker; installing the plain build instead is not an install). Same shape as the .bak refusal.
#: {was} is the original clause: ", and it is not the original that install kept" + " as <name>"
#: while that file is still there and unchanged (""), or " (there was no <name> before it)" for a
#: set-up that found none.
REPLACED_EXE_TEXT = (
    "Your game installation folder was set up for Thinker by an earlier Longevity Vaccine install, but "
    "its {name} is no longer the file that install put there{was}. This should only happen if you "
    "replaced that file by hand. Longevity Vaccine cannot install over it. To install, verify that you "
    "do not need the current {name} and delete it, then re-run this installer. Your game was not modified.")


def _refusal(text: str) -> "ix.InstallerError":
    """A refusal in the user's shape: red, the install stops before any write, the window waits
    for any key; no second disk line (the text itself says the game was not modified)."""
    return ix.InstallerError(text, code=ix.EXIT_ABORTED, changed=False, title="INSTALLATION FAILED",
                             red=True, disk_note=False, any_key=True)


def _foreign_backup(bak: Path) -> "ix.InstallerError":
    return _refusal(FOREIGN_BACKUP_TEXT.format(name=bak.name))


def _replaced_exe(game_dir: Path, exe: str, record: dict) -> "ix.InstallerError":
    kept = record.get("backup")
    if not kept:
        was = f" (there was no {exe} before it)"
    else:
        still = record.get("original_md5") and _md5_or_none(Path(game_dir) / kept) == record["original_md5"]
        was = ", and it is not the original that install kept" + (f" as {kept}" if still else "")
    return _refusal(REPLACED_EXE_TEXT.format(name=exe, was=was))


# ---------------------------------------------------------------- the backup rule
#
# ONE rule for every exe the patch pass writes (its bytes are a clean shipping base, so the pass
# backs it up under <exe>.bak -- and keeps whatever already sits under that name). What may sit
# there: nothing, or that clean base -- fine. GOG's original game exe (an old tool's backup) is
# set aside under an _original name, or dropped when the folder keeps that copy already; a build
# of ours is dropped (it is remade from the clean base at will, as patch_target does). Anything
# else is a file nobody can vouch for: the install stops before any write, in the user's words.
# Without this the pass patched over such a .bak and left a build no later run could recognise
# and no --uninstall could restore. plan_thinker_setup applies the rule to the exe Thinker starts
# (with the set-up's record); plan_backups applies it to every other exe the pass will patch.

def _backup_kind(bak_md5: str, base_md5: str) -> str:
    """What a <exe>.bak beside an exe of base `base_md5` is: "clean" (that base), "gog" (the stock
    original game exe), "ours" (a build this project made from that base: any release, any
    configuration), or "foreign"."""
    if bak_md5 == base_md5:
        return "clean"
    if bak_md5 in _stock_original_hashes():
        return "gog"
    if bak_md5 in ap.builds_on_base(_targets(), base_md5):
        return "ours"
    return "foreign"


def plan_backups(game_dir: Path, candidates: list, thinker_plan) -> list:
    """Decide what the install does about the <exe>.bak of every exe the pass will patch. Writes
    nothing; raises the refusal now, before any write. Returns the items apply_backup_plan carries
    out just before the pass: {"bak", "md5", "base_md5", "kind", "aside_to"} per spare .bak (GOG's
    copy moved to `aside_to`, or dropped when None; a build of ours dropped).

    The exe Thinker starts is the Thinker plan's business whenever there is one (it already applied
    this rule to it); after a set-up the pass patches only that exe, so nothing else is checked."""
    if thinker_plan and thinker_plan["action"] == "setup":
        return []
    game_dir = Path(game_dir)
    ov = _thinker_overlay()
    beside = ap.folder_files(game_dir)
    out = []
    for p in candidates:
        if thinker_plan and ov is not None and p.name.lower() == ov.exe.lower():
            continue
        md5 = _md5_or_none(p)
        if md5 is None:
            continue
        try:
            spec = ap.resolve_target(md5, targets=_targets(), launch_only=True, present_files=beside,
                                     exe_name=p.name)
        except ap.UnsupportedBase:
            continue                      # not a clean base: the pass does not back this exe up
        bak = p.with_suffix(p.suffix + ".bak")
        bak_md5 = _md5_or_none(bak)
        if bak_md5 is None:
            continue
        kind = _backup_kind(bak_md5, spec.base_md5)
        if kind == "clean":
            continue
        if kind == "foreign":
            raise _foreign_backup(bak)
        aside_to = None
        if kind == "gog":
            # ov is not None: the stock hashes come from it. A copy the folder keeps already --
            # the stock exe under its own name, or at the _original name -- makes this a duplicate.
            aside_to = _backup_name(game_dir, ov, bak_md5)
            if _md5_or_none(game_dir / ov.exe) == bak_md5 or _md5_or_none(aside_to) == bak_md5:
                aside_to = None
            elif aside_to.exists():
                # Both _original names taken by other files: said now, with nothing written, not
                # at apply after the questions.
                raise ix.InstallerError(
                    f"{bak.name} could not be moved out of the way.",
                    detail=f"  {aside_to.name} is already there and is a different file", code=ix.EXIT_ABORTED,
                    changed=False, remedy=f"move {bak.name} out of the game folder yourself, then run the installer again.")
        out.append({"bak": bak, "md5": bak_md5, "base_md5": spec.base_md5, "kind": kind,
                    "aside_to": aside_to})
    return out


def _backup_still_as_planned(bak: Path, md5: str, base_md5: str) -> bool:
    """At apply: is this planned .bak still what the plan saw? False when it is gone or the clean
    base is back under its name (nothing to do); raises the refusal for anything else -- a file
    swapped in since the plan is nobody's to vouch for."""
    now = _md5_or_none(bak)
    if now is None or now == base_md5:
        return False
    if now != md5:
        raise _foreign_backup(bak)
    return True


def recheck_planned_backups(plan: list, thinker_plan) -> None:
    """Before the first write of the install: every .bak the two plans will move or drop is hashed
    again, so a refusal for one swapped since the plan comes before ANY drop (a dropped duplicate
    cannot be undone, and the refusal's text says the game was not modified)."""
    for item in plan:
        _backup_still_as_planned(item["bak"], item["md5"], item["base_md5"])
    if thinker_plan and thinker_plan["action"] in ("aside", "setup"):
        ov = thinker_plan["ov"]
        for key, kind in (("gog_bak", "gog"), ("drop_bak", "ours")):
            bak = thinker_plan.get(key)
            if bak is not None:
                now = _md5_or_none(bak)
                if now is not None and now != ov.base_md5 and _backup_kind(now, ov.base_md5) != kind:
                    raise _foreign_backup(bak)


def apply_backup_plan(plan: list) -> list:
    """Carry plan_backups' plan out, just before the patch pass (recheck_planned_backups first, so
    a swapped .bak is refused before anything is dropped; the check here is the belt to that brace).
    Returns the moves made as [(aside_to, bak)], for undo_backup_moves should a later step fail
    before the pass. Fails with the moves undone when a file cannot be moved."""
    moves = []
    for item in plan:
        bak, aside_to = item["bak"], item["aside_to"]
        try:
            if not _backup_still_as_planned(bak, item["md5"], item["base_md5"]):
                continue
        except ix.InstallerError:
            undo_backup_moves(moves)
            raise
        now = item["md5"]
        try:
            if item["kind"] == "ours":
                bak.unlink()
                line = f"  {bak.name} (a Longevity Vaccine build) was removed; the installer writes a fresh backup"
            elif aside_to is None:
                bak.unlink()              # a spare copy of a file the folder keeps: without a word
                line = None
            elif _md5_or_none(aside_to) == now:
                bak.unlink()              # kept there since the plan (a second spare, say): a duplicate
                line = None
            else:
                if aside_to.exists():
                    raise OSError(f"{aside_to.name} is already there and is a different file")
                os.replace(bak, aside_to)
                moves.append((aside_to, bak))
                line = f"  {bak.name} (GOG's original) is kept as {aside_to.name} (delete it if you do not need it)"
        except OSError as e:
            undo_backup_moves(moves)
            raise ix.InstallerError(
                f"{bak.name} could not be moved out of the way.",
                detail=f"  {type(e).__name__}: {e}", code=ix.EXIT_ABORTED, changed=False,
                remedy=f"move {bak.name} out of the game folder yourself, then run the installer again.")
        if line:
            ix.say(f"\n--- {bak.name} ---")
            ix.say(line)
    return moves


def undo_backup_moves(moves: list) -> None:
    """Put set-aside backups back where they were (a later step failed before the pass)."""
    for aside_to, bak in reversed(moves):
        try:
            if not bak.exists():
                os.replace(aside_to, bak)
        except OSError:
            pass


def _aside_name_for(game_dir: Path, ov, gog_md5: str, taken: Path = None) -> Path:
    """Where GOG's spare copy goes: the _original name when free (or already that file), else a
    hashed one; never the name the set-up is giving the live file (`taken`)."""
    stem, suf = os.path.splitext(ov.exe)
    aside = _backup_name(game_dir, ov, gog_md5)
    if taken is not None and aside == taken:
        aside = game_dir / f"{stem}{THINKER_ORIGINAL_STEM}-{gog_md5[:8]}{suf}"
    return aside


def _set_gog_backup_aside(game_dir: Path, ov, gog_bak: Path, aside_to: Path, gog_md5: str) -> str:
    """Move GOG's spare terranx.exe.bak to `aside_to`, or drop it when `aside_to` already IS that
    file. Returns "moved" / "dropped". Re-checks the bytes first: a file swapped in since the plan
    is refused like any other foreign .bak (nothing written yet). Raises OSError on a clash."""
    now = _md5_or_none(gog_bak)
    if now != gog_md5 or now not in _stock_original_hashes():
        raise _foreign_backup(gog_bak)
    if aside_to.exists():
        if _md5_or_none(aside_to) == gog_md5:
            gog_bak.unlink()
            return "dropped"
        raise OSError(f"{aside_to.name} is already there and is a different file")
    os.replace(gog_bak, aside_to)
    return "moved"


def _drop_our_backup(bak: Path, ov) -> bool:
    """Drop a terranx.exe.bak that is a build of ours (planned so). Hashed again first: gone, or
    the clean base back under the name -> nothing to do; anything else -> refused like any other
    foreign .bak (swapped since the plan; nothing of ours written yet). True when dropped."""
    now = _md5_or_none(bak)
    if now is None or now == ov.base_md5:
        return False
    if _backup_kind(now, ov.base_md5) != "ours":
        raise _foreign_backup(bak)
    bak.unlink()
    ix.say(f"  {bak.name} (a Longevity Vaccine build) was removed; the installer writes a fresh backup")
    return True


def _keeps_record(game_dir: Path, record: dict, md5) -> bool:
    """GOG's stock exe written over a set-up whose recorded original is the player's own and still
    kept: the record keeps pointing at that TRUE original, and the stock copy is only set aside
    (named in the record, so the uninstall can say where it is). With the kept original gone, the
    stock exe simply becomes the original."""
    if not record or md5 is None or not record.get("original_md5") or md5 == record["original_md5"]:
        return False
    kept = record.get("backup")
    return bool(kept) and _md5_or_none(Path(game_dir) / kept) == record["original_md5"]


def apply_thinker_setup(plan) -> bool:
    """Carry out plan_thinker_setup's plan: True when the folder was set up. Fails with nothing
    changed when it cannot finish (disk space, a locked file)."""
    if not plan:
        return False
    game_dir = plan["game_dir"]
    if plan["action"] == "release":
        revert_thinker_setup(game_dir, remedy="close Alpha Centauri and Thinker if they are running, then run the installer again.")
        return False
    if plan["action"] == "aside":
        # terranx.exe is already PRACX; only a spare terranx.exe.bak is in the way of the patch
        # pass's own backup: a build of ours is dropped; GOG's copy is set aside (or a duplicate
        # dropped) and named; a record, when there is one, names it too for --uninstall.
        ov, gog_bak, record = plan["ov"], plan["gog_bak"], plan["record"]
        if plan.get("drop_bak") is not None:
            _drop_our_backup(plan["drop_bak"], ov)
        if gog_bak is None:
            return False
        gog_md5 = _md5_or_none(gog_bak)
        if gog_md5 is None:
            return False                              # gone since the plan: nothing to do
        aside_to = _aside_name_for(game_dir, ov, gog_md5)
        try:
            how = _set_gog_backup_aside(game_dir, ov, gog_bak, aside_to, gog_md5)
            if how == "moved" and record:
                _write_setup(game_dir, {**record, "set_aside": (record.get("set_aside") or []) + [aside_to.name]})
        except OSError as e:
            raise ix.InstallerError(
                f"{gog_bak.name} (GOG's original) could not be moved out of the way.",
                detail=f"  {type(e).__name__}: {e}", code=ix.EXIT_ABORTED, changed=False,
                remedy=f"move {gog_bak.name} out of the game folder yourself, then run the installer again.")
        ix.say(f"  the {gog_bak.name} that was here (GOG's original) is "
               + (f"kept as {aside_to.name}" if how == "moved" else f"a duplicate of {aside_to.name}: removed"))
        return False
    ov, live, src = plan["ov"], plan["live"], plan["src"]
    backup, md5, record = plan["backup"], plan["md5"], plan["record"]
    # The copy, then the patch pass: its .bak and its temporary build.
    need = 3 * src.stat().st_size + (1 << 20)
    try:
        free = shutil.disk_usage(game_dir).free
    except OSError:
        free = None
    if free is not None and free < need:
        raise ix.InstallerError(
            "There is not enough free disk space to set the folder up for Thinker.",
            detail=f"  {need >> 20} MB is needed on the game's drive; {free >> 20} MB is free.",
            code=ix.EXIT_ABORTED, changed=False,
            remedy="free some space on that drive and run the installer again.")

    tmp = game_dir / (ov.exe + _SETUP_TMP)
    # An identical copy of the original is already kept (GOG put it back after an earlier
    # set-up): the live file is a duplicate, so it is simply replaced -- never renamed over the
    # kept one, which a failure could then not undo.
    kept_already = backup is not None and _md5_or_none(backup) == md5
    # A terranx.exe.bak that is GOG's original (plan_thinker_setup accepted it): the set-up's own
    # backup needs that name (the patch pass writes it, and only when it is free). A duplicate of
    # the original being kept goes; anything else is set aside under an _original name and
    # recorded, so --uninstall names it. Done AFTER the live file is kept and BEFORE the copy
    # lands, so a failure undoes it along with the rest.
    gog_bak = plan.get("gog_bak")
    gog_md5 = _md5_or_none(gog_bak) if gog_bak is not None else None
    if gog_md5 is not None and gog_md5 not in _stock_original_hashes():
        raise _foreign_backup(gog_bak)                # swapped since the plan: nothing written yet
    # Where the spare goes, decided before the record is written: a duplicate of the live file
    # (kept as `backup` in a moment) or of a copy already at the _original name is dropped;
    # anything else is set aside and the record names it.
    aside_to = None
    if gog_md5 is not None and gog_md5 != md5:
        aside_to = _aside_name_for(game_dir, ov, gog_md5, taken=backup)
        if aside_to.exists() and _md5_or_none(aside_to) == gog_md5:
            aside_to = None                           # already kept there: the .bak is a duplicate
    extra_aside = [aside_to.name] if aside_to is not None else []
    moved = False
    dropped = False
    moved_aside = False
    dropped_ours = False
    try:
        # A build of ours under the name the pass needs for its backup: dropped first (the rule;
        # remade from the clean base by the pass that follows).
        if plan.get("drop_bak") is not None:
            dropped_ours = _drop_our_backup(plan["drop_bak"], ov)
        shutil.copy2(src, tmp)
        if _md5(tmp) != ov.base_md5:
            raise OSError(f"the copy of {src.name} does not match the original")
        if backup is not None:
            if plan.get("keep_record"):
                aside = [n for n in record.get("set_aside") or [] if n != backup.name]
                _write_setup(game_dir, {**record, "set_aside": aside + [backup.name] + extra_aside})
            else:
                _write_setup(game_dir, {"exe": ov.exe, "backup": backup.name, "original_md5": md5,
                                        "source": _plain_name(src),
                                        **({"set_aside": extra_aside} if extra_aside else {})})
            if not kept_already:
                os.replace(live, backup)
                moved = True
        elif not record.get("backup"):
            _write_setup(game_dir, {"exe": ov.exe, "backup": None, "original_md5": None,
                                    "source": _plain_name(src),
                                    **({"set_aside": extra_aside} if extra_aside else {})})
        elif extra_aside:
            # A set-up that stopped between its two steps keeps its record; the spare it sets
            # aside now is named in it too, or --uninstall could never name it.
            _write_setup(game_dir, {**record, "set_aside": (record.get("set_aside") or []) + extra_aside})
        if gog_md5 is not None and gog_bak.is_file():
            if aside_to is None:
                if _md5_or_none(gog_bak) != gog_md5:
                    raise _foreign_backup(gog_bak)    # swapped since the plan
                gog_bak.unlink()                      # a duplicate of a copy that is kept
                dropped = True
            else:
                moved_aside = _set_gog_backup_aside(game_dir, ov, gog_bak, aside_to, gog_md5) == "moved"
        os.replace(tmp, live)
    except ix.InstallerError:
        # The foreign-backup refusal, raised with nothing of ours left behind: the copy, and a
        # record that only this run wrote, go first.
        for p in (tmp,) + ((game_dir / THINKER_SETUP_NAME,) if not record else ()):
            try:
                p.unlink()
            except OSError:
                pass
        if record:
            try:
                _write_setup(game_dir, record)
            except OSError:
                pass
        try:
            if moved and not live.exists():
                os.replace(backup, live)
        except OSError:
            pass
        raise
    except OSError as e:
        # Undo in reverse: nothing half-done is left behind.
        try:
            if moved_aside and not gog_bak.exists():
                os.replace(aside_to, gog_bak)
                moved_aside = False
        except OSError:
            pass
        try:
            if moved and not live.exists():
                os.replace(backup, live)
                moved = False
        except OSError:
            pass
        for p in (tmp,) + ((game_dir / THINKER_SETUP_NAME,) if not moved and not record else ()):
            try:
                p.unlink()
            except OSError:
                pass
        if record and not moved:
            try:
                _write_setup(game_dir, record)          # the record as it was before this run
            except OSError:
                pass
        detail = f"  {type(e).__name__}: {e}"
        if dropped:
            detail += (f"\n  {gog_bak.name} (a spare copy of GOG's original) was removed; the original is still "
                       f"{backup.name if backup is not None and backup.exists() else ov.exe}.")
        if dropped_ours:
            detail += f"\n  {plan['drop_bak'].name} (a Longevity Vaccine build) was removed."
        raise ix.InstallerError(
            "The folder could not be set up for Thinker.",
            detail=detail,
            code=ix.EXIT_ABORTED,
            changed=None if (moved or moved_aside) else (True if (dropped or dropped_ours) else False),
            remedy="close Alpha Centauri and Thinker if they are running, then run the installer again.")
    # One line, the user's wording (2026-11-06); the shapes that differ from it say so.
    ix.say("\n--- Thinker ---")
    if md5 is None:
        kept = record.get("backup")
        kept_intact = bool(kept) and record.get("original_md5") \
            and _md5_or_none(game_dir / kept) == record["original_md5"]
        ix.say(f"  Put the PRACX .exe in place as {ov.exe}. This is required to load both Longevity and Thinker. "
               + (f"Your original is kept as {kept}. Uninstalling Longevity Vaccine puts it back." if kept_intact
                  else "Uninstalling Longevity Vaccine removes it again." if not kept
                  # the kept original gone or changed since: undo leaves the PRACX copy and says so (state 5)
                  else f"Uninstalling Longevity Vaccine undoes this where it can ({kept}, the original it kept, "
                       f"{'is no longer in the folder' if not (game_dir / kept).is_file() else 'has changed since it was kept'})."))
    elif plan.get("keep_record"):
        ix.say(f"  Swapped the vanilla Alien Crossfire .exe for the PRACX .exe. This is required to load both Longevity "
               f"and Thinker. Your earlier {ov.exe} is still kept as {record['backup']}, and GOG's copy as "
               f"{backup.name}. Uninstalling Longevity Vaccine puts your earlier {ov.exe} back.")
    else:
        what = "the vanilla Alien Crossfire .exe" if plan.get("vanilla") else f"the {ov.exe} that was here"
        ix.say(f"  Swapped {what} for the PRACX .exe. This is required to load both Longevity and Thinker. "
               f"Your original is kept as {backup.name}. Uninstalling Longevity Vaccine puts it back.")
    if moved_aside:
        ix.say(f"  The {ov.exe}.bak that was here (GOG's original) is kept as {aside_to.name}.")
    # An earlier plain install's patched terranx_PRACX.exe: back to clean, so no second patched
    # copy is left to go stale (see the section comment). Ours by its hash; nothing else touched.
    if src.name.lower().endswith(".bak"):
        other = src.with_name(_plain_name(src))
        if _md5_or_none(other) in _our_builds(ov):
            try:
                shutil.copy2(src, other)
                ix.say(f"  {other.name} is back to the clean PRACX build (Thinker players start thinker.exe)")
            except OSError as e:
                ix.warn(f"  {other.name} could not be put back to the clean PRACX build ({e}); it keeps an older build of the mod.")
    return True


def thinker_setup_folder(game_dir: Path) -> bool:
    """Was this folder set up for Thinker by the installer (its record is there)?"""
    return (Path(game_dir) / THINKER_SETUP_NAME).is_file()


def revert_thinker_setup(game_dir: Path, held: list = None, remedy: str = None,
                         title: str = None) -> int:
    """Put a folder the installer set up for Thinker back the way it was (--uninstall, and a
    set-up that will not be redone): our build of terranx.exe back to the clean PRACX copy, then
    undo_thinker_setup. Returns the number of files put back; `held` collects the exe it had to
    leave as it is (the uninstall's generic restore must not touch it either).

    The clean copy comes from terranx.exe.bak when that is the clean base; for a build the table
    records (any release's, by its hash), also from the folder's other clean PRACX copy when the
    .bak is gone. A build known only by its place (one the table does not record) needs the .bak:
    without it nothing proves the file is ours, and undo leaves it."""
    game_dir = Path(game_dir)
    rec = _read_setup(game_dir)
    ov = _thinker_overlay()
    if not rec or ov is None:
        return 0
    exe = rec.get("exe") or ov.exe
    live, bak = game_dir / exe, game_dir / (exe + ".bak")
    live_md5 = _md5_or_none(live)
    n = 0
    kept = False
    clean = None
    if live_md5 not in (None, ov.base_md5) and _ours_in_setup(game_dir, ov):
        if _md5_or_none(bak) == ov.base_md5:
            clean = bak
        elif live_md5 in _our_builds(ov):
            clean = _pracx_source(game_dir, ov)
    if clean is not None:
        try:
            if live_md5 not in _our_builds(ov):
                # A build the table does not record, recognised by its place, not its hash: keep
                # the bytes beside, named by their hash, as patch_target does -- never destroy them.
                keep = live.with_name(f"{live.name}.replaced-{live_md5[:8]}")
                if not keep.exists():
                    shutil.copy2(live, keep)
                    kept = True
                    ix.say(f"  the replaced {exe} is kept as {keep.name} (delete it if you do not need it)")
            shutil.copy2(clean, live)
            n += 1
        except OSError as e:
            raise ix.InstallerError(
                f"Could not restore {exe} from its backup.", detail=f"  {e}", changed=kept, title=title,
                remedy=remedy or "close the game and Thinker if they are running and try again.")
    put_back = undo_thinker_setup(game_dir, held, changed=bool(n), remedy=remedy, title=title)
    if n and not put_back:
        ix.say(f"  restored {exe} from {clean.name}")
    return put_back or n


def _free_pracx_name(game_dir: Path, rec: dict, ov) -> Path:
    """Where the folder's only PRACX copy goes when it is not to stay as the overlay's exe: the
    name it was copied from, else a numbered one beside it."""
    first = game_dir / (rec.get("source") or "terranx_PRACX.exe")
    if not first.exists():
        return first
    stem, suf = os.path.splitext(first.name)
    i = 2
    while (game_dir / f"{stem} ({i}){suf}").exists():
        i += 1
    return game_dir / f"{stem} ({i}){suf}"


def undo_thinker_setup(game_dir: Path, held: list = None, changed: bool = False,
                       remedy: str = None, title: str = None) -> int:
    """Put the original terranx.exe back where the installer set the folder up for Thinker, from
    whatever state the folder is in now. Returns the number of files put back. Never destroys a
    PRACX build the folder has no other copy of, nor a file it did not put there. Where it has to
    leave the exe as it is, it says where the original is and appends the exe to `held`.

    The states, in order (live = terranx.exe now, kept = the original the set-up kept):
      1. live is the recorded original                -> in place; a duplicate kept copy goes.
      2. live is GOG's stock exe and the kept original is still there and is something else (the
         player's own) -> the stock copy is set aside, the kept original goes back.
      3. live is GOG's stock exe otherwise            -> it is the original now; in place.
      4. live is not the clean PRACX copy             -> left as it is, said so, held.
      5. the kept original changed / is gone          -> live stays (clean PRACX or none), said so.
      6. otherwise (live is the clean copy, or none)  -> the kept original goes back (or our copy
         goes, when there was none before), the only PRACX copy kept under a plain name.

    Two things always hold afterwards, so no later run acts on a stale picture: the record is gone
    (once undone or explained it describes nothing left to do; keeping it would make the folder
    read as installed for good), and our clean-PRACX terranx.exe.bak is gone too -- deleted when
    the folder has another PRACX copy or live is the base, else renamed to a plain PRACX exe --
    because a .bak of ours beside some other terranx.exe is exactly what the uninstall's generic
    restore copies over it. Files the set-up set aside are named."""
    game_dir = Path(game_dir)
    rec = _read_setup(game_dir)
    ov = _thinker_overlay()
    if not rec or ov is None:
        return 0
    exe = rec.get("exe") or ov.exe
    live, bak = game_dir / exe, game_dir / (exe + ".bak")
    back = (game_dir / rec["backup"]) if rec.get("backup") else None
    original = rec.get("original_md5")
    put_back = 0
    left = None
    gog = "GOG Galaxy's \"verify files\" restores the original"
    try:
        live_md5 = _md5_or_none(live)
        back_md5 = _md5_or_none(back) if back is not None else None
        stock = live_md5 is not None and live_md5 in _stock_original_hashes()
        still = f"; the original is still {back.name}." if back_md5 is not None else "."
        now = (f"{exe} stays a clean PRACX copy" if live_md5 == ov.base_md5
               else f"there is no {exe} in the folder now")
        aside_names = [n for n in rec.get("set_aside") or [] if (game_dir / n).is_file()]
        stock_aside = next((game_dir / n for n in aside_names
                            if _md5_or_none(game_dir / n) in _stock_original_hashes()), None)
        if live_md5 is not None and original and live_md5 == original:                       # 1
            if back is not None and back_md5 == live_md5:
                back.unlink()                         # a duplicate of the file now in place
                changed = True
        elif stock and original and back_md5 == original:                                    # 2
            aside = live.with_name(f"{live.name}.replaced-{live_md5[:8]}")
            same = [game_dir / n for n in aside_names if _md5_or_none(game_dir / n) == live_md5]
            if same:
                aside = same[0]
                live.unlink()                         # the same copy is already set aside
            elif aside.exists() and _md5_or_none(aside) == live_md5:
                live.unlink()                         # the same copy is already set aside
            else:
                i = 2
                while aside.exists():
                    aside = live.with_name(f"{live.name}.replaced-{live_md5[:8]}-{i}")
                    i += 1
                os.replace(live, aside)
            changed = True
            if not same:                              # a set-aside copy is named below
                ix.say(f"  GOG's copy of {exe} is kept as {aside.name} (delete it if you do not need it)")
            os.replace(back, live)
            ix.say(f"  put your earlier {exe} back (from {back.name})")
            put_back += 1
        elif stock:                                                                           # 3
            if back is not None and back_md5 == live_md5:
                back.unlink()
                changed = True
            elif back_md5 is not None:
                ix.say(f"  your earlier {exe} is still kept as {back.name}")
        elif live_md5 is not None and live_md5 != ov.base_md5:                               # 4
            left = f"  {exe} is not the PRACX copy the installer put there, so it was left as it is" + still
        elif ((back_md5 is not None and original and back_md5 != original)
              or (back is not None and back_md5 is None)) and stock_aside:                   # 5, GOG's copy at hand
            if live_md5 is not None and _pracx_source(game_dir, ov) is None \
                    and _md5_or_none(bak) != ov.base_md5:
                keep = _free_pracx_name(game_dir, rec, ov)
                os.replace(live, keep)
                ix.say(f"  {exe} was the folder's only PRACX copy: kept as {keep.name}")
            os.replace(stock_aside, live)
            changed = True
            ix.say(f"  the kept original is {'gone' if back_md5 is None else 'changed'}: put GOG's copy of {exe} back (from {stock_aside.name})")
            put_back += 1
        elif back_md5 is not None and original and back_md5 != original:                    # 5
            ix.warn(f"  {back.name} has changed since the installer kept it, so it was not put back as {exe}: {now}, and {gog}.")
        elif back is not None and back_md5 is None:
            ix.warn(f"  The original {exe} the installer kept ({back.name}) is no longer in the folder: {now}, and {gog}.")
        else:                                                                                 # 6
            if live_md5 is not None and _pracx_source(game_dir, ov) is None \
                    and _md5_or_none(bak) != ov.base_md5:
                # The folder's only PRACX build: keep it under the name it was copied from.
                keep = _free_pracx_name(game_dir, rec, ov)
                os.replace(live, keep)
                changed = True
                ix.say(f"  {exe} was the folder's only PRACX copy: kept as {keep.name}")
                live_md5 = None
            if back_md5 is not None:
                os.replace(back, live)
                changed = True
                ix.say(f"  put the original {exe} back (from {back.name})")
                put_back += 1
            elif back is None and live_md5 is not None:
                live.unlink()
                changed = True
                ix.say(f"  removed the {exe} the installer had put there (there was none before)")
                put_back += 1
        if left is not None:
            ix.warn(left)
            if held is not None:
                held.append(exe)
        if _md5_or_none(bak) == ov.base_md5:
            if _pracx_source(game_dir, ov) is not None or _md5_or_none(live) == ov.base_md5:
                bak.unlink()                          # nobody's backup any more
            else:
                keep = _free_pracx_name(game_dir, rec, ov)
                os.replace(bak, keep)                 # the folder's only PRACX copy: kept
                ix.say(f"  the folder's only PRACX copy is kept as {keep.name}")
            changed = True
        for name in rec.get("set_aside") or []:
            if (game_dir / name).is_file() and name != (back.name if back is not None else None):
                ix.say(f"  GOG's copy of {exe} that the installer set aside is kept as {name} (delete it if you do not need it)")
        for p in (game_dir / (exe + _SETUP_TMP), game_dir / (THINKER_SETUP_NAME + ".tmp"),
                  game_dir / THINKER_SETUP_NAME):
            if p.exists():
                p.unlink()
    except OSError as e:
        raise ix.InstallerError(
            f"Could not put the original {exe} back.", detail=f"  {e}", changed=bool(changed), title=title,
            remedy=remedy or "close the game and Thinker if they are running and run --uninstall again.")
    return put_back


def patch_target(target: Path, force: bool) -> str:
    """Patch one exe according to its MD5, via the patches.yaml target table.

    Resolution is `resolve_target(md5, launch_only=True)`: a base that is not a SHIPPING target is
    refused even if the patcher technically knows how to patch it. That is what enforces the
    "vanilla is no longer a supported configuration" decision -- before this, the table recorded
    the decision and the installer ignored it, happily patching the dropped base and reporting
    success.
    """
    md5 = _md5_safe(target)
    beside = ap.folder_files(target.parent)   # thinker.dll beside the exe selects the overlay

    try:
        spec = ap.resolve_target(md5, targets=_targets(), launch_only=True, present_files=beside,
                                 exe_name=target.name)
    except ap.UnsupportedBase:
        # Not a supported clean base. If its backup IS one, this exe is already patched.
        bak = target.with_suffix(target.suffix + ".bak")
        if bak.exists():
            try:
                bak_spec = ap.resolve_target(_md5_safe(bak), targets=_targets(), launch_only=True,
                                             present_files=beside, exe_name=target.name)
            except ap.UnsupportedBase:
                bak_spec = None
            if bak_spec is not None:
                ix.say(f"\n--- {target.name} ---")
                # The .bak says WHICH base; the live bytes say which BUILD. The folder may have
                # changed since that build was made (Thinker added or removed beside it), and the
                # two configurations share one base: a build for the other one is not "already
                # patched", it is the wrong build -- Thinker's own launch guard refuses the plain
                # PRACX build, and the plain configuration runs the Thinker build without the DLL
                # pass that completes it (audit 2026-10-28). Re-patch from the backup.
                current = bak_spec.patched_md5 is None or md5 == bak_spec.patched_md5
                if not force and current:
                    ix.say("  already patched (backup is a supported clean base) -- skipping."
                           " Use --force to re-patch.")
                    _patch_log_line(f"{target.name}: already patched (MD5 {md5}; .bak is the clean base "
                                    f"{bak_spec.base_md5}, target {bak_spec.id!r})")
                    return "already-patched"
                if not current:
                    ix.say(f"  not the build this installer produces for this folder (a different configuration, or an "
                           f"older build) -> restoring from .bak, then re-patching as {bak_spec.label}...")
                    # Those live bytes may be a player's own edit or another tool's work on top of ours:
                    # never destroy them (audit 2026-10-29) -- keep them beside, named by their hash.
                    # A build this project made (any release, any configuration of this base) is
                    # ours to replace: re-made from the backup at will, so no copy is kept.
                    keep = target.with_name(f"{target.name}.replaced-{md5[:8]}")
                    ours = ap.builds_on_base(_targets(), bak_spec.base_md5)
                    if md5 not in ours and not keep.exists():
                        try:
                            shutil.copy2(target, keep)
                            ix.say(f"  the replaced file is kept as {keep.name} (delete it if you do not need it)")
                        except OSError as e:
                            ix.say(f"  WARNING: could not keep a copy of the replaced file ({e}); nothing restored.")
                            return "error"
                else:
                    ix.say("  --force: restoring from .bak, then re-patching...")
                if ap.restore(target) != 0:
                    return "error"
                return patch_target(target, force=False)
        # Some other executable in the folder (the stock terranx.exe beside terranx_PRACX.exe,
        # most often). Not a candidate, not worth a line: "not a supported configuration"
        # printed for a file the player never asked about only read as a fault (user, 2026-09-27).
        return "unknown"

    ix.say(f"\n--- {target.name} ---")
    if spec.requires_files:
        ix.say(f"  {', '.join(spec.requires_files)} found beside it -> {spec.label} configuration")
    # (No "run the installer again and let it set the folder up" note here any more: the set-up
    # happens before this pass, unasked, whenever a clean PRACX source exists, so the only Thinker
    # folder that reaches a plain build has no clean PRACX copy to set up from.)
    ix.say(f"  patching (clean {spec.label})")
    _patch_log_line(f"{target.name}: target {spec.id!r} ({spec.label}), clean base {md5}, "
                    f"expected result {spec.patched_md5 or '(unrecorded)'}")
    # Pass the spec, not loose hashes: the baseline, size and group selection all come from the
    # one table entry, and the installer inherits its dry-run-only rule -- a base with no
    # recorded patched_md5 can never be written to, even if someone adds it to the launch set.
    # `summary`: one coloured line per phase instead of the patcher's 3,000-line transcript.
    rc = ap.apply_patches(target, Path(ap.DEFAULT_PATCHES), do_apply=True, spec=spec,
                          summary=_patch_summary)
    if rc == 2:
        # A refusal, not a failure: the target is declared but not ready to be written to (no
        # recorded lock, no measured baseline). "A patch step failed -- re-run the installer"
        # would be the wrong thing to tell the user, because re-running cannot help.
        return "unsupported"
    if rc == 4:
        ix.bad("  ERROR: the patched result does not match this release's recorded lock; nothing "
               "was written.")
        return "error"
    if rc != 0:
        return "error"
    if spec.patched_md5:
        got = _md5_safe(target, changed=True)
        if got != spec.patched_md5:
            ix.bad(f"  ERROR: patched MD5 {got} != expected {spec.patched_md5}")
            return "error"
    return "patched"


_PATCH_LOG = None   # the open fresh-install patch log while fresh_patch_log() is active; _patch_summary tees into it


def _patch_log_line(text: str) -> None:
    """One line into the fresh-install patch log, if one is open. Never raises."""
    if _PATCH_LOG is None:
        return
    try:
        _PATCH_LOG.write(ix.redact_profile(text) + "\n")     # the log goes into public bug reports
        _PATCH_LOG.flush()
    except (OSError, ValueError):
        pass


def _patch_summary(status: str, text: str) -> None:
    """apply_patches' per-phase report line, coloured: green SUCCESS, red ERROR, plain otherwise -- and
    recorded in the fresh-install patch log (fresh_patch_log) as `[status] text`."""
    if status == "ok":
        ix.good("  " + text)
    elif status == "error":
        ix.bad("  " + text)
    else:
        ix.say("  " + text)
    _patch_log_line(f"[{status}] {text}")


def check_companion_declarations() -> None:
    """Refuse a patch set whose companion entries are incomplete, BEFORE anything is written.

    Raised early on purpose: a companion is only patched after the exe, so discovering the
    problem there would mean aborting with the exe already modified.
    """
    bad = []
    for t in _targets().values():
        if not t.launch:
            continue
        for fname, cinfo in (t.companion or {}).items():
            if not isinstance(cinfo, dict):
                bad.append(f"{t.id}:{fname} (not a table)")
                continue
            missing = [k for k in ("patches", "base_md5", "base_size")
                       if cinfo.get(k) in (None, "")]
            # ALSO check the patch file is actually in the payload. companion_specs() skips a
            # missing one silently, which reproduces the original defect -- shipped but never
            # applied -- with no diagnostics at all.
            if cinfo.get("patches") and not (
                    Path(ap.DEFAULT_PATCHES).parent / cinfo["patches"]).is_file():
                missing.append(f"the file {cinfo['patches']}")
            # base_size must come from the TABLE, never from the live file. The live prax.dll may
            # already be patched (or be some other build entirely), and feeding its size in as
            # "the size the clean base should have" made the check compare the .bak against the
            # patched file's length -- so the companion aborted on every second update, and on a
            # foreign prax.dll the check was simply vacuous.
            if missing:
                bad.append(f"{t.id}:{fname} (no {', '.join(missing)})")
    if bad:
        raise ix.InstallerError(
            "The installer's patch data is incomplete.",
            code=ix.EXIT_ABORTED, changed=False,
            detail="  incomplete companion entries: " + "; ".join(bad),
            remedy="download the installer again; if it keeps happening, report this.")


def companion_specs(game_dir: Path, spec) -> list:
    """[(live_path, patches_path, TargetSpec)] for each companion of `spec` present on disk."""
    out = []
    for fname, cinfo in (spec.companion or {}).items():
        live = Path(game_dir) / fname
        cpatch = Path(ap.DEFAULT_PATCHES).parent / (cinfo.get("patches") or "")
        if not (live.is_file() and cpatch.is_file()):
            continue
        out.append((live, cpatch, ap.TargetSpec(f"{spec.id}:{fname}", {
            "label": f"{spec.label} companion {fname}", "exe": fname, "launch": spec.launch,
            "base_md5": cinfo.get("base_md5"), "base_size": cinfo.get("base_size"),
            "groups": "all", "patched_md5": cinfo.get("patched_md5")})))
    return out


#: What a companion binary on disk turns out to be.
C_CLEAN, C_PATCHED, C_STALE, C_FOREIGN = "clean", "patched", "stale", "foreign"


def _known_patched(spec, fname: str) -> set:
    """Every MD5 this project has ever produced for `fname`.

    Needed to tell OUR older output from somebody else's file. Without it, a companion patched by
    a previous release matches neither `base_md5` nor the current `patched_md5`, so it was
    written off as foreign and left stale beside a freshly-patched exe -- exactly the drift
    `patch_companions` exists to prevent. `known_patched:` in patches.yaml is the append-only
    history; the current lock is always a member.
    """
    cinfo = (spec.companion or {}).get(fname) or {}
    out = {str(m).lower() for m in (cinfo.get("known_patched") or []) if m}
    if cinfo.get("patched_md5"):
        out.add(str(cinfo["patched_md5"]).lower())
    return out


def companion_state(live: Path, cspec, known: set, changed=False) -> tuple:
    """(state, md5) for a companion binary on disk.

    `changed` is threaded through to _md5_safe: this runs BEFORE any write on the update path
    and AFTER the exe has been patched on the fresh path, and an unreadable file must not be
    reported as "nothing was changed" in the second case.
    """
    md5 = _md5_safe(live, changed=changed)
    if md5 == cspec.base_md5:
        return C_CLEAN, md5
    if cspec.patched_md5 and md5 == cspec.patched_md5:
        return C_PATCHED, md5
    if md5 in known:
        return C_STALE, md5
    return C_FOREIGN, md5


def _verified_backup(live: Path, cspec, changed=False):
    """The companion's .bak, but ONLY when it really is the clean base. Else None.

    `ap.restore()` is an unconditional copy, so calling it on an unverified backup overwrites a
    good file with whatever happens to sit in the .bak -- and for a companion there is no second
    copy anywhere. The exe path has always verified this; the companion path did not.
    """
    bak = live.with_suffix(live.suffix + ".bak")
    if bak.is_file() and _md5_safe(bak, changed=changed) == cspec.base_md5:
        return bak
    return None


def patch_companions(game_dir: Path, spec, force: bool) -> dict:
    """Patch the target's companion binaries (prax.dll on the PRACX targets).

    Returns {file name: "ok" | "skip" | "error"} for the summary table.

    Without this a FRESH install patched only the exe. patches_prax.yaml -- the PRACX
    bottom/left/right edge-scroll console-exemption fix -- shipped in the payload and was never
    applied, and because the exe was already-patched afterwards, nothing brought the companion up
    to date later either: only the --update path ever looked at `companion:`.

    A companion failure is never fatal. The mod is complete without it; only the PRACX-specific
    click fix is missed, and saying so is better than refusing an install that would work.
    """
    out = {}
    for live, cpatch, cspec in companion_specs(game_dir, spec):
        ix.say(f"\n--- {live.name} ---")
        out[live.name] = "skip"
        # changed=True throughout: the exe was patched before this function was called.
        state, md5 = companion_state(live, cspec, _known_patched(spec, live.name), changed=True)

        if state == C_PATCHED and not force:
            ix.say("  already patched -- skipping. Use --force to re-patch.")
            out[live.name] = "ok"
            continue
        if state == C_FOREIGN:
            # Someone else's build -- a newer PRACX, most likely. Restoring OUR backup over it
            # would silently downgrade their install, so never touch it.
            ix.say(f"  MD5 {md5} is not a build this patch set knows -- leaving it alone.")
            ix.say("  (the mod works without it; only the PRACX edge-scroll fix is missed)")
            continue
        if state != C_CLEAN:
            bak = _verified_backup(live, cspec, changed=True)
            if bak is None:
                ix.say(f"  {live.name} is patched but its backup is missing or is not the "
                       f"original -- leaving it alone.")
                ix.say("  (the mod works without it; only the PRACX edge-scroll fix is missed)")
                continue
            ix.say(f"  restoring from the verified {bak.name}, then patching...")
            if ap.restore(live) != 0:
                ix.warn("  WARNING: could not restore it from .bak; leaving it alone.")
                continue
            if _md5_safe(live, changed=True) != cspec.base_md5:
                ix.warn("  WARNING: the restored file is not the clean base; leaving it alone.")
                continue

        _patch_log_line(f"{live.name}: target {cspec.id!r} ({cspec.label}), clean base {cspec.base_md5}, "
                        f"expected result {cspec.patched_md5 or '(unrecorded)'}")
        if ap.apply_patches(live, cpatch, do_apply=True, spec=cspec, summary=_patch_summary) != 0:
            ix.warn(f"  WARNING: patching {live.name} failed; the mod still works without it.")
            out[live.name] = "error"
            continue
        got = _md5_safe(live, changed=True)
        if cspec.patched_md5 and got != cspec.patched_md5:
            ix.bad(f"  ERROR: patched MD5 {got} != expected {cspec.patched_md5}")
            out[live.name] = "error"
            continue
        out[live.name] = "ok"
    return out


#: Thinker's build. Every detour the mod makes into thinker.dll is pinned to one build (version.py
#: THINKER_SUPPORTED_*); the game shows a dialog in these same words on any other build (uimod thinkerversion.cpp),
#: and the installer says it here so the player learns it before the first launch. A warning only: the install is
#: complete and correct either way.
def thinker_build(game_dir: Path):
    """(stamp, image size, label) of the folder's thinker.dll, or None when there is none. An unreadable PE
    reads as (None, None, 'an unreadable thinker.dll')."""
    p = game_dir / "thinker.dll"
    if not p.is_file():
        return None
    try:
        data = p.read_bytes()
        if data[:2] != b"MZ":
            raise ValueError("no MZ header")
        pe = int.from_bytes(data[0x3C:0x40], "little")
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("no PE signature")
        if len(data) < pe + 0x54:
            raise ValueError("truncated PE header")   # a short slice would read as 0, not raise
        stamp = int.from_bytes(data[pe + 8:pe + 12], "little")
        size = int.from_bytes(data[pe + 0x50:pe + 0x54], "little")
    except (OSError, ValueError, IndexError):
        return None, None, "an unreadable thinker.dll"
    m = re.search(rb"Thinker Mod v(\d+(?:\.\d+)*)", data)
    label = f"Thinker Mod v{m.group(1).decode('ascii')}" if m else "an unrecognised build of Thinker Mod"
    return stamp, size, label


def thinker_build_warning(game_dir: Path) -> str:
    """The warning text for a thinker.dll that is not the supported build, or '' (none / supported)."""
    import manifest
    found = thinker_build(game_dir)
    if found is None:
        return ""
    stamp, size, label = found
    if stamp == ver.THINKER_SUPPORTED_STAMP and size == ver.THINKER_SUPPORTED_IMAGE_SIZE:
        return ""
    return (f"Longevity Vaccine {ver.VERSION} was built for Thinker Mod {ver.THINKER_SUPPORTED_VERSION}. "
            f"This game folder's thinker.dll is {label}. Some game features may not work as intended. "
            f"We recommend checking for a Longevity Vaccine update in-game or at {manifest.DOWNLOAD_URL}. "
            "If no update exists, consider playing with the officially supported version of Thinker.")


def warn_thinker_build(game_dir: Path) -> bool:
    """Print the thinker.dll build warning when one applies. Returns True when it did."""
    text = thinker_build_warning(game_dir)
    if not text:
        return False
    ix.say("")
    ix.warn("  WARNING: " + text)
    return True


#: The Datalinks text: helpx.txt's cloaking / submarine detection rules and conceptsx.txt's Waypoints paragraph
#: (scripts/patch_helptext.py, shipped in the base payload). Applied on every install and update run, to the game
#: folder and to Thinker's smac_mod\ copies when present; undone by --uninstall. Documentation only: a file that is
#: not the stock text (another mod's, a translation) is left alone and said so, and a missing file is skipped --
#: never a reason to fail an install that works.
def patch_datalinks_text(game_dir: Path) -> dict:
    """Returns {relative file name: patch_helptext status} for the summary; {} when nothing was found."""
    try:
        import patch_helptext as ph
    except Exception as e:  # noqa: BLE001 - a mistrimmed payload is the same user-facing problem whatever the type
        ix.warn(f"  WARNING: the Datalinks text patch is missing from this installer ({type(e).__name__}: {e}) "
                "-- skipped; the mod works without it.")
        return {}
    ix.say("\n--- Datalinks text ---")
    try:
        res = ph.patch_folder(game_dir, say=lambda t: ix.say("  " + t))
    except OSError as e:
        ix.warn(f"  WARNING: could not update the Datalinks text ({e}); the mod works without it.")
        return {}
    if not res:
        ix.say("  no helpx.txt / conceptsx.txt in the game folder -- skipped")
    return res


def restore_datalinks_text(game_dir: Path) -> int:
    """--uninstall: the .longevity.bak copies go back wherever the live text is ours. Returns the count restored."""
    try:
        import patch_helptext as ph
    except Exception as e:  # noqa: BLE001
        ix.warn(f"  WARNING: the Datalinks text patch is missing from this installer ({type(e).__name__}: {e}) "
                "-- its text files were not restored.")
        return 0
    res = ph.restore_folder(game_dir, say=lambda t: ix.say("  " + t) if "restored" in t or "left alone" in t else None)
    failed = [n for n, s in res.items() if s == ph.FAILED]
    if failed:
        ix.warn(f"  WARNING: could not restore {', '.join(failed)} -- close the game and run --uninstall again.")
    return sum(1 for s in res.values() if s == ph.RESTORED)


def datalinks_text_status(res: dict) -> str:
    """One summary word for the table: ok when any file found took (or already had) the text, else skip. A file
    left alone beside patched ones is named under the row (datalinks_text_note), not hidden behind a Skipped."""
    return "ok" if res and any(s in ("patched", "current") for s in res.values()) else "skip"


def datalinks_text_note(res: dict) -> str:
    """The files patch_folder left alone, for the line under the summary row; '' when none."""
    left = sorted(n for n, s in res.items() if s not in ("patched", "current"))
    return f"  ({', '.join(left)} left alone: not the stock text)" if left else ""


def deploy_runtime(game_dir: Path, quiet: bool = False) -> bool:
    """Copy uimod.dll + the slim ffmpeg DLLs into the game dir.

    `quiet`: a pack-only update re-deploys the same files; six "deployed x.dll" lines above
    "Nothing else needed changing" contradicted it."""
    if quiet:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return deploy_runtime(game_dir, quiet=False)
    ix.say("\n--- mod files ---")
    dll, ffmpeg = uimod_dll(), ffmpeg_bin()
    if not dll.exists():
        ix.say(f"  ERROR: {dll} not found -- build it first (src\\uimod\\build.bat).")
        return False
    if not ffmpeg.is_dir():
        ix.say(f"  ERROR: ffmpeg DLLs not found at {ffmpeg}.")
        return False

    dlls = sorted(ffmpeg.glob("*.dll"))
    if not dlls:
        ix.say(f"  ERROR: no DLLs in {ffmpeg}.")
        return False
    # Copy failures here are USER-side conditions (the game is running and holds uimod.dll, an
    # antivirus has the file open, the directory is read-only), not installer bugs. Letting an
    # OSError escape would route them to the crash handler, which tells the user "this is a bug
    # in the installer, not something you did wrong" -- false, and unhelpful.
    for src in [dll] + dlls:
        try:
            shutil.copy2(src, game_dir / src.name)
        except OSError as e:
            ix.bad(f"  ERROR: could not write {src.name}: {e}")
            return False
    copied = len(dlls) + 1

    # The popup picture is cosmetic: the DLL shows a text-only prompt without it. Neither a
    # payload that lacks it (a resident updater from before the picture existed) nor a copy
    # failure may strand an update -- warn and go on.
    art = popup_art()
    if not art.exists():
        ix.warn(f"  WARNING: {art.name} is not in this installer; the update prompt will show no picture.")
    else:
        try:
            shutil.copy2(art, game_dir / art.name)
            copied += 1
        except OSError as e:
            ix.warn(f"  WARNING: could not write {art.name} ({e}); the update prompt will show no picture.")
    # The replacement-portraits guide: the folder players put their own portraits in.  Cosmetic, like the
    # picture: a payload without it or a copy failure warns and goes on.  Only README.txt is ever written there
    # by the base mod -- the player's images in portraits\ are never touched.
    readme = portraits_readme()
    if readme.exists():
        try:
            (game_dir / "portraits").mkdir(parents=True, exist_ok=True)
            shutil.copy2(readme, game_dir / "portraits" / PORTRAITS_README)
            copied += 1
        except OSError as e:
            ix.warn(f"  WARNING: could not write portraits\\{PORTRAITS_README} ({e}).")
    # One line, not one per file: the names mean nothing to a player (audit 2026-09-27).
    ix.say(f"  {copied} files copied")
    return True


def set_ini_flag(game_dir: Path, enabled: bool) -> None:
    """Write [UIMod] UpscaledVideos via the Win32 profile API (preserves the file)."""
    ini = _ini_path_str(game_dir)
    k = _kernel32()
    try:
        if k is None:
            raise OSError("no Win32 profile API available")
        ok = k.WritePrivateProfileStringW("UIMod", "UpscaledVideos",
                                          "1" if enabled else "0", ini)
    except (OSError, ValueError, AttributeError) as e:
        # A non-ANSI-encodable path, or the call itself failing, must not crash a finished
        # install: everything else is already on disk and the game runs without this flag.
        print(f"  WARNING: could not write the ini flag ({e}).")
        print(f"  Set [UIMod] UpscaledVideos={1 if enabled else 0} by hand in {ini} if needed.")
        return
    if not ok:
        ix.warn(f"  WARNING: could not write [UIMod] UpscaledVideos={1 if enabled else 0} to {ini}.")


def install_movie_pack(game_dir: Path, pack: Path) -> bool:
    """Copy the prebuilt HD mp4 pack into <game>\\movies\\hd."""
    mp4s = sorted(pack.glob("*.mp4")) if pack.is_dir() else []
    if not mp4s:
        print(f"  WARNING: no .mp4 found in {pack} -- the HD pack is empty.")
        print("           Cinematics will fall back to the original .wve until the pack is built")
        print("           (work\\run-build-hd-pack.bat). Leaving UpscaledVideos on so they")
        print("           upgrade automatically once the pack is present.")
        return False
    dst = game_dir / "movies" / "hd"
    # Guarded like deploy_runtime: a read-only game dir, a full disk, or a clip held open by a
    # media indexer are USER-side conditions. Letting an OSError escape would reach the crash
    # handler, which tells the user "this is a bug in the installer" and -- worse -- this runs
    # AFTER the exe is patched, so the honest report is a partial install, not a crash.
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ix.say(f"  WARNING: could not create {dst}: {e}")
        ix.say("           The mod is installed; cinematics stay on the original .wve.")
        return False
    copied = 0
    for f in mp4s:
        try:
            shutil.copy2(f, dst / f.name)
        except OSError as e:
            ix.say(f"  WARNING: could not copy {f.name}: {e}")
            ix.say(f"           Copied {copied} of {len(mp4s)}. The mod itself is installed;"
                   " re-run to finish the pack.")
            return False
        copied += 1
    ix.say(f"  copied {copied} cinematic(s) -> {dst}")
    return True


#: The portraits-hd art's required credit (work\art\portraits-hd-PROVENANCE.txt): shown whenever the pack is installed.
PORTRAIT_ART_CREDIT = ("  Portrait art by Turovetsky -- https://www.youtube.com/@Turovetsky -- "
                       "https://x.com/turovetsky")


#: What the portraits-hd pack is made of (build_portraits_hd.py): the leader images, its manifest and the art's
#: provenance/credit.  Nothing else is copied from a local pack folder.
_PORTRAIT_PACK_GLOBS = ("*.png", "pack-manifest.json", "PROVENANCE.txt")


def prune_pack_dir(game_dir: Path, rel: str, keep: set) -> int:
    """Delete the files directly in <game>\\<rel> that the new pack no longer carries.  ONLY for a folder the pack
    owns outright (portraits\\hd: players keep their own images one level up, and the README says so); never
    recursive, never a directory, never anything outside it.  Returns how many were removed."""
    d = Path(game_dir) / rel
    removed = 0
    try:
        root = Path(game_dir).resolve()
        # never through a junction / symlink: the folder must be exactly <game>\<rel>, or nothing is touched
        if not d.is_dir() or os.path.normcase(str(d.resolve())) != os.path.normcase(str(root / Path(*rel.split("/")))):
            return 0
        entries = list(d.iterdir())
    except OSError:
        return 0
    keep_l = {k.lower() for k in keep}                    # Windows names are case-insensitive
    for p in entries:
        try:
            if p.is_file() and not p.is_symlink() and p.name.lower() not in keep_l:
                p.unlink()
                removed += 1
        except OSError as e:
            ix.say(f"  WARNING: could not remove the old {p.name}: {e}")
    return removed


def install_portrait_pack(game_dir: Path, pack: Path) -> bool:
    """Copy a local portraits-hd pack (a checkout's dist\\portraits-hd) into <game>\\portraits\\hd."""
    files = sorted({p for g in _PORTRAIT_PACK_GLOBS for p in pack.glob(g) if p.is_file()}) if pack.is_dir() else []
    if not any(p.suffix.lower() == ".png" for p in files):
        ix.say(f"  WARNING: no portraits found in {pack} -- the pack is empty (work\\scripts\\art\\build_portraits_hd.py).")
        return False
    dst = Path(game_dir) / "portraits" / "hd"
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ix.say(f"  WARNING: could not create {dst}: {e}")
        return False
    copied = 0
    for f in files:
        try:
            shutil.copy2(f, dst / f.name)
        except OSError as e:
            ix.say(f"  WARNING: could not copy {f.name}: {e}")
            ix.say(f"           Copied {copied} of {len(files)}. Re-run to finish the pack.")
            return False
        copied += 1
    pruned = prune_pack_dir(game_dir, "portraits/hd", {f.name for f in files})
    ix.say(f"  copied {copied} file(s) -> {dst}" + (f", removed {pruned} old" if pruned else ""))
    ix.say(PORTRAIT_ART_CREDIT)
    return True


def _ini_path_str(game_dir: Path) -> str:
    """Absolute path to Alpha Centauri.ini.

    RESOLVED: the Win32 profile API looks in %WINDIR% for a relative path, so a relative
    --game-dir would silently read and write C:\\Windows\\Alpha Centauri.ini.
    """
    try:
        return str(Path(game_dir).resolve() / "Alpha Centauri.ini")
    except OSError:
        return str(Path(game_dir) / "Alpha Centauri.ini")


def _kernel32():
    """kernel32, or None where there is no Win32 API to call."""
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None


#: Room for any value we store, plus a hand-edited one. The old buffer was 512 BYTES through the
#: ANSI entry points; the wide ones count characters.
_INI_BUF = 1024
#: Cannot occur in an ini value, so it distinguishes "no such key" from "present but empty".
_INI_ABSENT = "\x01<absent>"
_INI_TRUE = ("1", "true", "yes", "on")


def read_ini_str(game_dir: Path, section: str, key: str, default: str = "") -> str:
    """One profile value, as a string. Never raises.

    WIDE entry points deliberately. The ANSI ones take an mbcs-encoded path, and encoding it with
    `errors="replace"` turned an unencodable character into `?` -- a DIFFERENT, perfectly valid
    path. A player whose install lives under a Cyrillic, CJK or accented folder name therefore had
    every read and write land on a file that was not theirs, silently and with a success return.
    """
    k = _kernel32()
    if k is None:
        return default
    try:
        buf = ctypes.create_unicode_buffer(_INI_BUF)
        n = k.GetPrivateProfileStringW(section, key, default, buf, _INI_BUF,
                                       _ini_path_str(game_dir))
        return buf[:n]
    except (OSError, ValueError, AttributeError):
        return default


def write_ini_str(game_dir: Path, section: str, key: str, value: str) -> bool:
    """Write one ini value via the Win32 profile API (preserves the rest of the file)."""
    k = _kernel32()
    if k is None:
        return False
    try:
        return bool(k.WritePrivateProfileStringW(section, key, value, _ini_path_str(game_dir)))
    except (OSError, ValueError, AttributeError):
        return False


def delete_ini_key(game_dir: Path, section: str, key: str) -> bool:
    """Remove one key (a NULL value deletes it; the section and the rest of the file stay)."""
    k = _kernel32()
    if k is None:
        return False
    try:
        return bool(k.WritePrivateProfileStringW(section, key, None, _ini_path_str(game_dir)))
    except (OSError, ValueError, AttributeError):
        return False


#: Thinker's external-movie-player key. thinker.dll reads it on EVERY launch (refs\thinker\src\patch.cpp,
#: video player setup); the game and PRACX never do (neither binary contains the string). Its states:
#: absent / `<DEFAULT>` -> Thinker scans two VLC paths and uses VLC if found, else asks Yes/No at launch;
#: a path -> that external player; BLANK -> Thinker's own "default video player" = amovie_project_2 =
#: our in-process player, and Thinker writes nothing back, so blank is sticky.
THINKER_MOVIE_SECTION = "Alpha Centauri"
THINKER_MOVIE_KEY = "MoviePlayerPath"


def ensure_in_process_movies(game_dir: Path) -> None:
    """`[Alpha Centauri] MoviePlayerPath=` (blank), on every install AND update run, Thinker present or
    not, silently (user decision 2026-10-28, movie ownership option A2: every player gets the mod's
    in-process player and the HD Cinematics; an auto-detected or hand-set external player is
    overridden without a notice). Written whether or not Thinker is installed so that adding Thinker
    later cannot hand the movies to VLC on its first launch. Idempotent; never raises."""
    if read_ini_str(game_dir, THINKER_MOVIE_SECTION, THINKER_MOVIE_KEY, _INI_ABSENT) == "":
        return
    if not write_ini_str(game_dir, THINKER_MOVIE_SECTION, THINKER_MOVIE_KEY, ""):
        ix.warn(f"  WARNING: could not write [{THINKER_MOVIE_SECTION}] {THINKER_MOVIE_KEY}= to "
                f"{_ini_path_str(game_dir)}; under Thinker the cinematics may play through an external player.")


def release_in_process_movies(game_dir: Path) -> None:
    """--uninstall: delete the key when it is still the blank we wrote, so Thinker starts from "absent"
    on its next launch and re-runs its own player detection exactly as a fresh Thinker install does
    (a blank is the one value Thinker never re-initialises). A value the player changed since is left
    alone. Nothing of ours is remembered across the install (user decision 2026-10-28)."""
    if read_ini_str(game_dir, THINKER_MOVIE_SECTION, THINKER_MOVIE_KEY, _INI_ABSENT) != "":
        return
    if not delete_ini_key(game_dir, THINKER_MOVIE_SECTION, THINKER_MOVIE_KEY):
        ix.say(f"  WARNING: could not remove [{THINKER_MOVIE_SECTION}] {THINKER_MOVIE_KEY} from "
               f"{_ini_path_str(game_dir)}; delete the line by hand to restore Thinker's video player prompt.")


# ---------------------------------------------------------------- display resolution check
#
# PRACX runs the game at [PRACX] ScreenWidth x ScreenHeight. Its default, written to the ini as
# "<DEFAULT>", is the display's CURRENT mode (pracxsettings.cpp Load: EnumDisplaySettings), but a
# player who once picked a lower resolution in PRACX's dialog, or changed monitors, has explicit
# numbers that no longer match. PRACX then switches the display to that mode in fullscreen
# (pracx.cpp SetVideoMode: ChangeDisplaySettings when the sizes differ) and the display or GPU
# scales it up -- blurred, and distorted when the aspect differs; the Borderless/Windowed modes
# stretch the backbuffer the same way. The whole point of the mod is a native-resolution UI, so
# the installer notices and offers to put "<DEFAULT>" back (which follows the display from then
# on) rather than baking today's numbers in.

def current_display_mode():
    """(width, height) of the primary display's CURRENT mode in physical pixels, or None.
    EnumDisplaySettings, not GetSystemMetrics: the latter is DPI-virtualised for an unaware
    process and would report 2560x1440 for a 4K display at 150%."""
    try:
        class DEVMODEW(ctypes.Structure):
            _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_ushort),
                        ("dmDriverVersion", ctypes.c_ushort), ("dmSize", ctypes.c_ushort),
                        ("dmDriverExtra", ctypes.c_ushort), ("dmFields", ctypes.c_uint),
                        ("dmPositionX", ctypes.c_int), ("dmPositionY", ctypes.c_int),
                        ("dmDisplayOrientation", ctypes.c_uint), ("dmDisplayFixedOutput", ctypes.c_uint),
                        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                        ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
                        ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_uint),
                        ("dmPelsWidth", ctypes.c_uint), ("dmPelsHeight", ctypes.c_uint),
                        ("dmDisplayFlags", ctypes.c_uint), ("dmDisplayFrequency", ctypes.c_uint),
                        ("dmICMMethod", ctypes.c_uint), ("dmICMIntent", ctypes.c_uint),
                        ("dmMediaType", ctypes.c_uint), ("dmDitherType", ctypes.c_uint),
                        ("dmReserved1", ctypes.c_uint), ("dmReserved2", ctypes.c_uint),
                        ("dmPanningWidth", ctypes.c_uint), ("dmPanningHeight", ctypes.c_uint)]
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):   # ENUM_CURRENT_SETTINGS
            return None
        if dm.dmPelsWidth <= 0 or dm.dmPelsHeight <= 0:
            return None
        return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    except (OSError, AttributeError, ValueError):
        return None


def resolution_mismatch(ini_w: str, ini_h: str, display) -> tuple:
    """(game_w, game_h) when the ini pins an explicit resolution DIFFERENT from `display`
    (a (w, h) pair), else None. "<DEFAULT>", an absent key, or anything non-numeric means PRACX
    follows the display, so there is nothing to fix; an unknown display means nothing to compare."""
    if not display:
        return None
    try:
        w, h = int(str(ini_w).strip()), int(str(ini_h).strip())
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or (w, h) == tuple(display):
        return None
    return w, h


def resolution_check_applies(target_id) -> bool:
    """The pinned-resolution check is for every configuration that carries PRACX (its prax.dll
    companion), not for the id "pracx" alone: PRACX + Thinker has the same keys and the same
    stretched-game hazard (audit 2026-10-28)."""
    spec = _targets().get(target_id) if target_id else None
    return spec is not None and bool(spec.companion)


def check_display_resolution(game_dir: Path, target_id, no_prompt: bool = False, ask: bool = True) -> None:
    """Warn about a pinned [PRACX] resolution that differs from the display; offer to reset it.
    Never raises: it runs after the install has succeeded."""
    if not resolution_check_applies(target_id):
        return                      # only the PRACX configurations have these keys
    if read_ini_str(game_dir, "PRACX", "Disabled", "").strip() == "1":
        return                      # PRACX switched off: its resolution keys do nothing
    ini_w = read_ini_str(game_dir, "PRACX", "ScreenWidth", "")
    ini_h = read_ini_str(game_dir, "PRACX", "ScreenHeight", "")
    display = current_display_mode()
    mm = resolution_mismatch(ini_w, ini_h, display)
    if not mm:
        return
    ix.say("")
    ix.warn(f"WARNING: Alpha Centauri.ini sets the game to {mm[0]}x{mm[1]}, but your display is "
            f"{display[0]}x{display[1]}. The game would run stretched instead of at your display's resolution.")
    # Ask whenever a keyboard is attached. `no_prompt` alone is not the gate: the bootstrap's
    # worker always carries --no-prompt (for optional extras) yet runs in the console the player
    # is watching (2026-09-27: the question was skipped there and only the hint printed).
    # `ask` is False only for a first install run with the PLAYER's --no-prompt ("never ask").
    if not ix.can_ask() or not ask:
        ix.say("Set [PRACX] ScreenWidth and ScreenHeight to <DEFAULT> in Alpha Centauri.ini to fix this.")
        return
    left = ("Left your resolution as is. Your game will likely appear stretched until you fix this! "
            "Set [PRACX] ScreenWidth and ScreenHeight to <DEFAULT> in Alpha Centauri.ini.")
    try:
        ans = ix.ask("Let the installer set the game to your display's resolution? [Y/n] ", title="Game resolution").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ix.warn(left)
        return
    if ans not in ("", "y", "yes"):
        ix.warn(left)
        return
    ok = write_ini_str(game_dir, "PRACX", "ScreenWidth", "<DEFAULT>") and \
        write_ini_str(game_dir, "PRACX", "ScreenHeight", "<DEFAULT>")
    if ok:
        ix.say(f"[PRACX] ScreenWidth/ScreenHeight = <DEFAULT>: the game now follows your display ({display[0]}x{display[1]}).")
    else:
        ix.say("WARNING: could not write Alpha Centauri.ini; set [PRACX] ScreenWidth and ScreenHeight to <DEFAULT> by hand.")


#: The game's own setting (update.cpp reads [UIMod] CheckForUpdates; absent = on). The installer writes it
#: ONLY from the player's answer to the question below, never a default.
UPDATE_CHECK_KEY = "CheckForUpdates"
UPDATE_CHECK_TITLE = "Automatic updates"
#: The user's wording (2026-11-06), as the block's note lines.
UPDATE_CHECK_NOTES = (
    "When the game starts, it can check our server for a newer version of Longevity Vaccine and its",
    "optional modules, and offer to install them. You can change this any time in Longevity Mod",
    "Preferences (\"Check for Updates at Startup\").",
)
UPDATE_CHECK_PROMPT = "Check for updates when the game starts? [Y/n] "
UPDATE_CHECK_PREFS = "Longevity Mod Preferences (\"Check for Updates at Startup\")"


def update_check_state(game_dir: Path):
    """[UIMod] CheckForUpdates as the GAME reads it (update.cpp: GetPrivateProfileIntW with default
    1): None when absent; else the leading integer != 0, an empty value the default (on), a word
    ("yes") 0. ini_flag_state's rules are UpscaledVideos', which our own Python reads; for this key
    the installer must report what the game will do (audit 2026-11-06)."""
    raw = read_ini_str(game_dir, "UIMod", UPDATE_CHECK_KEY, _INI_ABSENT)
    if raw == _INI_ABSENT:
        return None
    s = raw.strip()
    if not s:
        return True
    import re
    m = re.match(r"[+-]?\d+", s)
    return bool(int(m.group(0))) if m else False


def ask_update_checks(game_dir: Path, args):
    """The opt-in for the game's update check (user 2026-11-06), asked once: on a FIRST install, at a
    console, without --no-prompt. Returns True / False for the answer (written the moment the exe is
    patched: before that every refusal says the game was not modified; after it the folder counts
    as installed and a re-run would not ask again), or None to leave the setting as it is -- a
    re-run keeps what the player has (the game's default, on, when the key was never written),
    --no-prompt and an unattended run keep the default; each says so in one line. Enter is yes.
    Ctrl+C cancels the install like the picker; a closed stdin keeps the default."""
    state = update_check_state(game_dir)
    first = state is None and not _has_install(game_dir)
    if first and ix.can_ask() and not getattr(args, "no_prompt", False):
        try:
            ans = ix.ask(UPDATE_CHECK_PROMPT, title=UPDATE_CHECK_TITLE, notes=UPDATE_CHECK_NOTES).strip().lower()
        except EOFError:
            ans = None
        if ans is not None:
            on = ans in ("", "y", "yes")
            ix.say("  The game will check for updates when it starts." if on else
                   f"  The game will not check for updates. Turn it on any time in {UPDATE_CHECK_PREFS}.")
            return on
    on = state is None or state
    ix.say("\n--- automatic updates ---")
    ix.say(f"  The game {'checks' if on else 'does not check'} for updates when it starts"
           f" ({'the default' if state is None else 'as before'}); change it in {UPDATE_CHECK_PREFS}.")
    return None


def write_update_check(game_dir: Path, on) -> None:
    """Record the answer as the game's setting; None (not asked) writes nothing."""
    if on is None:
        return
    if not write_ini_str(game_dir, "UIMod", UPDATE_CHECK_KEY, "1" if on else "0"):
        ix.warn(f"  WARNING: could not write [UIMod] {UPDATE_CHECK_KEY}={1 if on else 0} to Alpha Centauri.ini; "
                f"set it in {UPDATE_CHECK_PREFS}.")


def ini_flag_state(game_dir: Path, key: str):
    """True / False / None, where None means the key is ABSENT.

    The absent case is the one that matters: a FIRST install has no ini at all, and treating that
    as "the user chose 0" silently skipped the headline HD-cinematics feature under --no-wait.
    GetPrivateProfileInt cannot express it -- it returns the caller's default for a missing key
    and 0 for a non-numeric one, so `UpscaledVideos=yes` read back as "off" -- so this reads the
    raw string and decides for itself.

    A key that is PRESENT but unreadable (`UpscaledVideos=`, or `0 ; keep it off`, which the
    profile API hands back comment and all) resolves to False, never to None. Mapping it to
    "absent" would have made it mean "first install", and a first install defaults the HD pack
    ON -- so blanking or commenting a `0` re-copied 1.3 GB and reversed the player's choice.
    """
    raw = read_ini_str(game_dir, "UIMod", key, _INI_ABSENT)
    if raw == _INI_ABSENT:
        return None
    s = raw.strip().strip('"').lower()
    s = s.split(";")[0].split("#")[0].strip()      # trailing ini comment
    s = s.split()[0] if s.split() else ""          # `1 whatever` -> `1`
    return s in _INI_TRUE


def pack_on_disk(game_dir: Path, name: str) -> bool:
    """Is the pack's content already in the game folder (left by an uninstall, say)? The picker
    calls it "already installed", so it uses the game's own test where the game has one: the
    portrait pack counts only with its pack-manifest.json (portrait_files.cpp PackInstalled; a
    stray image or an interrupted copy in portraits\\hd is not the pack, and the game would not
    use it -- audit 2026-11-06)."""
    for rel in MODULE_ROOTS.get(name, ()):
        d = Path(game_dir) / rel
        try:
            if name == "portraits-hd":
                if (d / "pack-manifest.json").is_file():
                    return True
            elif d.is_dir() and any(p.is_file() for p in d.rglob("*")):
                return True
        except OSError:
            pass
    return False


def _dir_size(d: Path) -> int:
    try:
        return sum(p.stat().st_size for p in Path(d).rglob("*") if p.is_file())
    except OSError:
        return 0


def module_catalogue(game_dir: Path, args) -> tuple:
    """What optional modules this run could install, and how big each is.

    Returns (catalogue, manifest). catalogue: name -> {"size": bytes, "source": "local" |
    "manifest", "on_disk": bool, ["path": Path]}. "local" is a pack folder inside this payload
    (a checkout); "manifest" is a download the signed release manifest offers. manifest is
    (raw, sig, doc) when one was fetched and verified, else None.

    The manifest is fetched only when the answer can matter -- someone is at the keyboard, or
    the command line names a pack -- and with the quick single-attempt profile, so an offline
    player waits seconds, not minutes. A failure lists nothing and says so in one line: the
    packs can always be added later from the game's own update prompt.
    """
    import manifest as mfmod
    catalogue = {}
    pack = Path(args.pack) if getattr(args, "pack", None) else default_pack()
    if pack.is_dir() and any(pack.glob("*.mp4")):
        catalogue["movies-hd"] = {"size": _dir_size(pack), "source": "local", "path": pack}
    ppack = Path(args.portrait_pack) if getattr(args, "portrait_pack", None) else default_portrait_pack()
    if ppack.is_dir() and any(ppack.glob("*.png")):
        catalogue["portraits-hd"] = {"size": _dir_size(ppack), "source": "local", "path": ppack}

    interactive = args.ai == "ask" and ix.can_ask()
    need = bool(args.modules) or interactive or (args.ai == "on" and "movies-hd" not in catalogue)
    manifest = None
    if need:
        try:
            raw, sig = fetch_manifest(game_dir, args.manifest, args.offline, quick=True)
            doc = mfmod.load_verified(raw, sig, allow_env=_local_manifest(args.manifest))
            manifest = (raw, sig, doc)
            mods = doc.get("modules") if isinstance(doc.get("modules"), dict) else {}
            for name in GAME_CONTENT_MODULES:
                info = mods.get(name)
                if name in catalogue or not isinstance(info, dict):
                    continue
                size = info.get("size")
                if mfmod.size_ok(size) and size > 0 and isinstance(info.get("url"), str):
                    catalogue[name] = {"size": int(size), "source": "manifest"}
        except ix.InstallerError as e:
            ix.say(f"Optional modules could not be listed: {e.message.rstrip('.')}. "
                   "You can add them later from the game's update prompt.")
        except mfmod.ManifestError:
            ix.say("Optional modules could not be listed: the update information did not verify. "
                   "You can add them later from the game's update prompt.")
    for name in catalogue:
        catalogue[name]["on_disk"] = pack_on_disk(game_dir, name)
    return catalogue, manifest


def choose_modules(catalogue: dict, args, game_dir: Path) -> list:
    """Which optional modules to install (pick_modules without the declines)."""
    return pick_modules(catalogue, args, game_dir)[0]


def pick_modules(catalogue: dict, args, game_dir: Path) -> tuple:
    """(the optional modules to install, the ones the player turned down, the ones the player
    ticked in the list -- [] when no list was shown).

    Flags decide without asking (`--modules a,b`, `--ai on|off`, `--no-prompt`); otherwise the
    player ticks packs in a list and the installer WAITS for Confirm -- an install must not run
    past this point on its own (user, 2026-09-27). Turned down = shown in the list and left
    unticked, or `--ai off` for the HD Cinematics: the install records each as the game's
    "Don't ask me again" (user 2026-11-01). Nothing is turned down when nobody chose: an
    unattended run, `--no-prompt`, a pack already on disk.
    """
    names = [n for n in GAME_CONTENT_MODULES if n in catalogue]
    if args.modules:
        wanted = validate_module_names(args.modules, game_dir)
        missing = [n for n in wanted if n not in catalogue]
        if missing:
            shown = ", ".join(_pack_label(n) for n in missing)
            raise ix.InstallerError(
                f"{shown} {'is' if len(missing) == 1 else 'are'} not available for download "
                "right now.", code=ix.EXIT_ABORTED, changed=False,
                remedy="run the installer without --modules; the pack can be added later from "
                       "the game's update prompt.")
        return wanted, [], []
    if args.ai == "on":
        return (["movies-hd"] if "movies-hd" in catalogue else []), [], []
    if args.ai == "off":
        # Not read from the catalogue: --ai off never fetches the manifest.
        return [], ([] if pack_on_disk(game_dir, "movies-hd") else ["movies-hd"]), []
    if not names:
        return [], [], []
    import receipt as rc
    declines = rc.declined(game_dir)
    ticked = {n: _starts_ticked(n, catalogue, game_dir, declines) for n in names}
    if getattr(args, "no_prompt", False):
        # The PLAYER's --no-prompt (only --update workers get the bootstrap's): nothing asked, and
        # the list taken as it would start (user 2026-11-01) -- every module on a first install;
        # on a re-run a pack the player said no to stays out (a default here once reversed an
        # opt-out, see below).
        chosen = [n for n in names if ticked[n]]
        ix.say("Selected: " + (", ".join(_pack_label(n) for n in chosen) if chosen else "none")
               + " (--no-prompt)")
        return chosen, [], []
    if not ix.can_ask():
        # Unattended: a first install takes the headline pack, a re-run keeps the earlier choice
        # (set_ini_flag runs every time, and a hardcoded default here once reversed an opt-out).
        state = ini_flag_state(game_dir, "UpscaledVideos")
        return (["movies-hd"] if (state is None or state) and "movies-hd" in names else []), [], []

    chosen = _tick_picker(names, catalogue, ticked)
    ix.say("Selected: " + (", ".join(_pack_label(n) for n in chosen) if chosen else "none"))
    return chosen, [n for n in names if n not in chosen and not catalogue[n].get("on_disk")], chosen


def record_module_choice(game_dir: Path, wanted: list, turned_down: list) -> None:
    """A pack turned down at install time is declined exactly as the game's "Don't ask me again"
    on its offer declines it (`new:<id>:*`): hidden from the update prompt, undone by Preferences'
    "Reset Skipped Updates", void under check_report's reset rule. A pack in `wanted` -- ticked
    by the player, or installed by this run -- forgets any earlier decline; one a flag or an
    unattended run merely tried and failed to fetch keeps it. Written after the install, never
    before (a cancelled install changes nothing); a failed write only means the game asks
    again. Never raises."""
    try:
        import receipt as rc
        for n in turned_down:
            rc.decline(game_dir, NEW_PACK_PREFIX + n, rc.DECLINE_ANY)
        for n in wanted:
            rc.forget_decline(game_dir, NEW_PACK_PREFIX + n)
    except Exception:  # noqa: BLE001 - bookkeeping must not fail a finished install
        pass


def _starts_ticked(name: str, catalogue: dict, game_dir: Path, declines: dict) -> bool:
    """Ticked unless the player said no before (user 2026-11-01: everything on a first run).
    No = "Don't ask me again" on the pack's offer (in game or by an earlier unticked install), or
    the HD Cinematics switched off. A pack already on disk is always ticked."""
    if catalogue[name].get("on_disk"):
        return True
    if NEW_PACK_PREFIX + name in declines:
        return False
    return not (name == "movies-hd" and ini_flag_state(game_dir, "UpscaledVideos") is False)


#: The DeclinedUpdates item for a pack the player does not have (update_core.h writes
#: `new:<id>:*` for "Don't ask me again" on a NEW pack's offer; check_report reads it).
NEW_PACK_PREFIX = "new:"


class _KeyEventRecord(ctypes.Structure):
    _fields_ = [("bKeyDown", ctypes.c_int), ("wRepeatCount", ctypes.c_ushort),
                ("wVirtualKeyCode", ctypes.c_ushort), ("wVirtualScanCode", ctypes.c_ushort),
                ("uChar", ctypes.c_wchar), ("dwControlKeyState", ctypes.c_ulong)]


class _InputRecord(ctypes.Structure):
    class _Event(ctypes.Union):
        _fields_ = [("KeyEvent", _KeyEventRecord), ("_size", ctypes.c_byte * 16)]
    _fields_ = [("EventType", ctypes.c_ushort), ("Event", _Event)]


_K32 = None


def _poll_key():
    """The next key PRESS waiting in the console's input buffer, as (virtual-key code, character),
    or None when nothing is waiting; never blocks. Read as the console's own key records, not
    through msvcrt.getwch: getwch hands out an arrow as "\\xe0" and its second code only on the
    NEXT call, so an arrow cannot be told from a typed a-grave (2026-11-03: every arrow was read
    as one and ignored, in a plain Command Prompt). The record says VK_UP outright."""
    global _K32
    if _K32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetStdHandle.restype = ctypes.c_void_p
        k.GetStdHandle.argtypes = [ctypes.c_ulong]
        k.GetNumberOfConsoleInputEvents.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.ReadConsoleInputW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_InputRecord), ctypes.c_ulong,
                                        ctypes.POINTER(ctypes.c_ulong)]
        _K32 = k
    h = _K32.GetStdHandle(ctypes.c_ulong(-10 & 0xFFFFFFFF))      # STD_INPUT_HANDLE
    n, rec = ctypes.c_ulong(), _InputRecord()
    while _K32.GetNumberOfConsoleInputEvents(h, ctypes.byref(n)) and n.value:
        if not _K32.ReadConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(n)) or not n.value:
            return None
        e = rec.Event.KeyEvent
        if rec.EventType == 0x0001 and e.bKeyDown:            # KEY_EVENT, pressed (not released)
            return e.wVirtualKeyCode, e.uChar
    return None                                                 # mouse / focus / resize events: skipped


_VK_KEYS = {0x26: "up", 0x28: "down", 0x0D: "enter", 0x20: "space"}


def _read_key() -> str:
    """One key from the console: "up", "down", "enter", "space", or "" for any other key.
    Ctrl-C raises KeyboardInterrupt, as at every other prompt. No console on stdin raises
    EOFError: the console would be waited on for a key nobody can press (only a caller that
    skipped ix.can_ask() gets here -- a test that forgot to supply the keys)."""
    if sys.stdin is None or not sys.stdin.isatty():
        raise EOFError("no console to read a key from")
    # Poll, never block: Python delivers a console Ctrl-C between bytecodes, so a blocking read
    # would hold the cancel back until the player pressed some other key.
    while True:
        key = _poll_key()
        if key is not None:
            break
        time.sleep(0.03)
    vk, ch = key
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\x1b":
        # A console with virtual-terminal input on hands an arrow over as text, ESC [ A / ESC O A,
        # one character per record. The rest follows within milliseconds; a lone ESC (the Esc
        # key) is no key of ours.
        seq = ""
        while len(seq) < 8:
            deadline, nxt = time.monotonic() + 0.05, _poll_key()
            while nxt is None and time.monotonic() < deadline:
                time.sleep(0.005)
                nxt = _poll_key()
            if nxt is None:
                break
            seq += nxt[1]
            if len(seq) > 1 and (seq[-1].isalpha() or seq[-1] == "~"):
                break
        return {"[A": "up", "OA": "up", "[B": "down", "OB": "down"}.get(seq, "")
    if vk in _VK_KEYS:
        return _VK_KEYS[vk]
    return {"\r": "enter", "\n": "enter", " ": "space"}.get(ch, "")


def _drop_typed_ahead() -> None:
    """Forget keys pressed before the list was drawn: an extra Enter at the folder prompt, typed
    while the manifest was fetched, would otherwise untick the first pack unseen. Never raises."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            for _ in range(1000):
                if _poll_key() is None:
                    break
    except Exception:  # noqa: BLE001 - a key we could not drop only flips a box the player sees
        pass


def _picker_rows(names: list, catalogue: dict, ticked: dict, at: int) -> list:
    """The pick list as printed: a box per pack (green ticked, red not), then the row that ends
    the pick: "Install selected modules", or "Skip optional modules" with nothing ticked (user
    wording 2026-11-04)."""
    rows = []
    width = max([28] + [len(_pack_label(n)) + 1 for n in names])    # the size column lines up under every label
    for i, n in enumerate(names):
        c = catalogue[n]
        note = "   already installed" if c.get("on_disk") else ""   # user wording 2026-11-06
        box = "[X]" if ticked[n] else "[ ]"
        row = ix.painted(f"{box} {_pack_label(n):<{width}}{human_size(c['size']):>8}{note}",
                         "good" if ticked[n] else "bad")
        rows.append((" > " if i == at else "   ") + row)
    last = INSTALL_SELECTED if any(ticked[n] for n in names) else SKIP_MODULES
    rows.append((" > " if at == len(names) else "   ") + last)
    return rows + [ix.RULE]                 # the block's closing rule, redrawn with the rows


INSTALL_SELECTED = "Install selected modules"
SKIP_MODULES = "Skip optional modules"


def _tick_picker(names: list, catalogue: dict, ticked: dict) -> list:
    """Up / Down move the marker, Enter (or Space) ticks or unticks the pack under it, and Enter
    on Confirm -- the last row -- ends the pick (user 2026-11-01). The marker starts on the first
    pack, never on Confirm: a reflex Enter (every earlier prompt trains Enter as "yes") then flips
    a box the player can see, instead of committing to gigabytes (audit 2026-09-27). Ctrl-C is NOT
    an answer: it propagates to run_guarded, which cancels with nothing changed."""
    ticked = dict(ticked)
    at = 0
    rows = _picker_rows(names, catalogue, ticked, at)
    _drop_typed_ahead()
    ix.ask_block("Choose the optional modules",
                 # user wording 2026-11-04. "Later": Preferences' "Reset Skipped Updates" makes the
                 # game's update prompt offer a pack left unticked here again.
                 ["Select which optional modules you would like to install. You can download these "
                  "later from the in-game preferences menu."],
                 rows, keys="Up/Down: move   Enter: tick or untick, or install")
    while True:
        key = _read_key()
        if key == "up":
            at = max(0, at - 1)
        elif key == "down":
            at = min(len(names), at + 1)
        elif key in ("enter", "space") and at < len(names):
            ticked[names[at]] = not ticked[names[at]]
        elif key == "enter":
            return [n for n in names if ticked[n]]
        else:
            continue
        rows = ix.redraw(rows, _picker_rows(names, catalogue, ticked, at))


def cinematics_flag(chosen: list, game_dir: Path, args, offered: list = ()) -> bool:
    """[UIMod] UpscaledVideos: play the HD clips when they are present.

    On when the pack was chosen, forced (`--ai on`) or is already on disk. Off when the player
    was shown the pack (`offered`) and did not take it, or said `--ai off`. Otherwise -- the
    pack could not be listed, or nobody was asked -- a first install leaves it on (harmless
    without the clips, and they play the moment a later run adds them) and a re-run keeps the
    player's earlier choice.
    """
    if "movies-hd" in chosen or args.ai == "on" or pack_on_disk(game_dir, "movies-hd"):
        return True
    if args.ai == "off" or "movies-hd" in offered:
        return False
    state = ini_flag_state(game_dir, "UpscaledVideos")
    return True if state is None else bool(state)


def banner() -> None:
    line = "=" * 60
    print(line)
    print("   LONGEVITY VACCINE")
    print("   UI Scaling + HD Cinematics for Sid Meier's Alpha Centauri")
    print(line)
    print(f"   version {ver.VERSION}")
    print(line)
    if any(a in ("--update", "--generation") for a in sys.argv[1:]):
        # Just the header: "This installer will patch ..." on an update read as a threat, and the
        # update flow says what it is doing line by line (waiting / updating / the verdict).
        # (--generation: the bootstrap's second generation, which has no --update on its line.)
        return
    # ix.say, not print: the blank line it ends with must count for ix.blank() (one, never two).
    ix.say("This installer will patch Alpha Centauri: Alien Crossfire with Longevity Vaccine. Your "
           "original game .exe will be backed up (.bak) and can be restored at any time.\n")


#: Modules whose payload belongs in the GAME directory, and the ONLY subtrees each may write to.
#: `base` is the installer's own payload and is handled by the bootstrap/self-update path, not
#: extracted over the game. The subtrees mirror release.py's MODULE_SPEC globs -- an optional pack
#: has no business writing anywhere else, least of all over the exe the commit phase just
#: verified.
MODULE_ROOTS = {
    "movies-hd": ("movies/hd",),
    # portraits\hd, NOT portraits: the player's own images live in portraits\ (and fill in whatever the pack
    # lacks), so the pack may never write (or prune) there.
    "portraits-hd": ("portraits/hd",),
}
#: Packs whose root folder they own outright, so an update removes files the new archive no longer carries.
PRUNED_MODULES = ("portraits-hd",)
GAME_CONTENT_MODULES = tuple(MODULE_ROOTS)

#: Player-facing names, COMPILED IN. The manifest carries ids only; a label is never read from it
#: (see manifest.py's trust boundary). These are what the game shows when it asks about a pack.
MODULE_LABELS = {
    "movies-hd": "HD Cinematics",
    "portraits-hd": "Turovetsky's HD Portrait Pack",
}


def validate_module_names(spec: str, game_dir=None) -> list:
    """`--modules a,b` may only name modules this installer knows how to place.

    An unknown name used to be downloaded in full and then silently ignored by install_modules,
    which iterates GAME_CONTENT_MODULES: 1.3 GB of bandwidth for no message at all. Called by
    the bootstrap too, BEFORE it fetches anything -- otherwise a typo still cached the manifest,
    downloaded a whole new installer and swapped it in before install.py got to refuse.
    """
    table = "\n".join(f"    {n:<14} {MODULE_LABELS.get(n, n)}" for n in GAME_CONTENT_MODULES)
    heading = "  Extras this installer knows (some may not be released yet):\n"
    example = (f'"{launcher_path(game_dir)}" --update --modules {GAME_CONTENT_MODULES[0]}'
               if game_dir else f"--update --modules {GAME_CONTENT_MODULES[0]}")
    remedy = "run it again with one of the names above, for example:\n    " + example
    raw = [m.strip() for m in str(spec).split(",") if m.strip()]
    if not raw:
        raise ix.InstallerError(
            "--modules was given without saying which extra.", code=ix.EXIT_ABORTED,
            changed=False, detail=heading + table, remedy=remedy)
    # The short id, or the label a player will have read in the extras notice -- either way,
    # case does not matter.
    by_key = {n.lower(): n for n in GAME_CONTENT_MODULES}
    by_key.update({MODULE_LABELS[n].lower(): n for n in GAME_CONTENT_MODULES if n in MODULE_LABELS})
    names, bad = [], []
    for m in raw:
        hit = by_key.get(m.lower())
        (names if hit else bad).append(hit or m)
    if bad:
        shown = ", ".join('"' + "".join(ch for ch in b if 32 <= ord(ch) < 127)[:40] + '"'
                          for b in bad)
        raise ix.InstallerError(
            f"{shown} is not an extra this installer knows.",
            code=ix.EXIT_ABORTED, changed=False, detail=heading + table, remedy=remedy)
    return list(dict.fromkeys(names))


def human_size(n) -> str:
    import fetch as fetchmod
    return fetchmod.human_size(n)


def _pack_label(name: str) -> str:
    return MODULE_LABELS.get(name, name)


def launcher_path(game_dir) -> Path:
    """The run-install.bat a player can actually paste: the resident copy once installed, else
    this payload's own. A bare `run-install.bat` only works from the right folder in cmd."""
    try:
        import bootstrap as bsmod
        p = bsmod.updater_dir(Path(game_dir)) / "scripts" / "run-install.bat"
        if p.is_file():
            return p
        return payload_root() / "scripts" / "run-install.bat"
    except Exception:  # noqa: BLE001 - a path in a notice must not become a failure
        return Path("run-install.bat")


def install_modules(game_dir: Path, staged: dict, versions: dict) -> dict:
    """Extract verified module archives into the game dir, skipping unchanged files.

    The archive as a whole was already sha256-verified against the SIGNED manifest, so integrity
    is settled; per-file CRC32 from the zip directory is then enough to decide what to skip, and
    it costs nothing to read. That is what turns "recopy 1.3 GB every run" into "copy what
    changed".

    Each module is isolated: one that fails does not abort the others, and does not roll back the
    core update that already succeeded.
    """
    import zipfile
    out = {}
    for name in GAME_CONTENT_MODULES:
        archive = staged.get(name)
        if not archive:
            continue
        ix.say(f"\n--- {_pack_label(name)} ---")
        copied = skipped = failed = 0
        files = []
        try:
            with zipfile.ZipFile(archive) as z:
                root = Path(game_dir).resolve()
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    dest = _contained(root, info.filename, MODULE_ROOTS.get(name, ()))
                    if dest is None:
                        # ZIP-SLIP. zipfile.extract() sanitises member names; z.open() plus a
                        # manual join does not. `game/"../../x"` escapes, and an ABSOLUTE member
                        # discards the game dir entirely -- so a member could land on another
                        # drive, or overwrite the exe we just committed. The archive is
                        # sha256-verified against a signed manifest, so this needs the signing
                        # key to weaponise, but the same bug fires on an ordinary packaging
                        # mistake, which is the likelier way to meet it.
                        failed += 1
                        ix.say(f"  REFUSED: {info.filename!r} is not somewhere {name} may write "
                               f"(allowed: {', '.join(MODULE_ROOTS.get(name, ())) or 'nothing'})")
                        continue
                    files.append(info.filename)
                    tmp = None
                    try:
                        if dest.is_file() and dest.stat().st_size == info.file_size \
                                and _crc32(dest) == info.CRC:
                            skipped += 1
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # A PRIVATE temp name, not `<dest>.part`. Two things went wrong with the
                        # obvious form: an archive carrying both `x.mp4` and `x.mp4.part` had one
                        # member destroy the other, and a player who already had a file called
                        # `x.mp4.part` (an interrupted download of their own) lost it. `.tmp-<pid>`
                        # is ours, is not a name anything else writes, and is swept below.
                        tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
                        with z.open(info) as src, tmp.open("wb") as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
                        tmp.replace(dest)     # rename, so an interrupted copy is never mistaken
                        tmp = None            # for a complete file
                        copied += 1
                    except (OSError, zipfile.BadZipFile) as e:
                        failed += 1
                        ix.say(f"  WARNING: {info.filename}: {e}")
                    finally:
                        # Leave NOTHING behind. A half-written temp used to survive every failure
                        # -- so a player who ran out of disk part-way through the 1.3 GB pack was
                        # left with the wasted space still occupied, and re-running made another.
                        if tmp is not None:
                            try:
                                tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
        except (zipfile.BadZipFile, OSError) as e:
            ix.say(f"  WARNING: {name} could not be read ({e}) -- skipping this module.")
            continue
        ix.say(f"  {copied} file{'s' if copied != 1 else ''} copied, {skipped} already current"
               + (f", {failed} FAILED" if failed else ""))
        if failed:
            ix.say(f"  {name} is incomplete; re-run the installer to finish it.")
            continue
        if name in PRUNED_MODULES:
            for rel in MODULE_ROOTS.get(name, ()):
                keep = {Path(m).name for m in files if Path(m).parent.as_posix().lower() == rel.lower()}
                pruned = prune_pack_dir(game_dir, rel, keep)
                if pruned:
                    ix.say(f"  removed {pruned} file{'s' if pruned != 1 else ''} the new pack no longer carries")
        if name == "portraits-hd":
            ix.say(PORTRAIT_ART_CREDIT)
        out[name] = {"version": versions.get(name, ver.UNKNOWN), "files": files}
    return out


#: Windows device names. Reserved at every directory level, with or without an extension.
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}


def _contained(root: Path, member: str, allowed: tuple = ()):
    """Resolve an archive member under `root`, or None if it may not be written.

    Two separate refusals, because containment alone is not enough:

      * ESCAPE -- `..` traversal, absolute paths, drive-qualified paths, and anything that
        resolves outside the tree. Also a name whose final component is empty or `.`, which on
        Windows turns into a file where a directory belongs.
      * OUT OF SCOPE -- a member that is contained but lands somewhere the module has no business
        writing. `movies-hd` ships `movies/hd/*`; a member called `terranx_PRACX.exe` is perfectly
        contained and would overwrite the executable the commit phase JUST verified byte for byte
        -- as would `uimod.dll`, `terranx_PRACX.exe.bak`, or `Alpha Centauri.ini`. Each optional
        module is confined to the subtree it is packaged from (release.py's MODULE_SPEC globs).
    """
    name = str(member).replace("\\", "/").strip()
    if not name or name.endswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    for p in parts:
        # Windows STRIPS trailing dots and spaces when it opens a path, so `".. "` and `"...."`
        # are `".."` to the filesystem while comparing unequal in Python. The open itself happens
        # to fail here, so nothing escaped -- but the mkdir above it still created undeletable
        # junk directories, and relying on an incidental open failure for a containment property
        # is not a guarantee.
        if p != p.rstrip(" ."):
            return None
        # Reserved DEVICE names. `NUL` can never be os.replace()d, so a module containing one
        # would report "incomplete; re-run the installer" forever and never record its version.
        if p.split(".")[0].upper() in _RESERVED_NAMES:
            return None
    if Path(name).is_absolute() or Path(name).drive or name.startswith("/"):
        return None
    if allowed:
        rel = "/".join(parts).lower()
        if not any(rel.startswith(a.lower().rstrip("/") + "/") for a in allowed):
            return None
    dest = (root / Path(*parts))
    try:
        resolved = dest.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    if allowed and not any(_inside_subtree(root, a, resolved) for a in allowed):
        # A junction or symlink at (or above) the module's subtree -- `portraits\hd` pointing at `portraits\` or the
        # game root -- would land a contained-looking member on the player's files or the exe.
        return None
    return dest


def _inside_subtree(root: Path, rel: str, resolved: Path) -> bool:
    """`resolved` lies under <root>/<rel>, and <root>/<rel> itself is where it says it is (no reparse point on the
    way: its resolved path equals the lexical one, case-insensitively as on Windows)."""
    lexical = Path(root) / Path(*[p for p in rel.split("/") if p])
    try:
        real = lexical.resolve()
    except OSError:
        return False
    if os.path.normcase(str(real)) != os.path.normcase(str(lexical)):
        return False
    r, x = os.path.normcase(str(resolved)), os.path.normcase(str(real))
    return r.startswith(x.rstrip("\\/") + os.sep)


def _crc32(p: Path, _buf: int = 1 << 20) -> int:
    import zlib
    crc = 0
    with Path(p).open("rb") as f:
        for chunk in iter(lambda: f.read(_buf), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _local_manifest(source) -> bool:
    """True when --manifest names a local path rather than a URL.

    Anything that is not http(s) is treated as a path, which is what makes the error message for
    a typo'd path say "that file does not exist" instead of "refusing to fetch a schemeless URL".
    """
    return bool(source) and not str(source).lower().startswith(("http://", "https://"))


def _looks_like_url(source) -> bool:
    """A scheme-ish argument that is not http(s) -- a typo, most likely."""
    s = str(source or "")
    return "://" in s and not s.lower().startswith(("http://", "https://"))


def _resolve_local_manifest(src) -> Path:
    """The manifest file behind a local --manifest argument, or an explained abort.

    Accepts a DIRECTORY too, because `--manifest <the folder release.py wrote>` is the obvious
    thing to type; it used to fall through to the downloader and report "refusing to fetch a
    schemeless URL: C:\\...\\dist", which names neither the real problem nor the fix.
    """
    if _looks_like_url(src):
        raise ix.InstallerError(
            f"{src} is not a web address this installer can use.",
            code=ix.EXIT_ABORTED, changed=False,
            remedy="update manifests are fetched over https -- check the address, or leave "
                   "--manifest off to use the normal update service.")
    p = Path(src)
    if p.is_dir():
        inner = p / "manifest.json"
        if inner.is_file():
            return inner
        raise ix.InstallerError(
            f"{p} is a folder with no manifest.json in it.",
            code=ix.EXIT_ABORTED, changed=False,
            remedy="point --manifest at the manifest.json file itself.")
    if p.is_file():
        return p
    raise ix.InstallerError(
        f"There is no update manifest at {p}.",
        code=ix.EXIT_ABORTED, changed=False,
        remedy="check the path, or leave --manifest off to use the normal update service.")


#: A manifest is a few KB; anything beyond this is not one.
MANIFEST_MAX_BYTES = 1 << 20


def fetch_manifest(game_dir: Path, source: str = None, offline: bool = False,
                   quick: bool = False):
    """Return (raw_bytes, signature_hex). `source` may be a URL or a local path.

    `quick`: one attempt with a short timeout -- for `--check`, whose caller (the game) kills it
    after ~10 s anyway; the default 3 attempts x 30 s x two files took minutes on a black-holed
    network."""
    import fetch as fetchmod
    import manifest as mfmod
    try:
        staging = fetchmod.staging_dir(game_dir)
        fetchmod.assert_writable(staging)
    except OSError as e:
        if not quick:
            raise ix.InstallerError(
                "The game folder is not writable, so nothing can be downloaded into it.",
                code=ix.EXIT_ABORTED, changed=False, detail=f"  {e}",
                remedy="run the installer as administrator, or move the game out of Program "
                       "Files.")
        # The CHECK must keep working: cache the manifest where this user can always write.
        staging = fetchmod.fallback_cache_dir(game_dir)
        print(f"--check: {game_dir} is not writable; caching the manifest under {staging}",
              file=sys.stderr)
    m_dest, s_dest = staging / "manifest.json", staging / "manifest.json.sig"

    # An explicit LOCAL --manifest satisfies --offline: reading a file off the player's own disk
    # is not network access. Refusing it produced a message that contradicted itself -- "pass
    # --manifest <path>" said to someone who had just passed exactly that.
    if offline and _local_manifest(source):
        offline = False

    if offline:
        if not (m_dest.is_file() and s_dest.is_file()):
            raise ix.InstallerError(
                "--offline was given but there is no manifest in staging to use.",
                code=ix.EXIT_ABORTED, changed=False,
                remedy="run once without --offline, or pass --manifest <path>.")
    else:
        src = source or mfmod.MANIFEST_URL
        local = _resolve_local_manifest(src) if _local_manifest(source) else None
        try:
            if local is not None:                   # a local path, for testing
                shutil.copy2(local, m_dest)
                sig_src = Path(str(local) + ".sig")
                if not sig_src.is_file():
                    # The manifest is ALREADY in staging by this point, and --offline prefers
                    # whatever is cached there. Leaving an unverified one behind is exactly
                    # what discard_staged_manifest exists to prevent.
                    discard_staged_manifest(game_dir)
                    raise ix.InstallerError(
                        f"no signature beside {local.name}.",
                        code=ix.EXIT_ABORTED, changed=False,
                        remedy=f"put {sig_src.name} next to it.")
                shutil.copy2(sig_src, s_dest)
            else:
                sig_url = source + ".sig" if source else mfmod.SIGNATURE_URL
                # A manifest is a few KB. Without a cap the CDN (or any host it redirected
                # to) could stream 4 GiB into staging on every launch, or trickle bytes to
                # hold the hidden check process open forever.
                kw = {"max_bytes": MANIFEST_MAX_BYTES}
                if quick:
                    import time as _t
                    kw.update(timeout=8, max_attempts=1, deadline=_t.monotonic() + 20)
                # Never RESUME a manifest. A 1 MiB `.part` left by one oversized response made
                # every later hidden check send `Range: bytes=1048576-`, get 416/206 from the CDN
                # and fail -- update checks silently dead for good (offline audit 2026-09-27). In
                # a full run a `.part` of an OLDER manifest resumed into the new one fails the
                # signature once for no reason. A manifest is a few KB: always start clean.
                for p in (m_dest, s_dest):
                    p.with_suffix(p.suffix + ".part").unlink(missing_ok=True)
                fetchmod.download(src, m_dest, label="manifest", **kw)
                fetchmod.download(sig_url, s_dest, label="signature", **kw)
        except fetchmod.FetchError as e:
            # Whatever landed (a manifest without its .sig, a .part) is unverified: never leave it
            # for --offline or the next check to trip over.
            discard_staged_manifest(game_dir)
            # The technical reason goes to stderr for a developer reading a log; the player gets
            # one plain sentence and one thing to do (the URL/errno soup read as a fault of theirs).
            if quick:
                print(f"  ({e})", file=sys.stderr)
            # The reason travels with the error in BOTH paths: a bug report from an interactive update
            # used to carry only "check your connection" (a certificate problem on an old Windows
            # looks exactly like no connection to the player).
            raise ix.InstallerError(
                "Could not reach the update server.",
                code=ix.EXIT_ABORTED, title="UPDATE FAILED", detail=f"  ({fetchmod.clean(e)})",
                disk_note="Check your connection and try again later. If it keeps happening, the message above "
                          "says why (a certificate problem means Windows is missing the server's certificate authority).")
        except OSError as e:
            discard_staged_manifest(game_dir)
            raise ix.InstallerError("Could not read the update information.", code=ix.EXIT_ABORTED,
                                    title="UPDATE FAILED", detail=f"  {e}",
                                    disk_note="Check your connection and try again later.")
    try:
        # Inside the try: a non-ASCII .sig raised UnicodeDecodeError out of here into the CRASH
        # handler ("this is a bug in the installer"), and -- because the bad pair stays cached in
        # staging -- every subsequent --offline run crashed the same way, permanently.
        return m_dest.read_bytes(), s_dest.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as e:
        discard_staged_manifest(game_dir)
        raise ix.InstallerError(
            "The update information could not be read.",
            code=ix.EXIT_ABORTED, detail=f"  {e}", changed=False,
            remedy="try again; if it keeps happening, download the installer by hand.")


def discard_staged_manifest(game_dir: Path) -> None:
    """Remove an unverified manifest from staging. Never raises.

    An unverified manifest must not persist in the game folder: it is attacker-supplied by
    definition, --offline prefers whatever is cached, and leaving it there made the installer
    tell the user "Nothing on disk was changed" while a tampered file sat in their install.
    """
    try:
        import fetch as fetchmod
        places = []
        try:
            places.append(fetchmod.staging_dir(game_dir))
        except OSError:
            pass
        places.append(fetchmod.fallback_cache_dir(game_dir))   # --check's read-only fallback
        # The .part files too: an abandoned partial was resumed by the next run with a Range
        # request, so a refused download was never really discarded.
        for staging in places:
            for name in ("manifest.json", "manifest.json.sig",
                         "manifest.json.part", "manifest.json.sig.part"):
                (staging / name).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


#: How long an update launched FROM the game waits for the game to finish exiting. The exe and
#: every DLL are held open until it does; starting the preflight a second early produced a
#: "terranx_PRACX.exe is in use" refusal for the player who had just clicked "update".
GAME_EXIT_WAIT = 90

#: The last line of every success verdict (printed by print_play_note; its own line on purpose).
ENJOY_LINE = "Open the game and enjoy playing Alpha Centauri!"

# ---------------------------------------------------------------- which exe to run + the desktop shortcut
#
# A player who installs over GOG's copy has several executables and a Play button that all start
# the game WITHOUT the mod. Every success verdict therefore ends by naming the one to run, and the
# installer offers a desktop shortcut to it when none exists (user, 2026-09-27).

#: A shortcut's name IS its file name, and Windows forbids ':' in file names -- "SMACX: Longevity"
#: (the first choice) cannot exist on a desktop. Also < > " / \ | ? *.
MOD_SHORTCUT_NAME = "SMACX Longevity"
_SHORTCUT_NAME = None          # the shortcut the verdict may name (found or just created)
_NOTE_ARGS = None              # (game_dir, target_id) once an install/update succeeded


def mod_exe(game_dir: Path, target_id) -> Path:
    """The program the player starts to play with the mod: the target's launcher when it has one
    (thinker.exe injects Thinker into terranx.exe; starting terranx.exe itself would skip Thinker),
    else its own exe, else the first configured candidate that exists. None when nothing is there."""
    names = []
    spec = _targets().get(target_id) if target_id else None
    if spec is not None and getattr(spec, "launcher", None):
        names.append(spec.launcher)
    if spec is not None and getattr(spec, "exe", None):
        names.append(spec.exe)
    names += _exe_names()
    for n in names:
        if n and (Path(game_dir) / n).is_file():
            return Path(game_dir) / n
    return None


def _desktop_dirs() -> list:
    """The player's Desktop, then the Public Desktop. LONGEVITY_DESKTOP_DIRS (os.pathsep-joined)
    overrides both -- tests use it so nothing ever touches a real desktop."""
    env = os.environ.get("LONGEVITY_DESKTOP_DIRS")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    # SHGetFolderPathW (XP-era), not PowerShell: a stock Windows 7 has PowerShell 2.0 on .NET 3.5, where
    # [Environment]::GetFolderPath('CommonDesktopDirectory') does not exist (.NET 4) -- the whole snippet failed,
    # the list came back empty, and every run asked for a shortcut and then said "could not find your desktop".
    out = []
    try:
        shell32 = ctypes.windll.shell32
        for csidl in (0x0010, 0x0019):            # CSIDL_DESKTOPDIRECTORY, CSIDL_COMMON_DESKTOPDIRECTORY
            buf = ctypes.create_unicode_buffer(260)
            if shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                out.append(Path(buf.value))
    except (AttributeError, OSError):
        pass
    return out


def running_under_wine() -> bool:
    """True inside Wine (Linux / macOS: plain Wine, Lutris, Heroic, Bottles, Proton, CrossOver): its ntdll
    exports wine_get_version, Windows' never does. LONGEVITY_ASSUME_WINE=1|0 overrides, for tests."""
    forced = os.environ.get("LONGEVITY_ASSUME_WINE")
    if forced in ("0", "1"):
        return forced == "1"
    try:
        return hasattr(ctypes.windll.ntdll, "wine_get_version")
    except (AttributeError, OSError):
        return False


def _ps(script: str) -> str:
    """Run a PowerShell snippet hidden; its stdout, or "" on any failure."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", script], capture_output=True, text=True, timeout=60,
                           creationflags=flags, stdin=subprocess.DEVNULL)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _ps_quote(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def list_shortcuts() -> list:
    """[(name, target)] for every .lnk on the desktop dirs (name without .lnk)."""
    dirs = _desktop_dirs()
    if not dirs:
        return []
    script = ("$sh = New-Object -ComObject WScript.Shell; foreach ($d in @(" +
              ", ".join(_ps_quote(d) for d in dirs) + ")) { Get-ChildItem -LiteralPath $d -Filter *.lnk "
              "-ErrorAction SilentlyContinue | ForEach-Object { $s = $sh.CreateShortcut($_.FullName); "
              "Write-Output ($_.BaseName + '|' + $s.TargetPath) } }")
    out = []
    for line in _ps(script).splitlines():
        name, sep, target = line.partition("|")
        if sep:
            out.append((name, target.strip()))
    return out


def find_shortcut_to(exe: Path):
    """The name of a desktop shortcut whose target is `exe`, else None."""
    want = str(Path(exe)).lower()
    for name, target in list_shortcuts():
        if target.lower() == want:
            return name
    return None


def shortcut_icon(game_dir: Path, target_id, exe: Path) -> Path:
    """The file whose icon the desktop shortcut shows: the GAME's exe, never a launcher's. A Thinker
    folder's shortcut starts thinker.exe, whose icon is a plain "Mod" square (user 2026-11-06: use
    the game's own art); the exe it launches (the target's `exe`, terranx.exe) carries the game's
    icon, as every PRACX build does. The target itself when it is the game exe or that file is gone."""
    spec = _targets().get(target_id) if target_id else None
    if spec is not None and getattr(spec, "launcher", None) and getattr(spec, "exe", None):
        game_exe = Path(game_dir) / spec.exe
        if game_exe.is_file():
            return game_exe
    return Path(exe)


def refresh_shortcut_icon(name: str, exe: Path, icon: Path) -> bool:
    """An existing shortcut of OURS (MOD_SHORTCUT_NAME) keeps the icon it was made with; when the
    icon rule changes (2026-11-06: the game's, not the launcher's) it is brought up to date, quietly,
    provided it still starts `exe`. Never a shortcut the player made (another name). True when
    changed. Never raises."""
    want = str(icon) + ",0"
    try:
        for d in _desktop_dirs():
            lnk = d / (name + ".lnk")
            if not lnk.is_file():
                continue
            out = _ps("$sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut(" + _ps_quote(lnk) + "); "
                      "if ($s.TargetPath -eq " + _ps_quote(exe) + " -and $s.IconLocation -ne " + _ps_quote(want) + ") "
                      "{ $s.IconLocation = " + _ps_quote(want) + "; $s.Save(); Write-Output 'refreshed' } "
                      "else { Write-Output 'kept' }")
            return "refreshed" in out
    except Exception:  # noqa: BLE001 - cosmetic; never fails an install
        pass
    return False


def create_shortcut(lnk: Path, exe: Path, icon: Path = None) -> str:
    """Write `lnk` starting `exe` from its own folder, with `icon`'s icon (default: `exe`'s own).
    "" on success, else why."""
    icon = Path(icon) if icon is not None else Path(exe)
    script = ("$sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut(" + _ps_quote(lnk) + "); "
              "$s.TargetPath = " + _ps_quote(exe) + "; $s.WorkingDirectory = " + _ps_quote(Path(exe).parent) + "; "
              "$s.IconLocation = " + _ps_quote(str(icon) + ",0") + "; $s.Save(); Write-Output 'saved'")
    out = _ps(script)
    if "saved" in out and Path(lnk).is_file():
        return ""
    return "the shortcut could not be written to the desktop"


def check_desktop_shortcut(game_dir: Path, target_id, ask: bool = True) -> None:
    """Stop and offer a desktop shortcut to the mod's exe when the player has none. Never raises."""
    global _SHORTCUT_NAME
    try:
        exe = mod_exe(game_dir, target_id)
        if exe is None:
            return
        if running_under_wine():
            # The .lnk is written through PowerShell, which Wine does not have, and a Windows desktop inside a
            # prefix is not where a Linux or Mac player starts games. INSTALL.sh --desktop-entry makes a menu entry.
            ix.say("")
            ix.say(f"Running under Wine: no Windows desktop shortcut. Start {exe.name} from your game launcher "
                   "(Lutris, Heroic, Bottles, CrossOver), or run INSTALL.sh --desktop-entry for a menu entry.")
            return
        found = find_shortcut_to(exe)
        if found:
            _SHORTCUT_NAME = found
            if found == MOD_SHORTCUT_NAME:
                refresh_shortcut_icon(found, exe, shortcut_icon(game_dir, target_id, exe))
            return
        ix.say("")
        ix.say("You do not have a desktop shortcut that starts the game with the mod.")
        if not ix.can_ask() or not ask:
            ix.say(f"Run the installer again to create one, or start {exe.name} from the game folder.")
            return
        try:
            ans = ix.ask("Create a desktop shortcut that starts the game with the Longevity Vaccine mod? [Y/n] ",
                         title="Desktop shortcut").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("", "y", "yes"):
            ix.say(f"No shortcut created. Start {exe.name} from the game folder to play with the mod.")
            return
        dirs = _desktop_dirs()
        if not dirs:
            ix.warn(f"Could not find your desktop folder. Start {exe.name} from the game folder to play with the mod.")
            return
        lnk = dirs[0] / (MOD_SHORTCUT_NAME + ".lnk")
        if lnk.exists():
            ix.say(f'A desktop shortcut named "{MOD_SHORTCUT_NAME}" already exists but starts something else.')
            try:
                ans = ix.ask("Replace it? [Y/n] ", title="Desktop shortcut").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in ("", "y", "yes"):
                ix.say(f"Left as it is. Start {exe.name} from the game folder to play with the mod.")
                return
        why = create_shortcut(lnk, exe, shortcut_icon(game_dir, target_id, exe))
        if why:
            ix.warn(f"Could not create the shortcut ({why}). Start {exe.name} from the game folder to play with the mod.")
            return
        _SHORTCUT_NAME = MOD_SHORTCUT_NAME
        ix.say(f'Created "{MOD_SHORTCUT_NAME}" on your desktop.')
    except Exception as e:  # noqa: BLE001 - never fail a finished install over a shortcut
        ix.say(f"(desktop shortcut check skipped: {e})")


def print_play_note(game_dir: Path, target_id) -> None:
    """The closing lines of every success verdict: which exe carries the mod, and the shortcut."""
    exe = mod_exe(game_dir, target_id)
    if exe is None:
        ix.good(ENJOY_LINE)
        return
    spec = _targets().get(target_id) if target_id else None
    if spec is not None and getattr(spec, "launcher", None) and exe.name.lower() == spec.launcher.lower():
        # A launcher configuration (Thinker): the patched exe IS terranx.exe, and the launcher
        # is what adds Thinker on top. "terranx.exe starts the unmodified game" would be wrong
        # here on both counts.
        ix.good(f"To play with the mod, start {exe.name} as before.")
        # GOG's own entries never start Thinker: its Play button starts terran.exe (SMAC, no mod),
        # "SMAX PRACX" terranx_PRACX.exe (no mod), "Alien Crossfire" terranx.exe -- here the mod
        # without Thinker (audit 2026-11-01, read from goggame-*.info and GOG's shortcuts).
        ix.warn(f"The GOG Galaxy Play button and the shortcuts GOG created do not start Thinker: some start the game without the mod, and starting {spec.exe} directly plays with the mod but without Thinker.")
    elif target_id == "pracx":
        ix.good(f"To play with the mod, start {exe.name}.")
        # Name only the stock exes that are NOT the one carrying the mod: a PRACX exe named
        # terranx.exe (Thinker's naming, or Thinker removed again) must not be called unmodified
        # in the next breath (audit 2026-10-29).
        others = [n for n in ("terranx.exe", "terran.exe") if n.lower() != exe.name.lower()]
        ix.warn("The GOG Galaxy Play button, the shortcuts GOG created" +
                ("".join(f", {n}" for n in others) if others else "") +
                (" all" if others else "") + " start the unmodified game, without the mod.")
    else:
        ix.good(f"To play with the mod, start {exe.name}.")
        ix.warn("The GOG Galaxy Play button and the shortcuts GOG created may start a different "
                "executable, without the mod.")
    name = _SHORTCUT_NAME or find_shortcut_to(exe)
    if name:
        ix.say(f'Your desktop shortcut "{name}" starts the right one.')
    ix.good(ENJOY_LINE)

#: --check's wall deadline (seconds), enforced by a watchdog thread in check_main. Above the
#: fetch's 8 s socket / 20 s read deadline, below the game's 30 s kill (update.cpp kDeadlineMs).
CHECK_WALL_SECONDS = 24


# ---- one installer at a time -----------------------------------------------------------------
# Two installers on one game folder interleave their writes: an unfinished update is completed by
# the next run, so a generation 2 still installing a pack and an update the player accepts from
# the game meanwhile used to run side by side over the same exe, staging folder and receipt.
#
# A NAMED MUTEX, not a lock file. The OS releases it when the holder dies however it dies (a
# crash, a closed console, Task Manager) -- the next installer then sees it ABANDONED and simply
# takes it, so nothing stale can block for ever. It lives in no folder an uninstall deletes, and
# it is named after the game folder's on-disk identity (volume serial + file index), so a
# junction, a subst drive or a different spelling of the same folder still finds the same lock.
#
# A process the holder runs and WAITS ON (the bootstrap's install.py worker) cannot own the same
# mutex -- ownership is per thread -- so the holder lends it: it also holds a second, private
# mutex named by a random token and passes the token in INSTALL_LOCK_ENV. A child that finds the
# lock taken proceeds only if that private mutex still exists, i.e. only while the holder that
# issued the token is alive -- and it queues a thread on the real lock at once, so if the holder
# is killed while the child still works, the abandoned lock passes to the CHILD, never to a new
# installer. The token is consumed on use, so nothing the child starts inherits it.
INSTALL_LOCK_ENV = "LONGEVITY_INSTALL_LOCK"
#: Everyone may wait on and release the lock, at a LOW integrity label: an installer elevated by
#: UAC and the game's own (medium) updater must see one lock, not "access denied" for each other.
_LOCK_SDDL = "D:(A;;0x00100001;;;WD)S:(ML;;NW;;;LW)"
_LOCK_ACCESS = 0x00100001             # SYNCHRONIZE | MUTEX_MODIFY_STATE
_HELD_LOCKS: dict = {}
_WAIT_TIMEOUT = 0x102


class InstallerLock:
    def __init__(self, name: str, handle=None, lent=None, delegated: bool = False, heir=None):
        self.name, self._handle, self._lent, self.delegated = name, handle, lent, delegated
        self._heir = heir                 # (thread, stop event) of a delegated child

    def child_env(self, env: dict) -> dict:
        """`env` for a child this process will WAIT for, carrying the lent lock."""
        env = dict(env)
        if self._lent:
            env[INSTALL_LOCK_ENV] = self._lent[0]
        return env

    def release(self) -> None:
        if _HELD_LOCKS.get(self.name) is not self:
            return
        del _HELD_LOCKS[self.name]
        k32 = _lock_kernel32()
        if self._heir:
            k32.SetEvent(self._heir[1])   # the heir thread releases what it may have inherited
            self._heir[0].join(timeout=5)
            if not self._heir[0].is_alive():
                k32.CloseHandle(self._heir[1])
        for h in (self._handle, self._lent[1] if self._lent else None):
            if h:
                k32.ReleaseMutex(h)
                k32.CloseHandle(h)
        self._handle = self._lent = self._heir = None


def _lock_kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes as wt
    k32.CreateMutexExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.DWORD]
    k32.CreateMutexExW.restype = wt.HANDLE
    k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    k32.OpenMutexW.restype = wt.HANDLE
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD]
    k32.WaitForMultipleObjects.restype = wt.DWORD
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    k32.CreateEventW.restype = wt.HANDLE
    k32.SetEvent.argtypes = [wt.HANDLE]
    k32.ReleaseMutex.argtypes = [wt.HANDLE]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    return k32


def installer_lock_name(game_dir: Path) -> str:
    """The lock's name for this game folder: its identity on disk, not its spelling."""
    try:
        st = os.stat(game_dir)
        if st.st_ino:
            return f"Global\\LongevityVaccineInstaller-{st.st_dev:x}-{st.st_ino:x}"
    except OSError:
        pass
    # A filesystem with no file index (some network shares): the resolved, case-folded path.
    try:
        key = str(Path(game_dir).resolve())
    except (OSError, ValueError):
        key = os.path.abspath(str(game_dir))
    return ("Global\\LongevityVaccineInstaller-"
            + hashlib.sha256(os.path.normcase(key).casefold().encode("utf-8")).hexdigest()[:32])


def _lock_attributes(k32):
    """A SECURITY_ATTRIBUTES for _LOCK_SDDL, or None (the default) if it cannot be built."""
    from ctypes import wintypes as wt

    class SA(ctypes.Structure):
        _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wt.BOOL)]
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        conv = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
        conv.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        conv.restype = wt.BOOL
        sd = ctypes.c_void_p()
        if not conv(_LOCK_SDDL, 1, ctypes.byref(sd), None):
            return None, None
    except (OSError, AttributeError):
        return None, None
    return SA(ctypes.sizeof(SA), sd, False), sd


def _open_mutex(k32, name: str):
    """A handle to the named mutex (created if absent), or None if a FOREIGN object squats the
    name and refuses us (ERROR_ACCESS_DENIED: ours always grants _LOCK_ACCESS)."""
    sa, sd = _lock_attributes(k32)
    try:
        # Only the rights _LOCK_SDDL grants. CreateMutexW asks for MUTEX_ALL_ACCESS, which that
        # descriptor refuses on an EXISTING mutex -- every contender then saw "access denied"
        # and never waited at all.
        h = k32.CreateMutexExW(ctypes.byref(sa) if sa else None, name, 0, _LOCK_ACCESS)
        err = ctypes.get_last_error()
    finally:
        if sd:
            k32.LocalFree(sd)
    if not h:
        if err == 5:
            return None
        raise OSError(err, f"CreateMutexExW failed ({err})")
    return h


def _create_mutex(k32, name: str, keep_waiting=None):
    """(handle, owned). Not owned = someone else holds it (or squats its name).

    `keep_waiting()`: while it returns True, wait for a held mutex instead of giving up."""
    h = _open_mutex(k32, name)
    if h is None:
        return None, False
    rc = k32.WaitForSingleObject(h, 0)
    while rc == _WAIT_TIMEOUT and keep_waiting and keep_waiting():
        rc = k32.WaitForSingleObject(h, 250)
    if rc in (0, 0x80):                                 # WAIT_OBJECT_0, WAIT_ABANDONED
        return h, True
    err = ctypes.get_last_error()
    k32.CloseHandle(h)
    if rc == _WAIT_TIMEOUT:
        return None, False
    raise OSError(err, "WaitForSingleObject failed on the installer lock")


def _lent_by_live_holder(k32, name: str) -> bool:
    """True if INSTALL_LOCK_ENV names a lock lent by a holder that is still running. Consumed."""
    token = os.environ.pop(INSTALL_LOCK_ENV, "")
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return False
    h = k32.OpenMutexW(0x00100000, False, f"{name}-lent-{token}")     # SYNCHRONIZE
    if not h:
        return False
    k32.CloseHandle(h)
    return True


def _queue_heir(k32, name: str):
    """A daemon thread queued on the real lock: it takes the lock over if the lending holder dies
    while this (delegated) process is still working. Returns (thread, stop event handle).

    ONE uninterrupted wait on [lock, stop]: a thread that waited in slices was not queued between
    them, and a new installer's Wait(0) could take the abandoned lock in that gap (audit: 1 in 80
    kills with a busy worker). Queued, it is handed the lock atomically on abandonment."""
    import threading
    from ctypes import wintypes as wt
    h = _open_mutex(k32, name)
    stop = k32.CreateEventW(None, True, False, None)
    if not stop:
        raise OSError(ctypes.get_last_error(), "CreateEventW failed")

    def heir():
        if h is None:
            return
        try:
            both = (wt.HANDLE * 2)(h, stop)
            if k32.WaitForMultipleObjects(2, both, False, 0xFFFFFFFF) in (0, 0x80):
                k32.WaitForSingleObject(stop, 0xFFFFFFFF)     # owned: hold it until release()
                k32.ReleaseMutex(h)
        finally:
            k32.CloseHandle(h)
    t = threading.Thread(target=heir, name="installer-lock-heir", daemon=True)
    t.start()
    return t, stop


def installer_running(game_dir: Path) -> bool:
    """True if another process holds this game folder's installer lock right now. Never waits,
    never raises: --check uses it to stay out of a running installer's way."""
    if os.name != "nt":
        return False
    try:
        k32 = _lock_kernel32()
        h = _open_mutex(k32, installer_lock_name(game_dir))
        if h is None:
            return False
        try:
            rc = k32.WaitForSingleObject(h, 0)
            if rc in (0, 0x80):
                k32.ReleaseMutex(h)
            return rc == _WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    except OSError:
        return False


def hold_installer_lock(game_dir: Path, wait_for=None, wait_s: int = 60) -> InstallerLock:
    """Take the one-installer-at-a-time lock for `game_dir`, or refuse with nothing changed.

    `wait_for`: a PID that holds the lock now and is about to exit (generation 1, for its
    generation 2). The lock is waited for while that process lives, up to `wait_s`; a lock
    that is still held after it has gone belongs to someone else, and is refused like any other.
    Taking it again in a process that already holds it returns a view whose release() is a
    no-op: only the first taker lets go.
    """
    if os.name != "nt":
        return InstallerLock("")
    name = installer_lock_name(game_dir)
    if name in _HELD_LOCKS:
        return InstallerLock(name, lent=_HELD_LOCKS[name]._lent)
    h = None
    try:
        k32 = _lock_kernel32()
        h, owned = _create_mutex(k32, name)
        if not owned and wait_for:
            # Wait ON THE MUTEX while that process lives, and a little after: Windows reports a
            # process as exited a moment before it abandons the mutexes it owned, so one retry
            # after "it has gone" could still find the lock held (seen under load).
            import time
            import preflight as pfm
            deadline, gone, waited = time.monotonic() + wait_s, [None], [False]

            def keep_waiting():
                waited[0] = True              # (not reached for a name squatted by a stranger)
                now = time.monotonic()
                if gone[0] is None and not pfm._pid_alive(int(wait_for)):
                    gone[0] = now
                return now < deadline and (gone[0] is None or now - gone[0] < 3)
            h, owned = _create_mutex(k32, name, keep_waiting)
            if not owned and waited[0] and gone[0] is None:
                raise ix.InstallerError(
                    "The previous installer did not exit, so this one stopped rather than run "
                    "alongside it.", code=ix.EXIT_ABORTED, changed=False,
                    remedy="close any other installer window and run it again.")
        if not owned:
            if _lent_by_live_holder(k32, name):
                lock = _HELD_LOCKS[name] = InstallerLock(name, delegated=True,
                                                         heir=_queue_heir(k32, name))
                return lock
            raise ix.InstallerError(
                "Another Longevity Vaccine installer is already working on this game, so this "
                "one stopped without changing anything.",
                code=ix.EXIT_ABORTED, changed=False, title="INSTALLER ALREADY RUNNING",
                remedy="let the other installer window finish, or close it if it is waiting for "
                       "you to press Enter (an update the game started runs in its own window), "
                       "then run this again.")
    except OSError as e:
        # The lock guards against an accident; it must never be the reason an install fails.
        if h:
            k32.ReleaseMutex(h)
            k32.CloseHandle(h)
        ix.say(f"  (could not take the one-installer-at-a-time lock: {e}; continuing without it)")
        return InstallerLock(name)
    token = os.urandom(16).hex()     # not `secrets`: the trimmed runtime does not ship it
    try:
        lent, _ = _create_mutex(k32, f"{name}-lent-{token}")
        if not lent:
            raise OSError(0, "the lending mutex was refused")
    except OSError as e:
        # Holding a lock we cannot lend would refuse our OWN worker as "another installer" --
        # an update that can never run. Fail open instead, like any other lock failure.
        k32.ReleaseMutex(h)
        k32.CloseHandle(h)
        ix.say(f"  (could not take the one-installer-at-a-time lock: {e}; continuing without it)")
        return InstallerLock(name)
    lock = InstallerLock(name, h, (token, lent))
    _HELD_LOCKS[name] = lock
    return lock


def wait_for_game(pid) -> None:
    """Block until `pid` (the game that launched us) has exited, or refuse."""
    if not pid:
        return
    import bootstrap as bsmod
    ix.say("\nWaiting for Alpha Centauri to close.")
    if not bsmod._wait_for_pid(int(pid), timeout=GAME_EXIT_WAIT):
        raise ix.InstallerError(
            "Alpha Centauri is still running, so the update did not start.",
            code=ix.EXIT_ABORTED, title="UPDATE NOT STARTED",
            disk_note="Close the game, then start it again: it will offer the update again.")
    ix.say("Updating...")


def do_update(game_dir: Path, args) -> int:
    """Manifest-driven update. Preflight everything, then commit transactionally."""
    import preflight as pfm
    import receipt as rc
    import transact as tx
    import manifest as mfmod

    check_companion_declarations()
    wait_for_game(getattr(args, "wait_pid", None))
    state = rc.read(game_dir)
    installed = rc.installed_modules(state)     # (the version is in the header; the packs show in the preflight)

    # EXPLICIT `--modules` is fetched as named (it doubles as the repair path for a pack);
    # otherwise the preflight fetches exactly the installed modules the manifest says are stale.
    explicit = validate_module_names(args.modules, game_dir) if args.modules else None
    # The game's hand-off: the packs its popups named and the player accepted, and ONLY those
    # (none without --modules). The base is still brought current -- the bootstrap runs the newest
    # installer by nature, and the game always lists the core when it is stale.
    only_listed = bool(getattr(args, "only_listed", False))
    installed_modules = {n: rc.module_version(state, n)
                         for n in installed if n in GAME_CONTENT_MODULES}
    base_version = rc.module_version(state, "base")

    raw, sig = fetch_manifest(game_dir, args.manifest, args.offline)
    # NOTE: LastUpdateCheck is recorded at the END, not here. Writing it before the preflight put
    # a write to Alpha Centauri.ini inside the phase that is documented as touching no game file
    # -- and it is wrong on its own terms: a check that failed verification is not a check that
    # happened, and recording it would suppress the next one.

    # Choose the exe by the RECEIPT's target, not by config.exe_name().
    #
    # `work\game-exe.txt` is machine-specific and correctly not shipped, so from a resident
    # updater config.exe_name() falls back to the default "terranx.exe" -- which exists on every
    # stock Planetary Pack install. `target.is_file()` was therefore true, the fallback never ran,
    # and the update aborted with "unrecognised base binary": the in-game update button was dead
    # in the one configuration players actually use.
    target = None
    tid = state.get("target_id")
    tbl = _targets()
    if tid and tid in tbl and tbl[tid].exe and (game_dir / tbl[tid].exe).is_file():
        target = game_dir / tbl[tid].exe
        pass                                  # the receipt's target; no need to say so
    if target is None:
        target = next((game_dir / n for n in _exe_names() if (game_dir / n).is_file()), None)
    if target is None:
        raise ix.InstallerError(
            "No Alpha Centauri executable was found to update.",
            code=ix.EXIT_NO_GAME, detail=f"Searched: {game_dir}", changed=False,
            remedy="check that --game-dir points at the folder containing the game.")
    rec = _read_setup(game_dir)
    if rec.get("original_md5") and target.name.lower() == str(rec.get("exe", "")).lower() \
            and _md5_or_none(target) == rec["original_md5"]:
        # A folder set up for Thinker whose original terranx.exe is back (GOG's "verify files"):
        # the preflight would refuse it as "a binary we did not make", which is true and useless.
        raise ix.InstallerError(
            f"The game's original {target.name} is back in this folder, so the update did not start.",
            code=ix.EXIT_ABORTED, changed=False,
            detail="  GOG Galaxy may have restored it while checking the game's files.",
            remedy=("run the installer again (INSTALL.bat in the download; download it again if you no longer have it)"
                    + (" to set the folder up for Thinker again"
                       if _thinker_overlay() is not None and _thinker_here(game_dir, _thinker_overlay()) else "")
                    + "; the game's update prompt then works again."))

    ix.say("")
    pf = pfm.run(game_dir=game_dir, target=target,
                 patches=Path(ap.DEFAULT_PATCHES), table=_targets(),
                 manifest_raw=raw, manifest_sig=sig,
                 installed_version=base_version,
                 receipt_target_id=state.get("target_id"),
                 wanted_modules=explicit, installed_modules=installed_modules,
                 only_listed=only_listed,
                 game_pid=getattr(args, "wait_pid", None),
                 public_key_hex=None, labels=MODULE_LABELS,
                 # file:// URLs are a TEST affordance and are enabled only when the operator
                 # pointed --manifest at something local. Hardcoding True in the production path
                 # meant a signed manifest could name `file:///...` module URLs and the installer
                 # would read them -- turning the signed document into a local-file reader for no
                 # reason anyone shipping needs.
                 allow_file=_local_manifest(args.manifest))
    pf.report(say=ix.say)

    if not pf.ok:
        # Leave nothing unverified behind, so "your install is untouched" is literally true.
        if any("signature" in c.name or "manifest" in c.name for c in pf.blockers):
            discard_staged_manifest(game_dir)
        unreleased = [c.name[:-len(": not released yet")] for c in pf.blockers
                      if c.name.endswith(": not released yet")]
        if unreleased and len(unreleased) == len(pf.blockers):
            # The generic "not everything checked out ... fix the items above" was wrong here:
            # nothing is broken and a retry fails identically.
            names = " and ".join(unreleased)
            raise ix.InstallerError(
                f"{names} {'is' if len(unreleased) == 1 else 'are'} not available for download "
                "yet. Nothing was downloaded or changed.",
                code=ix.EXIT_ABORTED, changed=False,
                remedy=f"nothing to do now; your game is fine. Run --update again after a "
                       f"release that includes {names}.")
        raise ix.InstallerError(
            "The installer stopped before changing anything, because not everything it needs "
            "checked out.",
            code=ix.EXIT_ABORTED,
            detail="\n".join(f"  - {c.name}: {c.detail}" for c in pf.blockers)
                   + "\n\nYour game files are exactly as they were. Anything already downloaded is "
                     "kept under LongevityMods\\staging so a retry does not fetch it again; that "
                     "folder is safe to delete.",
            changed=False,
            remedy="fix the items above and run it again; your game is untouched.")

    # `--modules` names specific modules, and a module's version is independent of the product
    # version, so "the product is current" is not an answer to "install movies-hd". Without the
    # exemption, `run-install.bat --update --modules movies-hd` -- the command
    # offer_optional_modules prints verbatim -- did nothing at all once the base was current.
    global _VERDICT
    base_stale = not ver.is_valid(base_version) or base_version == ver.UNKNOWN or (
        pf.decision is not None and pf.decision.version is not None
        and ver.is_newer(pf.decision.version, base_version))
    # --only-listed: stale packs the game's popups did not name stay as they are (the preflight
    # said so above, one line each) -- never "up to date", and never fetched.
    left = [n for n in (pf.decision.modules if pf.decision is not None else [])
            if n not in pf.wanted] if only_listed else []
    if pf.decision is not None and (
            (pf.decision.action == mfmod.NOTHING and not args.modules)
            or (only_listed and not pf.wanted and not base_stale)):
        ix.say("\nAlready up to date. Nothing needs updating." if not left else
               "\nNothing the game listed needs updating.")
        for n in sorted(installed_modules):
            if n not in left:
                ix.say(f"  {_pack_label(n)}: up to date (v{installed_modules[n]})")
        rc.note_check(game_dir)      # a completed check, even though there was nothing to apply
        # A player who is current is exactly the one who has not heard about a new pack: the
        # notice used to be reachable only as a side effect of a base update.
        offer_optional_modules(game_dir, pf.doc, installed, args)
        body = ("Everything you have installed is current. Nothing was changed." if not left else
                "Nothing was changed: nothing listed for this update needed it. "
                + " and ".join(_pack_label(n) for n in left)
                + (" has" if len(left) == 1 else " have")
                + " a newer version that was not part of this update; it was left as it is.")
        _VERDICT = ("ALREADY UP TO DATE",
                    body + ("  An optional extra is available -- see \"optional extras\" above."
                            if announced_modules(game_dir, pf.doc, installed) else ""))
        return ix.EXIT_OK
    wanted = list(pf.wanted)
    for n in sorted(installed_modules):
        if n not in wanted and n not in left:
            ix.say(f"  {_pack_label(n)}: up to date (v{installed_modules[n]}), no download needed")

    # A pack that failed to download is only ADVISORY in the preflight (an extras problem must
    # not block the core patch). But when the pack IS the update -- the base is current and
    # nothing else is stale -- committing anyway re-patched the exe, wrote the receipt and said
    # "UPDATE COMPLETE" about an update that had not happened.
    not_staged = [n for n in wanted if n not in pf.staged]

    def _offered(n):
        info = (pf.doc.get("modules") or {}).get(n)
        v = info.get("version") if isinstance(info, dict) else None
        return ver.normalise(v) if isinstance(v, str) and ver.is_valid(v) else None

    def _pack_failure(n) -> tuple:
        """("The update to <label> v1.1.0 could not be downloaded. <label> v1.0.0 ...", detail). No article
        or "Your" before the label: "Turovetsky's HD Portrait Pack" takes neither (audit 2026-11-06)."""
        label, offered, have = _pack_label(n), _offered(n), installed_modules.get(n)
        what = f"{label} v{offered}" if offered else label
        if have and have != ver.UNKNOWN:
            msg = (f"The update to {what} could not be downloaded. {label} v{have} "
                   f"{'are' if label.endswith('s') else 'is'} still installed and "
                   f"{'work' if label.endswith('s') else 'works'} as before.")
        else:
            msg = f"{what} could not be downloaded."
        why = next((c.detail for c in pf.warnings if c.name.startswith(label)), "")
        return msg, f"  - {label}: {why or 'see the line marked above'}"

    if not_staged and not base_stale:
        msgs, details = zip(*(_pack_failure(n) for n in not_staged))
        raise ix.InstallerError(
            " ".join(msgs) + " Nothing was changed.",
            code=ix.EXIT_ABORTED, changed=False, detail="\n".join(details),
            remedy="check your connection and run it again; anything already downloaded is "
                   "kept and is not fetched twice.")

    ix.say("\n--- commit ---")
    spec = pf.spec
    # A companion the commit cannot resolve must NOT sink the core update.
    #
    # transact.stage_one raises when neither the live companion nor its .bak is the clean base --
    # the state a player who updated PRACX is in -- and do_update turned that into "The patched
    # binary could not be installed ... close the game and try again": the wrong file, a remedy
    # that cannot work, and an update that could never succeed again. The fresh-install path has
    # always tolerated exactly this state; the two now agree.
    companions, left_alone = [], []
    for live, cpatch, cspec in companion_specs(game_dir, spec):
        # NOT `state`: that is the receipt dict this function reads again further down, and
        # shadowing it crashed the run AFTER the commit had already swapped the new binaries in.
        cstate, md5 = companion_state(live, cspec, _known_patched(spec, live.name))
        if cstate == C_CLEAN or _verified_backup(live, cspec) is not None:
            companions.append((live, cpatch, cspec))
        else:
            ix.say(f"  {live.name} (MD5 {md5}) is not a build this patch set knows and has no "
                   f"clean backup -- leaving it alone.")
            ix.say("  (the mod updates without it; only the PRACX edge-scroll fix is missed)")
            left_alone.append(f"{live.name}: left alone (MD5 {md5}: not a build this patch set knows, "
                              f"no clean .bak)")

    work = fetchmod_staging(game_dir) / "commit"
    # On a pack-only run the patcher also re-verifies an exe that is not changing, which read as
    # "installed terranx_PRACX.exe" right above "Nothing else needed changing". Everything goes
    # to a log (the patcher's phase summary, never its per-site transcript); the console gets one
    # line per outcome.
    patch_log = _log_path(game_dir, "patch")
    try:
        run_patch_logged(patch_log, game_dir, target, spec, work, companions, notes=left_alone)
    except tx.CommitError as e:
        raise ix.InstallerError(
            "The patched binary could not be installed.", detail=f"  {e}\n  (log: {patch_log})",
            # The commit is transactional, so no live file was swapped. Staging a candidate can
            # still have created a .bak that was previously missing -- a correct backup of the
            # player's own bytes, which is why this is still "nothing was changed" for the game
            # itself rather than a partial install.
            changed=False,
            remedy="close the game if it is running and try again; your game files are as they "
                   "were.")
    if base_stale:
        ix.say(f"  game patch: applied (details in {patch_log})")
    else:
        ix.say("  game patch: already current, checked (no changes)")

    if not deploy_runtime(game_dir, quiet=not base_stale):
        raise ix.InstallerError(
            "The game was patched, but copying the mod files into place failed.",
            changed=True,
            remedy="close the game if it is running and re-run the installer.")
    # Every update run, not only the fresh install: the resident updater reaches this path on each
    # release, and the decision is "every run" (a player who added Thinker since install included).
    ensure_in_process_movies(game_dir)
    # ... and the Datalinks text follows each release's wording (its marker says which revision a file carries).
    patch_datalinks_text(game_dir)
    # A Thinker build this release was not measured on: say so here too (the game says it at start).
    warn_thinker_build(game_dir)

    versions = {}
    for n in wanted:
        info = (pf.doc.get("modules") or {}).get(n)
        v = info.get("version") if isinstance(info, dict) else None
        # normalise(): the receipt stores the CANONICAL text (version.py's rule), not the
        # manifest's bytes with their possible surrounding whitespace.
        versions[n] = ver.normalise(v) if isinstance(v, str) and ver.is_valid(v) else ver.UNKNOWN
    installed_now = install_modules(game_dir, pf.staged, versions)
    if "movies-hd" in installed_now:
        # The clips play only with [UIMod] UpscaledVideos on; a player who once said no and now
        # takes the pack from the game's prompt must not get silent .wve playback.
        set_ini_flag(game_dir, True)

    # `base` records the version of the installer that ACTUALLY ran, never the manifest's.
    # Taking it from the manifest recorded a version that was downloaded but never extracted --
    # after which decide() said "already current" and the real release could never install.
    # The base payload is applied by the self-update (bootstrap.py), not by this function.
    # Only names this installer knows survive a rewrite: a hand-edited receipt entry used to be
    # copied forward forever (and printed raw).
    merged = {n: i for n, i in (state.get("modules") or {}).items()
              if n == "base" or n in GAME_CONTENT_MODULES}
    merged.update(installed_now)
    merged["base"] = {"version": ver.VERSION}
    rc.write(game_dir, target_id=spec.id, modules=merged, say=ix.say)
    rc.note_check(game_dir)
    config.remember_game_dir(game_dir)

    # The core update is committed and recorded. A pack that did not land after that is still a
    # failed run -- "UPDATE COMPLETE" would be a lie about the one thing the player may have
    # run this for -- and staging is kept so the retry does not download it again.
    missing = [n for n in wanted if n not in installed_now]
    if missing:
        msgs, details = zip(*(_pack_failure(n) for n in missing))
        raise ix.InstallerError(
            f"Longevity Vaccine v{ver.VERSION} is installed and the game is ready to play. "
            + " ".join(msgs),
            code=ix.EXIT_ERROR, changed=True, title="UPDATE PARTLY COMPLETE",
            detail="\n".join(details),
            disk_note="The game is patched and up to date; only the pack update is missing.",
            remedy="run the update again to get "
                   + " and ".join(f"{_pack_label(n)} v{_offered(n)}" if _offered(n)
                                  else _pack_label(n) for n in missing)
                   + "; nothing already downloaded is fetched twice.")

    # Reclaim the staging space now that everything is installed. Nothing removed it before, so a
    # 1.3 GB pack stayed on disk twice over, indefinitely.
    try:
        import fetch as fetchmod
        fetchmod.clear_staging(game_dir)
    except Exception as e:  # noqa: BLE001 - cleanup must not fail a completed install
        ix.say(f"  (could not clear staging: {e})")

    # What is installed NOW, not what was before this run: the old list offered the pack that
    # had just been installed as "available but not installed".
    # The configuration as it is NOW (the receipt was just rewritten; the pre-update state may carry
    # no target_id at all -- audit 2026-10-29), not the one the run started from.
    tid_now = _resolved_target_id(game_dir, state) or state.get("target_id")
    check_display_resolution(game_dir, tid_now, no_prompt=bool(getattr(args, "no_prompt", False)))
    check_desktop_shortcut(game_dir, tid_now)
    offer_optional_modules(game_dir, pf.doc, installed + list(installed_now), args)
    # Say what happened. "Your installation is up to date" after adding a pack told the player
    # nothing about the pack, and "INSTALLATION SUCCESSFUL ... ready to play" after a base
    # update did not say what was updated.
    done = [f"{_pack_label(n)} v{installed_now[n]['version']}" for n in sorted(installed_now)]
    # A pack the game's list did not name was left on purpose: say so, never "nothing else".
    stays = "".join(f" {_pack_label(n)} stays at v{installed_modules.get(n, '?')}"
                    f" (v{_offered(n) or '?'} was not part of this update)." for n in sorted(left))
    if base_stale:
        done.insert(0, f"Longevity Vaccine v{ver.VERSION}")
        _VERDICT = ("UPDATE COMPLETE", " and ".join(done) + " installed." + stays)
    elif done:
        _VERDICT = ("UPDATE COMPLETE", ", ".join(done) + " installed."
                    + (stays or " Nothing else needed changing."))
    global _NOTE_ARGS
    _NOTE_ARGS = (game_dir, tid_now)
    return ix.EXIT_OK


def install_packs_from_manifest(game_dir: Path, args, names: list, manifest, target_id,
                                target: Path) -> tuple:
    """Download and place the optional packs a fresh install chose, through the update's own
    verified path (preflight stages the archives against the signed manifest; install_modules
    extracts them). Returns ({name: receipt entry}, {name: "ok" | "error"}).

    Never raises for a pack: the game is already patched and playable, so a pack that cannot be
    fetched is reported and left for the game's update prompt to offer again.
    """
    import preflight as pfm
    statuses = {n: "error" for n in names}
    if not manifest:
        ix.warn("  The optional modules could not be downloaded: no verified update information.")
        return {}, statuses
    raw, sig, _doc = manifest
    ix.say("\n--- downloads ---")
    pf = pfm.run(game_dir=game_dir, target=target, patches=Path(ap.DEFAULT_PATCHES),
                 table=_targets(), manifest_raw=raw, manifest_sig=sig,
                 installed_version=ver.VERSION, receipt_target_id=target_id,
                 wanted_modules=list(names), installed_modules={},
                 public_key_hex=None, labels=MODULE_LABELS,
                 allow_file=_local_manifest(args.manifest))
    # Not pf.report(): its twenty "[ok  ]" lines are the update's preflight checklist, which
    # tells a first-time player nothing here. Only what went wrong is worth a line.
    if not pf.ok:
        for c in pf.blockers:
            ix.warn(f"  {c.name}: {c.detail}")
    versions = {}
    for n in names:
        info = (pf.doc.get("modules") or {}).get(n) if pf.doc else None
        v = info.get("version") if isinstance(info, dict) else None
        versions[n] = ver.normalise(v) if isinstance(v, str) and ver.is_valid(v) else ver.UNKNOWN
    staged = {n: a for n, a in (pf.staged or {}).items() if n in names}
    got = install_modules(game_dir, staged, versions) if staged else {}
    for n in names:
        if n in got:
            statuses[n] = "ok"
        else:
            ix.warn(f"  {_pack_label(n)} could not be downloaded. The mod is installed without it; "
                    "the game's update prompt will offer it again.")
    if all(statuses[n] == "ok" for n in names):
        try:
            import fetch as fetchmod
            fetchmod.clear_staging(game_dir)
        except Exception as e:  # noqa: BLE001 - cleanup must not fail a completed install
            ix.say(f"  (could not clear staging: {e})")
    return got, statuses


PATCH_LOGS_KEPT = 3   # LongevityMods\logs\patch-<stamp>.log: the newest this many survive a run (audit 2026-11-07:
                      # one per update, never pruned, was the mod's only file that grew without bound)


def _log_path(game_dir: Path, kind: str) -> Path:
    import time as _t
    d = Path(game_dir) / "LongevityMods" / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = Path(tempfile.gettempdir())
    return d / f"{kind}-{_t.strftime('%Y%m%d-%H%M%S')}.log"


def prune_logs(log_dir: Path, kind: str, keep: int = PATCH_LOGS_KEPT) -> int:
    """Delete all but the newest `keep` `<kind>-YYYYMMDD-HHMMSS.log` in `log_dir`; returns how many went.

    Newest by NAME: the stamp sorts in time order, so a player's clock or a copied folder cannot reorder them.
    ONLY names of exactly that shape count (audit 2026-11-07: a `patch-notes.log` matched the glob, sorted
    after every stamp, and would have displaced -- or deleted -- the run's own fresh log); check.log and
    anything else stays. `log_dir` is where the run's log went (_log_path may fall back to %TEMP%).
    Never raises: a log that cannot be deleted is left for the next run."""
    d = Path(log_dir)
    shape = re.compile(re.escape(kind) + r"-\d{8}-\d{6}\.log\Z")
    try:
        names = sorted(p for p in d.glob(f"{kind}-*.log") if shape.fullmatch(p.name) and p.is_file())
    except OSError:
        return 0
    gone = 0
    for p in names[:-keep] if keep > 0 else names:
        try:
            p.unlink()
            gone += 1
        except OSError:
            pass
    return gone


def _log_summary(status: str, text: str) -> None:
    """apply_patches' per-phase line, as the patch log records it (stdout is redirected into the log)."""
    print(f"[{status}] {text}")


@contextlib.contextmanager
def fresh_patch_log(game_dir: Path):
    """The fresh-install counterpart of run_patch_logged: LongevityMods\\logs\\patch-<stamp>.log around the
    patch_target / patch_companions pass -- a header, then whatever _patch_summary and _patch_log_line record
    (per-exe target / measured base / expected result, the phase lines, a companion's outcome), `done` or
    `[error] <text>` on the way out, then the prune. Until 2026-11-07 a first install -- the common 1.0.0
    case -- left no record at all; the console closed with the window and the receipt carries no hashes.
    A log that cannot be opened disables the tee for this run and nothing else."""
    global _PATCH_LOG
    import time as _t
    path = _log_path(game_dir, "patch")
    try:
        f = open(path, "w", encoding="utf-8", errors="replace")
    except OSError:
        f = None
    _PATCH_LOG = f
    try:
        _patch_log_line(f"Longevity Vaccine {ver.VERSION} -- install patch run {_t.strftime('%Y-%m-%d %H:%M:%S')}")
        _patch_log_line(f"game folder: {game_dir}")
        yield path
        _patch_log_line("done")
    except BaseException as e:   # noqa: BLE001 - recorded, then propagated unchanged
        _patch_log_line(f"[error] {str(e) or type(e).__name__}")
        raise
    finally:
        _PATCH_LOG = None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        prune_logs(path.parent, "patch")


def run_patch_logged(patch_log: Path, game_dir: Path, target: Path, spec, work: Path, companions: list,
                     notes: list = ()) -> None:
    """The transactional patch of the exe (+ companions), narrated into `patch_log`.

    The log is a RECORD, not a transcript: a header (installer version, run time, each binary's target id,
    clean-base and expected hashes, `notes` = decisions taken before the run, e.g. a companion left alone),
    then the patcher's one-line-per-phase summary -- skipped sites by name, a FAILING site with its bytes --
    the stage / commit lines, and `done`; a failure is recorded as `[error] <text>` before it propagates
    (a CommitError's text -- the clean-base refusal, the lock mismatch, a swap rolled back -- lived only in
    the exception until the audit of 2026-11-07, so the one log support needs most said nothing). Before
    2026-11-07 the full per-site transcript (4,200 lines, 850 KB) went here on every update. Older patch logs
    beyond PATCH_LOGS_KEPT are pruned whether the run succeeds or raises."""
    import time as _t
    import transact as tx   # lazy, as do_update imports it: install.py has no module-level transact
    binaries = [(Path(target), spec)] + [(Path(c[0]), c[2]) for c in (companions or [])]
    try:
        with _captured(patch_log):
            print(f"Longevity Vaccine {ver.VERSION} -- update patch run {_t.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"game folder: {game_dir}")
            for live, cspec in binaries:
                print(f"{live.name}: target {cspec.id!r} ({cspec.label}), clean base {cspec.base_md5}, "
                      f"expected result {cspec.patched_md5 or '(unrecorded)'}")
            for n in notes:
                print(n)
            try:
                tx.apply_transactional(target, Path(ap.DEFAULT_PATCHES), spec, work,
                                       companions=companions, say=print, summary=_log_summary)
            except BaseException as e:   # noqa: BLE001 - recorded, then propagated unchanged
                print(f"[error] {str(e) or type(e).__name__}")
                raise
            print("done")
    finally:
        prune_logs(Path(patch_log).parent, "patch")


class _captured:
    """Redirect stdout into a log file for the duration; the console keeps the outcome line."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        import contextlib
        # The log goes into public bug reports: the profile folder (the account name) is redacted.
        self._f = ix.RedactingWriter(open(self.path, "w", encoding="utf-8", errors="replace"))
        self._cm = contextlib.redirect_stdout(self._f)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._f.close()
        return False


def fetchmod_staging(game_dir: Path) -> Path:
    import fetch as fetchmod
    return fetchmod.staging_dir(game_dir)


def absent_modules(doc: dict, installed: list) -> list:
    """Content modules the manifest offers that the player does not have. Only names this
    installer knows (GAME_CONTENT_MODULES): a module it cannot place is not one it can offer."""
    mods = (doc or {}).get("modules") if isinstance(doc, dict) else None
    if not isinstance(mods, dict):
        return []
    return [n for n in GAME_CONTENT_MODULES
            if n not in installed and isinstance(mods.get(n), dict)]


def announced_modules(game_dir: Path, doc: dict, installed: list) -> list:
    """absent_modules less the packs the player turned down ("Don't ask me again" on the game's
    offer, or left unticked at install): what the console tells an up-to-date player about.
    The game's own offer (--check) applies the declines through check_report's reset rule."""
    import receipt as rc
    declines = rc.declined(game_dir)
    mods = doc.get("modules") if isinstance(doc, dict) else None

    import version as _ver           # this runs from the bootstrap too, before _load_modules

    def offered(n):
        v = str((mods.get(n) or {}).get("version") or "")
        return _ver.normalise(v) if _ver.is_valid(v) else v    # as check_report compares it
    return [n for n in absent_modules(doc, installed)
            if not rc.is_declined(declines, NEW_PACK_PREFIX + n, offered(n))]


def offer_optional_modules(game_dir: Path, doc: dict, installed: list, args) -> None:
    """After everything else is done, announce the optional modules that are NOT installed.

    Sequencing is the user's explicit instruction: update what is there first, then ask about
    extras. A decline here cannot affect the core update, which is already committed. Takes the
    verified manifest dict (not a Preflight) so the bootstrap can call it on its own
    already-current path too.
    """
    # NOT gated on --no-prompt: this asks nothing, it informs. Gating it meant the bootstrap's
    # worker (always --no-prompt) never announced extras after a real update, so a player only
    # ever heard about a pack on an idle check.
    if not doc:
        return
    absent = announced_modules(game_dir, doc, installed)
    if not absent:
        return
    import manifest as mfmod
    # The command has to be one the player can paste: a bare `run-install.bat` only works from
    # the right folder in cmd (and not at all in PowerShell). Name the resident copy in full.
    launcher = launcher_path(game_dir)
    extra = f' --manifest "{args.manifest}"' if args.manifest else ""
    ix.say("\n--- optional extras ---")
    for name in absent:
        info = doc["modules"][name] or {}
        size = info.get("size")
        dl = f" ({human_size(size)} download)" if mfmod.size_ok(size) and size > 0 else ""
        # Do NOT ask a yes/no question here: the in-game prompt is where a pack gets offered
        # interactively (Phase 8); this is the console's honest form -- what is available and
        # exactly what to paste to get it.
        ix.say(f"  {_pack_label(name)}: available, not installed{dl}")
        ix.say("    To add it, open Command Prompt and paste:")
        ix.say(f'      "{launcher}" --update --modules {name}{extra}')


# ---------------------------------------------------------------- --check (the game's query)
#
# At launch the game runs `python.exe install.py --check --game-dir <game>` as a hidden
# subprocess with stdout on a pipe, and reads the result. This is how the game learns WHAT is
# available -- a base update, a stale pack, a pack it does not have -- with the manifest
# signature verified by the same code that would install it, and the one decide() rule applied
# once rather than ported to C++.
#
# THE CONTRACT (the DLL's parser is written against this; changing it strands shipped clients):
#   * exit code: ix.CHECK_NOTHING / CHECK_UPDATES / CHECK_MANUAL / CHECK_FAILED
#   * stdout: ASCII `key=value` lines and NOTHING else -- no banner, no prompt, no progress.
#     Every diagnostic goes to stderr. A stray line would be parsed as an item; a prompt would
#     hang the game's reader.
#       latest=<ver>                                 always (except manual/failed/yanked)
#       base=<have>><offered>                        the installer/core patch is stale
#       stale=<id>|<label>|<have>><offered>|<bytes>  an installed pack is stale
#       new=<id>|<label>|<offered>|<bytes>           a pack the player does not have
#       manual=<reason>                              kill switch; <reason> is a REASONS key or ''
#   * Only compiled-in ids (GAME_CONTENT_MODULES) and labels (MODULE_LABELS), normalised
#     versions and integer sizes cross the pipe. No manifest text, ever.
#   * Declined items ([Longevity] DeclinedUpdates): `<item>:<ver>` names that version only (the
#     game's "Don't ask me again" on an update offer), `<item>:*` every version (its "Don't ask me
#     again" on a NEW pack's offer, `new:<id>:*`; a legacy `base:*` / `<id>:*` reads the same).
#     THE RESET RULE (user, 2026-10-31): "a decline of any pack is reset when any new update
#     releases for a module that is already installed". If the base or an installed pack has an
#     offer the player has NOT declined at that version, EVERY decline is ignored and every
#     candidate is listed. Otherwise:
#       - a declined BASE offer hides everything, new packs included (user, 2026-10-31): the game
#         hands off `--update --only-listed --modules <accepted>`, and while that fetches exactly
#         the packs listed, a stale core is always brought along -- so any accept would install
#         the declined core unannounced;
#       - a declined stale PACK hides only itself: a new pack is still offered, and accepting it
#         hands off `--only-listed --modules <that pack>`, which leaves the declined one alone;
#       - a new pack is listed unless `new:<id>` is declined at `*` or that version.
#     The base hiding ends with the next base release past the declined version, with Reset
#     Skipped Updates, or -- for a legacy `base:*`, which the game no longer writes -- only with
#     Reset. With nothing left the answer is exit 0 with `latest=` alone. check_report is the ONE
#     filter point; --update, --repair and --modules never consult the declines, and only the
#     game's --only-listed narrows what an update fetches (to exactly --modules).
#     `manual-<reason>:*` (reason = the REASONS key, `unspecified` for none) turns THAT
#     manual-install verdict into exit 0 with no lines; a different reason still shows.
#   * Disk: writes only the manifest cache under LongevityMods\staging and LastUpdateCheck.

_LINE_OK = set(chr(c) for c in range(0x20, 0x7f))


def _pipe_field(s) -> str:
    """One protocol field: printable ASCII, no separators. Belt and braces -- every input is
    already compiled-in or validated, and this makes that a property rather than an audit."""
    return "".join(ch for ch in str(s) if ch in _LINE_OK and ch not in "|=\n\r")


def _pipe_size(info: dict) -> str:
    import manifest as mfmod
    s = info.get("size") if isinstance(info, dict) else None
    return str(s) if mfmod.size_ok(s) else ""


def check_report(doc: dict, state: dict, declined: dict, target_id: str = None) -> tuple:
    """(exit code, protocol lines) for a VERIFIED manifest and a receipt. Pure: no I/O.

    `target_id` is the id resolved from the exe's own bytes when the caller has it; the
    receipt's is the fallback. The surgical kill switch keyed on a stale or hand-edited
    receipt disagreed with --update, which resolves by MD5."""
    import manifest as mfmod
    import receipt as rc
    installed = rc.installed_modules(state)
    content = {n: rc.module_version(state, n) for n in installed if n in GAME_CONTENT_MODULES}
    base = rc.module_version(state, "base")
    d = mfmod.decide(doc, base, target_id or state.get("target_id"), installed_modules=content)
    declined = declined if isinstance(declined, dict) else {}

    if d.action == mfmod.MANUAL:
        reason = d.reason if isinstance(d.reason, str) and d.reason in mfmod.REASONS else ""
        if declined.get(rc.manual_decline_item(reason)) == rc.DECLINE_ANY:
            # "Don't show this again" on the manual-install notice, keyed by REASON: a later,
            # different kill switch is a different situation and shows again. The condition
            # still holds (say so on stderr for the log); the game just is not told to nag.
            print(f"--check: manual install required ({reason or 'unspecified'}); "
                  f"notice silenced by DeclinedUpdates {rc.manual_decline_item(reason)}:*",
                  file=sys.stderr)          # never the pipe, whoever calls this
            return ix.CHECK_NOTHING, []
        return ix.CHECK_MANUAL, [f"manual={_pipe_field(reason)}"]

    latest = doc.get("latest")
    if not isinstance(latest, str) or not ver.is_valid(latest):
        return ix.CHECK_NOTHING, []          # decide() already refused this as MANUAL; defensive
    latest = ver.normalise(latest)
    yanked, _ok = mfmod._str_list(doc, "yanked")
    if latest in yanked:
        return ix.CHECK_NOTHING, []          # "stay quiet" means quiet about everything
    mods = doc.get("modules") if isinstance(doc.get("modules"), dict) else {}

    # Every candidate first, as (line, decline item, offered version, for something installed).
    offers = []
    if d.action == mfmod.UPDATE and (not ver.is_valid(base) or ver.is_newer(latest, base)):
        have = ver.normalise(base) if ver.is_valid(base) else ver.UNKNOWN
        offers.append((f"base={have}>{latest}", "base", latest, True))
    for n in d.modules:
        info = mods.get(n)
        offered = info.get("version") if isinstance(info, dict) else None
        if not isinstance(offered, str) or not ver.is_valid(offered):
            continue
        offered = ver.normalise(offered)
        have = content.get(n)
        have = ver.normalise(have) if isinstance(have, str) and ver.is_valid(have) else ver.UNKNOWN
        offers.append((f"stale={_pipe_field(n)}|{_pipe_field(MODULE_LABELS.get(n, n))}"
                       f"|{have}>{offered}|{_pipe_size(info)}", n, offered, True))
    for n in absent_modules(doc, installed):
        info = mods.get(n)
        offered = info.get("version") if isinstance(info, dict) else None
        if not isinstance(offered, str) or not ver.is_valid(offered):
            continue
        offered = ver.normalise(offered)
        offers.append((f"new={_pipe_field(n)}|{_pipe_field(MODULE_LABELS.get(n, n))}"
                       f"|{offered}|{_pipe_size(info)}", "new:" + n, offered, False))

    # THE RESET RULE (user, 2026-10-31): "a decline of any pack is reset when any new update
    # releases for a module that is already installed". An offer for something installed (the
    # base, a stale pack) that the player has NOT declined at that version makes every decline
    # void and lists every candidate. Only when no such offer exists do the declines hide what
    # they name -- and then a declined BASE hides new packs too: the game hands off with
    # --only-listed, which fetches exactly the packs it lists, but a stale core is always brought
    # along, so offering a pack would install the declined core unannounced. A declined stale PACK
    # alone hides nothing else: --only-listed leaves it where it is.
    pending = any(own and not rc.is_declined(declined, item, v) for _, item, v, own in offers)
    base_held = any(item == "base" and rc.is_declined(declined, item, v)
                    for _, item, v, _own in offers)
    if pending:
        shown = offers                       # every decline void: one popup with everything
    elif base_held:
        shown = []                           # the core is declined; anything else drags it in
    else:
        shown = [o for o in offers if not rc.is_declined(declined, o[1], o[2])]
    lines = [f"latest={latest}"] + [line for line, _item, _v, _own in shown]
    code = ix.CHECK_UPDATES if len(lines) > 1 else ix.CHECK_NOTHING
    return code, lines


def restorable_base_hashes() -> set:
    """Every base MD5 an uninstall may restore FROM a .bak: the bases of the SHIPPING targets and
    their companions. A dev-only entry (the plain vanilla exe, patched only by --pristine tooling)
    is not among them: a terranx.exe.bak of that build that another tool left behind is not ours,
    and copying it over the live file "restored" a different game (audit 2026-10-28)."""
    ours = set()
    for t in _targets().values():
        if not t.launch:
            continue
        if t.base_md5:
            ours.add(t.base_md5)
        for cinfo in (t.companion or {}).values():
            if isinstance(cinfo, dict) and cinfo.get("base_md5"):
                ours.add(str(cinfo["base_md5"]).lower())
    return ours


def _resolved_target_id(game_dir: Path, state: dict):
    """The target id from the exe's (or its .bak's) MD5, the way --update resolves it; None if
    that cannot be determined (then the receipt's id is used)."""
    try:
        tbl = _targets()
        tid = state.get("target_id")
        exe = None
        if tid and tid in tbl and tbl[tid].exe and (game_dir / tbl[tid].exe).is_file():
            exe = game_dir / tbl[tid].exe
        if exe is None:
            exe = next((game_dir / n for n in _exe_names() if (game_dir / n).is_file()), None)
        if exe is None:
            return None
        bak = exe.with_suffix(exe.suffix + ".bak")
        probe = bak if bak.is_file() else exe
        return ap.resolve_target(_md5_safe(probe, changed=False), tbl, launch_only=True,
                                 present_files=ap.folder_files(game_dir), exe_name=exe.name).id
    except Exception:  # noqa: BLE001 - a check must degrade, never raise over this
        return None


def do_check(game_dir: Path, args) -> tuple:
    """Fetch + verify, then check_report. Returns (code, lines); raises nothing but lets
    InstallerError/ManifestError propagate to check_main, which maps them to CHECK_FAILED."""
    import manifest as mfmod
    import receipt as rc
    raw, sig = fetch_manifest(game_dir, args.manifest, args.offline, quick=True)
    try:
        doc = mfmod.load_verified(raw, sig, allow_env=_local_manifest(args.manifest))
    except mfmod.ManifestError:
        discard_staged_manifest(game_dir)
        raise
    state = rc.read(game_dir)
    code, lines = check_report(doc, state, rc.declined(game_dir),
                               target_id=_resolved_target_id(game_dir, state))
    rc.note_check(game_dir)          # a check that verified is a check that happened
    # One line for the developer log (stderr): what was offered. Success used to be silent.
    print("--check: verified; " + (", ".join(lines[1:]) if len(lines) > 1 else "nothing to offer"),
          file=sys.stderr)
    return code, lines


# ---- mod health, for check.log and a player's own --check console ---------------------------------
#
# STDERR ONLY, never the pipe: the DLL's parser (update_core.h ParseCheckOutputInto) refuses a key it does
# not know, so one extra stdout line would turn every check into "could not check" for the DLL that is
# installed today. stderr is the game's LongevityMods\logs\check.log -- the file a bug report comes with.

def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mod_files_line(game_dir: Path) -> str:
    """Are the DLLs beside the exe the ones the resident installer carries? uimod.dll + the five codec
    DLLs (deploy_runtime's set; the popup art is optional). Missing or altered ones are named: an
    antivirus quarantine or a foreign ffmpeg build is exactly what the DLL's own codec probe reports
    in-game, and here the installer says the same thing to whoever reads the log."""
    try:
        dll, ffmpeg = uimod_dll(), ffmpeg_bin()
        want = {}
        if dll.is_file():
            want["uimod.dll"] = dll
        if ffmpeg.is_dir():
            want.update({p.name: p for p in sorted(ffmpeg.glob("*.dll"))})
        if not want:
            return "Mod files: cannot compare -- the resident installer carries no DLLs"
        missing, altered = [], []
        for name, src in want.items():
            dst = Path(game_dir) / name
            if not dst.is_file():
                missing.append(name)
            elif dst.stat().st_size != src.stat().st_size or _sha256_file(dst) != _sha256_file(src):
                altered.append(name)
        if not missing and not altered:
            return f"Mod files: OK ({len(want)} DLLs match the installer's)"
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if altered:
            parts.append("altered: " + ", ".join(altered))
        return "Mod files: " + "; ".join(parts) + " -- re-run the installer (--repair restores them)"
    except OSError as e:
        return f"Mod files: could not check ({e})"


_INIT_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \[\d+\] \[uimod\] Init: v(\S+)")


def mod_loaded_line(game_dir: Path) -> str:
    """Did the game last start WITH the mod? From uimod.log's newest "[uimod] Init: v..." stamp against the
    receipt's install time -- the exe-side boot guard and a renamed DLL both leave a game that starts
    without the mod and without a word in this log. Adds what the DLL said about its codecs and movie
    sound at that start, when it said anything."""
    import json
    import time
    import receipt as rc
    game_dir = Path(game_dir)
    log = game_dir / "uimod.log"
    if not log.is_file():
        return "Mod loaded: never -- no uimod.log beside the exe (the game has not started with the mod)"
    try:
        with log.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - (1 << 20)))
            text = f.read().decode("utf-8", "replace")
    except OSError as e:
        return f"Mod loaded: could not read uimod.log ({e})"
    newest = None
    codecs = audio = None
    for ln in text.splitlines():
        m = _INIT_LINE.match(ln)
        if m:
            newest = (m.group(1), m.group(2))
            codecs = audio = None            # what follows belongs to this start
        elif newest and "[uimod] codecs: " in ln and ("missing" in ln or "not the file" in ln or "version" in ln):
            codecs = ln.split("[uimod] codecs: ", 1)[1].strip()
        elif newest and "[uimod] movie audio: " in ln and audio is None:
            audio = ln.split("[uimod] movie audio: ", 1)[1].strip()
    if not newest:
        return "Mod loaded: never -- uimod.log has no Init line"
    stamp, version = newest
    installed_at = None
    try:
        doc = json.loads((game_dir / rc.JSON_NAME).read_text(encoding="utf-8"))
        installed_at = doc.get("installed_at") if isinstance(doc, dict) else None
    except (OSError, ValueError):
        pass
    try:
        seen = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        seen = None
    if isinstance(installed_at, (int, float)) and seen is not None and seen < installed_at:
        return f"Mod loaded: not since this install (last seen {stamp}, v{version})"
    out = f"Mod loaded: yes, last at {stamp} (v{version})"
    if codecs:
        out += f" -- cinematics OFF: {codecs}"
    if audio:
        out += f" -- movie audio: {audio}"
    return out


def mod_health(game_dir: Path) -> list:
    return [mod_files_line(game_dir), mod_loaded_line(game_dir)]


def _log_health(game_dir) -> None:
    """The health lines to stderr (check.log); never raises -- a health line must not fail a check."""
    if game_dir is None:
        return
    try:
        for health in mod_health(game_dir):
            print("--check: " + health, file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"--check: mod health could not be determined ({type(e).__name__}: {e})", file=sys.stderr, flush=True)


def check_main() -> int:
    """Entry for --check. Bypasses run_guarded on purpose: no banner, no verdict, no wait --
    stdout is a pipe the game parses, so it carries the protocol lines and nothing else."""
    real_stdout, real_stderr = sys.stdout, sys.stderr
    # stderr is the game's check.log, which goes into public bug reports: the profile folder (the account
    # name) is redacted from every line, tracebacks included.
    sys.stderr = ix.RedactingWriter(real_stderr)
    sys.stdout = sys.stderr          # fetch progress, ix.say, anything chatty -> stderr
    lines, code = [], ix.CHECK_FAILED
    health_dir = None                # set once --game-dir is a directory; _log_health runs on both paths
    # A REAL wall deadline. fetch's own deadline is only checked between reads, so a host with
    # several dead addresses (8 s each) or a trickling body ran past 30 s and the game had to
    # kill us with an empty check.log (offline audit 2026-09-27). Nothing has been written to
    # stdout before the answer, so exiting here is the ordinary "could not check".
    print("--check: starting", file=sys.stderr, flush=True)
    import threading

    def _give_up():
        print(f"--check: gave up after {CHECK_WALL_SECONDS} s (network not answering)",
              file=sys.stderr, flush=True)
        os._exit(ix.CHECK_FAILED)
    watchdog = threading.Timer(CHECK_WALL_SECONDS, _give_up)
    watchdog.daemon = True
    watchdog.start()
    try:
        _load_modules()
        apr = argparse.ArgumentParser(add_help=False)
        apr.add_argument("--check", action="store_true")
        apr.add_argument("--game-dir", default=None)
        apr.add_argument("--manifest", default=None)
        apr.add_argument("--offline", action="store_true")
        apr.add_argument("--payload-root", default=None)
        args, _unknown = apr.parse_known_args()
        if args.payload_root:
            os.environ["LONGEVITY_PAYLOAD_ROOT"] = str(Path(args.payload_root).resolve())
        # NEVER autodetect here. The game passes its own directory; an omitted or empty
        # --game-dir used to fall through to SMACX_GAME_DIR / game-path.txt / the GOG paths --
        # i.e. a check launched from one install could fetch into, and stamp the ini of, a
        # different one.
        if not args.game_dir or not str(args.game_dir).strip():
            print("--check: --game-dir is required", file=sys.stderr)
            return ix.CHECK_FAILED
        game_dir = Path(args.game_dir)
        if not game_dir.is_dir():
            print(f"--check: no game directory at {game_dir}", file=sys.stderr)
            return ix.CHECK_FAILED
        if installer_running(game_dir):
            # Not locked (the game must never wait), but out of the way: a check fetches into the
            # same staging folder and writes the same ini the running installer is using.
            print("--check: an installer is working on this game; not checking now",
                  file=sys.stderr)
            return ix.CHECK_FAILED
        health_dir = game_dir
        code, lines = do_check(game_dir, args)
        # The two health lines go to check.log AFTER the fetch (its deadline accounting is not charged
        # the disk reads: ~8 MB of DLLs to hash, 10 ms warm, well under a second cold) -- and on the
        # failure path too, so a bug report's log says whether the mod's files are intact and whether the
        # game last started with the mod even when the network did not answer.
        _log_health(health_dir)
    except BaseException as e:  # noqa: BLE001 - the pipe must see a code, never a traceback
        expected = isinstance(e, ix.InstallerError) or type(e).__name__ in (
            "ManifestError", "FetchError")
        if not expected:
            import traceback
            traceback.print_exc(file=sys.stderr)
        msg = getattr(e, "message", None) or str(e)
        print(f"--check: could not check: {type(e).__name__}: {msg}", file=sys.stderr)
        _log_health(health_dir)
        return ix.CHECK_FAILED
    finally:
        sys.stdout = real_stdout
        sys.stderr = real_stderr
    # BYTES with a bare LF. A text-mode stdout on Windows wrote CRLF, so a parser splitting on
    # '\n' saw `manual=\r` (not empty) and `latest=0.1.0\r` (not a version).
    payload = b"".join(
        ("".join(ch for ch in ln if ch in _LINE_OK)).encode("ascii") + b"\n" for ln in lines)
    try:
        out = getattr(real_stdout, "buffer", None)
        if out is None:
            raise OSError("no stdout")
        out.write(payload)
        out.flush()
    except BaseException:  # noqa: BLE001 - the reader went away, or there never was one
        # Park fd 1 on NUL so the interpreter's shutdown flush cannot fail: an unflushed
        # buffer on a closed pipe made Python exit with status 120, and a missing stdout with
        # an AttributeError -- both outside the contract's four codes.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        except OSError:
            pass
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)                    # the code is the contract; nothing at shutdown may alter it


def do_repair(game_dir: Path) -> int:
    """Re-deploy the resident updater from THIS payload. Patches nothing.

    For the case the resident copy is corrupt or half-swapped -- which is precisely when the
    normal update path cannot be trusted to fix itself.
    """
    import bootstrap as bsmod
    ix.say("\n--- repair: resident updater ---")
    bsmod.reconcile(game_dir, say=ix.say)
    before = bsmod.resident_version(game_dir)
    ix.say(f"  currently resident: {before}")
    try:
        bsmod.deploy(game_dir, payload_root(), ver.VERSION, say=ix.say)
    except OSError as e:
        raise ix.InstallerError(
            "The resident updater could not be re-deployed.", detail=f"  {e}",
            # NOT False: reconcile() and deploy() both rewrite <game>\LongevityMods, and a
            # failure part-way through has certainly written something. The GAME is untouched,
            # which is what the message below says instead.
            changed=None,
            remedy="close any open installer window and run --repair again; your game files "
                   "were not touched.")
    now = bsmod.resident_version(game_dir)
    ix.say(f"  now resident: {now}")
    ix.say("  (the game itself was not touched)")
    global _VERDICT
    _VERDICT = ("REPAIR COMPLETE",
                f"The resident updater under LongevityMods was re-deployed (version {now}). "
                "Your game files were not touched.")
    return ix.EXIT_OK


def _uninstall_files() -> list:
    """The files an uninstall removes from the game folder (the folder picker's "installed here"
    test reads the same list, so a part-finished uninstall is still found by the retry)."""
    try:
        dlls = [p.name for p in sorted(ffmpeg_bin().glob("*.dll"))]
    except OSError:
        dlls = []
    return ["uimod.dll", POPUP_ART_NAME] + dlls


def do_uninstall(game_dir: Path, args) -> int:
    """Restore the original exe from .bak and remove what we deployed."""
    import receipt as rc
    ix.say("\n--- uninstall ---")

    # Companions (prax.dll) are ours to undo too -- leaving a patched companion behind is not an
    # uninstall. Their names come from the target table, never hardcoded.
    names = list(_exe_names())
    for t in _targets().values():
        for fname in (t.companion or {}):
            if fname not in names:
                names.append(fname)

    #: Every base MD5 we could have patched FROM, exe and companion alike. A .bak is only ours to
    #: restore if it hashes to one of these. Without the check, `--uninstall` copied ANY
    #: `<exe>.bak` it found over the live file -- another mod's backup included -- and counted it
    #: as a successful restore of ours.
    ours = restorable_base_hashes()

    restored, already_clean, foreign = 0, 0, 0
    # A folder the installer set up for Thinker FIRST: its terranx.exe.bak is our PRACX copy's,
    # and copying it over whatever terranx.exe is now in place (the original, put back by GOG or
    # by hand) would destroy the only copy of the original. This reads the folder as it is and
    # puts the original back; an exe it had to leave as it is (the player's own, say), the loop
    # below leaves alone too.
    held: list = []
    # Its undo changes files even when it puts none back. An unreadable record describes nothing:
    # it goes, said so, and does not count as work done.
    had_setup = bool(_read_setup(game_dir))
    if thinker_setup_folder(game_dir) and not had_setup:
        try:
            (game_dir / THINKER_SETUP_NAME).unlink()
            ix.say(f"  removed an unreadable {THINKER_SETUP_NAME}")
        except OSError:
            pass
    restored += revert_thinker_setup(game_dir, held, title="UNINSTALL FAILED")
    if had_setup and not thinker_setup_folder(game_dir):
        ix.say("  the installer's Thinker set-up of this folder is undone")
    hold = {n.lower() for n in held}
    for name in names:
        exe = game_dir / name
        bak = exe.with_suffix(exe.suffix + ".bak")
        if not (exe.is_file() and bak.is_file()) or name.lower() in hold:
            continue
        changed = bool(restored)         # honest once the first restore has landed
        if _md5_safe(bak, changed=changed) not in ours:
            foreign += 1
            ix.say(f"  {bak.name} is not a backup this installer made -- leaving {name} alone.")
            continue
        # COMPARE FIRST. Restoring unconditionally meant a second --uninstall copied the .bak
        # over an already-clean file, counted it, and reported a successful uninstall of
        # something that was not installed.
        if _md5_safe(exe, changed=changed) == _md5_safe(bak, changed=changed):
            already_clean += 1
            continue
        try:
            shutil.copy2(bak, exe)
            ix.say(f"  restored {name} from {bak.name}")
            restored += 1
        except OSError as e:
            raise ix.InstallerError(
                f"Could not restore {name} from its backup.", detail=f"  {e}", changed=True,
                remedy="close the game if it is running and try again.")

    removed, remove_failed = 0, 0
    for f in _uninstall_files():
        p = game_dir / f
        if not p.exists():
            continue
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            remove_failed += 1
            ix.say(f"  WARNING: could not remove {f}: {e}")

    # The Datalinks text files go back to the originals (their .longevity.bak copies) wherever ours is in place --
    # BEFORE the refusal below, so a folder whose exe and DLL were already cleaned by hand still gets its text back.
    text_restored = restore_datalinks_text(game_dir)
    if text_restored:
        ix.say(f"  restored {text_restored} Datalinks text file(s)")
        restored += text_restored

    # Nothing restored and nothing removed is a REFUSAL, whatever the receipt says.
    #
    # Gating this on the receipt let a stray or corrupt record stand in for real work: the run
    # printed "UNINSTALL COMPLETE -- 0 file(s) restored, 0 removed. The game is back to how it
    # was", which is both useless and untrue. The record is still cleared on the way out, so a
    # leftover one never persists and re-running stays idempotent.
    if not restored and not removed and not had_setup:
        rc.clear(game_dir, say=ix.say)
        config.forget_game_dir(game_dir)
        if foreign:
            msg = ("Nothing to uninstall: the backup beside your executable was not made by this "
                   "installer, so it was left alone.")
        elif already_clean:
            msg = "Nothing to uninstall: this copy of the game is already unmodified."
        else:
            msg = "Nothing to uninstall: no patched executable with a backup was found."
        raise ix.InstallerError(
            msg, code=ix.EXIT_ABORTED, changed=False,
            remedy="if you moved or deleted the .bak file, reinstall the game.")
    if removed:
        ix.say(f"  removed {removed} mod file(s)")
    # Thinker's video-player key goes back to "absent" (if still our blank), so Thinker re-detects
    # its player on the next launch as if the mod had never been there.
    release_in_process_movies(game_dir)
    # The HD clips, the resident updater and the [UIMod] preferences are deliberately LEFT: the
    # clips are a large download the player may want to keep, the updater is what an uninstall
    # run may itself be executing from, and the preferences are theirs, not our bookkeeping.
    # SAY SO, item by item -- an uninstall that leaves a folder behind without mentioning it
    # reads as a failed uninstall the next time the player looks.
    ix.say("\n  Left in place, deliberately:")
    ix.say("    movies\\hd            - the HD Cinematics (a large download; delete it yourself")
    ix.say("                           if you want the space back)")
    ix.say("    portraits\\           - your own portraits, and Turovetsky's HD Portrait Pack in")
    ix.say("                           portraits\\hd (unused once the mod is gone)")
    ix.say("    LongevityMods\\       - the resident updater and its staging folder; safe to")
    ix.say("                           delete once this window is closed")
    ix.say("    [UIMod] settings     - your preferences in Alpha Centauri.ini")
    rc.clear(game_dir, say=ix.say)
    if not remove_failed:
        # Kept while something is left to remove: the verdict asks for another --uninstall,
        # which has to find this folder again.
        config.forget_game_dir(game_dir)

    global _VERDICT
    # Say only what is true. "The game is back to how it was" contradicted the list of things
    # deliberately left in place printed directly above it -- and was printed unchanged even when
    # a removal had just warned that it failed.
    body = f"{restored} file(s) put back to the originals, {removed} removed."
    if remove_failed:
        body += (f" {remove_failed} file(s) could NOT be removed (see the warnings above) -- "
                 "close the game and run --uninstall again to finish.")
    if held:
        # An exe left to the player (see its warning above) may still carry an older build of
        # the mod: say so in the verdict, not in one console line above it.
        body += (f" {', '.join(held)} was left as it is (see the warning above): if it still starts "
                 "the game with the mod, replace it with the original yourself; GOG Galaxy's \"verify files\" restores it.")
    body += " The items listed above were deliberately left in place."
    _VERDICT = ("UNINSTALL COMPLETE" if not (remove_failed or held) else "UNINSTALL INCOMPLETE", body)
    ix.say(f"\n  {restored} file(s) restored, {removed} removed.")
    return ix.EXIT_OK


# ---- the game folder --------------------------------------------------------------------------
# A player never needs a command prompt to install: without --game-dir the installer lists the
# folders it found (the ones it installed into before, the GOG locations, every folder the GOG
# registry names), lets the player pick one or another, and -- when it found none -- asks for the
# folder in a Windows folder picker or as a typed path. Every choice is checked before anything is
# written; a non-interactive run never asks, it takes the first good folder or explains.

#: The folder holds a game build this mod installs onto (clean, or ours with its backup).
FOLDER_OK = "ok"


def _folder_exe_spec(d: Path, name: str):
    """The launch target `d/name` installs as -- from its own bytes, else from its .bak (an
    already-patched exe) -- or None. Never raises."""
    p = d / name
    try:
        beside = ap.folder_files(d)
        for probe in (p, p.with_suffix(p.suffix + ".bak")):
            if not probe.is_file():
                continue
            try:
                return ap.resolve_target(_md5(probe), targets=_targets(), launch_only=True,
                                         present_files=beside, exe_name=name)
            except ap.UnsupportedBase:
                continue
    except Exception:  # noqa: BLE001 - a probe is a verdict, never a crash
        return None
    return None


def _has_install(d: Path) -> bool:
    """Is the mod (or what is left of a part-finished uninstall) here: a receipt, the mod's DLL, a
    Thinker set-up record, or one of our backups beside an exe that differs from it? Not the
    resident updater, and not a .bak alone: an uninstall leaves both behind on purpose (it copies
    the backup back and keeps it)."""
    import receipt as rc
    try:
        state = rc.read(d)
        if state.get("target_id") or state.get("modules") or thinker_setup_folder(d) \
                or any((d / f).is_file() for f in _uninstall_files()):
            return True
        ours = None
        for n in _exe_names():
            exe, bak = d / n, d / (n + ".bak")
            if not (exe.is_file() and bak.is_file()):
                continue
            if ours is None:
                ours = restorable_base_hashes()
            b = _md5(bak)
            if b in ours and _md5(exe) != b:
                return True
        return False
    except Exception:  # noqa: BLE001 - a probe is a verdict, never a crash
        return False


def check_game_folder(d: Path, installed: bool = False):
    """(FOLDER_OK, label) when `d` is a folder the installer can work on, else (problem, the
    plain-words reason). `installed`: the mode needs the mod to be there already (uninstall,
    repair, update). Never raises for the folder itself: an access-denied folder (is_dir() still
    answers for one) or a dropped network share is a reason, not a crash."""
    try:
        return _check_game_folder(d, installed)
    except OSError as e:
        return "missing", f"that folder cannot be read ({e.strerror or e})"


def _check_game_folder(d: Path, installed: bool):
    try:
        if d.is_file():
            if d.suffix.lower() == ".lnk":
                return "file", "that is a shortcut; choose the game folder itself (the one with terranx.exe)"
            return "file", "that is a file, not a folder; choose the folder that holds terranx.exe"
        if not d.is_dir():
            return "missing", "there is no folder at that location"
    except OSError as e:
        return "missing", f"that folder cannot be read ({e.strerror or e})"
    names = [n for n in _exe_names() if (d / n).is_file()]
    if installed:
        if _has_install(d):
            return FOLDER_OK, "Longevity Vaccine is installed here"
        return "not-installed", "Longevity Vaccine is not installed in that folder"
    if not names:
        inner = []
        try:
            inner = [c for c in sorted(d.iterdir()) if c.is_dir()
                     and any((c / n).is_file() for n in _exe_names())]
        except OSError:
            pass
        if inner:
            return "nested", (f"the game is one folder further in -- choose \"{inner[0].name}\" "
                              f"inside it")
        return "no-game", ("Alpha Centauri is not in that folder: choose the folder that holds "
                           "terranx.exe")
    specs = [s for s in (_folder_exe_spec(d, n) for n in names) if s is not None]
    if not specs:
        return "unsupported", ("the Alpha Centauri there is not a version this mod supports: it "
                               "needs the GOG Planetary Pack (Alpha Centauri with Alien "
                               "Crossfire) with PRACX, on its own or with Thinker")
    label = specs[0].label
    ov = _thinker_overlay()
    if ov is not None and _thinker_here(d, ov):
        label = ov.label                  # the install sets a stock Thinker folder up for Thinker
    return FOLDER_OK, label + (" -- Longevity Vaccine is installed here" if _has_install(d) else "")


def browse_for_folder(title: str):
    """The Windows folder picker (IFileOpenDialog, FOS_PICKFOLDERS) in front of this console.
    The chosen folder, or None (cancelled, or no picker on this system). Never raises."""
    try:
        from ctypes import wintypes
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        hr_init = ole32.CoInitializeEx(None, 0x2 | 0x4)     # STA, no OLE1 DDE
        try:
            return _show_folder_dialog(ole32, title)
        finally:
            if hr_init in (0, 1):                           # S_OK / S_FALSE: ours to balance
                ole32.CoUninitialize()
    except Exception:  # noqa: BLE001 - the typed path is always there as the fallback
        return None


def _guid(text: str):
    import uuid
    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)


def _com_call(obj, index: int, restype, *args):
    """Call vtable slot `index` of the COM object `obj` (a c_void_p). Each arg is a ctypes value;
    an out-pointer is passed as `_ptr(x)`."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *[type(a) for a in args])
    return proto(vtbl[index])(obj, *args)


def _ptr(x) -> ctypes.c_void_p:
    return ctypes.c_void_p(ctypes.addressof(x))


# IFileDialog vtable slots (IUnknown 0-2, IModalWindow::Show 3, then IFileDialog in order).
_FD_SHOW, _FD_SETOPTIONS, _FD_GETOPTIONS, _FD_SETTITLE, _FD_GETRESULT, _RELEASE = 3, 9, 10, 17, 20, 2
_SI_GETDISPLAYNAME = 5                       # IShellItem::GetDisplayName
_FOS_PICKFOLDERS, _FOS_FORCEFILESYSTEM, _FOS_PATHMUSTEXIST = 0x20, 0x40, 0x800
_SIGDN_FILESYSPATH = 0x80058000 - (1 << 32)  # an enum: a signed int


def _show_folder_dialog(ole32, title: str):
    from ctypes import wintypes
    dlg = ctypes.c_void_p()
    clsid = _guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")    # FileOpenDialog
    iid = _guid("D57C7288-D4AD-4768-BE02-9D969532D960")      # IFileOpenDialog
    if ole32.CoCreateInstance(_ptr(clsid), None, 1, _ptr(iid), ctypes.byref(dlg)) != 0 or not dlg.value:
        return None
    try:
        opts = wintypes.DWORD()
        _com_call(dlg, _FD_GETOPTIONS, ctypes.HRESULT, _ptr(opts))
        _com_call(dlg, _FD_SETOPTIONS, ctypes.HRESULT,
                  wintypes.DWORD(opts.value | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST))
        _com_call(dlg, _FD_SETTITLE, ctypes.HRESULT, wintypes.LPCWSTR(title))
        k32 = ctypes.windll.kernel32
        k32.GetConsoleWindow.restype = ctypes.c_void_p
        try:
            # Owned by the console window: Show() disables its owner while it is up. Under Windows
            # Terminal that is the hidden pseudo-console window, which the terminal owns -- so the
            # picker still stacks above the terminal, and the terminal itself is never disabled
            # (its root owner as the owner froze every tab, and stayed frozen if the installer was
            # killed with the picker open: re-audit 2026-11-01).
            ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
            owner = k32.GetConsoleWindow() or ctypes.windll.user32.GetForegroundWindow()
            _com_call(dlg, _FD_SHOW, ctypes.HRESULT, ctypes.c_void_p(owner))
        except OSError:
            return None                                       # cancelled (HRESULT 0x800704C7)
        item = ctypes.c_void_p()
        _com_call(dlg, _FD_GETRESULT, ctypes.HRESULT, _ptr(item))
        try:
            name = ctypes.c_void_p()
            _com_call(item, _SI_GETDISPLAYNAME, ctypes.HRESULT, ctypes.c_int(_SIGDN_FILESYSPATH), _ptr(name))
            try:
                return Path(ctypes.wstring_at(name.value))
            finally:
                ole32.CoTaskMemFree(name)
        finally:
            _com_call(item, _RELEASE, ctypes.c_ulong)
    finally:
        _com_call(dlg, _RELEASE, ctypes.c_ulong)


def _no_folder_chosen(installed: bool) -> "ix.InstallerError":
    return ix.InstallerError("No game folder was chosen, so nothing was installed."
                             if not installed else "No game folder was chosen.",
                             code=ix.EXIT_ABORTED, changed=False,
                             remedy="run the installer again and choose the folder.")


def copy_error_summary(e: BaseException) -> str:
    """One readable line for a failed copy. shutil.copytree raises shutil.Error carrying a list
    of (source, destination, reason) for EVERY file it could not copy, and printing that dumped
    a screenful of paths (2026-11-04). The count, the first destination and its reason; plus a
    plain word when Windows refused a path for its length (WinError 206)."""
    errors = e.args[0] if isinstance(e, shutil.Error) and e.args and isinstance(e.args[0], list) else None
    if not errors:
        return str(e)
    first = errors[0]
    where, why = (first[1], first[2]) if isinstance(first, tuple) and len(first) == 3 else (first, "")
    text = (f"{len(errors)} file{'s' if len(errors) != 1 else ''} could not be copied, the first "
            f"{where}: {why}")
    if any("WinError 206" in str(x) for x in errors):
        text += "; some paths in the game folder are longer than Windows allows (260 characters)"
    return text


def _absolute(d) -> Path:
    """Absolute, so nothing later resolves it against another working directory (a shortcut's
    target, generation 2 started in the game folder, the profile API's %WINDIR%)."""
    return Path(os.path.abspath(str(d)))


def _arrow_menu(title: str, items: list, before=(), at: int = 0) -> int:
    """A ruled question block (ix.ask_block) whose rows the arrow keys move through: Up / Down move
    the marker over `items` -- each a list of printed lines, the marker on the first, which is
    shown green -- and Enter takes the one under it: its index. `before`: output lines printed
    above the block (why the list is what it is). Keys typed before the list was drawn are
    dropped. Ctrl-C raises KeyboardInterrupt."""
    def rows(at):
        out = []
        for i, lines in enumerate(items):
            out.append((" > " + ix.painted(lines[0], "good")) if i == at else ("   " + lines[0]))
            out += ["   " + ln for ln in lines[1:]]
        return out + [ix.RULE]
    shown = rows(at)
    _drop_typed_ahead()
    if before:
        ix.blank()
        for line in before:
            ix.say(line)
    ix.ask_block(title, [], shown, keys="Up/Down: move   Enter: choose")
    while True:
        key = _read_key()
        if key == "enter":
            return at
        nxt = max(0, at - 1) if key == "up" else min(len(items) - 1, at + 1) if key == "down" else at
        if nxt != at:
            at = nxt
            shown = ix.redraw(shown, rows(at))


def pick_game_folder(found: list, installed: bool, rejected: list = ()) -> Path:
    """Ask the player which folder to use: a list the arrow keys move through (user 2026-11-03) --
    each folder found, then Browse, then Quit -- and Enter takes the highlighted row. A folder is
    chosen from the list or in the Windows folder window; a typed or pasted path is not taken
    (user 2026-11-03). `found`: [(folder, label)] that already checked out; `rejected`: [(folder,
    reason)] that were looked at and do not. Loops until a folder checks out or the player quits;
    every rejection says why. Ctrl-C is "no folder chosen": nothing was written yet."""
    verb = "Use" if installed else "Install into"
    browse_title = "Choose your Alpha Centauri folder (the one with terranx.exe)"
    while True:
        if found:
            before = []
            items = [[f"{verb} {d}   ({label})"] for d, label in found]     # user 2026-11-04
            items.append(["Browse for a different folder"])
        else:
            if rejected:
                before = ["No folder the installer found can be used:"]
                before += [f"  Found {d}, but {why}." for d, why in rejected]
            elif installed:
                before = ["The installer could not find a game folder Longevity Vaccine is installed in."]
            else:
                before = ["The installer could not find Alpha Centauri on this PC by itself."]
            items = [["Choose the game folder in a window"]]
        items.append(["Quit"])
        try:
            pick = _arrow_menu("Choose the game folder", items, before)
        except KeyboardInterrupt:
            raise _no_folder_chosen(installed)
        if pick == len(items) - 1:
            raise _no_folder_chosen(installed)
        if pick < len(found):
            return found[pick][0]
        chosen = browse_for_folder(browse_title)
        if chosen is None:
            ix.say("  No folder chosen in the window.")
            continue
        chosen = _absolute(chosen)
        status, why = check_game_folder(chosen, installed=installed)
        if status == FOLDER_OK:
            ix.say(f"  {chosen}: {why}")
            return chosen
        ix.warn(f"  Not that folder: {why}.")
        ix.say("  Try again.")


def resolve_game_dir(args, installed: bool = False) -> Path:
    """The game folder this run works on, always absolute.

    --game-dir wins and is taken as given (scripted runs; its own checks follow). A resident
    updater works on the folder it lives in. Otherwise: the folders found on this PC, checked;
    an interactive run lets the player pick one or another, a non-interactive one takes the one
    good folder or stops with a reason a player can act on -- and with more than one install it
    does not guess which one an --uninstall / --update / --repair is meant for.
    """
    if getattr(args, "game_dir", None):
        d = _absolute(args.game_dir)
        try:
            if d.suffix.lower() == ".exe" and d.is_file():
                d = d.parent                    # --game-dir given the game's program: its folder
        except OSError:
            pass
        return d
    resident = config.resident_game_dir(_SCRIPTS)
    if resident is not None:
        return resident
    cands = config._dedup_dirs(config.remembered_game_dirs() + config.candidate_game_dirs())
    found, rejected = [], []
    for d in cands:
        d = _absolute(d)
        status, label = check_game_folder(d, installed=installed)
        (found if status == FOLDER_OK else rejected).append((d, label))
    if ix.can_ask() and not ix.is_worker() and not getattr(args, "no_prompt", False):
        return pick_game_folder(found, installed, rejected)
    if found and (len(found) == 1 or not installed):
        return found[0][0]
    flag = "--game-dir \"<the folder that holds terranx.exe>\""
    if found:
        raise ix.InstallerError(
            "Longevity Vaccine is installed in more than one game folder, so this run did not guess which one.",
            code=ix.EXIT_ABORTED, changed=False,
            detail="\n".join(f"  {d}" for d, _ in found),
            remedy=f"run it again from a console window and choose the folder when it asks, or pass {flag}.")
    if installed:
        raise ix.InstallerError(
            "No game folder with Longevity Vaccine installed could be found.",
            code=ix.EXIT_NO_GAME, changed=False,
            detail="\n".join(f"  Found {d}, but {why}." for d, why in rejected) or None,
            remedy=f"run it again from a console window and choose the folder when it asks, or pass {flag}.")
    raise ix.InstallerError(
        "The game folder could not be found." if not rejected else
        "No folder the installer found can be used.",
        code=ix.EXIT_NO_GAME, changed=False,
        detail=("\n".join(f"  Found {d}, but {why}." for d, why in rejected) if rejected else
                "  Alpha Centauri is not in any of the usual GOG locations on this PC."),
        remedy=f"start the installer by double-clicking INSTALL.bat and choose the game folder when it asks; a scripted run passes {flag}.")


def main() -> int:
    _load_modules()     # must precede banner(): it prints ver.VERSION
    if not ix.is_worker():
        banner()        # a worker child runs in the bootstrap's console, which printed it already
    apr = argparse.ArgumentParser(
        description="Longevity Vaccine installer (SMACX UI scaling + HD Cinematics)")
    apr.add_argument("--ai", choices=["on", "off", "ask"], default="ask",
                     help="Install the HD Cinematics pack? Default: list the optional modules "
                          "and ask. `--modules a,b` picks packs by name instead.")
    apr.add_argument("--payload-root", default=None,
                     help="Where the installer's own files live (default: derived from this "
                          "script's location, which works in a checkout and in an extracted "
                          "release archive alike).")
    apr.add_argument("--pack", default=None,
                     help="HD Cinematics pack dir (default: dist\\movies-hd).")
    apr.add_argument("--portrait-pack", default=None,
                     help="Turovetsky's HD Portrait Pack dir (default: dist\\portraits-hd).")
    apr.add_argument("--force", action="store_true",
                     help="Restore from .bak and re-patch even if already patched.")
    apr.add_argument("--game-dir", default=None, help="Override the game install dir.")
    apr.add_argument("--no-wait", action="store_true",
                     help="Do not wait for a keypress before exiting (for scripted runs). "
                          "The outcome is still always printed.")
    apr.add_argument("--update", action="store_true",
                     help="Manifest-driven update: preflight everything, then bring the core "
                          "patch and every installed module up to date.")
    apr.add_argument("--manifest", default=None, metavar="URL|PATH",
                     help="Where to read the update manifest from (default: the compiled-in "
                          "URL). A local path is allowed for testing.")
    apr.add_argument("--modules", default=None,
                     help="Comma-separated extras to add or re-install (default: the installed "
                          "ones that have a newer version). Extras: "
                          + ", ".join(f"{n} ({MODULE_LABELS.get(n, n)})"
                                      for n in GAME_CONTENT_MODULES) + ".")
    apr.add_argument("--only-listed", action="store_true",
                     help="(used by the game) with --update: fetch exactly the packs named by "
                          "--modules -- the ones the game's update prompt listed and the player "
                          "accepted -- and no other; none without --modules. The core is still "
                          "brought up to date when it is stale.")
    apr.add_argument("--offline", action="store_true",
                     help="Do not touch the network; use only what is already in staging.")
    apr.add_argument("--no-prompt", action="store_true",
                     help="Never ask anything: install every optional module on offer, set a "
                          "Thinker folder up when its game file is recognised, and create no "
                          "desktop shortcut and leave the game's resolution setting as it is "
                          "(each says how to do it yourself). --modules and --ai still choose "
                          "the modules when given.")
    apr.add_argument("--wait-pid", type=int, default=None, metavar="PID",
                     help="(used by the game) wait for this process -- the game that launched "
                          "the update -- to exit before checking or touching anything.")
    apr.add_argument("--uninstall", action="store_true",
                     help="Restore the original exe from .bak and remove the mod's files.")
    apr.add_argument("--repair", action="store_true",
                     help="Re-deploy the resident updater from this payload, without patching "
                          "anything. Use when <game>\\LongevityMods\\updater is corrupt.")
    apr.add_argument("--check", action="store_true",
                     help="(used by the game at launch) print what updates are available as "
                          "key=value lines and exit; installs nothing. Handled before main().")
    # Accept `--no-wait=1` as well as the bare flag. argparse rejects the `=` form for a
    # store_true, so a scripted run that wrote it had the wait suppressed (installer_exit scans
    # argv) and then aborted with a usage error -- the guarantee held while the install silently
    # did nothing. Rewrite the token to the bare flag BEFORE parsing; do not just drop it, or
    # installer_exit's argv scan loses the only thing it can see.
    argv = []
    for a in sys.argv[1:]:
        if a.startswith("--no-wait="):
            if a.split("=", 1)[1].strip().lower() in ("1", "true", "yes", "on", ""):
                argv.append("--no-wait")
            # an explicit false value simply drops out
        else:
            argv.append(a)
    sys.argv[1:] = argv

    args = apr.parse_args()
    if args.no_wait:
        os.environ["LONGEVITY_NO_WAIT"] = "1"
    if args.payload_root:
        # Exported rather than passed around, so every payload_root() caller agrees -- including
        # the ones reached from modules that never see `args`.
        os.environ["LONGEVITY_PAYLOAD_ROOT"] = str(Path(args.payload_root).resolve())

    # Every pairing, not just one. main() dispatches uninstall > repair > update in that order,
    # so an unchecked `--repair --update` silently did the repair and the verdict claimed only
    # that -- the player asked for two things and was told about one, with no word about the
    # other.
    modes = [n for n, on in (("--uninstall", args.uninstall), ("--repair", args.repair),
                             ("--update", args.update)) if on]
    if len(modes) > 1:
        raise ix.InstallerError(f"{' and '.join(modes)} cannot be combined.",
                                code=ix.EXIT_ABORTED, changed=False,
                                remedy="run one of them at a time.")
    if args.only_listed and not args.update:
        # It narrows an UPDATE's pack set; anywhere else it would be silently ignored.
        raise ix.InstallerError("--only-listed works only with --update.",
                                code=ix.EXIT_ABORTED, changed=False,
                                remedy="add --update, or leave --only-listed out.")
    # After the flag checks (a refused combination never asks for a folder), before the lock
    # (which is named after the folder).
    game_dir = resolve_game_dir(args, installed=bool(args.uninstall or args.repair or args.update))
    global _CRASH_LOG_GAME_DIR
    _CRASH_LOG_GAME_DIR = game_dir      # from here a crash log goes to LongevityMods\logs, with the rest
    ix.say(f"Game dir: {game_dir}")
    if not game_dir.is_dir():
        raise ix.InstallerError(
            "The game folder could not be found.",
            code=ix.EXIT_NO_GAME,
            detail=f"Looked for: {game_dir}",
            remedy="check the --game-dir path: it must be the folder that holds terranx.exe.")
    # Before the first write of every mode, and released before run_guarded offers to start the
    # game, so an update the game then offers is never refused by the window that is closing.
    lock = hold_installer_lock(game_dir)
    try:
        if args.uninstall:
            return do_uninstall(game_dir, args)
        if args.repair:
            return do_repair(game_dir)
        if args.update:
            return do_update(game_dir, args)
        return _install(game_dir, args)
    finally:
        lock.release()


def _install_candidates(game_dir: Path) -> list:
    """The exes the install pass patches: every configured name that is in the folder -- except
    that in a folder the installer set up for Thinker, only the exe Thinker starts (terranx_PRACX.exe
    is left as it is; see "setting a folder up for Thinker")."""
    candidates: list[Path] = []
    for name in _exe_names():
        p = game_dir / name
        if p.exists() and p not in candidates:
            candidates.append(p)
    ov = _thinker_overlay()
    if ov is not None and _setup_in_effect(game_dir):
        candidates = [p for p in candidates if p.name.lower() == ov.exe.lower()]
    return candidates


def _install(game_dir: Path, args) -> int:
    """The first-time install (and its re-runs): main() without a mode flag."""
    # Candidate exes come from the target table's `exe` fields plus the configured name -- no
    # hardcoded executable names here (CLAUDE.md's opening invariant), and a base that is not a
    # shipping target contributes no candidate.
    # Before the first write: a companion declared without its baseline is a packaging fault, and
    # companions are patched AFTER the exe -- discovering it then would mean aborting with the exe
    # already modified.
    check_companion_declarations()
    # A Thinker folder whose terranx.exe is not PRACX: decided now (no question), set up just before
    # the patch pass (after every question), so the pass patches the exe Thinker actually starts.
    thinker_plan = plan_thinker_setup(game_dir, args)
    candidates = _install_candidates(game_dir)
    if not candidates:
        raise ix.InstallerError(
            "No Alpha Centauri executable was found in the game directory.",
            code=ix.EXIT_NO_GAME,
            detail=f"Searched: {game_dir}",
            remedy="check that this is the folder containing terranx.exe.")
    # The backup rule for every other exe the pass will patch: refuses now (nothing written),
    # moves just before the pass.
    backup_plan = plan_backups(game_dir, candidates, thinker_plan)

    # The optional modules are chosen BEFORE anything is written: the player sees what is on
    # offer, with sizes, and the installer waits for the answer (user, 2026-09-27). The downloads
    # themselves happen after the game is patched.
    ix.say("\n--- optional modules ---")
    catalogue, manifest = module_catalogue(game_dir, args)
    chosen, turned_down, ticked_now = pick_modules(catalogue, args, game_dir)
    if args.ai == "off" and not args.modules:
        ix.say("  none requested")
    elif manifest is not None and not catalogue:
        ix.say("No optional modules are available for download right now.")
    # The update-check opt-in, with the other "what do you want" question, before any write.
    update_checks = ask_update_checks(game_dir, args)

    # The first writes: the spare backups out of the way, then the Thinker set-up; a set-up that
    # fails leaves the folder as it found it, moved backups included. Every planned .bak is hashed
    # again before the first of them, so a refusal never follows a drop.
    recheck_planned_backups(backup_plan, thinker_plan)
    moves = apply_backup_plan(backup_plan)
    try:
        if apply_thinker_setup(thinker_plan):
            candidates = _install_candidates(game_dir)
        elif thinker_plan and thinker_plan["action"] == "release":
            # The revert changed what terranx.exe is (the kept original back, or the clean PRACX
            # copy left): the rule for it runs on the folder as it is now. Its foreign shape was
            # refused at plan time, so this only moves or drops.
            candidates = _install_candidates(game_dir)
            moves += apply_backup_plan(plan_backups(game_dir, candidates, None))
    except BaseException:
        undo_backup_moves(moves)
        raise
    with fresh_patch_log(game_dir):
        results = {p.name: patch_target(p, args.force) for p in candidates}
        if any(v == "error" for v in results.values()):
            raise ix.InstallerError(
                "A patch step failed, so the installer stopped before deploying anything.",
                detail="\n".join(f"  {n}: {s}" for n, s in results.items()),
                # NOT changed=True. An "error" can come from apply_patches long before any write
                # (a missing patches.yaml, a missing target), and telling the user their exe may be
                # half-patched -- and to restore a .bak that does not exist -- is worse than saying
                # we do not know. None renders the honest "not certain how far it got" wording.
                changed=None,
                remedy="re-run the installer; if it keeps failing, report the output above.")
        if not any(v in ("patched", "already-patched") for v in results.values()):
            # Only MEASURED launch targets may be advertised. Listing a declared-but-unmeasured one
            # tells a Thinker user "Supported: ... Thinker ..." and then refuses their exe, which is
            # self-contradictory and unactionable.
            try:
                labels = [t.label for t in _targets().values() if t.launch and t.measured]
            except Exception:  # noqa: BLE001 - explaining a failure must not become a second failure
                labels = []
            raise ix.InstallerError(
                "No game executable this mod can patch was found in the game folder.",
                code=ix.EXIT_UNSUPPORTED,
                detail="The mod is built for: " + ", ".join(labels) + ".\n"
                       + "Found: " + ", ".join(f"{n} (MD5 {_md5_safe(game_dir / n)})"
                                               for n in results) + ".",
                remedy="install the GOG Planetary Pack with the PRACX patch, or report your "
                       "executable's MD5 so it can be added.")

        # Which target was actually applied. Resolved ONCE here, then used for the companions and the
        # receipt alike, so the two can never disagree about what is installed.
        # With two patched candidates (a Thinker folder that also keeps terranx_PRACX.exe) the one the
        # player's launcher runs -- the configuration with a `launcher` -- is the installation; the
        # candidate ORDER (config.exe_name()) used to decide, and on a machine whose game-exe.txt names
        # terranx_PRACX.exe the receipt, play note and shortcut all bypassed Thinker (audit 2026-10-29).
        patched, spec = None, None
        for n, s in results.items():
            if s not in ("patched", "already-patched"):
                continue
            try:
                bak = game_dir / (n + ".bak")
                probe = bak if bak.is_file() else game_dir / n
                cand = ap.resolve_target(_md5_safe(probe, changed=True), _targets(),
                                         launch_only=True, present_files=ap.folder_files(game_dir),
                                         exe_name=n)
            except (ap.UnsupportedBase, ix.InstallerError):
                cand = None
            if patched is None or (cand is not None and getattr(cand, "launcher", None)
                                   and not (spec is not None and getattr(spec, "launcher", None))):
                patched, spec = n, cand
        target_id = spec.id if spec else None
        # The update-check answer, the moment the exe is patched: from here the folder counts as installed
        # (_has_install), so a re-run would not ask again -- an answer lost to a later failure (a copy, a
        # download, Ctrl+C) would leave the game checking against a stated "no" (audit 2026-11-06). Every
        # refusal that says the game was not modified comes before this line.
        write_update_check(game_dir, update_checks)
        companion_results = patch_companions(game_dir, spec, args.force) if spec is not None else {}
    thinker_warned = warn_thinker_build(game_dir)

    if not deploy_runtime(game_dir):
        raise ix.InstallerError(
            "The game was patched, but copying the mod files into place failed.",
            changed=True,
            remedy="close the game if it is running and re-run the installer.")
    text_results = patch_datalinks_text(game_dir)

    # The chosen optional modules. A local pack (a checkout) is copied; the rest are downloaded
    # through the same verified path the update uses. A pack that fails never fails the install:
    # the mod is complete without it, and the game's update prompt offers it again.
    pack_status = {n: "skip" for n in GAME_CONTENT_MODULES}
    installed_packs = {}
    local = [n for n in chosen if catalogue.get(n, {}).get("source") == "local"]
    remote = [n for n in chosen if catalogue.get(n, {}).get("source") == "manifest"]
    for name in local:
        ix.say(f"\n--- {_pack_label(name)} ---")
        copy_pack = install_portrait_pack if name == "portraits-hd" else install_movie_pack
        if copy_pack(game_dir, catalogue[name]["path"]):
            pack_status[name] = "ok"
            installed_packs[name] = {"version": ver.module_version(name)}
        else:
            pack_status[name] = "error"
    if remote:
        got, statuses = install_packs_from_manifest(game_dir, args, remote, manifest, target_id,
                                                    game_dir / patched)
        installed_packs.update(got)
        pack_status.update(statuses)
    # NOT inferred from disk: a folder that happens to hold files is not an installed pack, and
    # recording one at the current version would hide it from the update prompt for good
    # (audit 2026-09-27). Content left by an uninstall is re-offered and, once the archive is
    # verified, install_modules skips every file that is already current.
    set_ini_flag(game_dir, cinematics_flag(chosen, game_dir, args, offered=list(catalogue)))
    record_module_choice(game_dir, sorted(set(ticked_now) | {n for n, s in pack_status.items() if s == "ok"}),
                         turned_down)
    ensure_in_process_movies(game_dir)
    failed_packs = [n for n in GAME_CONTENT_MODULES if pack_status.get(n) == "error"]

    # A PRACX install carried over from before may pin an old resolution: warn, offer the fix.
    # A first install's --no-prompt is the PLAYER's (only --update workers get the bootstrap's):
    # neither question is asked, and each prints how to do it by hand.
    asks = not getattr(args, "no_prompt", False)
    check_display_resolution(game_dir, target_id, ask=asks)
    # ... and the player needs a way to start the RIGHT exe.
    check_desktop_shortcut(game_dir, target_id, ask=asks)
    global _NOTE_ARGS
    _NOTE_ARGS = (game_dir, target_id)

    # Record what was installed. Until this existed, "which release is this?" had no answer on
    # the player's disk and an update could not tell an old patch set from a current one.
    # Make the installer RESIDENT. Without this the in-game update prompt has nothing to spawn
    # once the player deletes their download, and whatever they kept would be the version they
    # first installed -- while patches.yaml changes every release.
    # Said BEFORE the copy: it is the slowest step after the questions, and a silent console
    # right after the shortcut question read as a hang (user 2026-11-04).
    ix.say("")
    ix.say("Setting up the mod's updater in the game folder (this can take a minute)...")
    try:
        import bootstrap as bsmod
        bsmod.reconcile(game_dir, say=ix.say)
        # Quiet: "resident updater deployed: <long temp path> (v0.1.0, 8 component(s))" is
        # bookkeeping; a failure is reported below.
        bsmod.deploy(game_dir, payload_root(), ver.VERSION, say=lambda *a: None)
        if not bsmod.is_usable(bsmod.updater_dir(game_dir)):
            raise OSError("missing " + ", ".join(bsmod.missing_parts(bsmod.updater_dir(game_dir))))
    except Exception as e:  # noqa: BLE001 - a resident copy is not worth failing the install for
        ix.say(f"  WARNING: could not install the resident updater ({copy_error_summary(e)}).")
        ix.say("  The mod itself IS installed and playable, but this copy cannot update itself:")
        ix.say("  future updates will have to be downloaded and run by hand.")

    import receipt as rc
    # MERGE with what the receipt already says: a re-run used to replace the whole module map,
    # forgetting packs that are still on disk. And the pack is recorded at ITS version
    # (version.py MODULE_VERSIONS), not the product's -- recording it as VERSION made a later
    # pack release invisible once the product version got ahead of it.
    mods = dict(rc.read(game_dir).get("modules") or {})
    mods["base"] = {"version": ver.VERSION}
    mods.update(installed_packs)
    rc.write(game_dir, target_id=target_id, modules=mods, say=ix.warn)   # warnings only
    # So the next run -- an --uninstall, a re-run of this download or a newer one -- finds this
    # folder by itself, wherever the player put the game.
    config.remember_game_dir(game_dir)

    # One row per thing the player cares about, coloured; the executables the folder happens
    # to hold but the mod does not touch are not rows.
    ix.say("\n--- summary ---")
    for name, st in results.items():
        if st == "unknown":
            continue
        ix.status_line(name, "ok" if st in ("patched", "already-patched")
                       else ("error" if st == "error" else "skip"))
    for name, st in companion_results.items():
        ix.status_line(name, st)
    ix.status_line("mod files", "ok")
    if text_results:
        ix.status_line("Datalinks text", datalinks_text_status(text_results))
        if datalinks_text_note(text_results):
            ix.say(datalinks_text_note(text_results))
    if thinker_warned:
        # Said in full above; the row keeps it from scrolling out of sight under the summary.
        ix.status_line("Thinker Mod version", "skip")
        ix.warn("  (Thinker Mod version: not the supported build -- see the warning above)")
    for name in GAME_CONTENT_MODULES:
        # Only packs the player was offered or asked for: a row "Skipped" for a pack that was
        # never on the table (not released yet) says nothing.
        if name in catalogue or pack_status.get(name) != "skip":
            ix.status_line(_pack_label(name), pack_status.get(name, "skip"))
    if failed_packs:
        # Same shape as do_update's partial outcome: the mod IS installed, the pack the player
        # asked for is not, and a scripted caller must see a non-zero code for that.
        shown = " and ".join(_pack_label(n) for n in failed_packs)
        raise ix.InstallerError(
            f"The mod is installed and the game is ready to play. {shown} "
            f"{'was' if len(failed_packs) == 1 else 'were'} not installed.",
            code=ix.EXIT_ERROR, changed=True, title="INSTALLATION PARTLY COMPLETE",
            disk_note="The game is patched and ready to play; only the optional download is missing.",
            remedy="run the installer again, or take the pack from the game's update prompt.")

    # Remember where the game is so the guard can offer to launch it.
    global _LAUNCH_EXE
    # The program that plays WITH the mod: the target's launcher where it has one (thinker.exe --
    # starting terranx.exe directly would skip Thinker, right after the play note said otherwise;
    # audit 2026-10-28), else the exe we just patched. Never an exe we refused as unsupported.
    _LAUNCH_EXE = None
    if target_id and any(results.get(n) in ("patched", "already-patched") for n in _exe_names()):
        _LAUNCH_EXE = mod_exe(game_dir, target_id)
    if _LAUNCH_EXE is None:
        _LAUNCH_EXE = next((game_dir / n for n in _exe_names()
                            if (game_dir / n).exists()
                            and results.get(n) in ("patched", "already-patched")), None)
    return 0


#: Set by main() once the game dir is known, so run_guarded can offer a relaunch on success.
_LAUNCH_EXE: Path = None
_CRASH_LOG_GAME_DIR: Path = None   # set by main() once the game folder is resolved (crash_log_dir)


def crash_log_dir():
    """Where run_guarded writes install-crash-<stamp>.log: the game's LongevityMods\\logs once the folder is
    known (the folder a bug report is asked to attach), else None = beside the script (audit 2026-11-07: the
    default landed in the extracted download or in an updater generation the next update deletes)."""
    return _CRASH_LOG_GAME_DIR / "LongevityMods" / "logs" if _CRASH_LOG_GAME_DIR else None

#: (title, body) for the success verdict, when this run was not an install. Defaults to the
#: install wording.
_VERDICT = None


if __name__ == "__main__":
    # --check is the GAME's query over a pipe: it has its own guarantee (an exit code, always;
    # protocol lines or nothing on stdout) and must never print a banner or wait for a key.
    if "--check" in sys.argv[1:]:
        sys.exit(check_main())
    # EVERY other exit goes through run_guarded: it prints a verdict for success, expected
    # failure, Ctrl-C and unhandled exceptions alike, and waits for a keypress before the window
    # closes. Nothing here may call sys.exit() directly.
    sys.exit(ix.run_guarded(
        main,
        relaunch_exe=lambda: _LAUNCH_EXE,
        success_title=lambda: _VERDICT[0] if _VERDICT else None,
        success_body=lambda: _VERDICT[1] if _VERDICT else None,
        success_after=lambda: print_play_note(*_NOTE_ARGS) if _NOTE_ARGS else None,
        log_dir=crash_log_dir))
