"""How a platform will play, answered before the grab rather than after.

A ROM filed under a platform with no route is *imported but dead*: it appears
in the library, it has a cover, it has metadata, and clicking it does nothing.
Nothing about the library afterwards says why. ROMarr could not say anything
about this at all, which is how the README came to claim that disc platforms
"cannot be streamed into a browser emulator" while nine of them shipped with
EmulatorJS cores in stock RomM.

**Four routes, and the fourth is not a failure.**

  * ``local``    -- EmulatorJS, in the browser, from the library server itself.
  * ``stream``   -- a stream server, rendering server-side and delivering
                    video. This is how the machines EmulatorJS has no core for
                    are played. Two kinds answer here: the headless RetroArch
                    stream server, which knows about platforms and can be
                    asked; and a Moonlight host -- Wolf, Sunshine, or Steam
                    Headless -- which is a desktop and cannot be. See
                    `MoonlightHost` for what that difference costs.
  * ``archive``  -- Archive.org's own in-page emulator. Opening a ``/details/``
                    page *is* playing the game there.
  * ``download`` -- always available. A ROM in a library is a ROM you can take
                    away and run on anything, and an operator who asked for
                    that has been served.

**Four routes, and four players inside the first one.**

`local` spent a long time meaning "EmulatorJS", which is one player wearing
the name of a tier. On a library that is mostly Flash and DOS that is wrong
three separate ways, so `local` now names *which* browser player -- see
`PLAYERS`, `routes_for_file` and `PlayerPolicy`. Every player can be turned
off, and where more than one will open a file they are offered best-first
with the reason for each.

**A route is about a file, not only about a platform.** The extension decides
more than the platform does: a `.swf` is Ruffle's and nobody else's, a `.zip`
of DOS files is any of three players', a `.sfc` is a libretro core's. And a
row whose bytes are not on the library server plays on nothing at all -- which
is a different answer from "unsupported", points at a different fix, and is
said in different words. See `ABSENT`.

Nothing here refuses an import, and nothing here is allowed to over-claim.
A platform wrongly reported playable sends somebody to a game that displays
and does nothing; a platform wrongly reported unplayable costs them a warning
they did not need. Those are not symmetric, so every table below fails in the
second direction.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .platforms import Platform, resolve

log = logging.getLogger(__name__)

LOCAL = "local"
STREAM = "stream"
ARCHIVE = "archive"
DOWNLOAD = "download"

#: Best first. `download` is last because it is the floor, not because it is
#: bad -- it is the only route that is always true.
_ORDER = (LOCAL, STREAM, ARCHIVE, DOWNLOAD)

#: Not a route. The absence of every route, with the cause named.
#:
#: `download` is the floor **only for a file that exists**. 94,428 rows on the
#: library this was measured against are catalogued and not present -- RomM
#: says `missing_from_fs`, and asking it for the content returns 404 -- so
#: there are no bytes to play, no bytes to stream and no bytes to download.
#: Reporting those as "download only" was the single most misleading sentence
#: this module produced: it named a route that 404s, and it buried the real
#: answer, which is that ROMarr has not fetched the game yet.
ABSENT = "absent"

#: The RomM release `EJS_CORES` was read from.
#:
#: Vendored rather than fetched, for the reason `rom-hub` states and this
#: inherits: RomM publishes no endpoint for its core map, `/api/config` does
#: not carry it, and the map lives in compiled frontend JavaScript. So it is a
#: copy, it is dated, and it will go stale -- which is fine as long as it says
#: which release it is stale relative to.
ROMM_VERSION = "4.9.2"

#: When the table below was last checked against a newer RomM, and which.
#:
#: Re-read from `frontend/src/utils/index.ts` at tag 5.1.0 -- the release the
#: maintainer's install runs -- and every platform ROMarr models still maps to
#: the same cores. 5.1.0 adds `doom` (`prboom`), `c-plus-4` and `cpet`, which
#: are not platforms ROMarr models, and a *nightly* map behind
#: `EJS_NETPLAY_ENABLED` carrying `azahar` for 3DS, `freeintv` for
#: Intellivision, `bsnes` for SNES and the `_wide` Sega cores. The nightly map
#: is deliberately not merged in here: it is reachable only with netplay
#: turned on, and `NO_EJS_CORE` already says so for the two platforms where it
#: is the difference between playable and not.
EJS_CORES_RECHECKED = ("2026-08-11", "5.1.0")

#: RomM's `_EJS_CORES_MAP`, restricted to the platforms ROMarr models, keyed by
#: ROMarr's slug.
#:
#: **Nine of these are optical systems**, which is the entire factual basis for
#: removing the disc exclusion: psx, psp, saturn, segacd, 3do, philips-cd-i,
#: pc-fx, turbografx-cd and amiga-cd32 play in a browser on a stock RomM, with
#: no configuration and no stream server.
#:
#: Two ROMarr slugs are not keys in RomM's map and are covered by a key naming
#: the same machine -- `genesis-slash-megadrive` by `genesis`,
#: `turbografx-16-slash-pc-engine-cd` by `turbografx-cd`. That equivalence is
#: recorded here rather than acted on anywhere else: this module *reports*, and
#: re-filing a ROM under a neighbouring slug is a different and much worse idea
#: than telling somebody which core will run it.
EJS_CORES: dict[str, tuple[str, ...]] = {
    # -- cartridges ---------------------------------------------------------
    "nes": ("fceumm", "nestopia"),
    "snes": ("snes9x",),
    "gb": ("gambatte", "mgba"),
    "gbc": ("gambatte", "mgba"),
    "gba": ("mgba",),
    "n64": ("mupen64plus_next", "parallel_n64"),
    "genesis-slash-megadrive": ("genesis_plus_gx",),   # RomM key: `genesis`
    "sms": ("genesis_plus_gx",),
    "gamegear": ("genesis_plus_gx",),
    "atari2600": ("stella2014",),
    "atari7800": ("prosystem",),
    "lynx": ("handy",),
    "turbografx16--1": ("mednafen_pce",),              # RomM key: `tg16`
    "wonderswan": ("mednafen_wswan",),
    "neo-geo-pocket": ("mednafen_ngp",),
    "virtualboy": ("beetle_vb",),
    "nds": ("melonds", "desmume", "desmume2015"),
    # -- optical ------------------------------------------------------------
    "psx": ("pcsx_rearmed", "mednafen_psx_hw"),
    "psp": ("ppsspp",),
    "saturn": ("yabause",),
    "segacd": ("genesis_plus_gx", "picodrive"),
    "3do": ("opera",),
    "philips-cd-i": ("same_cdi",),
    "pc-fx": ("mednafen_pcfx",),
    "turbografx-16-slash-pc-engine-cd": ("mednafen_pce",),  # RomM: turbografx-cd
    "amiga-cd32": ("puae",),
    # -- consoles, handhelds and boards -------------------------------------
    "jaguar": ("virtualjaguar",),
    "arcade": ("mame2003", "mame2003_plus", "fbneo",
               "fbalpha2012_cps1", "fbalpha2012_cps2"),
    "neogeoaes": ("fbneo",),
    "neogeomvs": ("fbneo",),
    "atari5200": ("a5200",),
    "colecovision": ("gearcoleco",),
    "sega32": ("picodrive",),
    "supergrafx": ("mednafen_pce",),
    "wonderswan-color": ("mednafen_wswan",),
    "neo-geo-pocket-color": ("mednafen_ngp",),
    "fds": ("fceumm", "nestopia"),
    "famicom": ("fceumm", "nestopia"),
    "sfam": ("snes9x",),
    # -- home computers -----------------------------------------------------
    "c64": ("vice_x64sc", "vice_x64"),
    "c128": ("vice_x128",),
    "vic-20": ("vice_xvic",),
    "amiga": ("puae",),
    "acpc": ("cap32", "crocods"),
    "zxs": ("fuse",),
    "dos": ("dosbox_pure",),
}

#: Deliberately absent from the table above, and why, so that "we checked"
#: cannot decay into "nobody looked". Each was confirmed to have no core in
#: RomM 4.9.2's base map and no other slug in that map covering the same
#: machine. They are not dead ends -- `ps2`, `ngc`, `wii`, `dc` and `3ds` all
#: play on the stream tier -- they simply do not play in a browser.
NO_EJS_CORE: dict[str, str] = {
    "ps2": "EmulatorJS ships no PlayStation 2 core; PCSX2 is server-side only",
    "ngc": "EmulatorJS ships no GameCube core; Dolphin is server-side only",
    "wii": "EmulatorJS ships no Wii core; Dolphin is server-side only",
    "dc": "EmulatorJS ships no Dreamcast core; flycast is server-side only",
    "3ds": "azahar is in RomM's nightly cores, which need netplay enabled",
    "neo-geo-cd": (
        "EmulatorJS ships no Neo Geo CD core; NeoCD is server-side only "
        "(fbneo's AES/MVS drivers are cartridge hardware and cannot open a "
        "cue or a chd)"),
    "atari-jaguar-cd": (
        "no emulator plays Jaguar CD. virtualjaguar is the only Jaguar core "
        "in libretro and declares j64|jag|rom|abs|cof|bin|prg -- cartridges "
        "only, no cue and no chd"),
    # Home computers EmulatorJS has no core for. All four run on the stream
    # tier, and all four want firmware the operator supplies -- which is a
    # different problem from having no emulator, and is said differently.
    "msx": "EmulatorJS ships no MSX core; blueMSX is server-side only",
    "msx2": "EmulatorJS ships no MSX2 core; blueMSX is server-side only",
    "vectrex": "EmulatorJS ships no Vectrex core; vecx is server-side only",
    "intellivision": (
        "freeintv is in RomM's nightly cores, which need netplay enabled; "
        "the stream server runs it server-side"),
    "sharp-x68000": (
        "EmulatorJS ships no X68000 core; px68k is server-side only"),
}

#: When ARCHIVE_EMULATED was measured, and against what.
#:
#: Archive.org records the emulator for an item in its `emulator` metadata
#: field -- 272,424 items carry one -- so this is their data, not an estimate.
#: Counts came from `advancedsearch.php?q=emulator:"<driver>"`.
ARCHIVE_MEASURED_ON = "2026-08-02"

#: Platforms Archive.org's in-page emulator actually runs, with the item count
#: behind each.
#:
#: **The disc systems are absent and that is the finding, not an omission.**
#: The drivers return nothing: `psj`/`psu`/`pse` 0 items, `saturn` 0, `3do` 0,
#: `dc` 2, `segacd` 1. Archive.org is a *source* for disc images and not a
#: player of them, so offering this route for a PlayStation request would send
#: an operator to a details page with a download button and no emulator.
ARCHIVE_EMULATED: dict[str, int] = {
    "genesis-slash-megadrive": 12877,   # drivers `genesis` + `megadriv`
    "atari2600": 3123,
    "nes": 514,
    "snes": 538,
    "gbc": 553,
    "gb": 553,
    "gba": 483,
    "n64": 8,
}

# ===========================================================================
# Browser players: EmulatorJS, Ruffle, js-dos, Emularity
# ===========================================================================
#
# The `local` tier had exactly one player in it and the detail string said so
# on every platform with a core. On the maintainer's library that is wrong in
# three directions at once, and each one cost a real answer:
#
#   * **EmulatorJS has never run a Flash file.** 71,570 rows here are `.swf`.
#     RomM plays those on Ruffle -- it ships Ruffle, `DISABLE_RUFFLE_RS` is
#     false on the live server -- and ROMarr had no name for that player, so
#     it said "no browser core for this platform" about files a browser plays
#     perfectly well.
#   * **A DOS zip has three players, not one.** EmulatorJS's `dosbox_pure`,
#     js-dos, and Emularity's EM-DOSBOX will all open it. Which is best
#     depends on what the operator runs, which is a thing this file cannot
#     know, so the job is to lay the options out best-first with a reason and
#     let them choose -- and to let them turn any of them off.
#   * **A row with no file plays on nothing.** See `ABSENT`.
#
# So a player is a first-class thing here: it has a name, it can be disabled,
# it is ordered against the others, and it is asked about a FILE.
#
# What is deliberately *not* here, so that "we checked" cannot decay into
# "nobody looked":
#
#   * **v86** (copy/v86, BSD-2-Clause) is a real x86 PC in a browser and will
#     boot DOS, Windows 9x and Linux. It is not offered as a player because
#     what it takes is a *disk image with an operating system on it*, not a
#     game: playing one DOS title through v86 means somebody first built a
#     bootable image with that title installed. That is a fine thing to do and
#     it is not something ROMarr can infer from a `.zip` in a library.
#   * **Nostalgist.js** (MIT) drives the same libretro cores EmulatorJS does,
#     through a smaller API. It would add no platform and no file type that
#     EmulatorJS does not already cover here, so adding it would be a second
#     row in the UI offering the identical answer.
#   * **RetroArch's own Emscripten web player** has no loader for a remote
#     library; it wants a file dropped into its virtual filesystem by hand.
#
# Both v86 and Nostalgist are credited in `ecosystem.py` regardless. Being
# wrong for this job is not the same as being unworthy of the credit.

EMULATORJS = "emulatorjs"
RUFFLE = "ruffle"
JSDOS = "jsdos"
EMULARITY = "emularity"

#: Best first, and the default when the operator has not said otherwise.
#:
#: EmulatorJS and Ruffle lead because the library server serves both itself:
#: a route through them costs no second service, no second hostname and no
#: configuration. js-dos and Emularity follow because they are somebody else's
#: page that the operator has to stand up and point ROMarr at -- a better DOS
#: player that is not installed is worse than the one that is.
#:
#: The order is the operator's to change. "Best" depends on what they run, and
#: somebody with a tuned js-dos in front of their DOS library wants it first.
DEFAULT_PLAYER_ORDER = (EMULATORJS, RUFFLE, JSDOS, EMULARITY)

#: Where a player comes from, which is the difference between a route that
#: exists today and one that needs an afternoon first.
FROM_LIBRARY = "library"     # the library server ships it; nothing to do
FROM_OPERATOR = "operator"   # somebody hosts it and tells ROMarr the URL
FROM_ARCHIVE = "archive"     # it runs on Archive.org's page, not this install


@dataclass(frozen=True)
class Player:
    """One browser player, and the second sentence about it.

    `cannot` is not decoration and is not optional. Every one of these
    projects is honest about its own limits in its own documentation, and
    every one of them gets described elsewhere as though it had none -- which
    is how somebody ends up filing a Flash projector `.exe` expecting Ruffle
    to open it, or a PSP ISO expecting EmulatorJS to start without
    cross-origin isolation. The refusal is the useful half.
    """

    key: str
    label: str
    #: What it runs, in one line.
    runs: str
    #: What it does not run, in one line.
    cannot: str
    repo: str
    site: str
    #: One of FROM_LIBRARY, FROM_OPERATOR, FROM_ARCHIVE.
    hosted: str


PLAYERS: dict[str, Player] = {
    EMULATORJS: Player(
        EMULATORJS, "EmulatorJS",
        runs=("libretro cores compiled to WebAssembly -- 40-odd consoles, "
              "handhelds, arcade boards and home computers"),
        cannot=("Flash, Shockwave, Java or any other web plugin format; "
                "GameCube, Wii, Dreamcast and PS2, which have no core"),
        repo="https://github.com/EmulatorJS/EmulatorJS",
        site="https://emulatorjs.org",
        hosted=FROM_LIBRARY),
    RUFFLE: Player(
        RUFFLE, "Ruffle",
        runs="Flash movies and games -- `.swf`, ActionScript 1, 2 and 3",
        cannot=("projector `.exe` files, Shockwave, Unity Web Player, "
                "Silverlight or Java applets -- none of those are SWFs"),
        repo="https://github.com/ruffle-rs/ruffle",
        site="https://ruffle.rs",
        hosted=FROM_LIBRARY),
    JSDOS: Player(
        JSDOS, "js-dos",
        runs=("DOS and Windows 9x, on DOSBox and DOSBox-X, with 3Dfx and "
              "IPX networking"),
        cannot="anything that is not a PC; it is a DOS player, not an emulator frontend",
        repo="https://github.com/caiiiycuk/js-dos",
        site="https://js-dos.com",
        hosted=FROM_OPERATOR),
    EMULARITY: Player(
        EMULARITY, "Emularity",
        runs=("Archive.org's loader: MAME for arcade and the MESS machines, "
              "EM-DOSBOX for DOS, and the Scripted Amiga Emulator"),
        cannot=("disc systems -- Archive.org's own `emulator` field returns 0 "
                "items for PlayStation, Saturn and 3DO"),
        repo="https://github.com/db48x/emularity",
        site="https://archive.org/details/softwarelibrary",
        hosted=FROM_ARCHIVE),
}


#: Which file each libretro core will open, from that core's own
#: `supported_extensions` line in libretro-super's `dist/info`.
#:
#: This is the table that makes the answer per-file rather than per-platform,
#: and it is upstream's data rather than a guess: EmulatorJS does not carry an
#: extension list of its own, it reads the core's (`supportsExtension` in
#: `data/src/emulator.js` tests against `this.extensions`, which is filled
#: from the core's info). Read 2026-08-11 from `libretro/libretro-super`
#: `master`.
#:
#: Only the cores `EJS_CORES` names are here. A core not in this table is
#: treated as declaring nothing, which fails towards "cannot" -- the safe
#: direction, per the module docstring.
CORE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "fceumm": (".fds", ".nes", ".unif", ".unf"),
    "nestopia": (".nes", ".fds", ".unf", ".unif"),
    "snes9x": (".smc", ".sfc", ".swc", ".fig", ".bs", ".st"),
    "gambatte": (".gb", ".gbc", ".dmg"),
    "mgba": (".gb", ".gbc", ".gba"),
    "mupen64plus_next": (".n64", ".v64", ".z64", ".ndd", ".bin", ".u1"),
    "parallel_n64": (".n64", ".v64", ".z64", ".bin", ".u1", ".ndd"),
    "genesis_plus_gx": (".mdx", ".md", ".smd", ".gen", ".bin", ".cue", ".iso",
                        ".sms", ".bms", ".gg", ".sg", ".68k", ".sgd", ".chd",
                        ".m3u"),
    "stella2014": (".a26", ".bin"),
    "prosystem": (".a78", ".bin", ".cdf"),
    "handy": (".lnx", ".lyx", ".o"),
    "mednafen_pce": (".pce", ".sgx", ".cue", ".ccd", ".chd", ".toc", ".m3u"),
    "mednafen_wswan": (".ws", ".wsc", ".pc2", ".pcv2"),
    "mednafen_ngp": (".ngp", ".ngc", ".ngpc", ".npc"),
    # RomM's map says `beetle_vb`; libretro publishes the same core's info as
    # `mednafen_vb`. Same core, two names, and the extensions are the core's.
    "beetle_vb": (".vb", ".vboy", ".bin"),
    "melonds": (".nds", ".ids", ".dsi"),
    "desmume": (".nds", ".ids", ".bin"),
    "desmume2015": (".nds", ".ids", ".bin"),
    "pcsx_rearmed": (".bin", ".cue", ".img", ".mdf", ".pbp", ".toc", ".cbn",
                     ".m3u", ".ccd", ".chd", ".iso", ".exe"),
    "mednafen_psx_hw": (".cue", ".toc", ".m3u", ".ccd", ".exe", ".pbp",
                        ".chd", ".bin"),
    "ppsspp": (".elf", ".iso", ".cso", ".prx", ".pbp", ".chd"),
    "yabause": (".bin", ".ccd", ".chd", ".cue", ".iso", ".mds", ".zip",
                ".m3u"),
    "picodrive": (".bin", ".gen", ".smd", ".md", ".32x", ".cue", ".iso",
                  ".chd", ".sms", ".gg", ".sg", ".sc", ".m3u", ".68k",
                  ".sgd", ".pco"),
    "opera": (".iso", ".bin", ".chd", ".cue"),
    "same_cdi": (".chd", ".iso", ".cue"),
    "mednafen_pcfx": (".cue", ".ccd", ".toc", ".chd"),
    "puae": (".adf", ".adz", ".dms", ".fdi", ".ipf", ".hdf", ".hdz", ".lha",
             ".slave", ".info", ".cue", ".ccd", ".nrg", ".mds", ".iso",
             ".chd", ".uae", ".m3u", ".zip", ".7z", ".rp9"),
    "virtualjaguar": (".j64", ".jag", ".rom", ".abs", ".cof", ".bin", ".prg",
                      ".cue", ".cdi"),
    "mame2003": (".zip",),
    "mame2003_plus": (".zip",),
    "fbneo": (".zip", ".7z", ".cue", ".ccd"),
    "fbalpha2012_cps1": (".zip",),
    "fbalpha2012_cps2": (".zip",),
    "a5200": (".a52", ".bin"),
    "gearcoleco": (".col", ".cv", ".bin", ".rom"),
    "vice_x64sc": (".d64", ".d71", ".d80", ".d81", ".d82", ".g64", ".g41",
                   ".x64", ".t64", ".tap", ".prg", ".p00", ".crt", ".bin",
                   ".zip", ".gz", ".cmd", ".m3u", ".vfl", ".vsf", ".nib"),
    "vice_x64": (".d64", ".d71", ".d80", ".d81", ".d82", ".g64", ".g41",
                 ".x64", ".t64", ".tap", ".prg", ".p00", ".crt", ".bin",
                 ".zip", ".gz", ".cmd", ".m3u", ".vfl", ".vsf", ".nib"),
    "vice_x128": (".d64", ".d71", ".d80", ".d81", ".d82", ".g64", ".g41",
                  ".x64", ".t64", ".tap", ".prg", ".p00", ".crt", ".bin",
                  ".zip", ".gz", ".cmd", ".m3u", ".vfl", ".vsf", ".nib"),
    "vice_xvic": (".d64", ".d71", ".d80", ".d81", ".d82", ".g64", ".g41",
                  ".x64", ".t64", ".tap", ".prg", ".p00", ".crt", ".bin",
                  ".zip", ".gz", ".cmd", ".m3u", ".vfl", ".vsf", ".nib",
                  ".20", ".40", ".60", ".a0", ".b0", ".rom"),
    "cap32": (".dsk", ".sna", ".zip", ".tap", ".cdt", ".voc", ".cpr", ".m3u"),
    "crocods": (".dsk", ".sna", ".kcr"),
    "fuse": (".tzx", ".tap", ".z80", ".rzx", ".scl", ".trd", ".dsk", ".dck",
             ".sna", ".szx", ".zip", ".ipf"),
    "dosbox_pure": (".zip", ".dosz", ".exe", ".com", ".bat", ".iso", ".chd",
                    ".cue", ".ins", ".img", ".ima", ".vhd", ".jrc", ".tc",
                    ".m3u", ".m3u8", ".conf"),
}

#: Archives EmulatorJS opens by itself, whatever the core declares.
#:
#: **Stated because the widely-repeated version of this is wrong.** "EmulatorJS
#: cannot open 7z" is a real bug report (linuxserver/docker-emulatorjs#45) and
#: it is stale: `data/src/compression.js` in the 4.2.3 build RomM ships sniffs
#: the first bytes -- `PK`, `7z\xBC\xAF`, `Rar!` -- and loads `extractzip.js`,
#: `extract7z.js` or `libunrar.js` accordingly. It reads magic bytes and not
#: the name, which is worth knowing in the other direction too: a mis-named
#: archive still opens, and a `.zip` that is not one still fails.
EJS_ARCHIVES = (".zip", ".7z", ".rar")

#: Cores that need `SharedArrayBuffer`, and so a cross-origin-isolated page.
#:
#: RomM's own `areThreadsRequiredForEJSCore`. This is a *deployment* limit
#: rather than a compatibility one, and it fails in the worst available way:
#: the page loads, the player draws its frame, and nothing ever starts. It is
#: named in the route detail rather than left to be discovered, because the
#: fix is two response headers (COOP and COEP) on the library server and
#: nobody guesses that from a black canvas.
EJS_THREADED_CORES = ("dosbox_pure", "ppsspp", "azahar")

#: Ruffle opens SWF bytes. That is the whole list and it is not a shorthand.
RUFFLE_EXTENSIONS = (".swf",)

#: Where the library server will actually *offer* its Ruffle player.
#:
#: RomM gates Ruffle on the platform slug alone -- `isRuffleEmulationSupported`
#: returns `["flash", "browser"].includes(slug)`, with no reference to the
#: file. Two consequences, and ROMarr reports both rather than picking one:
#: a `.swf` filed under any other platform will not get a Play button even
#: though Ruffle would run it, and every row on `browser` gets one even when
#: the file is not a SWF and even when there is no file at all.
RUFFLE_PLATFORMS = ("browser", "flash")

#: How well Ruffle runs what it runs, in Ruffle's own numbers.
#:
#: From ruffle.rs/compatibility, read 2026-08-11: AVM 1 -- ActionScript 1 and
#: 2 -- 99% of the language and 82% of the API; AVM 2 -- ActionScript 3 -- 90%
#: of the language and 82% of the API. Their sentence for AVM 2 is that "most
#: games will work well enough to be played", which is a good bet and not a
#: guarantee, and a SWF cannot be inspected from a filename to tell which of
#: the two it is. So the route is offered for any SWF and promised for none.
RUFFLE_COVERAGE = ("AVM 1 at 99% of the language and 82% of the API, "
                   "AVM 2 (ActionScript 3) at 90% and 82%")

#: Files that turn up in Flash archives wearing Flash's clothes, and are not
#: SWFs. Each is a different runtime, and none of them is Ruffle's.
#:
#: The projector is the one that actually costs people time. A Flash projector
#: is a Windows or Mac *executable* with the movie appended to a player stub;
#: it opens in a file manager, it has a Flash icon, and it is not a SWF.
#: Ruffle takes SWF bytes and has no projector reader -- asked for repeatedly
#: upstream and still open (ruffle-rs/ruffle#2279 from 2021,
#: ruffle-rs/ruffle#11539 from 2023).
NOT_A_SWF: dict[str, str] = {
    ".exe": ("a Flash projector is an executable with the movie bundled "
             "inside a player stub, not a SWF; Ruffle loads SWF bytes and "
             "has no projector reader (ruffle-rs/ruffle#11539, still open)"),
    ".dcr": "Shockwave, which is Director and not Flash -- a different runtime",
    ".dir": "Shockwave source (Director), not Flash",
    ".dxr": "Shockwave (protected Director movie), not Flash",
    ".cst": "a Shockwave cast file, not Flash",
    ".unity3d": "Unity Web Player, retired by Unity and not a Flash format",
    ".xap": "Silverlight, which is Microsoft's plugin and not Flash",
    ".jar": "a Java applet -- a JVM, not a Flash runtime",
    ".class": "a Java applet class -- a JVM, not a Flash runtime",
}

#: What js-dos will mount. `.jsdos` is its own bundle -- a zip with a
#: `.jsdos/dosbox.conf` in it -- and a plain zip of a DOS directory works too.
#: Nothing else: js-dos is a DOS and Windows 9x player and has never claimed
#: to be anything wider.
JSDOS_EXTENSIONS = (".jsdos", ".zip")

#: The platforms js-dos speaks for.
JSDOS_PLATFORMS = ("dos",)

#: Emularity's engines, from its own README: MAME, EM-DOSBOX and the Scripted
#: Amiga Emulator. Mapped to the ROMarr slugs each one covers, with what the
#: engine takes.
#:
#: Deliberately narrower than "everything MAME has a driver for". MAME's driver
#: list is enormous and Archive.org runs a *configured subset* of it
#: (`internetarchive/emularity-config` is hundreds of `.cfg` files, one per
#: machine); claiming a platform because MAME theoretically drives it would be
#: exactly the over-claim this module exists to avoid. Arcade, DOS and Amiga
#: are the three the loader's own documentation names.
EMULARITY_ENGINES: dict[str, tuple[str, tuple[str, ...]]] = {
    "dos": ("EM-DOSBOX", (".zip",)),
    "arcade": ("MAME", (".zip",)),
    "neogeoaes": ("MAME", (".zip",)),
    "neogeomvs": ("MAME", (".zip",)),
    "amiga": ("Scripted Amiga Emulator", (".adf", ".adz", ".zip")),
}

#: Emularity is also how Flash plays on an Archive.org `/details/` page, and
#: the engine there is Ruffle rather than anything Emularity wrote. Recorded
#: because it is the fact that keeps the two entries from looking like rivals:
#: Archive.org added Ruffle to the Emularity loader in November 2020, and the
#: `ruffle` item type is named in `internetarchive/emularity-config`.
EMULARITY_FLASH_ENGINE = "Ruffle"


class PlayerPolicy:
    """Which browser players this install offers, and in what order.

    Optional in exactly the sense the stream server is optional: ROMarr works
    identically with all four turned off, minus the routes only they can
    offer. Turning one off is a real operator decision and not a preference --
    an install that does not want its users leaving for Archive.org turns
    Emularity off, and an install whose library server has `DISABLE_RUFFLE_RS`
    set turns Ruffle off so ROMarr stops promising a button that is not there.

    The operator's order beats the table's, because "best" depends on what
    they run and nothing in this file knows that.

    Unset is not the same as empty. An unset `ROMARR_PLAYERS` means "all four,
    default order", because environment variables get blanked by accident all
    the time and an install that silently lost every play route would look
    like a bug in ROMarr. Turning them all off is spelled `none`, which
    nobody types by accident.
    """

    #: Players ROMarr can point a browser at without being told a URL,
    #: because the library server serves them from its own origin.
    SERVED_BY_LIBRARY = (EMULATORJS, RUFFLE)

    def __init__(self, order=None, urls=None):
        chosen: list[str] = []
        if order is None:
            chosen = list(DEFAULT_PLAYER_ORDER)
        else:
            for key in order:
                key = str(key or "").strip().lower()
                if key in PLAYERS and key not in chosen:
                    chosen.append(key)
                elif key and key not in ("none", "") and key not in PLAYERS:
                    log.warning("unknown player %r ignored; known players are %s",
                                key, ", ".join(sorted(PLAYERS)))
        self.order = tuple(chosen)
        self.urls = {k: str(v or "").rstrip("/")
                     for k, v in (urls or {}).items() if k in PLAYERS}

    @classmethod
    def from_env(cls, env) -> "PlayerPolicy":
        """`ROMARR_PLAYERS`, plus a URL for each player that needs one."""
        raw = str(env.get("ROMARR_PLAYERS", "") or "").strip()
        order = None if not raw else [p for p in raw.replace(" ", ",").split(",")
                                      if p]
        return cls(order=order, urls={
            JSDOS: env.get("ROMARR_JSDOS_URL", ""),
            EMULARITY: env.get("ROMARR_EMULARITY_URL", ""),
        })

    def enabled(self, key: str) -> bool:
        return key in self.order

    def rank(self, key: str) -> int:
        """Position in the operator's order; last for anything not in it."""
        return self.order.index(key) if key in self.order else len(PLAYERS)

    def url(self, key: str) -> str:
        return self.urls.get(key, "")

    def reachable(self, key: str) -> bool:
        """Whether a route through this player can actually be handed over.

        Enabled is not reachable. EmulatorJS and Ruffle come off the library
        server's own origin, so enabling them is the whole of it. js-dos and
        Emularity are somebody else's page, and ROMarr will not print a link
        it does not have -- it says which setting is missing instead.
        """
        if not self.enabled(key):
            return False
        if key in self.SERVED_BY_LIBRARY:
            return True
        if key == EMULARITY:
            # Archive.org's own page needs no URL from anybody. A self-hosted
            # Emularity does, and that is a separate route -- see
            # `_emularity_routes`.
            return True
        return bool(self.url(key))

    def as_dict(self) -> list[dict]:
        """The registry as the API serves it: every player, on or off."""
        return [
            {
                "key": p.key,
                "label": p.label,
                "runs": p.runs,
                "cannot": p.cannot,
                "repo": p.repo,
                "site": p.site,
                "hosted": p.hosted,
                "enabled": self.enabled(p.key),
                "rank": self.rank(p.key),
                "url": self.url(p.key),
                "reachable": self.reachable(p.key),
            }
            for p in sorted(PLAYERS.values(),
                            key=lambda pl: (self.rank(pl.key), pl.key))
        ]


