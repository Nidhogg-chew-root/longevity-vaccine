// uimod.dll entry point — Init().
//
// This is what the bootstrap loader calls once the DLL is in the process. There are two
// ways we get loaded, decided at PATCH TIME by which target binary we patch (patch group
// `uimod_bootstrap` in work\patches\patches.yaml):
//
//   * PRACX exe  (terranx_PRACX.exe, current target): our loader thunk is CHAINED off the
//     end of PRACX's own loader stub, so Init() runs immediately after PRACX init returns.
//   * native exe (vanilla terranx.exe): no PRACX stub exists, so we install our OWN loader
//     stub the way PRACX does -- write a "uimod" loader into the free _SELFMOD slot at
//     0x668160 and point the null CRT initializer-table entry at 0x682000 to it, so _initterm
//     calls it at startup. See patches.yaml group `uimod_bootstrap_native` (--pristine apply).
//
// Either way the DLL artifact and this Init() are identical. The "is PRACX present?" that
// matters HERE is a RUNTIME one: GetModuleHandle("prax.dll") tells us whether to cooperate
// with PRACX (read its scale state / hook on top of it) or run standalone on native.
//
// Calling convention is load-bearing: the loader does `push hModule; call Init; ...; ret`,
// so Init MUST be __stdcall (it pops its own argument). Exported as ordinal 1 + name "Init"
// via uimod.def so either an ordinal-style (PRACX-mimicking) or by-name loader can reach it.

#include <windows.h>
#include <cstdio>
#include <cstring>

#include "aivis.h"
#include "debugdump.h"
#include "dragdelay.h"
#include "modernctl.h"
#include "movie.h"
#include "praxdrag.h"
#include "mapscroll.h"
#include "portrait.h"
#include "prefs.h"
#include "pssphere.h"
#include "musicfix.h"
#include "riotfix.h"
#include "terrainhd.h"
#include "blinkfix.h"
#include "cursorscale.h"
#include "hudmovesfix.h"
#include "thinkercompat.h"
#include "thinkeraivis.h"
#include "thinkertext.h"
#include "thinkerversion.h"
#include "hudsync.h"
#include "viewdragfix.h"
#include "rowindent.h"
#include "techwin.h"
#include "update.h"
#include "version.h"
#include "vidmode.h"
#include "wheel.h"
#include "interludekey.h"
#include "lanjoin.h"
#include "logredact.h"
#include "mptimefix.h"

namespace {

HMODULE g_self = nullptr;

} // namespace

