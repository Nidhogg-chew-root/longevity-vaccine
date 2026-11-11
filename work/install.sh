#!/usr/bin/env bash
# Longevity Vaccine - installer entry point for Linux and macOS (Wine, Lutris, Heroic, Bottles, Proton, CrossOver).
#
# The game is a Windows program, so on Linux and macOS it lives in a Wine prefix. This script finds the game and its
# prefix, then runs the same installer as Windows (INSTALL.bat, the bundled Python) under that prefix's Wine -- so the
# patching, backups, updates and uninstall are exactly the tested Windows path.
#
#   ./INSTALL.sh                               find the game, then install (asks what to add, as on Windows)
#   ./INSTALL.sh --game-dir DIR [--prefix P]   say where it is
#   ./INSTALL.sh --desktop-entry               also add a Linux menu entry that starts the game with Thinker
#   ./INSTALL.sh -- --uninstall                anything after -- goes to the installer (--uninstall, --ai off, ...)
#
# Bash 3.2 compatible (macOS). Exit codes: installer's own, or 2 usage, 3 game not found, 4 more than one game found,
# 5 no Wine, 9 incomplete archive.
set -u

HERE=$(cd "$(dirname "$0")" && pwd -P)
GAME_DIR=""
PREFIX="${WINEPREFIX:-}"
WINE_BIN="${WINE:-}"
BOTTLE=""
DESKTOP_ENTRY=0
DRY_RUN=0
PASS=()

die() { code=$1; shift; printf '\n  INSTALLATION FAILED\n  %s\n\n' "$*" >&2; exit "$code"; }
say() { printf '%s\n' "$*"; }

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --game-dir) [ $# -ge 2 ] || usage 2; GAME_DIR=$2; shift 2 ;;
        --prefix) [ $# -ge 2 ] || usage 2; PREFIX=$2; shift 2 ;;
        --wine) [ $# -ge 2 ] || usage 2; WINE_BIN=$2; shift 2 ;;
        --bottle) [ $# -ge 2 ] || usage 2; BOTTLE=$2; shift 2 ;;
        --desktop-entry) DESKTOP_ENTRY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage 0 ;;
        --) shift; PASS=("$@"); break ;;
        *) PASS+=("$1"); shift ;;
    esac
done

[ -f "$HERE/INSTALL.bat" ] && [ -f "$HERE/scripts/run-install.bat" ] && [ -f "$HERE/tools/python/python.exe" ] ||
    die 9 "This archive is incomplete. Extract the WHOLE archive, then run INSTALL.sh from the extracted folder."

# --- the game ------------------------------------------------------------------------------------------------------
is_game_dir() { [ -f "$1/terranx.exe" ] && { [ -f "$1/terranx_PRACX.exe" ] || [ -f "$1/thinker.exe" ] || [ -f "$1/Alpha Centauri.Ini" ]; }; }

# Where Wine front-ends keep prefixes and games. Searched only when --game-dir is not given.
search_roots() {
    printf '%s\n' \
        "$HOME/.wine" \
        "$HOME/Games" \
        "$HOME/.local/share/bottles/bottles" \
        "$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles" \
        "$HOME/.steam/steam/steamapps/compatdata" \
        "$HOME/.local/share/Steam/steamapps/compatdata" \
        "$HOME/Library/Application Support/CrossOver/Bottles"
    [ -n "$PREFIX" ] && printf '%s\n' "$PREFIX"
}

if [ -z "$GAME_DIR" ]; then
    found=""
    count=0
    while IFS= read -r root; do
        [ -d "$root" ] || continue
        while IFS= read -r exe; do
            d=$(cd "$(dirname "$exe")" && pwd -P)
            is_game_dir "$d" || continue
            case "
$found
" in *"
$d
"*) continue ;; esac
            found="$found
$d"
            count=$((count + 1))
        done <<EOF
$(find "$root" -maxdepth 9 -type f -name terranx.exe 2>/dev/null)
EOF
    done <<EOF
$(search_roots)
EOF
    [ "$count" -gt 0 ] || die 3 "Could not find Alpha Centauri (terranx.exe) in your Wine prefixes. Run again with
  --game-dir \"/path/to/Sid Meier's Alpha Centauri Planetary Pack\" (and --prefix if the game is outside a prefix)."
    if [ "$count" -gt 1 ]; then
        printf '\nFound more than one Alpha Centauri install:%s\n\nRun again with --game-dir and the one to patch.\n' \
            "$found" >&2
        exit 4
    fi
    GAME_DIR=$(printf '%s' "$found" | sed -n '2p')
fi
[ -d "$GAME_DIR" ] || die 3 "--game-dir: no such folder: $GAME_DIR"
GAME_DIR=$(cd "$GAME_DIR" && pwd -P)
is_game_dir "$GAME_DIR" || die 3 "$GAME_DIR does not look like Alpha Centauri (no terranx.exe beside terranx_PRACX.exe / thinker.exe)."

# --- the prefix ----------------------------------------------------------------------------------------------------
# The folder holding drive_c above the game, else --prefix / $WINEPREFIX (Heroic and Lutris may install the game
# outside the prefix, where Wine reaches it through Z:).
if [ -z "$PREFIX" ]; then
    d=$GAME_DIR
    while [ "$d" != "/" ] && [ -n "$d" ]; do
        if [ -d "$d/drive_c" ]; then PREFIX=$d; break; fi
        d=$(dirname "$d")
    done