#: The policy used when a caller does not supply one: everything on, default
#: order, no self-hosted URLs. Keeps `routes_for` answering exactly what it
#: answered before players existed.
ALL_PLAYERS = PlayerPolicy()


#: How long to wait on a stream server. It is a LAN service answering out of an
#: in-memory table, and one that is down must not hold up a page.
STREAM_TIMEOUT = 5.0

#: The one endpoint this module calls. A GET that reads a routing table: it
#: allocates no display, starts no emulator and creates no session.
PLAY_ROUTE_PATH = "/api/play/route"


@dataclass(frozen=True)
class Route:
    """One way this platform, or this file, can be played."""

    kind: str
    detail: str
    #: Which browser player renders it, for `local` and `archive`. Empty for
    #: `stream` and `download`, which are not browser players.
    player: str = ""


@dataclass(frozen=True)
class Playability:
    """Every route open to one platform or one file, best first."""

    platform: str
    routes: tuple[Route, ...]
    #: Set when a stream server was configured and could not be reached, so
    #: the absence of a stream route can be told apart from a refusal.
    stream_unreachable: str = ""
    #: Why there is nothing here to play *or download*, when that is the case.
    #: Only ever set for a file the library server does not hold. Distinct
    #: from an empty `routes` for any other reason, because "we have not
    #: fetched this yet" and "nothing can run this" want different next steps.
    absent: str = ""
    #: Players that could open this and are not offering a route right now,
    #: each carrying the reason. This is the half of the answer that is worth
    #: acting on: "js-dos would run this, you have not told ROMarr where
    #: yours is" is a fix, where silence is a shrug.
    alternatives: tuple[Route, ...] = ()

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(r.kind for r in self.routes)

    @property
    def players(self) -> tuple[str, ...]:
        """Which browser players are offering a route, best first."""
        return tuple(r.player for r in self.routes if r.player)

    @property
    def plays_without_downloading(self) -> bool:
        """Whether somebody can press play and have a game start.

        The honest question, and the reason `download` is not counted: taking
        a 4GB image away to run it elsewhere is a real answer to "can I have
        this", and not an answer to "can I play this here".
        """
        return any(r.kind != DOWNLOAD for r in self.routes)

    def summary(self) -> str:
        """One line for a UI, an API field or a log."""
        parts = [f"{self.platform}:"]
        if self.absent:
            parts.append(self.absent)
        parts.extend(r.detail for r in self.routes)
        if self.stream_unreachable:
            parts.append(f"(stream server unreachable: {self.stream_unreachable})")
        return " ".join(parts)


