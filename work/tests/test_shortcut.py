"""The desktop shortcut offer and the closing note that names the exe carrying the mod.

Every test points LONGEVITY_DESKTOP_DIRS at temp folders: the real desktop is never read or
written here. Shortcuts are real .lnk files made through the same PowerShell/WScript path the
installer uses, so a created one is found back by the finder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import install as inst        # noqa: E402
import installer_exit as ix   # noqa: E402

inst._load_modules()


@pytest.fixture
def desk(tmp_path, monkeypatch):
    user, public = tmp_path / "Desktop", tmp_path / "Public Desktop"
    user.mkdir()
    public.mkdir()
    monkeypatch.setenv("LONGEVITY_DESKTOP_DIRS", os.pathsep.join([str(user), str(public)]))
    monkeypatch.setattr(inst, "_SHORTCUT_NAME", None)
    return user, public


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Game Folder"
    g.mkdir()
    (g / "terranx_PRACX.exe").write_bytes(b"MZ")
    (g / "terranx.exe").write_bytes(b"MZ")
    return g


def test_desktop_dirs_come_from_the_override(desk):
    assert inst._desktop_dirs() == [desk[0], desk[1]]


def test_the_shortcut_name_is_a_legal_file_name():
    assert not any(c in inst.MOD_SHORTCUT_NAME for c in '<>:"/\\|?*'), "a shortcut's name is its file name"


def test_create_then_find(desk, game):
    exe = game / "terranx_PRACX.exe"
    assert inst.find_shortcut_to(exe) is None
    assert inst.create_shortcut(desk[1] / "pracx.lnk", exe) == ""
    assert inst.find_shortcut_to(exe) == "pracx"                  # the Public Desktop counts too
    assert inst.find_shortcut_to(game / "terranx.exe") is None    # a different target does not
    assert ("pracx", str(exe)) in inst.list_shortcuts()


def test_the_shortcut_shows_the_games_icon_not_a_launchers(desk, game):
    """User 2026-11-06: a Thinker folder's shortcut starts thinker.exe, whose icon is a plain "Mod"
    square; the shortcut takes the game exe's icon instead (terranx.exe, the exe the launcher
    starts). A plain PRACX target keeps its own icon, which IS the game's."""
    (game / "thinker.exe").write_bytes(b"MZ")
    assert inst.shortcut_icon(game, "pracx", game / "terranx_PRACX.exe") == game / "terranx_PRACX.exe"
    assert inst.shortcut_icon(game, None, game / "terranx_PRACX.exe") == game / "terranx_PRACX.exe"
    assert inst.shortcut_icon(game, "pracx_thinker", game / "thinker.exe") == game / "terranx.exe"
    (game / "terranx.exe").unlink()
    assert inst.shortcut_icon(game, "pracx_thinker", game / "thinker.exe") == game / "thinker.exe"
    (game / "terranx.exe").write_bytes(b"MZ")
    # ...and the written .lnk carries it: target thinker.exe, icon terranx.exe
    lnk = desk[0] / "Thinker test.lnk"
    assert inst.create_shortcut(lnk, game / "thinker.exe", inst.shortcut_icon(game, "pracx_thinker", game / "thinker.exe")) == ""
    out = inst._ps("$sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut(" + inst._ps_quote(lnk) + "); "
                   "Write-Output ('T=' + $s.TargetPath); Write-Output ('I=' + $s.IconLocation)")
    assert f"T={game / 'thinker.exe'}" in out and f"I={game / 'terranx.exe'},0" in out, out


def _lnk_icon(lnk):
    return inst._ps("$sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut(" + inst._ps_quote(lnk) + "); "
                    "Write-Output ('I=' + $s.IconLocation)").strip()