fi
[ -n "$PREFIX" ] || die 3 "The game is not inside a Wine prefix. Run again with --prefix /path/to/prefix (the folder
  that holds drive_c; Lutris and Heroic show it in the game's settings)."
[ -d "$PREFIX/drive_c" ] || die 3 "--prefix: $PREFIX has no drive_c, so it is not a Wine prefix."
PREFIX=$(cd "$PREFIX" && pwd -P)

# Unix path -> the Windows path this prefix's Wine sees: C: for drive_c, Z: (the whole filesystem) otherwise.
to_win() {
    case "$1/" in
        "$PREFIX/drive_c/"*) rest=${1#"$PREFIX/drive_c"}; printf 'C:%s' "$(printf '%s' "${rest:-/}" | tr '/' '\\')" ;;
        *) printf 'Z:%s' "$(printf '%s' "$1" | tr '/' '\\')" ;;
    esac
}
GAME_WIN=$(to_win "$GAME_DIR")
PAYLOAD_WIN=$(to_win "$HERE")

# --- Wine ----------------------------------------------------------------------------------------------------------
CROSSOVER_WINE="/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"
case "$PREFIX" in
    */CrossOver/Bottles/*) [ -n "$BOTTLE" ] || BOTTLE=$(basename "$PREFIX")
                           [ -n "$WINE_BIN" ] || WINE_BIN=$CROSSOVER_WINE ;;
esac
if [ -z "$WINE_BIN" ]; then
    WINE_BIN=$(command -v wine 2>/dev/null || true)
fi
[ -n "$WINE_BIN" ] && { [ -x "$WINE_BIN" ] || command -v "$WINE_BIN" >/dev/null 2>&1; } ||
    die 5 "Wine was not found. Install Wine, or pass --wine /path/to/wine (Proton: \".../Proton x.y/files/bin/wine\";
  Lutris: ~/.local/share/lutris/runners/wine/<version>/bin/wine)."

if [ -n "$BOTTLE" ]; then
    RUN=("$WINE_BIN" --bottle "$BOTTLE")
else
    RUN=(env "WINEPREFIX=$PREFIX" "$WINE_BIN")
fi
# `call` first: cmd strips the outer quotes of a /c line that STARTS with a quote and holds more than two of them.
RUN+=(cmd /c call "$PAYLOAD_WIN\\INSTALL.bat" --game-dir "$GAME_WIN")
[ ${#PASS[@]} -gt 0 ] && RUN+=("${PASS[@]}")

say "Game:   $GAME_DIR"
say "Prefix: $PREFIX${BOTTLE:+ (CrossOver bottle \"$BOTTLE\")}"
say "Wine:   $WINE_BIN"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'Would run:'; printf ' [%s]' "${RUN[@]}"; printf '\n'
    rc=0
else
    "${RUN[@]}"
    rc=$?
fi

# --- optional Linux menu entry -------------------------------------------------------------------------------------
# Desktop Entry spec: in Exec, an argument in double quotes escapes " ` $ \ with a backslash, and every backslash is
# then doubled again by the file's own string escaping; % starts a field code, so a literal one is %%.
exec_arg() {
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\\\\\/g' -e 's/"/\\\\"/g' -e 's/`/\\\\`/g' \
                                          -e 's/\$/\\\\$/g' -e 's/%/%%/g')"
}
if [ "$DESKTOP_ENTRY" -eq 1 ] && [ "$rc" -eq 0 ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        say "--desktop-entry is for Linux; on macOS start thinker.exe from your CrossOver bottle."
    else
        apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
        entry="$apps/longevity-vaccine-smacx.desktop"
        if [ -n "$BOTTLE" ]; then
            exec_line="$(exec_arg "$WINE_BIN") --bottle $(exec_arg "$BOTTLE") $(exec_arg "$GAME_DIR/thinker.exe")"
        else
            exec_line="env $(exec_arg "WINEPREFIX=$PREFIX") $(exec_arg "$WINE_BIN") $(exec_arg "$GAME_DIR/thinker.exe")"
        fi
        content="[Desktop Entry]
Type=Application
Name=Alpha Centauri (Longevity Vaccine)
Comment=Sid Meier's Alpha Centauri: Alien Crossfire with Longevity Vaccine and Thinker
Exec=$exec_line
Path=$(printf '%s' "$GAME_DIR" | sed 's/\\/\\\\/g')
Terminal=false
Categories=Game;StrategyGame;"
        if [ "$DRY_RUN" -eq 1 ]; then
            printf 'Would write %s:\n%s\n' "$entry" "$content"
        else
            mkdir -p "$apps" && printf '%s\n' "$content" > "$entry" && say "Added menu entry: $entry"
        fi
    fi
fi

if [ "$rc" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    say ""
    say "To play: start thinker.exe in $GAME_DIR with this prefix's Wine -- set it as the game's executable in"
    say "Lutris / Heroic / Bottles, or run:  cd \"$GAME_DIR\" && WINEPREFIX=\"$PREFIX\" \"$WINE_BIN\" thinker.exe"
fi
exit "$rc"