def routes_for(platform, *, stream=None, players=None) -> Playability:
    """How `platform` will play. Accepts a `Platform` or any name that resolves.

    `stream` is an optional object with `.tier(slug) -> str | None`, normally a
    `StreamServer`. It is asked last and trusted first: it is the only source
    that knows which cores are actually installed on the operator's own
    machine, while the tables here know only what the software ships with.

    `players` is an optional `PlayerPolicy`. Left out, every player is on and
    the answer is the platform-wide one this function has always given. This
    is the *platform* question -- "would anything here play at all" -- and
    `routes_for_file` is the one that can be exact, because it has the
    extension.
    """
    return _routes(platform, extension="", present=True, stream=stream,
                   players=players)


def routes_for_file(name="", platform="", *, present=True, stream=None,
                    players=None) -> Playability:
    """How one file will play, which is the question people actually have.

    `name` is a filename, a bare extension, or RomM's `fs_extension` -- all
    three arrive in practice and all three are accepted. `platform` is the
    slug or name it is filed under, and it still matters: a `.zip` is a MAME
    romset under `arcade`, a DOS program under `dos` and a C64 disk under
    `c64`, and those are three different players.

    `present=False` is the case worth being blunt about. It means the library
    server holds a row and not the bytes -- RomM's `missing_from_fs`, which is
    94,428 of 166,548 rows on the install this was written against, and asking
    for the content of one returns 404. Nothing plays it, nothing streams it
    and nothing downloads it. The answer says so in those words and lists what
    *would* play it once ROMarr has fetched it, which is the actionable part.
    """
    return _routes(platform, extension=_extension(name), present=present,
                   stream=stream, players=players)