// Append a line to uimod.log next to the DLL (game dir) AND mirror to the debugger.
// A file makes the load verifiable without DebugView; the path is derived from our own
// module handle so it lands beside uimod.dll regardless of the process working directory.
// Non-static: feature modules (prefs.cpp) log through this too.
void UimodLog(const char* msg)
{
    // Players attach this log to public bug reports: a path under their profile folder would name their Windows
    // account, so it reads %USERPROFILE% in every line from every module (logredact.h).  The profile is a plain
    // buffer filled once (nothing to destroy at detach), and UimodLog never throws: callers include out-of-memory
    // handlers and worker threads, so a line that cannot be redacted is dropped, never written as is.
    static char profile[MAX_PATH];
    static INIT_ONCE profileOnce = INIT_ONCE_STATIC_INIT;
    InitOnceExecuteOnce(&profileOnce, [](PINIT_ONCE, PVOID, PVOID*) -> BOOL {
        const DWORD n = GetEnvironmentVariableA("USERPROFILE", profile, MAX_PATH);
        if (n == 0 || n >= MAX_PATH) profile[0] = '\0';
        return TRUE;
    }, nullptr, nullptr);
    std::string redacted;
    try {
        redacted = logredact::RedactProfile(msg ? msg : "", profile);
    } catch (...) {
        return;
    }
    msg = redacted.c_str();

    OutputDebugStringA(msg);

    char path[MAX_PATH];
    DWORD n = g_self ? GetModuleFileNameA(g_self, path, MAX_PATH) : 0;
    if (n == 0 || n >= MAX_PATH) return;
    char* slash = nullptr;
    for (char* p = path; *p; ++p)
        if (*p == '\\' || *p == '/') slash = p;
    if (!slash) return;
    lstrcpyA(slash + 1, "uimod.log");

    // Every line carries local time (ms) and the process id: a log appended over many launches (and, since the
    // display watchdog and the GPU device worker, from several threads) has to say which run and when.
    SYSTEMTIME t;
    GetLocalTime(&t);
    char stamp[48];
    _snprintf_s(stamp, sizeof stamp, _TRUNCATE, "%04u-%02u-%02u %02u:%02u:%02u.%03u [%lu] ", t.wYear, t.wMonth, t.wDay,
                t.wHour, t.wMinute, t.wSecond, t.wMilliseconds, GetCurrentProcessId());

    // One writer at a time (the game thread, the watchdog, the update / prewarm / device workers).
    static SRWLOCK lock = SRWLOCK_INIT;
    AcquireSRWLockExclusive(&lock);

    // Bounded growth: the log is appended forever (about 240 B per launch from the update
    // checker alone, more from the rest of Init). At the first line of a process, if it is over 1 MB,
    // keep it as uimod.log.old and start afresh; within one process, again whenever this process has
    // appended 4 MB more (a long session's display / debug lines), so the pair stays under ~10 MB.
    static bool rotated = false;
    static size_t written = 0;
    if (!rotated || written > (4u << 20)) {
        WIN32_FILE_ATTRIBUTE_DATA fa;
        const bool big = GetFileAttributesExA(path, GetFileExInfoStandard, &fa) &&
                         (fa.nFileSizeHigh || fa.nFileSizeLow > (rotated ? (4u << 20) : (1u << 20)));
        if (big) {
            char old[MAX_PATH + 8];
            lstrcpyA(old, path);
            lstrcatA(old, ".old");
            MoveFileExA(path, old, MOVEFILE_REPLACE_EXISTING);
        }
        rotated = true;
        written = 0;
    }

    // A line built from several calls is assembled per THREAD and written,
    // stamped once, only when it is complete: two threads' partial lines can no longer interleave.
    thread_local char part[1024];
    thread_local size_t partLen = 0;
    if (FILE* f = std::fopen(path, "a")) {
        for (const char* p = msg; *p;) {
            const char* nl = std::strchr(p, '\n');
            const size_t seg = nl ? static_cast<size_t>(nl - p + 1) : std::strlen(p);
            if (partLen + seg >= sizeof part || nl) {             // complete (or too long to hold): out it goes
                std::fputs(stamp, f);
                std::fwrite(part, 1, partLen, f);
                std::fwrite(p, 1, seg, f);
                if (!nl) std::fputc('\n', f);
                written += std::strlen(stamp) + partLen + seg;
                partLen = 0;
            } else {
                std::memcpy(part + partLen, p, seg);
                partLen += seg;
            }
            p += seg;
        }
        std::fclose(f);
    }
    ReleaseSRWLockExclusive(&lock);
}

// Developer diagnostics: geometry dumps, per-row layout, settings changes.  Written only with [UIMod] DebugLog=1
// in Alpha Centauri.ini (a player never has it), so a player's uimod.log stays to what helps a bug report:
// load / install results, warnings, failures.  Read once per process.
bool UimodDebugLogOn()
{
    static int on = -1;
    if (on < 0) {
        char ini[MAX_PATH];
        DWORD n = GetModuleFileNameA(nullptr, ini, MAX_PATH);
        char* slash = nullptr;
        for (char* p = ini; n && *p; ++p)
            if (*p == '\\' || *p == '/') slash = p;
        if (!slash) return false;
        lstrcpyA(slash + 1, "Alpha Centauri.ini");
        on = GetPrivateProfileIntA("UIMod", "DebugLog", 0, ini) != 0 ? 1 : 0;
    }
    return on != 0;
}

void UimodDbg(const char* msg)
{
    if (UimodDebugLogOn()) UimodLog(msg);
}

