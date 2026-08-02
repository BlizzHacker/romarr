"""Platform knowledge: names, RomM folder slugs, and ROM file extensions.

Three separate things have to agree before a downloaded file can become a
playable entry in RomM:

  1. what a user (or GG Requestz) calls the platform      -- "SNES", "Super Nintendo"
  2. what RomM calls the library folder                    -- `snes`
  3. which file inside the download is actually the ROM    -- `.smc`, not `.nfo`

Getting (3) wrong is the most common failure: game torrents routinely ship
readmes, box art, and sometimes several regional dumps in one archive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


#: What kind of medium a game shipped on. This is not decoration: it decides
#: the magnitude of the size ceiling, the wording when a release is rejected
#: for size, and -- the part that actually mattered -- whether one file is a
#: complete game or the first of several.
#:
#: A cartridge dump is one file. A disc rip is a `.cue` naming `.bin` tracks,
#: or a `.gdi` naming five of them, and importing the sheet alone produces a
#: library entry that looks correct and boots nothing. `selection.pick_rom_set`
#: exists because of this field.
CARTRIDGE = "cartridge"
DISC = "disc"
COMPUTER = "computer"


@dataclass(frozen=True)
class Platform:
    """A console RomM can hold and an emulator can run."""

    slug: str                      # RomM's fs_slug -- the library folder name
    name: str                      # human label
    extensions: tuple[str, ...]    # ROM extensions, most-preferred first
    aliases: tuple[str, ...] = field(default=())
    # The largest plausible download for ONE game on this system. See MB below.
    max_size: int = 32 * 1024 * 1024
    media: str = CARTRIDGE
    # Words in FOREIGN_PLATFORM_MARKERS that are native here, and so must not
    # disqualify a release.
    #
    # The marker list is shared, and a marker that names another system for
    # fifteen platforms can name *this* one for the sixteenth. "wad" is the
    # case that forced this: it is the marker that catches a Wii Virtual
    # Console repackage of a Genesis game, and it is also the extension every
    # legitimate WiiWare title ships as. Without an exemption, adding Wii as a
    # platform would have made most of its catalogue unrequestable.
    native_markers: tuple[str, ...] = field(default=())

    @property
    def is_disc(self) -> bool:
        return self.media == DISC


MB = 1024 * 1024
GB = 1024 * MB

# How big a release for each system can plausibly be.
#
# Scoring used a single 512MB ceiling for every platform, which is roughly
# eighty times the largest SNES cartridge ever made. A 452MB PC build of Final
# Fantasy III sailed under it, ranked top on seeders and was picked for a SNES
# request -- the same failure as a translation hack, arrived at through size.
#
# Each number is the biggest cartridge the system shipped, rounded up hard for
# headroom, because a release is not always a bare ROM: it may be zipped, carry
# box art and a readme, or hold several regional dumps. The headroom is what
# makes this safe to tighten -- the job is to exclude PC ports and romsets, not
# to second-guess how a dump was packaged.
#
#   system   biggest cartridge          ceiling here
#   NES      1MB   (mapper-heavy carts)  8MB
#   SNES     6MB   (Tales of Phantasia)  24MB
#   GBA      32MB  (full 256Mbit carts)  128MB
#   N64      64MB  (Resident Evil 2)     256MB
#   Genesis  8MB   (Pier Solar)          32MB
#
# Disc ceilings follow the same rule one medium up: the capacity of the disc,
# rounded for a rip that may carry uncompressed audio tracks. They still do
# real work -- a 12GB PS2 ceiling is what keeps a 60GB PC repack out of a PS2
# request, which is the same job the 24MB SNES ceiling does against a 452MB PC
# build.
#
#   system   disc capacity              ceiling here
#   PSX      700MB (CD)                  2GB
#   Saturn   700MB (CD)                  2GB
#   Dreamcast 1.2GB (GD-ROM)             3GB
#   GameCube 1.5GB (miniDVD)             4GB
#   PSP      1.8GB (UMD)                 4GB
#   PS2      8.5GB (dual-layer DVD)      12GB
#   Wii      8.5GB (dual-layer DVD)      12GB
PLATFORMS: tuple[Platform, ...] = (
    Platform("nes", "Nintendo Entertainment System", (".nes", ".fds", ".unf"),
             ("nintendo", "famicom", "nintendo entertainment system"),
             max_size=8 * MB),
    Platform("snes", "Super Nintendo", (".smc", ".sfc", ".swc", ".fig"),
             ("super nintendo", "super famicom", "sfc", "super nes"),
             max_size=24 * MB),
    Platform("gb", "Game Boy", (".gb",), ("gameboy", "game boy"),
             max_size=8 * MB),
    Platform("gbc", "Game Boy Color", (".gbc",), ("gameboy color", "game boy color"),
             max_size=16 * MB),
    Platform("gba", "Game Boy Advance", (".gba",), ("gameboy advance", "game boy advance"),
             max_size=128 * MB),
    Platform("n64", "Nintendo 64", (".z64", ".n64", ".v64"),
             ("nintendo 64", "n 64"), max_size=256 * MB),
    Platform("genesis-slash-megadrive", "Sega Genesis / Mega Drive",
             (".md", ".gen", ".smd", ".bin"),
             ("genesis", "mega drive", "megadrive", "sega genesis"),
             max_size=32 * MB),
    Platform("sms", "Sega Master System", (".sms",), ("master system",),
             max_size=8 * MB),
    Platform("gamegear", "Game Gear", (".gg",), ("game gear",), max_size=8 * MB),
    Platform("atari2600", "Atari 2600", (".a26", ".bin"), ("2600", "vcs"),
             max_size=8 * MB),
    Platform("atari7800", "Atari 7800", (".a78",), ("7800",), max_size=8 * MB),
    Platform("lynx", "Atari Lynx", (".lnx",), (), max_size=8 * MB),
    Platform("turbografx16--1", "TurboGrafx-16", (".pce",), ("pc engine", "turbografx"),
             max_size=16 * MB),
    Platform("wonderswan", "WonderSwan", (".ws", ".wsc"), (), max_size=16 * MB),
    Platform("neo-geo-pocket", "Neo Geo Pocket", (".ngp", ".ngc"), (), max_size=8 * MB),
    Platform("virtualboy", "Virtual Boy", (".vb",), ("virtual boy",), max_size=8 * MB),

    # -- large cartridges ---------------------------------------------------
    #
    # Excluded before for the same reason discs were, and just as wrongly: a
    # DS cartridge is 512MB at the very most and EmulatorJS runs `melonds` for
    # it out of the box.
    Platform("nds", "Nintendo DS", (".nds", ".dsi", ".ids"),
             ("nintendo ds", "ds"), max_size=512 * MB),
    Platform("3ds", "Nintendo 3DS", (".3ds", ".cci", ".cxi", ".cia"),
             ("nintendo 3ds", "3ds"), max_size=8 * GB),

    # -- optical media ------------------------------------------------------
    #
    # Extension order is preference order, and for a disc that ordering is a
    # correctness rule rather than a taste: a `.chd` or `.rvz` is ONE file that
    # cannot be separated from its tracks, while a `.cue` is a text file that
    # is worthless without the `.bin` beside it. Preferring the whole-disc
    # image makes the fragile case the fallback rather than the default.
    #
    # `.cue` and `.bin` are always declared together. A platform that named
    # the sheet without the tracks would import a pointer to nothing --
    # `test_a_cue_never_appears_without_its_bin` fails if that is ever
    # narrowed.
    Platform("psx", "Sony PlayStation",
             (".chd", ".pbp", ".cue", ".bin", ".img", ".ccd", ".iso", ".m3u"),
             ("playstation", "sony playstation", "ps1", "psone", "psx"),
             max_size=2 * GB, media=DISC),
    Platform("ps2", "Sony PlayStation 2",
             (".chd", ".iso", ".cso", ".bin", ".gz"),
             ("playstation 2", "sony playstation 2", "ps2"),
             max_size=12 * GB, media=DISC),
    Platform("psp", "Sony PlayStation Portable",
             (".cso", ".iso", ".chd", ".pbp"),
             ("playstation portable", "psp"),
             max_size=4 * GB, media=DISC),
    Platform("saturn", "Sega Saturn",
             (".chd", ".cue", ".bin", ".iso", ".ccd", ".mds"),
             ("sega saturn",), max_size=2 * GB, media=DISC),
    Platform("segacd", "Sega CD / Mega-CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("sega cd", "mega cd", "megacd", "mega-cd", "sega mega-cd"),
             max_size=2 * GB, media=DISC),
    Platform("dc", "Sega Dreamcast",
             (".chd", ".gdi", ".cdi", ".cue", ".bin"),
             ("dreamcast", "sega dreamcast"), max_size=3 * GB, media=DISC),
    Platform("ngc", "Nintendo GameCube",
             (".rvz", ".iso", ".gcm", ".ciso", ".gcz"),
             ("gamecube", "nintendo gamecube", "game cube", "gcn"),
             max_size=4 * GB, media=DISC),
    Platform("wii", "Nintendo Wii",
             (".rvz", ".wbfs", ".iso", ".wad", ".ciso", ".gcz"),
             ("nintendo wii",), max_size=12 * GB, media=DISC,
             # See Platform.native_markers. Every Wii release that is a
             # WiiWare or Virtual Console title says so, and those are the
             # markers that exist to catch such a title being offered for a
             # *cartridge* platform. Here they are the catalogue.
             native_markers=("wad", "wiiware", "virtual console", "eshop")),
    Platform("3do", "3DO Interactive Multiplayer",
             (".chd", ".cue", ".bin", ".iso"),
             ("3do interactive", "panasonic 3do"), max_size=2 * GB, media=DISC),
    Platform("philips-cd-i", "Philips CD-i",
             (".chd", ".cue", ".bin", ".iso"),
             ("cd-i", "cdi", "philips cdi"), max_size=2 * GB, media=DISC),
    Platform("pc-fx", "NEC PC-FX",
             (".chd", ".cue", ".bin", ".iso"),
             ("pcfx", "nec pc-fx"), max_size=2 * GB, media=DISC),
    # RomM has two slugs for this machine. This is the one the live library
    # materialises and the one `RommStreamServer/tiers.py` routes, so it is the
    # one that can actually be filed into and played.
    Platform("turbografx-16-slash-pc-engine-cd", "TurboGrafx-CD / PC Engine CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("turbografx cd", "turbografx-cd", "pc engine cd", "pcecd",
              "super cd-rom"),
             max_size=2 * GB, media=DISC),
    Platform("amiga-cd32", "Amiga CD32",
             (".chd", ".cue", ".bin", ".iso"),
             ("cd32", "amiga cd32"), max_size=2 * GB, media=DISC),
    Platform("neo-geo-cd", "Neo Geo CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("neogeo cd", "neo geo cd"), max_size=2 * GB, media=DISC),
    Platform("atari-jaguar-cd", "Atari Jaguar CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("jaguar cd",), max_size=2 * GB, media=DISC),
)

_BY_SLUG = {p.slug: p for p in PLATFORMS}


def all_extensions() -> set[str]:
    """Every extension any supported platform recognises."""
    return {ext for p in PLATFORMS for ext in p.extensions}


def by_slug(slug: str) -> Platform | None:
    return _BY_SLUG.get(slug.strip().lower())


def resolve(text: str) -> Platform | None:
    """Find the platform a free-text name refers to.

    Matches the slug, the display name, or any alias. See the ranking note
    below for why position beats length.
    """
    if not text:
        return None
    needle = text.strip().lower()

    if needle in _BY_SLUG:
        return _BY_SLUG[needle]

    # Ranked by where the alias starts, then by how much of the name it covers.
    #
    # Position matters more than length, and this is not a nicety: "super
    # nintendo entertainment system" contains BOTH "super nintendo" (SNES) and
    # "nintendo entertainment system" (NES). Longest-match alone picks NES and
    # every SNES request silently becomes an NES request. The qualifier comes
    # first in English, so the earliest match is the specific one.
    candidates: list[tuple[int, int, Platform]] = []
    for platform in PLATFORMS:
        for label in (platform.name.lower(), *platform.aliases):
            if label == needle:
                return platform
            start = needle.find(label)
            if start >= 0 and not _leaves_a_model_number(needle, start + len(label)):
                candidates.append((start, -len(label), platform))

    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


# A model number left over after a partial alias match, e.g. the "5" in
# "playstation 5" once "playstation" has matched.
_MODEL_NUMBER = re.compile(r"\s*\d")


def _leaves_a_model_number(needle: str, end: int) -> bool:
    """Whether a partial match leaves a digit that names a different machine.

    Console families number their successors, so a partial match on the family
    name is the *most* dangerous kind: "playstation" is inside "playstation 2",
    "playstation 3", "playstation 4" and "playstation 5" alike, and the
    position-ranked match that correctly picks SNES out of "super nintendo
    entertainment system" happily answers a PlayStation 5 request with a
    PlayStation 1 folder.

    An exact alias is unaffected -- `resolve` returns those before it gets
    here, which is how "playstation 2" still reaches the PS2 row. What this
    rejects is only the case where an alias matched a prefix and the part it
    did not match begins with a number. That is never a decoration; it is the
    generation.

    Trailing words are deliberately still allowed. "super nintendo
    entertainment system" has to keep working, and no console family
    distinguishes its generations by a trailing word alone.
    """
    return _MODEL_NUMBER.match(needle, end) is not None


def platform_for_file(filename: str) -> Platform | None:
    """Which platform a ROM file belongs to, judged by its extension.

    Ambiguous extensions (`.bin` is both Atari 2600 and Mega Drive) resolve to
    the first platform that claims them, so callers that already know the
    platform should not rely on this.
    """
    lowered = filename.lower()
    for platform in PLATFORMS:
        for ext in platform.extensions:
            if lowered.endswith(ext):
                return platform
    return None
