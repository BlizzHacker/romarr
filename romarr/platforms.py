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


# Cartridge-era systems only. Disc-based platforms are deliberately excluded:
# their images are gigabytes, frequently multi-file, and cannot be streamed to
# a browser emulator, which is the point of this pipeline.
PLATFORMS: tuple[Platform, ...] = (
    Platform("nes", "Nintendo Entertainment System", (".nes", ".fds", ".unf"),
             ("nintendo", "famicom", "nintendo entertainment system")),
    Platform("snes", "Super Nintendo", (".smc", ".sfc", ".swc", ".fig"),
             ("super nintendo", "super famicom", "sfc", "super nes")),
    Platform("gb", "Game Boy", (".gb",), ("gameboy", "game boy")),
    Platform("gbc", "Game Boy Color", (".gbc",), ("gameboy color", "game boy color")),
    Platform("gba", "Game Boy Advance", (".gba",), ("gameboy advance", "game boy advance")),
    Platform("n64", "Nintendo 64", (".z64", ".n64", ".v64"),
             ("nintendo 64", "n 64")),
    Platform("genesis-slash-megadrive", "Sega Genesis / Mega Drive",
             (".md", ".gen", ".smd", ".bin"),
             ("genesis", "mega drive", "megadrive", "sega genesis")),
    Platform("sms", "Sega Master System", (".sms",), ("master system",)),
    Platform("gamegear", "Game Gear", (".gg",), ("game gear",)),
    Platform("atari2600", "Atari 2600", (".a26", ".bin"), ("2600", "vcs")),
    Platform("atari7800", "Atari 7800", (".a78",), ("7800",)),
    Platform("lynx", "Atari Lynx", (".lnx",), ()),
    Platform("turbografx16--1", "TurboGrafx-16", (".pce",), ("pc engine", "turbografx")),
    Platform("wonderswan", "WonderSwan", (".ws", ".wsc"), ()),
    Platform("neo-geo-pocket", "Neo Geo Pocket", (".ngp", ".ngc"), ()),
    Platform("virtualboy", "Virtual Boy", (".vb",), ("virtual boy",)),
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