def _extension(name: str) -> str:
    """`.swf`, from `game.swf`, `swf`, `.SWF` or `Game (USA) [!].SWF`.

    Bare extensions are accepted because that is the shape RomM reports:
    `fs_extension` on a rom row is `swf`, with no dot. Getting this wrong
    silently answers "no player opens this" for every file in the library,
    which is not a failure that announces itself.
    """
    text = str(name or "").strip().lower()
    if not text:
        return ""
    if "." not in text:
        return f".{text}"
    return text[text.rfind("."):]


def _resolve_platform(platform) -> tuple[str, str]:
    """(name, slug) for a `Platform`, a slug, or anything that resolves.

    An unresolvable name is passed through as its own slug rather than
    discarded. RomM's `browser` platform is the case that matters: ROMarr does
    not model it -- it has no ROM extensions, no size ceiling and nothing to
    import -- and 94,415 rows live on it, so answering "unknown platform" for
    the majority of a library would be useless.
    """
    if isinstance(platform, Platform):
        return platform.name, platform.slug
    resolved = resolve(str(platform or ""))
    slug = resolved.slug if resolved else str(platform or "").strip().lower()
    return (resolved.name if resolved else slug), slug


def _routes(platform, *, extension, present, stream, players) -> Playability:
    policy = players if players is not None else ALL_PLAYERS
    name, slug = _resolve_platform(platform)

    offered, alternatives = _player_routes(slug, extension, policy)

    if not present:
        # No bytes: no local player, no stream, no download. The one route
        # that survives is Archive.org's own page, because that is somebody
        # else's copy rather than this library's -- and it is exactly the
        # route a catalogued row is catalogued *for*.
        kept = tuple(r for r in offered if r.kind == ARCHIVE)
        missing_players = tuple(r for r in offered if r.kind != ARCHIVE)
        return Playability(
            name, kept,
            absent=("no file on the library server -- the row is catalogued "
                    "and the bytes are not here, so nothing can play, stream "
                    "or download it until ROMarr fetches it"),
            alternatives=missing_players + alternatives)

    found: dict[str, list[Route]] = {}
    unreachable = ""

    for route in offered:
        found.setdefault(route.kind, []).append(route)

    if stream is not None:
        tier = None
        try:
            tier = stream.tier(slug)
        except Exception as exc:            # a LAN service being down is normal
            log.warning("stream server did not answer for %r: %s", slug, exc)
            unreachable = str(exc)
        if not unreachable and not getattr(stream, "reachable", True):
            unreachable = "no answer"
        # Who answered, and what is doing the rendering there. The engine used
        # to be the literal string "headless RetroArch", which was true of the
        # only stream server that existed and became a lie the moment a
        # Moonlight host could answer here -- an operator streaming PS2 off
        # Wolf's PCSX2 does not want to be told RetroArch is running it.
        #
        # Asked per platform rather than read off the object, because with
        # more than one server configured the one that answered for *this*
        # slug is the only one worth naming.
        where, engine = _stream_attribution(stream, slug)
        if tier == STREAM:
            found[STREAM] = [Route(
                STREAM,
                f"streams server-side from {where} ({engine})")]
        elif tier == LOCAL and LOCAL not in found:
            # The server distinguishes the tiers, and so must this: reporting
            # "streamed server-side" for something the browser runs is a lie
            # about where the work happens.
            found[LOCAL] = [Route(
                LOCAL, f"plays in the browser on EmulatorJS (per {where})",
                player=EMULATORJS)]

    if LOCAL not in found and STREAM not in found:
        # Prefer the stream server's own words when it gave any.
        #
        # It knows things this module cannot: which cores are installed, and
        # whether the firmware they need is present. Without that, a platform
        # whose core is installed but BIOS-less reads "configure a stream
        # server to play it here" to an operator who has already configured
        # one -- pointing at the wrong fix, and hiding the one that works.
        # That is the same firmware gate that exists because a core with no
        # BIOS does not fail loudly: it draws an error screen and streams it
        # at a perfectly healthy 30fps.
        served = _stream_reason(stream, slug) if stream is not None else ""
        if served:
            detail = f"download only -- {served}"
        elif ARCHIVE in found:
            # Not a dead end and must not read as one. Archive.org's page is
            # the route, and telling somebody to stand up a stream server for
            # a platform they can already play is advice that costs an evening
            # and changes nothing.
            detail = "downloadable"
        else:
            reason = _no_local_reason(slug, extension, alternatives)
            detail = (f"download only -- {reason}; configure a stream server "
                      "to play it here")
        found[DOWNLOAD] = [Route(DOWNLOAD, detail)]
    else:
        found[DOWNLOAD] = [Route(DOWNLOAD, "downloadable")]

    ordered: list[Route] = []
    for kind in _ORDER:
        ordered.extend(found.get(kind, ()))
    return Playability(name, tuple(ordered), stream_unreachable=unreachable,
                       alternatives=alternatives)


def _no_local_reason(slug: str, extension: str, alternatives) -> str:
    """Why nothing runs this in a browser, as specifically as it can be said.

    Three different sentences, because three different things are wrong. The
    platform having no core at all is `NO_EJS_CORE`'s answer and the oldest
    one. A file whose *extension* no core declares is a newer and much more
    common failure on a mixed library, and saying "no browser core for this
    platform" about a `.swf` on a platform full of them is simply false. And
    when a player would run it but is switched off or unconfigured, that is
    the operator's own setting and belongs at the top of the answer.
    """
    fixable = next((r.detail for r in alternatives if r.kind == LOCAL), "")
    if fixable:
        return fixable
    if extension and slug in EJS_CORES:
        return (f"no EmulatorJS core for {slug} declares `{extension}` "
                f"({', '.join(EJS_CORES[slug])})")
    return NO_EJS_CORE.get(slug, "no browser core for this platform")


# ---------------------------------------------------------- player routing --
#
# One function per player, each answering the same two questions about the
# same file: can you open this, and if you could, why are you not.
#
# They are separate rather than one table because the players do not fail
# alike, and the *reason* is the product here. EmulatorJS fails on an
# extension its cores do not declare. Ruffle fails on a file that is not a
# SWF, and separately on a SWF the library server will not offer it for.
# js-dos and Emularity mostly fail because nobody has stood one up. Four
# different sentences, four different fixes, and a single lookup table would
# have flattened all of them into "unsupported".


def _player_routes(slug, extension, policy) -> tuple[tuple[Route, ...],
                                                     tuple[Route, ...]]:
    """(routes on offer, routes that would be on offer), in the operator's order."""
    offered: list[Route] = []
    blocked: list[Route] = []
    handlers = {
        EMULATORJS: _emulatorjs_route,
        RUFFLE: _ruffle_route,
        JSDOS: _jsdos_route,
        EMULARITY: _emularity_routes,
    }
    # Every player is asked, including the disabled ones -- a player that is
    # off still has something worth saying, and "Emularity would play this,
    # you turned it off" is the whole point of being able to turn it off.
    for key in sorted(PLAYERS, key=lambda k: (policy.rank(k), k)):
        for route, why in handlers[key](slug, extension, policy):
            (blocked if why else offered).append(
                Route(route.kind, why or route.detail, player=key))
    return tuple(offered), tuple(blocked)


def _off(key: str, policy) -> str:
    """The one refusal every player shares, phrased so the fix is obvious."""
    if policy.enabled(key):
        return ""
    return (f"{PLAYERS[key].label} is turned off on this install "
            "(ROMARR_PLAYERS)")


