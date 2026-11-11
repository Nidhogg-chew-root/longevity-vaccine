"""INSTALL.sh, the Linux / macOS entry point (work\\install.sh): finds the game and its Wine prefix and runs
INSTALL.bat under that Wine.

Every test runs the real script under bash with --dry-run against fake prefixes and a fake payload, so nothing
here needs Wine or touches a real prefix. On Windows it uses Git's bash (MSYS); WSL's bash.exe is skipped, since it
cannot see the temp folders by their Windows paths.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO / "work" / "install.sh"


def _bash():
    b = shutil.which("bash")
    if b and sys.platform == "win32" and Path(b).parent.name.lower() == "system32":
        git = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        b = str(git) if git.is_file() else None
    return b


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash")


def sh_path(p: Path) -> str:
    """The path as the script sees it: MSYS (/c/Users/...) under Git's bash, unchanged elsewhere."""
    if sys.platform != "win32":
        return str(p)
    s = p.resolve().as_posix()
    return f"/{s[0].lower()}{s[2:]}" if s[1] == ":" else s


def make_game(d: Path) -> Path:
    d.mkdir(parents=True)
    for n in ("terranx.exe", "terranx_PRACX.exe", "thinker.exe"):
        (d / n).write_bytes(b"MZ")
    return d


def make_prefix(d: Path) -> Path:
    (d / "drive_c" / "windows").mkdir(parents=True)
    (d / "system.reg").write_text("WINE REGISTRY Version 2\n")
    return d


@pytest.fixture
def payload(tmp_path) -> Path:
    p = tmp_path / "Longevity Vaccine 1.0.0"
    (p / "scripts").mkdir(parents=True)
    (p / "tools" / "python").mkdir(parents=True)
    (p / "INSTALL.bat").write_bytes(b"@echo off\r\n")
    (p / "scripts" / "run-install.bat").write_bytes(b"@echo off\r\n")
    (p / "tools" / "python" / "python.exe").write_bytes(b"MZ")
    shutil.copyfile(_SCRIPT, p / "INSTALL.sh")
    return p


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def wine(tmp_path) -> Path:
    w = tmp_path / "bin" / "wine"
    w.parent.mkdir()
    w.write_text("#!/bin/sh\nexit 0\n", newline="\n")
    w.chmod(0o755)
    return w


def run(payload: Path, home: Path, *args, env_extra=None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("WINEPREFIX", "WINE")}
    env["HOME"] = sh_path(home)
    env["XDG_DATA_HOME"] = sh_path(home / ".local" / "share")
    env.update(env_extra or {})
    return subprocess.run([BASH, sh_path(payload / "INSTALL.sh"), "--dry-run", *args],
                          capture_output=True, text=True, env=env, timeout=60)


def would_run(p: subprocess.CompletedProcess) -> list:
    line = next(ln for ln in p.stdout.splitlines() if ln.startswith("Would run:"))
    return [part.split("]")[0] for part in line.split(" [")[1:]]


def test_the_script_is_lf_and_bash():
    data = _SCRIPT.read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in data


def test_finds_a_gog_install_in_the_default_prefix(payload, home, wine):
    prefix = make_prefix(home / ".wine")
    game = make_game(prefix / "drive_c" / "GOG Games" / "Sid Meier's Alpha Centauri Planetary Pack")
    p = run(payload, home, "--wine", sh_path(wine))
    assert p.returncode == 0, p.stderr
    cmd = would_run(p)
    assert cmd[:3] == ["env", f"WINEPREFIX={sh_path(prefix)}", sh_path(wine)]
    assert cmd[3:6] == ["cmd", "/c", "call"]
    assert cmd[6] == "Z:" + sh_path(payload).replace("/", "\\") + "\\INSTALL.bat"
    assert cmd[7:] == ["--game-dir", "C:\\GOG Games\\Sid Meier's Alpha Centauri Planetary Pack"]
    assert f"Game:   {sh_path(game)}" in p.stdout


def test_finds_galaxy_layout_in_a_lutris_prefix_and_passes_installer_args(payload, home, wine):
    prefix = make_prefix(home / "Games" / "gog-galaxy")
    make_game(prefix / "drive_c" / "Program Files (x86)" / "GOG Galaxy" / "Games" / "Sid Meier's Alpha Centauri Planetary Pack")
    p = run(payload, home, "--wine", sh_path(wine), "--", "--uninstall", "--no-wait")
    assert p.returncode == 0, p.stderr
    cmd = would_run(p)
    assert cmd[1] == f"WINEPREFIX={sh_path(prefix)}"
    assert cmd[7:] == ["--game-dir", "C:\\Program Files (x86)\\GOG Galaxy\\Games\\Sid Meier's Alpha Centauri Planetary Pack",
                       "--uninstall", "--no-wait"]