namespace {

// True iff PRACX (prax.dll) is already loaded in this process. Decides whether we cooperate
// with PRACX's runtime state or behave as the standalone UI layer on native Alpha Centauri.
bool PracxPresent()
{
    return GetModuleHandleA("prax.dll") != nullptr;
}

// Alpha Centauri.ini beside the game exe.
bool GameIniPath(char* out, size_t n)
{
    DWORD len = GetModuleFileNameA(nullptr, out, static_cast<DWORD>(n));
    if (len == 0 || len >= n) return false;
    char* slash = nullptr;
    for (char* p = out; *p; ++p)
        if (*p == '\\' || *p == '/') slash = p;
    if (!slash || static_cast<size_t>(slash + 1 - out) + sizeof "Alpha Centauri.ini" > n) return false;
    lstrcpyA(slash + 1, "Alpha Centauri.ini");
    return true;
}

// System-DPI awareness for the process, set before any window exists ([UIMod] DpiAware, default 1).
// Without it, at a Windows scaling other than 100% the game sees a logical (scaled-down) monitor:
// PRACX's window comes out larger than that desktop and a native-size Fullscreen classifies as
// non-native. This is what the HIGHDPIAWARE compatibility flag does, without a registry entry per
// install. Resolved at run time so the DLL has no import a pre-Vista user32 would lack. Once.
void EnsureDpiAware()
{
    static bool done = false;
    if (done) return;
    done = true;
    char ini[MAX_PATH];
    if (GameIniPath(ini, sizeof ini) && GetPrivateProfileIntA("UIMod", "DpiAware", 1, ini) == 0) {
        UimodLog("[uimod] DPI: [UIMod] DpiAware=0 -> left to Windows\n");
        return;
    }
    typedef BOOL (WINAPI *FSetAware)();
    const HMODULE user32 = GetModuleHandleA("user32.dll");
    const FSetAware set = user32 ? reinterpret_cast<FSetAware>(GetProcAddress(user32, "SetProcessDPIAware")) : nullptr;
    if (!set) { UimodLog("[uimod] DPI: SetProcessDPIAware unavailable\n"); return; }
    UimodLog(set() ? "[uimod] DPI: process set system-DPI-aware\n"
                   : "[uimod] DPI: awareness already set (compatibility flag or manifest)\n");
}

// No ghost window for the game. Windows calls a window "not responding" after 5 s without a message pump, and a
// late-game AI turn runs that long on the game thread while it keeps drawing; Windows would then swap the game for a
// frozen ghost copy. Games turn this off (DDrawCompat's ddraw.dll in a PRACX install carries the same call). Before
// any window exists; resolved at run time like SetProcessDPIAware. Once.
void EnsureNoGhosting()
{
    static bool done = false;
    if (done) return;
    done = true;
    typedef void (WINAPI *FNoGhost)();
    const HMODULE user32 = GetModuleHandleA("user32.dll");
    const FNoGhost off = user32 ? reinterpret_cast<FNoGhost>(GetProcAddress(user32, "DisableProcessWindowsGhosting")) : nullptr;
    if (!off) { UimodLog("[uimod] ghosting: DisableProcessWindowsGhosting unavailable\n"); return; }
    off();
    UimodLog("[uimod] ghosting: off (a busy game is never swapped for a 'not responding' copy)\n");
}

bool g_preInitRan = false;

} // namespace

// Bootstrap phase one (thunk_uimod_preload, group uimod_bootstrap): prax.dll is loaded, its ordinal-1
// init (PRACXHook) has NOT run. Whatever that init does irreversibly is headed off here: its boot
// display-mode switch (vidmode) and the process DPI awareness (set before PRACX reads the screen).
// An exe baked before the two-phase chain never calls this; Init then does what it still can.
extern "C" void __stdcall PreInit(HMODULE prax)
{
    if (!g_self)
        GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           reinterpret_cast<LPCSTR>(&PreInit), &g_self);
    g_preInitRan = true;
    UimodLog("[uimod] PreInit: before PRACX init\n");
    EnsureDpiAware();
    EnsureNoGhosting();
    VidModePreInit(prax);
}