def _emulatorjs_route(slug, extension, policy):
    """The library server's own player, and the only one already installed."""
    cores = EJS_CORES.get(slug)
    if not cores:
        return
    off = _off(EMULATORJS, policy)

    if not extension:
        # The platform question. Unchanged wording on purpose: this string is
        # on the Platforms page and in the status counts, and the point of
        # adding players was not to churn it.
        detail = f"plays in the browser on EmulatorJS ({', '.join(cores)})"
        yield Route(LOCAL, detail), off
        return

    matched = tuple(c for c in cores
                    if extension in CORE_EXTENSIONS.get(c, ()))
    if matched:
        detail = (f"plays in the browser on EmulatorJS -- `{extension}` is "
                  f"declared by {', '.join(matched)}")
    elif extension in EJS_ARCHIVES:
        # EmulatorJS unpacks the archive itself and hands the core what is
        # inside, so the archive extension says nothing about the core. Except
        # where the archive *is* the ROM: a MAME or FBNeo `.zip` is a romset
        # of chip dumps and the core opens it whole, which is why those cores
        # declare `zip` and why extracting one produces something unplayable.
        detail = (f"plays in the browser on EmulatorJS -- it unpacks "
                  f"`{extension}` in the browser and hands the ROM inside to "
                  f"{', '.join(cores)}")
    else:
        yield Route(LOCAL, ""), (
            f"EmulatorJS has cores for {slug} ({', '.join(cores)}) and none "
            f"of them declares `{extension}`")
        return

    if all(c in EJS_THREADED_CORES for c in (matched or cores)):
        # The failure this prevents is a black canvas and no error. Named
        # here rather than in a doc, because nobody reads a doc about a
        # player that appeared to load.
        detail += (" -- needs SharedArrayBuffer, so the library server must "
                   "serve cross-origin isolation headers (COOP + COEP)")
    yield Route(LOCAL, detail), off


def _ruffle_route(slug, extension, policy):
    """Flash, and the three ways a Flash request is not one."""
    off = _off(RUFFLE, policy)
    flash_platform = slug in RUFFLE_PLATFORMS

    if extension in RUFFLE_EXTENSIONS:
        if not flash_platform:
            # Ruffle would run it. The library server will not offer it,
            # because RomM's gate is the platform slug and not the file, and
            # ROMarr must not print a Play button that is not going to exist.
            yield Route(LOCAL, ""), (
                f"Ruffle runs any SWF, and the library server only offers its "
                f"Ruffle player on {' and '.join(RUFFLE_PLATFORMS)} -- this "
                f"row is filed under {slug}, so no Play button appears")
            return
        yield Route(LOCAL, f"plays in the browser on Ruffle ({RUFFLE_COVERAGE})"), off
        return

    if extension in NOT_A_SWF and flash_platform:
        yield Route(LOCAL, ""), (
            f"Ruffle cannot open `{extension}`: {NOT_A_SWF[extension]}")
        return

    if not extension and flash_platform:
        yield Route(LOCAL, "plays in the browser on Ruffle, for the `.swf` "
                           "files on this platform"), off


def _jsdos_route(slug, extension, policy):
    """DOS, from somebody else's page, and only if there is one."""
    if slug not in JSDOS_PLATFORMS and extension != ".jsdos":
        return
    if extension and extension not in JSDOS_EXTENSIONS:
        yield Route(LOCAL, ""), (
            f"js-dos mounts {' or '.join(JSDOS_EXTENSIONS)} bundles and this "
            f"is `{extension}`")
        return
    off = _off(JSDOS, policy)
    if off:
        yield Route(LOCAL, ""), off
        return
    where = policy.url(JSDOS)
    if not where:
        yield Route(LOCAL, ""), (
            "js-dos would run this on DOSBox or DOSBox-X; set "
            "ROMARR_JSDOS_URL to your js-dos player to get a link")
        return
    yield Route(LOCAL, f"plays in the browser on js-dos at {where} "
                       "(DOSBox / DOSBox-X, with 3Dfx and IPX)"), ""


def _emularity_routes(slug, extension, policy):
    """Two routes with one name, and they are genuinely different things.

    Archive.org's `/details/` page runs Emularity against *their* copy, which
    needs nothing from the operator and is why turning Emularity off is a
    meaningful setting -- it is the only route that sends somebody away from
    this install. A self-hosted Emularity runs the same loader against the
    operator's own file, needs a URL, and is a local route.
    """
    off = _off(EMULARITY, policy)

    if slug in ARCHIVE_EMULATED:
        yield Route(ARCHIVE,
                    f"plays in the page on Archive.org "
                    f"({ARCHIVE_EMULATED[slug]:,} emulated items, "
                    f"measured {ARCHIVE_MEASURED_ON})"), off

    engine = EMULARITY_ENGINES.get(slug)
    if not engine:
        return
    label, takes = engine
    if extension and extension not in takes:
        yield Route(LOCAL, ""), (
            f"Emularity's {label} takes {' or '.join(takes)} here and this "
            f"is `{extension}`")
        return
    if off:
        yield Route(LOCAL, ""), off
        return
    where = policy.url(EMULARITY)
    if not where:
        yield Route(LOCAL, ""), (
            f"a self-hosted Emularity would run this on {label}; set "
            "ROMARR_EMULARITY_URL to your own loader to get a link")
        return
    yield Route(LOCAL,
                f"plays in the browser on Emularity at {where} ({label})"), ""


def _stream_attribution(stream, slug: str) -> tuple[str, str]:
    """Who to name for `slug`, and what to call the thing rendering there.

    A single server answers for itself. A `StreamSources` holding several has
    to work out which of them claimed this platform, which is why the lookup
    takes a slug at all -- naming the first configured server for a route the
    second one granted is exactly the kind of confident wrong sentence this
    module exists to avoid.
    """
    attribute = getattr(stream, "attribution", None)
    if callable(attribute):
        try:
            label, engine = attribute(slug)
            return str(label), str(engine)
        except Exception:
            pass
    return (getattr(stream, "label", "the stream server"),
            getattr(stream, "engine", "headless RetroArch"))


def _stream_reason(stream, slug: str) -> str:
    """The stream server's own explanation, if it offered one."""
    why = getattr(stream, "why", None)
    if not callable(why):
        return ""
    try:
        return str(why(slug) or "")
    except Exception:
        return ""


class StreamServer:
    """Read-only client for a headless RetroArch stream server's routing.

    One endpoint, one question: could this server play this platform, and on
    which tier. It has no method that starts anything -- the session routes
    take a ROM name that must resolve inside the server's own directory, so
    there is nothing here for ROMarr to hand them anyway.

    Answers are cached, because a Library page must not make one HTTP call per
    row. **With a TTL, not for the process lifetime**, which is what it was
    until installing a core proved the assumption wrong: the routing table is
    built from what is on disk, and "that does not change while the server is
    up" is false the moment somebody installs a core and restarts it. Neo Geo
    CD went from unplayable to streaming and ROMarr kept saying "no emulator
    exists" until it was restarted, which is a confusing way to be told that
    your work succeeded.

    **A server that is down stops being asked**, which is not an optimisation.
    Only successful answers are cached -- a failure must not be remembered as
    "cannot play this" -- so without a breaker the first status page after the
    stream server goes down makes one timing-out request per platform. At
    thirty-odd platforms and a five-second timeout that is nearly three
    minutes of hanging page for a service that is optional. Measured at 69
    seconds before this existed.
    """

    #: How long to stop asking after a failure. Long enough that a down server
    #: costs one timeout per page rather than thirty; short enough that a
    #: restarted one is picked up without restarting ROMarr.
    DOWN_COOLDOWN = 30.0

    #: How long a successful answer is trusted. Short enough that installing a
    #: core is visible without restarting ROMarr, long enough that browsing
    #: costs nothing.
    CACHE_TTL = 300.0

    def __init__(self, base_url: str, timeout: float = STREAM_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._reachable = True
        self._problem = ""
        self._down_until = 0.0
        self._why: dict[str, str] = {}

    def why(self, slug: str) -> str:
        """Why this server cannot play `slug`, in its own words.

        The server distinguishes "no core exists", "the core is not installed"
        and "supply firmware" -- three very different problems for an operator,
        and only it knows which applies. Blank when it has not been asked, or
        answered with a tier.
        """
        return self._why.get(slug, "")

    @property
    def label(self) -> str:
        return self.base_url or "the stream server"

    @property
    def reachable(self) -> bool:
        return self._reachable

    def tier(self, slug: str) -> str | None:
        """"local", "stream", or None when the server cannot play it.

        None is also the answer when the server is unreachable. The caller
        tells the two apart with `reachable`, because "cannot play this" and
        "did not answer" lead an operator to different fixes.
        """
        if not self.base_url or not slug:
            return None
        cached = self._cache.get(slug)
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]
        if time.monotonic() < self._down_until:
            self._reachable = False
            return None

        url = (f"{self.base_url}{PLAY_ROUTE_PATH}?"
               + urllib.parse.urlencode({"platform": slug}))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            tier = body.get("tier")
            tier = tier if isinstance(tier, str) else None
            self._reachable = True
        except urllib.error.HTTPError as exc:
            # 404 is the server's documented "unplayable" answer and is a real
            # answer, not a failure -- it carries `why`.
            self._reachable = True
            tier = None
            if exc.code == 404:
                try:
                    said = json.loads(exc.read().decode("utf-8")).get("why")
                    if isinstance(said, str) and said:
                        self._why[slug] = said
                except Exception:
                    pass
            else:
                self._reachable, self._problem = False, f"HTTP {exc.code}"
        except Exception as exc:
            self._reachable, self._problem = False, str(exc)
            tier = None

        if self._reachable:
            self._cache[slug] = (tier, time.monotonic() + self.CACHE_TTL)
            self._down_until = 0.0
        else:
            self._down_until = time.monotonic() + self.DOWN_COOLDOWN
        return tier


# ===========================================================================
# Moonlight hosts: Wolf, Sunshine, Steam Headless
# ===========================================================================
#
# A different animal from the RetroArch stream server above, and the
# difference is the whole reason this section is long.
#
# The stream server was *built* to answer "can you play this platform". It has
# `/api/play/route`, it reads its own core directory, and one GET settles the
# question. A Moonlight host has no such endpoint and never will, because it
# is not a platform router -- it is a machine with a desktop on it. Wolf runs
# containers, Sunshine runs whatever you put in `apps.json`, Steam Headless
# runs Steam. None of them has any concept of "PlayStation 2"; they have a
# list of *applications*, and what those applications can open is a question
# about somebody's filesystem that no API exposes.
#
# So the honest position, and the one every table below implements:
#
#   * A reachable Moonlight host is worth reporting. It is a real play route
#     for a human being, and a status page that omits it is lying by silence.
#   * A reachable Moonlight host grants a *platform* a stream route only when
#     an app on it names an emulator that plays exactly one machine. "PCSX2"
#     is evidence. "RetroArch" and "Steam" are not, and are refused below by
#     name rather than by accident.
#   * When the app list could not be read at all -- which is the default,
#     because reading it needs an admin credential ROMarr may not have -- the
#     host grants nothing and says so. Reachability is not capability.
#
# The over-claim this avoids is specific and was worth designing around: an
# operator with Wolf running sees "PS2 streams from Wolf", clicks a PS2 game,
# and gets Wolf's app list with Firefox and Steam on it. That is a worse
# outcome than never having offered the route.