def test_a_game_outside_the_prefix_needs_prefix_and_is_reached_through_z(payload, home, wine, tmp_path):
    game = make_game(home / "Games" / "Heroic" / "Sid Meiers Alpha Centauri")
    prefix = make_prefix(home / "Games" / "Heroic" / "Prefixes" / "default" / "Alpha Centauri")
    p = run(payload, home, "--wine", sh_path(wine), "--game-dir", sh_path(game))
    assert p.returncode == 3 and "--prefix" in p.stderr
    p = run(payload, home, "--wine", sh_path(wine), "--game-dir", sh_path(game), "--prefix", sh_path(prefix))
    assert p.returncode == 0, p.stderr
    assert would_run(p)[7:] == ["--game-dir", "Z:" + sh_path(game).replace("/", "\\")]


def test_wineprefix_from_the_environment_is_used(payload, home, wine):
    game = make_game(home / "Games" / "Heroic" / "SMAC")
    prefix = make_prefix(home / "pfx")
    p = run(payload, home, "--wine", sh_path(wine), "--game-dir", sh_path(game),
            env_extra={"WINEPREFIX": sh_path(prefix)})
    assert p.returncode == 0, p.stderr
    assert would_run(p)[1] == f"WINEPREFIX={sh_path(prefix)}"


def test_two_installs_are_refused_with_both_named(payload, home, wine):
    a = make_game(make_prefix(home / ".wine") / "drive_c" / "GOG Games" / "SMAC")
    b = make_game(make_prefix(home / ".local" / "share" / "bottles" / "bottles" / "Games") / "drive_c" / "GOG Games" / "SMAC")
    p = run(payload, home, "--wine", sh_path(wine))
    assert p.returncode == 4
    assert sh_path(a) in p.stderr and sh_path(b) in p.stderr


def test_nothing_found_explains_game_dir(payload, home, wine):
    p = run(payload, home, "--wine", sh_path(wine))
    assert p.returncode == 3 and "--game-dir" in p.stderr


def test_not_a_game_folder_is_refused(payload, home, wine, tmp_path):
    other = tmp_path / "Documents"
    other.mkdir()
    p = run(payload, home, "--wine", sh_path(wine), "--game-dir", sh_path(other))
    assert p.returncode == 3 and "does not look like Alpha Centauri" in p.stderr


def test_an_incomplete_archive_stops_before_anything_else(payload, home, wine):
    (payload / "scripts" / "run-install.bat").unlink()
    p = run(payload, home, "--wine", sh_path(wine))
    assert p.returncode == 9 and "Extract the WHOLE archive" in p.stderr


def test_no_wine_is_explained(payload, home, tmp_path):
    make_game(make_prefix(home / ".wine") / "drive_c" / "GOG Games" / "SMAC")
    empty = tmp_path / "emptybin"
    empty.mkdir()
    shell_dir = Path(BASH).parent
    p = run(payload, home, env_extra={"PATH": os.pathsep.join([sh_path(empty), sh_path(shell_dir), "/usr/bin", "/bin"])
                                       if sys.platform != "win32" else f"{sh_path(empty)}:/usr/bin:/bin"})
    assert p.returncode == 5 and "--wine" in p.stderr


def test_a_crossover_bottle_runs_through_the_bottle(payload, home, wine):
    bottle = make_prefix(home / "Library" / "Application Support" / "CrossOver" / "Bottles" / "SMAC Bottle")
    make_game(bottle / "drive_c" / "GOG Games" / "SMAC")
    p = run(payload, home, "--wine", sh_path(wine))
    assert p.returncode == 0, p.stderr
    cmd = would_run(p)
    assert cmd[:3] == [sh_path(wine), "--bottle", "SMAC Bottle"] and "env" not in cmd
    assert cmd[-2:] == ["--game-dir", "C:\\GOG Games\\SMAC"]


def test_desktop_entry_escapes_exec_and_points_at_thinker(payload, home, wine):
    prefix = make_prefix(home / ".wine")
    game = make_game(prefix / "drive_c" / "GOG Games" / "Alpha $Centauri 100%")
    p = run(payload, home, "--wine", sh_path(wine), "--desktop-entry")
    assert p.returncode == 0, p.stderr
    text = p.stdout.split("Would write ", 1)[1]
    assert text.startswith(sh_path(home / ".local" / "share" / "applications" / "longevity-vaccine-smacx.desktop"))
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("Exec="))
    game_exec = sh_path(game).replace("$", "\\\\$").replace("%", "%%")
    assert exec_line == (f'Exec=env "WINEPREFIX={sh_path(prefix)}" "{sh_path(wine)}" "{game_exec}/thinker.exe"')
    assert f"Path={sh_path(game)}" in text and "Categories=Game;" in text