def test_the_installer_writes_and_refreshes_its_own_shortcuts_icon(desk, game, monkeypatch, capsys):
    """End to end through check_desktop_shortcut for a Thinker target: a new shortcut gets the game
    exe's icon, and one that already exists with the launcher's icon (made before 2026-11-06) is
    brought up to date; a shortcut under the player's own name is left alone."""
    (game / "thinker.exe").write_bytes(b"MZ")
    monkeypatch.setattr(ix, "can_ask", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    inst.check_desktop_shortcut(game, "pracx_thinker")
    ours = desk[0] / (inst.MOD_SHORTCUT_NAME + ".lnk")
    assert ours.is_file() and _lnk_icon(ours) == f"I={game / 'terranx.exe'},0"
    capsys.readouterr()
    # the old rule's shortcut: same target, the launcher's icon
    assert inst.create_shortcut(ours, game / "thinker.exe") == ""
    assert _lnk_icon(ours) == f"I={game / 'thinker.exe'},0"
    monkeypatch.setattr(inst, "_SHORTCUT_NAME", None)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("an existing shortcut asks nothing"))
    inst.check_desktop_shortcut(game, "pracx_thinker")
    assert _lnk_icon(ours) == f"I={game / 'terranx.exe'},0"
    assert "You do not have a desktop shortcut" not in capsys.readouterr().out
    # the player's own shortcut to the same exe: found, never rewritten
    mine = desk[1] / "My Thinker.lnk"
    ours.unlink()
    assert inst.create_shortcut(mine, game / "thinker.exe") == ""
    monkeypatch.setattr(inst, "_SHORTCUT_NAME", None)
    inst.check_desktop_shortcut(game, "pracx_thinker")
    assert _lnk_icon(mine) == f"I={game / 'thinker.exe'},0"


def test_mod_exe_prefers_the_targets_exe(game):
    assert inst.mod_exe(game, "pracx").name == "terranx_PRACX.exe"
    assert inst.mod_exe(game, None) is not None
    assert inst.mod_exe(game / "missing", "pracx") is None