#: Wolf, Sunshine and NVIDIA's original GameStream all speak this. The base
#: port is 47989 for plain HTTP and 47984 for TLS; Wolf hardcodes the same two
#: (`state/data-structures.hpp`, `HTTP_PORT`/`HTTPS_PORT`) and Sunshine maps
#: them off the same base, so one probe reaches every implementation.
MOONLIGHT_HTTP_PORT = 47989
MOONLIGHT_HTTPS_PORT = 47984

#: Sunshine's admin API and web UI, which is the Moonlight base port plus one.
#: HTTPS with a self-signed certificate, always -- see `_sunshine_call` for
#: why that forces an unverified context and what is given up by it.
SUNSHINE_WEB_PORT = 47990

#: The one thing every Moonlight host answers without a credential, without a
#: certificate and without being paired.
#:
#: It is served on the *plain HTTP* port by both implementations
#: (`nvhttp.cpp` `http_server.resource["^/serverinfo$"]` in Sunshine,
#: `servers.cpp` `server->resource["^/serverinfo$"]` in Wolf) and it returns
#: XML naming the host, its version, its HTTPS port and whether a game is
#: currently running. Everything else in the Moonlight protocol -- `/applist`,
#: `/launch`, `/resume` -- is registered on the HTTPS server behind a *client
#: certificate check*, so this is not a starting point that leads anywhere:
#: it is the entire unauthenticated surface, and it is a dead end by design.
SERVERINFO_PATH = "/serverinfo"

#: Host kinds. Declared by the operator, never sniffed.
#:
#: There is no reliable way to tell Wolf from Sunshine over `/serverinfo`.
#: Wolf emits the same field set, including the literal `SUNSHINE_SERVER_FREE`
#: in `root.state` (`moonlight.cpp`), because Moonlight clients require that
#: exact string. Wolf's stock `hostname` is "Wolf", which is a hint and not a
#: fact -- it is the first thing an operator renames -- so it is not acted on.
WOLF = "wolf"
SUNSHINE = "sunshine"
STEAM_HEADLESS = "steam-headless"
MOONLIGHT_KINDS = (WOLF, SUNSHINE, STEAM_HEADLESS)

#: What each kind is called, for the UI and the route detail.
#:
#: Steam Headless is not a fourth protocol and is not modelled as one: it is a
#: container that runs Sunshine (`ENABLE_SUNSHINE=true`, port 47990) next to a
#: noVNC/neko desktop. Its streaming surface *is* Sunshine's, so it reuses
#: Sunshine's client wholesale and differs only in what the UI calls it and in
#: carrying a web-desktop URL alongside.
KIND_LABELS: dict[str, str] = {
    WOLF: "Wolf",
    SUNSHINE: "Sunshine",
    STEAM_HEADLESS: "Steam Headless",
}

#: Wolf's REST API prefix. UNIX socket only unless the operator proxies it.
WOLF_API_PREFIX = "/api/v1"

#: Sunshine's admin REST API prefix, on `SUNSHINE_WEB_PORT`.
SUNSHINE_API_PREFIX = "/api"

#: Emulators that play exactly one machine, so their presence on a host's app
#: list is evidence rather than a guess.
#:
#: Matched against the app *title*, case-insensitively. Every entry here is an
#: emulator whose entire purpose is one console -- there is no configuration
#: of PCSX2 that plays a Dreamcast disc -- which is what makes the inference
#: safe. Dolphin carries two slugs because Dolphin genuinely is the GameCube
#: *and* Wii emulator, not because the table is hedging.
#:
#: Anything whose scope depends on what the operator installed inside it is
#: deliberately absent; see `MULTI_MACHINE_APPS`. The empty tuples are entries
#: that were considered and rejected, kept so the reasoning is on record.
EMULATOR_APPS: dict[str, tuple[str, ...]] = {
    "pcsx2": ("ps2",),
    "dolphin": ("ngc", "wii"),
    "flycast": ("dc",),
    "redream": ("dc",),
    "reicast": ("dc",),
    "duckstation": ("psx",),
    "ppsspp": ("psp",),
    "azahar": ("3ds",),
    "citra": ("3ds",),
    "lime3ds": ("3ds",),
    "melonds": ("nds",),
    "desmume": ("nds",),
    "yabause": ("saturn",),
    "dosbox": ("dos",),
    # Considered and rejected, kept so the reasoning is on record rather than
    # rediscovered. Mesen was briefly mapped to `nes` and that was wrong:
    # Mesen 2 also runs SNES, Game Boy and PC Engine, so the name does not
    # narrow to one machine and the whole basis of this table collapses.
    "mesen": (),
    "mednafen": (),          # multi-system, for the same reason
    "scummvm": (),           # not a platform ROMarr models
}

#: Apps that are real and useful and prove nothing about any one platform.
#:
#: This table exists so that "we checked" cannot decay into "nobody looked" --
#: the same reason `NO_EJS_CORE` exists. Each of these ships in Wolf's stock
#: `config.v5.toml` or is the obvious thing an operator adds, each will happily
#: play half this library, and none of them can be *asked* what it will play.
#: A RetroArch with no cores installed and a RetroArch with forty look
#: identical from outside the container.
MULTI_MACHINE_APPS: dict[str, str] = {
    "retroarch": ("RetroArch plays whatever cores are installed inside it, "
                  "and the app list does not say which"),
    "emulationstation": ("EmulationStation is a frontend; what it launches "
                         "is decided by config files ROMarr cannot see"),
    "pegasus": ("Pegasus is a frontend; what it launches is decided by "
                "config files ROMarr cannot see"),
    "steam": "Steam plays PC games, not the platforms in this library",
    "lutris": "Lutris runs whatever runners are installed inside it",
    "kodi": "Kodi is a media player",
    "firefox": "Firefox is a browser",
    "prismlauncher": "Prism Launcher runs Minecraft",
    "desktop": ("a desktop session plays whatever is installed on it, which "
                "is not a question any API answers"),
    "testball": "Wolf's built-in test pattern",
    # Ships on every current Wolf -- it and "Test ball" were the *only* two
    # entries a live `GET /api/v1/apps` returned -- so it is named here rather
    # than left to fall through as something nobody had looked at.
    "wolfui": "Wolf's own configuration UI",
}

#: How long to wait on a Moonlight host. Same reasoning as `STREAM_TIMEOUT`:
#: a LAN service that is down must not hold up a page.
MOONLIGHT_TIMEOUT = 5.0

#: One sentence, kept in one place, because it has to appear in the API, the
#: UI and the docs and must not drift between them.
PAIRING_IS_MANUAL = (
    "Moonlight pairing cannot be automated. The PIN is created by your "
    "Moonlight client, on your device -- no host API can produce it or skip "
    "it. Start pairing in Moonlight, read the PIN it shows you, and type it "
    "here.")


def _match_app(title: str) -> tuple[str, tuple[str, ...]] | None:
    """Which table entry an app title matches, if any.

    Compared against the title with its spaces and punctuation removed,
    because the titles in the wild are "PCSX2", "pcsx2-qt", "Dolphin
    Emulator", "RetroArch (Flatpak)" and Wolf's own "Test ball" -- and a
    word-boundary match misses half of those. The single-machine table is
    consulted first so that a host with both PCSX2 and RetroArch on it still
    gains PS2 rather than being talked out of it by the generic entry.
    """
    squashed = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    if not squashed:
        return None
    for name, slugs in EMULATOR_APPS.items():
        if name in squashed:
            return name, slugs
    for name in MULTI_MACHINE_APPS:
        if name in squashed:
            return name, ()
    return None


def platforms_from_apps(titles) -> dict[str, str]:
    """slug -> the app title that proves it, for every app that proves one.

    The public form of the rule this whole section exists to state: an app
    list becomes platform coverage only through `EMULATOR_APPS`, and only for
    machines ROMarr actually models.
    """
    found: dict[str, str] = {}
    for title in titles or ():
        matched = _match_app(title)
        if not matched:
            continue
        for slug in matched[1]:
            if resolve(slug) is not None:
                found.setdefault(slug, title)
    return found


@dataclass
class MoonlightPairing:
    """What a human has to do, and what ROMarr can do around it.

    **Moonlight pairing cannot be automated, and this class does not pretend
    otherwise.** The protocol is a four-phase exchange in which the *client*
    generates a PIN, both sides derive `SHA256(SALT + PIN)[0:16]` as an AES
    key, and the client proves possession of it. Wolf's own field
    documentation says it outright -- `PairRequest.pin` in `api/api.hpp` is
    annotated "The PIN created by the remote Moonlight client". The PIN exists
    on a screen in somebody's hand. No API on the host side can produce it,
    guess it, or skip it, and any ROMarr feature that claimed to pair a client
    on its own would be a lie with a progress bar.

    What ROMarr *can* do is everything on either side of the human:

      * notice that a client is waiting (Wolf lists them; Sunshine does not),
      * be the box the PIN is typed into, and post it to the host,
      * report afterwards whether the host now lists the client as paired.

    That is a real convenience -- it is one fewer admin panel to find, and on
    Wolf it replaces hunting through container logs for a URL with a secret
    in the fragment -- and it is the whole of what is possible.
    """

    #: Pending requests the host is willing to name. Wolf answers this from
    #: `/api/v1/pair/pending` with `{pair_secret, client_ip}` per request.
    #: Sunshine has no equivalent endpoint at all, so this is always empty for
    #: Sunshine and Steam Headless and the UI must not read empty as "nobody
    #: is waiting" there.
    pending: list[dict] = field(default_factory=list)
    #: False when the host has no way to report pending requests, so that the
    #: absence of a list can be told apart from an empty one.
    can_list_pending: bool = True
    #: Why pending requests could not be listed, in one line.
    detail: str = ""


