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

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Platform:
    """A console RomM can hold and an emulator can run."""

    slug: str                      # RomM's fs_slug -- the library folder name
    name: str                      # human label
    extensions: tuple[str, ...]    # ROM extensions, most-preferred first
    aliases: tuple[str, ...] = field(default=())
    # The largest plausible download for ONE game on this system. See MB below.
    max_size: int = 32 * 1024 * 1024


MB = 1024 * 1024

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
# makes this safe to tighten -- the job is to exclude disc images and PC ports,
# not to second-guess how a dump was packaged.
#
#   system   biggest cartridge          ceiling here
#   NES      1MB   (mapper-heavy carts)  8MB
#   SNES     6MB   (Tales of Phantasia)  24MB
#   GBA      32MB  (full 256Mbit carts)  128MB
#   N64      64MB  (Resident Evil 2)     256MB
#   Genesis  8MB   (Pier Solar)          32MB
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
            if start >= 0:
                candidates.append((start, -len(label), platform))

    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


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