def test_yes_creates_the_named_shortcut_and_the_note_names_it(desk, game, monkeypatch, capsys):
    monkeypatch.setattr(ix, "can_ask", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    inst.check_desktop_shortcut(game, "pracx")
    out = capsys.readouterr().out
    assert f"You do not have a desktop shortcut that starts the game with the mod.\n\n{ix.RULE}\n" \
           f" DESKTOP SHORTCUT\n{ix.RULE}\n" in out
    assert asked == [" Create a desktop shortcut that starts the game with the Longevity Vaccine mod? [Y/n] "]
    assert f'Created "{inst.MOD_SHORTCUT_NAME}" on your desktop.' in out
    lnk = desk[0] / (inst.MOD_SHORTCUT_NAME + ".lnk")
    assert lnk.is_file() and inst.find_shortcut_to(game / "terranx_PRACX.exe") == inst.MOD_SHORTCUT_NAME
    inst.print_play_note(game, "pracx")
    note = capsys.readouterr().out.rstrip("\n").split("\n")
    assert note[0] == "To play with the mod, start terranx_PRACX.exe."
    assert note[1].startswith("The GOG Galaxy Play button") and "\n" not in note[1]
    assert "Alien Crossfire" not in "\n".join(note)
    assert note[2] == f'Your desktop shortcut "{inst.MOD_SHORTCUT_NAME}" starts the right one.'
    assert note[-1] == inst.ENJOY_LINE


def test_a_first_installs_no_prompt_asks_nothing_and_makes_no_shortcut(desk, game, monkeypatch, capsys):
    """The PLAYER's --no-prompt ("never ask anything"): the hint only, even with a keyboard."""
    monkeypatch.setattr(ix, "can_ask", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask"))
    inst.check_desktop_shortcut(game, "pracx", ask=False)
    assert "Run the installer again to create one" in capsys.readouterr().out
    assert not (desk[0] / (inst.MOD_SHORTCUT_NAME + ".lnk")).exists()


def test_no_leaves_the_desktop_alone(desk, game, monkeypatch, capsys):
    monkeypatch.setattr(ix, "can_ask", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    inst.check_desktop_shortcut(game, "pracx")
    assert "No shortcut created. Start terranx_PRACX.exe from the game folder" in capsys.readouterr().out
    assert not list(desk[0].glob("*.lnk")) and not list(desk[1].glob("*.lnk"))
    inst.print_play_note(game, "pracx")
    assert "Your desktop shortcut" not in capsys.readouterr().out


def test_an_existing_shortcut_means_no_question(desk, game, monkeypatch, capsys):
    exe = game / "terranx_PRACX.exe"
    assert inst.create_shortcut(desk[0] / "My SMAC.lnk", exe) == ""
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask"))
    inst.check_desktop_shortcut(game, "pracx")
    assert capsys.readouterr().out == ""
    inst.print_play_note(game, "pracx")
    assert 'Your desktop shortcut "My SMAC" starts the right one.' in capsys.readouterr().out


def test_without_a_keyboard_it_only_explains(desk, game, monkeypatch, capsys):
    monkeypatch.setattr(ix, "can_ask", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask"))
    inst.check_desktop_shortcut(game, "pracx")
    out = capsys.readouterr().out
    assert "You do not have a desktop shortcut" in out
    assert "Run the installer again to create one, or start terranx_PRACX.exe from the game folder." in out
    assert not list(desk[0].glob("*.lnk"))


def test_a_foreign_shortcut_with_our_name_is_replaced_only_on_yes(desk, game, monkeypatch, capsys):
    ours = desk[0] / (inst.MOD_SHORTCUT_NAME + ".lnk")
    assert inst.create_shortcut(ours, game / "terranx.exe") == ""    # wrong target, our name
    monkeypatch.setattr(ix, "can_ask", lambda: True)
    answers = iter(["", "n"])                                        # create? yes; replace? no
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    inst.check_desktop_shortcut(game, "pracx")
    out = capsys.readouterr().out
    assert "already exists but starts something else" in out and "Left as it is." in out
    assert inst.find_shortcut_to(game / "terranx.exe") == inst.MOD_SHORTCUT_NAME
    answers = iter(["", ""])                                         # create? yes; replace? yes
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    inst.check_desktop_shortcut(game, "pracx")
    assert inst.find_shortcut_to(game / "terranx_PRACX.exe") == inst.MOD_SHORTCUT_NAME


def test_windows_is_not_mistaken_for_wine(monkeypatch):
    monkeypatch.delenv("LONGEVITY_ASSUME_WINE", raising=False)
    assert inst.running_under_wine() is (hasattr(__import__("ctypes").windll.ntdll, "wine_get_version"))
    monkeypatch.setenv("LONGEVITY_ASSUME_WINE", "1")
    assert inst.running_under_wine() is True
    monkeypatch.setenv("LONGEVITY_ASSUME_WINE", "0")
    assert inst.running_under_wine() is False


def test_under_wine_no_shortcut_is_offered_or_written(desk, game, monkeypatch, capsys):
    """Wine has no PowerShell to write the .lnk with, and a Linux / Mac player starts the game from Lutris,
    Heroic, Bottles or CrossOver: say so instead of asking, and leave the prefix's desktop alone."""
    monkeypatch.setenv("LONGEVITY_ASSUME_WINE", "1")
    (game / "thinker.exe").write_bytes(b"MZ")
    monkeypatch.setattr(ix, "can_ask", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask under Wine"))
    inst.check_desktop_shortcut(game, "pracx_thinker")
    out = capsys.readouterr().out
    assert "Running under Wine: no Windows desktop shortcut. Start thinker.exe from your game launcher" in out
    assert "INSTALL.sh --desktop-entry" in out
    assert not list(desk[0].glob("*.lnk")) and not list(desk[1].glob("*.lnk"))
    inst.print_play_note(game, "pracx_thinker")
    assert "Your desktop shortcut" not in capsys.readouterr().out


def test_the_note_for_a_non_pracx_target_stays_generic(desk, game, capsys):
    inst.print_play_note(game, "vanilla")
    out = capsys.readouterr().out
    assert "To play with the mod, start" in out and "PRACX patch" not in out
    assert out.rstrip("\n").endswith(inst.ENJOY_LINE)


def test_every_note_line_is_a_paragraph(desk, game, capsys):
    for target in ("pracx", "vanilla"):
        inst.print_play_note(game, target)
        for ln in capsys.readouterr().out.rstrip("\n").split("\n"):
            assert ln.endswith((".", "!")), ln