extern "C" void __stdcall Init(HMODULE self)
{
    g_self = self;
    UimodLog("[uimod] Init: v" UIMOD_VERSION_STRING "\n");
    if (g_preInitRan) UimodLog("[uimod] Init: PreInit ran (two-phase bootstrap)\n");
    EnsureDpiAware();   // a no-op after PreInit; the one-phase chain (older exe) sets it here, still before any window
    EnsureNoGhosting(); // likewise

    const bool pracx = PracxPresent();
    if (pracx) {
        UimodLog("[uimod] Init: PRACX detected -> cooperative mode\n");
    } else {
        UimodLog("[uimod] Init: no PRACX -> standalone mode (native Alpha Centauri)\n");
    }

    // PRACX + Thinker: Thinker (thinker.dll) has already verified and patched the image in memory. The sites the
    // patcher left clean on disk for it go in now, the clobbered ones come back, and the two base-screen thunks are
    // chained into Thinker's functions -- FIRST, so every later module byte-checks the final bytes. Without Thinker
    // this still writes the sites a Thinker-folder exe started directly would otherwise lack. See thinkercompat.cpp.
    InstallThinkerCompat();

    // A thinker.dll that is not the build this release is pinned to: one warning dialog (OK / Don't Show Again),
    // before any window, in the installer's words. The pinned modules below still disable themselves on their own
    // checks; this only tells the player. See thinkerversion.cpp.
    ThinkerVersionWarning();

    // Thinker's own base-screen lines (satellite bonuses, stockpile, pop size, nerve staple) re-anchored at our scale:
    // its two text-writer function pointers are swapped for caller-aware wrappers. A no-op without thinker.dll;
    // pinned to the 5.5 build. See thinkertext.cpp.
    InstallThinkerText();

    // Movie player: intercept amovie_project_2 so cinematics play in-process -- no external app (playuv15)
    // and no display-mode change. See src\movie.cpp. ONLY when its five codec DLLs are beside us and the
    // shipped builds: they are delay-loaded, so a missing or quarantined one no longer takes uimod.dll
    // down with it -- the hook stays out (movies take the engine's own path), uimod.log names the file,
    // and the player gets one notice at the main menu (update.cpp shows it). See codecs.h.
    {
        static char codecLog[1024], codecNotice[2048], codecSig[256];
        if (MovieCodecsOk(codecLog, sizeof codecLog, codecNotice, sizeof codecNotice, codecSig, sizeof codecSig)) {
            UimodLog(codecLog);
            UpdateClearStartupNotice();        // a set announced earlier is fixed: a recurrence is news again
            InstallMovieHook(pracx);
            UimodLog("[uimod] Init: movie hook installed\n");
        } else {
            UimodLog(codecLog);
            UimodLog("[uimod] Init: movie hook NOT installed -> cinematics take the engine's own path; everything else on\n");
            UpdateQueueStartupNotice(codecNotice, codecSig);
        }
    }

    // Fullscreen / Borderless / Windowed on top of PRACX's window code (byte-checked binding to
    // prax.dll; off without PRACX). The boot mode is applied from the wheel module's message hook
    // once the game window exists. See vidmode.cpp.
    InstallVidMode(pracx);

    // Drag-move hold delay (accessibility): the [UIMod] DragMoveHoldMs setting rewrites the two imm32s of
    // MapWin_on_left_down's goto-timer delay. Byte-checked and cave-free, so both exes; before the prefs tab
    // BUILDS, whose "Drag Hold Time" slider exists only when this site checked out. See dragdelay.cpp.
    InstallDragDelay();

    // Modern Control Set (opt-in [UIMod] ModernControls: right click moves, hover move-to line, Shift+left click
    // = the orders menu; child RightClickDragsToMove; MapDragButton under PRACX) plus two unconditional fixes:
    // no Shift+click instant drag, and Shift/Ctrl auto-repeats no longer swallow the next click. The PRACX
    // drag-button binding comes first (modernctl applies through it). Both exes, byte-checked. See modernctl.cpp.
    InstallPraxDrag(pracx);
    InstallMapScroll(pracx);
    InstallModernControls();

    // Longevity Mod Preferences: a new PrefWin tab with the map-label font-scale
    // slider. Byte-checked patches; self-disables on an unexpected exe. See prefs.cpp.
    InstallPrefsTab();

    // AI perception filter (vanilla bug: the AI sees cloaked / deep-pressure-hull units). Cave-free and
    // byte-checked, so it installs on the unpatched exe too. Before the prefs tab BUILDS (desktop_init)
    // is all the ordering the checkbox needs; Init order is irrelevant. See aivis.cpp.
    InstallAiVisibility();

    // ... and its Thinker half: Thinker's own planner (combat_move, move_upkeep, ...) reads the unit array directly,
    // so the filter and the bump are ported into thinker.dll 5.5 by version-pinned, byte-checked detours. A no-op
    // without thinker.dll. After the two installs above. See thinkeraivis.cpp.
    InstallThinkerAiVis();

    // Mouse wheel scrolls the list popup under the cursor even while the map shows (PRACX would zoom
    // the map). A message hook only -- no code patched. See wheel.cpp.
    InstallWheelCapture();

    // The keystroke that closes a story interlude reached the map a second time as its WM_CHAR and opened the game
    // menu over the interlude (base-game bug, user 2026-10-30). A thread-local message hook keyed on the InterludeWin
    // class swallows that one character. Both exes. See interludekey.cpp.
    InstallInterludeKeyFix();

    // Multiplayer over TCP/IP: two PCs behind the same router find each other's games even when the router does not
    // loop traffic back through its public address (Windows' DirectPlay NAT helper only recognises its own machine's
    // mappings, so the reply went to the public address and was dropped -- "Game Not Found"). DirectPlay's own
    // "is this address behind my router?" answer is completed; nothing changes elsewhere. Both exes; [UIMod]
    // DirectPlayLanFix=0 turns it off. See lanjoin.cpp.
    InstallLanJoinFix();

    // Base-game bug: the multiplayer time widget ("No Time Control", the turn timer) flashed black when it was
    // redrawn between timer ticks -- two locals were written only on the tick path. Both exes. See mptimefix.cpp.
    InstallMpTimeFix();

    // Update check: the resident installer's `--check` runs as a hidden child on a worker thread
    // (zero start-time cost); the wheel module's message hook consumes the answer. Cave-free, both
    // exes; [UIMod] CheckForUpdates=0 means no process is ever started. See update.cpp.
    InstallUpdateCheck();

    // Tech breakthrough window: the description / unlock text boxes open at the top, not on the engine's
    // chat-log last page (vanilla bug). Three byte-checked E8 retargets, cave-free. See techwin.cpp.
    InstallTechWinTopAnchor();

    // Reply rows (diplomacy replies, Quit menu, ...): the button-edge -> text channel scales with the UI.
    // One byte-checked in-register detour, cave-free; a no-op at g <= 16. See rowindent.cpp.
    InstallRowIndent();

    // Punishment Sphere bases show the drones the sphere suppresses (PSYCH tab last row, citizens row, F4),
    // each drone face badged. Display only; byte-checked, all-or-nothing, chains to the patcher's face thunks
    // when present, so it installs on the unpatched exe too. See pssphere.cpp.
    InstallPunishmentSphereView();

    // Base-game crash: a rioting base's defection roll read uninitialised weights and could divide by zero;
    // its REVOLT roll was inverted as well. Both exes. See riotfix.cpp.
    InstallRiotFix();

    // Base-game bug: the in-game music kept playing over the main menu after leaving a game. Both exes.
    // See musicfix.cpp.
    InstallMusicFix();

    // Base-game bug: the selected unit's blink regenerated only its own tile, leaving a sliver of any sprite
    // wider than the tile drawn solid. Both exes. See blinkfix.cpp.
    InstallBlinkFix();

    // Drop-in HD terrain textures: <game>\terrain\texture_x2.pcx / texture_x4.pcx replace the map's terrain at the
    // main map's zoom. Nothing is patched without a pack on disk; [UIMod] TerrainHD=0 turns it off. Byte-checked
    // E8 retargets plus the UV tables / immediates, cave-free, both exes. See terrainhd.cpp.
    InstallTerrainHd();

    // The engine map cursors (go-to, patrol, airdrop, ...) at vanilla x g/16. Patched exe only. See cursorscale.cpp.
    InstallCursorScale();

    // Base-game bug: a go-to the engine gives up on (no route) left the HUD's "Moves" readout stale. Both
    // exes. See hudmovesfix.cpp.
    InstallHudMovesFix();

    // Base-game desync: a loaded game resumed with a pending unit in VIEW mode (half-selected), and the HUD unit
    // panel keys on a stored tile that only explicit navigation updates. Both exes. See hudsync.cpp.
    InstallHudSync();

    // Base-game bug: a view-mode (V) drag armed the move line, but the release only selected the unit. Both exes,
    // every control scheme. See viewdragfix.cpp.
    InstallViewDragFix();

    // Leader portraits enlarge smoothly (de-scanline + Lanczos + dithered palette fit) instead of as blocks, and
    // <game>\portraits\ (then the portraits-hd pack in portraits\hd\) replaces leader / native-life portraits.
    // The enlarge hooks are patched-exe only; every entry is byte-checked. See portrait.cpp.
    InstallSmoothPortraits();

    // Developer diagnostic, OFF unless uimod_debug.flag exists next to the exe. See debugdump.cpp.
    InstallDebugDump();
}