class MoonlightHost:
    """Read-mostly client for a Wolf, Sunshine or Steam Headless host.

    Duck-types `StreamServer` -- `tier`, `why`, `reachable`, `label` -- so
    `routes_for` consumes it unchanged. That was the point of extending the
    stream tier rather than inventing a fifth route: "played somewhere else
    and delivered as video" is one idea, and an operator does not care whether
    the pixels came from RetroArch or from Wolf.

    Three things this can do, and it is worth being blunt that the list is
    short because the hosts are what they are, not because the client is
    unfinished:

      * **Probe.** `GET /serverinfo` on the plain HTTP port, no credential.
        Works on every implementation. Yields the host's name, version and
        whether a game is running. This is the only call that never fails for
        want of configuration.
      * **List apps.** Needs the admin API: Sunshine's `GET /api/apps` behind
        basic auth on 47990, or Wolf's `GET /api/v1/apps` over its UNIX
        socket. Without one of those, ROMarr knows the host is up and knows
        nothing else, which is exactly what it then reports.
      * **Relay a pairing PIN.** See `MoonlightPairing` for why "relay" is
        the strongest verb available.

    Three things it deliberately cannot do:

      * **Launch a game.** `/launch` is on the Moonlight HTTPS server behind a
        paired client certificate, in both implementations. Launching would
        mean ROMarr pairing *itself* as a Moonlight client -- implementing the
        four-phase crypto, holding a client key, and asking a human for a PIN
        on ROMarr's behalf -- and it would still have nowhere to put the video
        afterwards. The UI hands over a host address and a command line
        instead.
      * **Deep-link into a Moonlight client.** There is no registered
        `moonlight://` URI scheme to hand a browser. moonlight-qt uses that
        string internally in `computermanager.cpp` to parse a typed-in host
        address and registers no scheme handler; the request to add one
        (moonlight-qt#29) is still a request. So the UI offers the
        `moonlight stream <host>` command line, which does exist.
      * **Know what an app can open.** See the header of this section.
    """

    #: Same breaker and TTL reasoning as `StreamServer`, and for the same
    #: measured reason: without them a status page makes one timing-out
    #: request per platform against a host that is simply switched off.
    DOWN_COOLDOWN = 30.0
    CACHE_TTL = 300.0

    def __init__(self, host: str, *, kind: str = WOLF,
                 username: str = "", password: str = "",
                 socket_path: str = "", api_url: str = "",
                 desktop_url: str = "", timeout: float = MOONLIGHT_TIMEOUT):
        self.host, self.port = _split_host(host, MOONLIGHT_HTTP_PORT)
        self.kind = kind if kind in MOONLIGHT_KINDS else WOLF
        self.username = username
        self.password = password
        #: Wolf only. The UNIX socket its API listens on -- `WOLF_SOCKET_PATH`
        #: in Wolf's own environment, `/var/run/wolf/wolf.sock` by convention.
        self.socket_path = socket_path
        #: Wolf only. Set when the operator has put the documented nginx proxy
        #: in front of the socket, which is the only way Wolf blesses for
        #: reaching its API across a container boundary.
        self.api_url = (api_url or "").rstrip("/")
        #: Steam Headless only. Its noVNC/neko desktop, which is a genuine
        #: second way in and is a link, not an integration.
        self.desktop_url = (desktop_url or "").rstrip("/")
        self.timeout = timeout

        self._reachable = False
        self._problem = "not probed"
        self._info: dict = {}
        self._apps: list[str] = []
        self._apps_read = False
        self._apps_problem = ""
        self._coverage: dict[str, str] = {}
        self._down_until = 0.0
        self._fresh_until = 0.0

    # -- the shape `routes_for` consumes ------------------------------------

    @property
    def label(self) -> str:
        name = self._info.get("hostname") or ""
        where = f"{self.host}:{self.port}"
        return f"{name} ({where})" if name else where

    @property
    def engine(self) -> str:
        """What the STREAM route detail calls the thing doing the rendering."""
        return KIND_LABELS.get(self.kind, "Moonlight")

    @property
    def reachable(self) -> bool:
        return self._reachable

    def tier(self, slug: str) -> str | None:
        """`"stream"` when an app on this host plays `slug`, else None.

        Never `"local"`: a Moonlight host renders somewhere else and sends
        video, which is the definition of the stream tier and is never the
        browser tier however the pixels get drawn.
        """
        self.refresh()
        if not self._reachable:
            return None
        return STREAM if slug in self._coverage else None

    def why(self, slug: str) -> str:
        """Why this host does not play `slug`, in terms an operator can act on.

        Three different sentences for three different fixes, because "I have
        not been told your admin password", "your app list has no PS2
        emulator" and "your host is off" want three different next steps.
        """
        if not self._reachable:
            return ""
        if not self._apps_read:
            return (f"{self.engine} is reachable at {self.host}:{self.port} "
                    "but ROMarr cannot read its app list "
                    f"({self._apps_problem or 'no admin credential configured'})"
                    ", so it cannot say which platforms it plays")
        if slug in self._coverage:
            return ""
        return (f"{self.engine} is reachable and its app list has no emulator "
                "for this platform; install one and it appears here")

    # -- probing ------------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Probe, and read the app list if there is a way in.

        Cached with the same TTL as `StreamServer` for the same reason: a
        Library page must not make one HTTP call per row, and installing an
        emulator must become visible without restarting ROMarr.
        """
        now = time.monotonic()
        if not force:
            if now < self._fresh_until:
                return
            if now < self._down_until:
                self._reachable = False
                return

        self._probe()
        if self._reachable:
            self._read_apps()
            self._fresh_until = now + self.CACHE_TTL
            self._down_until = 0.0
        else:
            self._down_until = now + self.DOWN_COOLDOWN
            self._fresh_until = 0.0

    def _probe(self) -> None:
        """`GET /serverinfo`, the one call that needs nothing."""
        url = f"http://{self.host}:{self.port}{SERVERINFO_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception as exc:                # a host being off is normal
            log.info("moonlight host %s did not answer: %s", self.label, exc)
            self._reachable, self._problem = False, str(exc)
            self._info = {}
            self._apps, self._apps_read, self._coverage = [], False, {}
            return
        self._info = _serverinfo_fields(body)
        # Answering at all is the signal. Both implementations hardcode a 200
        # status_code into the body, so it is read and reported rather than
        # used as a gate ROMarr invented.
        self._reachable, self._problem = True, ""

    def _read_apps(self) -> None:
        """Fetch the app list, if this host has given ROMarr a way to."""
        self._apps, self._apps_read, self._apps_problem = [], False, ""
        self._coverage = {}
        try:
            if self.kind == WOLF:
                titles = self._wolf_apps()
            else:
                titles = self._sunshine_apps()
        except _NoWayIn as exc:
            self._apps_problem = str(exc)
            return
        except Exception as exc:
            log.warning("could not read apps from %s: %s", self.label, exc)
            self._apps_problem = str(exc)
            return
        self._apps = titles
        self._apps_read = True
        self._coverage = platforms_from_apps(titles)

    def _wolf_apps(self) -> list[str]:
        """Every app Wolf has, which is not what `/api/v1/apps` returns.

        Observed against a live Wolf (`stable`, 2026-08-11) and worth the
        extra call: its stock `config.toml` declares **no top-level apps at
        all**. Every entry -- including all nine of Firefox, RetroArch, Steam,
        Pegasus, Lutris, Prismlauncher, Desktop (xfce), EmulationStation and
        Kodi -- lives under `[[profiles.apps]]`, and `GET /api/v1/apps`
        answered with only `Wolf UI` and `Test ball`. Its OpenAPI schema
        describes that endpoint as "all apps that will be shown in the
        Moonlight client", so this is a per-profile view rather than the whole
        host.

        Reading `/apps` alone therefore missed everything an operator would
        actually install, and a PCSX2 added the only way Wolf's own config
        offers would never have granted PS2 anything. The union is what "what
        does this host have on it" means. `/profiles` failing must not lose
        the `/apps` answer, so it is allowed to fail quietly -- a Wolf too old
        to have profiles is still a Wolf.
        """
        titles = [str(a.get("title") or a.get("name") or "")
                  for a in (self._wolf_get("/apps").get("apps") or [])]
        try:
            profiles = self._wolf_get("/profiles").get("profiles") or []
        except Exception as exc:
            log.info("wolf profiles unreadable on %s: %s", self.label, exc)
            return titles
        for profile in profiles:
            for app in profile.get("apps") or []:
                title = str(app.get("title") or app.get("name") or "")
                if title and title not in titles:
                    titles.append(title)
        return titles

    def _sunshine_apps(self) -> list[str]:
        body = self._sunshine_get("/apps")
        return [str(a.get("name") or "") for a in (body.get("apps") or [])]

    # -- pairing ------------------------------------------------------------

    def pairing(self) -> MoonlightPairing:
        """Clients waiting to pair, when the host will name them."""
        if self.kind != WOLF:
            return MoonlightPairing(
                can_list_pending=False,
                detail=("Sunshine has no endpoint that lists waiting clients "
                        "-- its API can accept a PIN and cannot tell you one "
                        "is wanted. Start the pairing from your Moonlight "
                        "client, then type the PIN it shows."))
        try:
            body = self._wolf_get("/pair/pending")
        except Exception as exc:
            return MoonlightPairing(can_list_pending=False, detail=str(exc))
        out = []
        for req in body.get("requests") or []:
            secret = str(req.get("pair_secret") or "")
            out.append({
                "pair_secret": secret,
                "client_ip": str(req.get("client_ip") or ""),
                # The page Wolf logs at startup, reconstructed rather than
                # fished out of container logs. Wolf serves it on the plain
                # HTTP port and puts the secret in the fragment, which is why
                # it can be rebuilt from the secret alone.
                "pin_url": (f"http://{self.host}:{self.port}/pin/#{secret}"
                            if secret else ""),
            })
        return MoonlightPairing(pending=out)

    def submit_pin(self, pin: str, *, pair_secret: str = "",
                   name: str = "ROMarr") -> dict:
        """Hand the host a PIN a human read off their Moonlight client.

        Returns `{ok, detail}` and is careful about what `ok` means, because
        neither host will actually tell you the pairing succeeded:

          * Wolf resolves its internal promise and answers 200 as soon as the
            secret matches. Whether the PIN was *right* is decided afterwards,
            inside the crypto, and is never reported back on this call.
          * Sunshine's `POST /api/pin` returns `{"status": true}` whenever any
            pairing request is outstanding, correct PIN or not -- reported
            upstream as Sunshine#3944 and **observed here** against a live
            2026.516.143833: a deliberately wrong PIN answered `true` and the
            client never appeared in `/api/clients/list`. Its `false` is
            meaningful; its `true` is not.

        Sunshine's `false` means one thing and not two: no client is waiting.
        A malformed PIN never reaches `nvhttp::pin` -- `confighttp` rejects it
        first with **HTTP 400** and `{"error": "PIN must be between 0000 and
        9999"}`, which arrives here as an exception carrying that sentence.

        So a success here means "the host accepted the submission", and the
        only way to know the client actually paired is to look at the paired
        client list afterwards. The UI says that in those words.
        """
        pin = (pin or "").strip()
        if not pin:
            return {"ok": False, "detail": "no PIN given"}
        try:
            if self.kind == WOLF:
                if not pair_secret:
                    return {"ok": False,
                            "detail": ("Wolf needs the pair secret of the "
                                       "waiting request; refresh the pending "
                                       "list and try again")}
                self._wolf_post("/pair/client",
                                {"pair_secret": pair_secret, "pin": pin})
            else:
                body = self._sunshine_post("/pin", {"pin": pin, "name": name})
                if body.get("status") in (False, "false"):
                    return {"ok": False,
                            "detail": ("Sunshine refused it: no Moonlight "
                                       "client is waiting to pair. Start the "
                                       "pairing on your client first.")}
        except Exception as exc:
            log.warning("PIN submission to %s failed: %s", self.label, exc)
            return {"ok": False, "detail": str(exc)}
        return {"ok": True,
                "detail": ("Submitted. The host does not report whether the "
                           "PIN was correct -- check that your client now "
                           "appears in the paired list.")}

    def paired_clients(self) -> list[dict]:
        """Who this host considers paired. The only honest pairing verdict."""
        try:
            if self.kind == WOLF:
                body = self._wolf_get("/clients")
                return [{"id": str(c.get("client_id") or ""), "name": ""}
                        for c in body.get("clients") or []]
            body = self._sunshine_get("/clients/list")
            rows = body.get("named_certs") or body.get("clients") or []
            return [{"id": str(c.get("uuid") or ""),
                     "name": str(c.get("name") or "")} for c in rows]
        except Exception as exc:
            log.info("could not list paired clients on %s: %s", self.label, exc)
            return []

    # -- reporting ----------------------------------------------------------

    def status(self) -> dict:
        """Everything the UI needs to describe this host without over-claiming."""
        self.refresh()
        pairing = (self.pairing() if self._reachable
                   else MoonlightPairing(can_list_pending=False,
                                         detail="host not reachable"))
        return {
            "configured": True,
            "kind": self.kind,
            "kind_label": self.engine,
            "host": self.host,
            "port": self.port,
            "label": self.label,
            "ok": self._reachable,
            "problem": self._problem,
            "hostname": self._info.get("hostname", ""),
            "appversion": self._info.get("appversion", ""),
            "https_port": self._info.get("HttpsPort", ""),
            "busy": self._info.get("state", "") == "SUNSHINE_SERVER_BUSY",
            "current_game": self._info.get("currentgame", ""),
            "apps_read": self._apps_read,
            "apps": list(self._apps),
            "apps_problem": self._apps_problem,
            "platforms": dict(self._coverage),
            "paired": self.paired_clients() if self._reachable else [],
            "desktop_url": self.desktop_url,
            "pairing": {
                "can_list_pending": pairing.can_list_pending,
                "pending": pairing.pending,
                "detail": pairing.detail,
                # Stated on every response rather than in a doc nobody opens.
                "manual": PAIRING_IS_MANUAL,
            },
            # Not a link. There is no `moonlight://` scheme handler to link
            # to; this is the command line that does exist.
            "connect_hint": f"moonlight stream {self.host}",
        }

    # -- transports ---------------------------------------------------------

    def _wolf_get(self, path: str) -> dict:
        return self._wolf_call("GET", path, None)

    def _wolf_post(self, path: str, payload: dict) -> dict:
        return self._wolf_call("POST", path, payload)

    def _wolf_call(self, method: str, path: str, payload) -> dict:
        """Wolf's API, over its UNIX socket or the operator's own TCP proxy.

        Wolf binds this to a UNIX socket and nothing else. Its documentation
        is emphatic about why -- "via the API you can pair clients to the
        server, execute arbitrary commands, and more" -- and the only exposure
        route it blesses is an nginx `proxy_pass` to the socket, which the
        operator sets up and secures. ROMarr therefore takes either a socket
        path it can open directly (same host, socket mounted) or that proxy's
        URL, and takes neither by default.
        """
        url_path = f"{WOLF_API_PREFIX}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}

        if self.api_url:
            return _json_request(f"{self.api_url}{url_path}", method,
                                 body, headers, self.timeout)
        if not self.socket_path:
            raise _NoWayIn(
                "Wolf's API is on a UNIX socket; set WOLF_SOCKET_PATH to a "
                "mounted wolf.sock, or WOLF_API_URL to an nginx proxy in "
                "front of it")
        if not hasattr(socket, "AF_UNIX"):
            raise _NoWayIn(
                "this platform has no AF_UNIX, so Wolf's socket cannot be "
                "opened here; use WOLF_API_URL with the documented proxy")
        conn = _UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            conn.request(method, url_path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", "replace")
            if response.status >= 400:
                raise RuntimeError(_error_sentence(response.status, raw[:400]))
            return json.loads(raw) if raw.strip() else {}
        finally:
            conn.close()

    def _sunshine_get(self, path: str) -> dict:
        return self._sunshine_call("GET", path, None)

    def _sunshine_post(self, path: str, payload: dict) -> dict:
        return self._sunshine_call("POST", path, payload)

    def _sunshine_call(self, method: str, path: str, payload) -> dict:
        """Sunshine's admin API: HTTPS on 47990, basic auth, self-signed.

        Two things about this are worth stating rather than leaving to be
        discovered.

        The certificate is generated by Sunshine on first run and signed by
        nobody, so verification is turned off -- which means this connection
        is encrypted and *not* authenticated, and somebody on the path could
        impersonate the host and collect the admin password. That is a real
        hole, it is the operator's LAN either way, and it belongs in
        SECURITY.md rather than in a comment apologising for itself.

        CSRF is not in the way, and that is by Sunshine's design rather than
        by luck: `validate_csrf_token` in `confighttp.cpp` returns true
        outright when a request carries neither `Origin` nor `Referer`, on the
        reasoning that a page in a browser cannot make a non-browser client
        issue requests. A server-side call from here sends neither header.
        """
        if not (self.username and self.password):
            raise _NoWayIn(
                "Sunshine's API needs its admin username and password; set "
                "MOONLIGHT_USER and MOONLIGHT_PASS")
        url = (f"https://{self.host}:{SUNSHINE_WEB_PORT}"
               f"{SUNSHINE_API_PREFIX}{path}")
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            # Sunshine's `check_content_type` rejects the request outright
            # without this, before authentication is even considered.
            headers["Content-Type"] = "application/json"
        return _json_request(url, method, body, headers, self.timeout,
                             insecure=True)


class _NoWayIn(Exception):
    """ROMarr has not been given a route to this host's admin API.

    Distinct from a network failure on purpose: one is fixed by configuring
    ROMarr and the other by fixing the host, and reporting both as "error"
    sends operators to the wrong one.
    """


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a UNIX socket, which `http.client` almost supports already.

    Wolf's API has no TCP port to point `urllib` at, and taking a dependency
    to get eight lines of socket setup would be the wrong trade in a project
    that ships stdlib plus `requests`.
    """

    def __init__(self, socket_path: str, timeout: float = MOONLIGHT_TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _json_request(url: str, method: str, body, headers: dict,
                  timeout: float, *, insecure: bool = False) -> dict:
    """One JSON call, with the errors kept legible."""
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in (401, 403):
            raise _NoWayIn(f"HTTP {exc.code}: the credential was refused")
        raise RuntimeError(_error_sentence(exc.code, detail)) from exc
    return json.loads(raw) if raw.strip() else {}


def _error_sentence(status: int, raw: str) -> str:
    """A failure an operator can read, out of a body meant for a browser.

    Both hosts already say something useful and then bury it: Sunshine
    answers a malformed PIN with `{"error": "PIN must be between 0000 and
    9999", ...}` and Wolf answers a stale pair secret with
    `{"success": false, "error": "Invalid pair secret"}`. Both reach the UI
    through `submit_pin`'s `detail`, so pasting the raw JSON there put braces
    and a status_code in front of the one sentence that mattered.
    """
    try:
        said = str((json.loads(raw) or {}).get("error") or "").strip()
    except ValueError:
        said = ""
    return f"HTTP {status}: {said or raw}"


def _serverinfo_fields(xml: str) -> dict:
    """The flat fields out of a `/serverinfo` body.

    Read with a regex rather than an XML parser, deliberately: this is
    untrusted input from a service on somebody's LAN, `xml.etree` is
    documented as unsafe against hostile documents, and the ten scalar fields
    wanted here do not justify a `defusedxml` dependency. The nested
    display-mode list is not wanted and is not extracted.
    """
    found: dict[str, str] = {}
    for tag, value in re.findall(r"<([A-Za-z][\w.]*)>([^<]*)</\1>", xml or ""):
        found.setdefault(tag, value.strip())
    return found


def _split_host(value: str, default_port: int) -> tuple[str, int]:
    """`host`, `host:port` or a full URL -> (host, port).

    A URL is accepted because operators paste one; its scheme is discarded
    rather than honoured, because the Moonlight probe port is not an HTTPS
    port and pasting `https://host` must not quietly produce a host nothing
    can reach.
    """
    text = (value or "").strip()
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        return parsed.hostname or "", parsed.port or default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        try:
            return host.strip(), int(port)
        except ValueError:
            return host.strip(), default_port
    return text, default_port


class StreamSources:
    """Several stream servers behind the one slot `routes_for` has.

    An operator can plausibly run both -- the headless RetroArch server for
    the retro machines it was built for, and Wolf or Sunshine for a PS2
    emulator on a real GPU -- and `routes_for` takes a single `stream`. Rather
    than teach it to take a list, and have every caller and every test learn a
    second shape, this presents any number of sources as one.

    First answer wins, in the order given, and that order is not arbitrary:
    the sources are not equally sure of themselves. The RetroArch server
    *knows* which cores it has installed, while a Moonlight host is inferring
    from app names, and an inferred answer must not displace a known one.
    """

    def __init__(self, *sources):
        self.sources = [s for s in sources if s is not None]

    @property
    def label(self) -> str:
        for source in self.sources:
            if getattr(source, "reachable", True):
                return getattr(source, "label", "the stream server")
        return "the stream server"

    @property
    def engine(self) -> str:
        for source in self.sources:
            if getattr(source, "reachable", True):
                return getattr(source, "engine", "headless RetroArch")
        return "headless RetroArch"

    @property
    def reachable(self) -> bool:
        # True when *any* source answered, because the flag exists to tell
        # "cannot play this" apart from "nothing was reachable", and one live
        # server means the first of those is the honest reading.
        return any(getattr(s, "reachable", True) for s in self.sources)

    def tier(self, slug: str) -> str | None:
        """The first source that claims `slug`, asked in confidence order."""
        for source in self.sources:
            try:
                answer = source.tier(slug)
            except Exception as exc:
                # One dead source must not hide a live one. `StreamServer`
                # signals failure by raising, so swallowing here and carrying
                # on is the difference between "Wolf plays your PS2 games" and
                # a blank page because RetroArch was rebooting.
                log.warning("stream source %r failed for %r: %s",
                            getattr(source, "label", source), slug, exc)
                continue
            if answer:
                return answer
        return None

    def attribution(self, slug: str) -> tuple[str, str]:
        """Name the source that actually claimed `slug`, not the first one.

        Re-asks rather than remembering which source answered a moment ago.
        Both source types cache their answers with a TTL, so the second ask is
        free, and a remembered value would be wrong the moment two requests
        overlapped -- the status page walks 59 platforms, and a threaded
        server can be walking two lists at once.
        """
        for source in self.sources:
            try:
                if source.tier(slug):
                    return (getattr(source, "label", "the stream server"),
                            getattr(source, "engine", "headless RetroArch"))
            except Exception:
                continue
        return self.label, self.engine

    def why(self, slug: str) -> str:
        """The first source that has something to say about `slug`.

        Sources that cannot explain themselves are skipped rather than allowed
        to end the search with an empty string -- otherwise a silent RetroArch
        server would suppress Wolf's much more useful "I am up, but you have
        not given me a credential, so I cannot tell you what I play".
        """
        for source in self.sources:
            said = _stream_reason(source, slug)
            if said:
                return said
        return ""
