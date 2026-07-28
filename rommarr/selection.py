"""Choosing which release to grab, and which file inside it is the ROM.

Both decisions are pure functions over metadata, so they are unit-testable
without a network, a download client, or a filesystem. That matters because
they are where this pipeline goes wrong in practice: grab the wrong release and
you download 40GB of a PC port instead of a 512KB cartridge; pick the wrong file
and RomM imports a readme.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .platforms import Platform

# Newznab/Torznab categories. 1000-1999 is Console, 4000-4999 is PC; only the
# console range and the PC-games sub-range are of interest here.
CONSOLE_CATEGORIES = range(1000, 2000)
PC_GAME_CATEGORIES = range(4050, 4070)

# Platform names that, when they appear in a title, mean the release is for a
# DIFFERENT system than the one asked for. A search for a SNES game happily
# returns "[Nintendo Switch] Super Mario World (NSP)" -- the title matches and
# the size is plausible, so nothing else in the scorer rejects it, and you end
# up importing a Switch package into your SNES folder.
FOREIGN_PLATFORM_MARKERS = {
    "nintendo switch", "switch", "nsp", "xci",
    "playstation", "ps1", "ps2", "ps3", "ps4", "ps5", "psp", "psx", "vita",
    "xbox", "x360", "xbla",
    "gamecube", "wii", "wiiu", "wii u", "3ds", "nds", "ds",
    "android", "apk", "ios",
    "pc", "windows", "steam", "gog", "repack",
    "dreamcast", "saturn", "psvr",
}

# Words that mean "this is not a plain cartridge dump".
_JUNK_MARKERS = (
    "beta", "proto", "prototype", "demo", "sample", "hack", "patched",
    "translation", "trainer", "repack", "update only", "dlc",
)

# Region preference: most emulator users want USA, then World, then Europe.
_REGION_SCORE = (
    (("usa", "(u)", "[u]", "ntsc-u"), 40),
    (("world", "(w)", "global"), 30),
    (("europe", "(e)", "pal"), 20),
    (("japan", "(j)", "ntsc-j"), 10),
)


@dataclass(frozen=True)
class Release:
    """One search result from an indexer."""

    title: str
    size: int                 # bytes
    seeders: int              # 0 for usenet
    categories: tuple[int, ...]
    download_url: str
    protocol: str             # "torrent" | "usenet"
    indexer: str = ""


def is_game_release(release: Release) -> bool:
    """Whether an indexer result is a game at all, by category."""
    return any(
        c in CONSOLE_CATEGORIES or c in PC_GAME_CATEGORIES
        for c in release.categories
    )


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_matches(release_title: str, wanted: str) -> bool:
    """Whether a release plausibly IS the requested game.

    Every significant word of the request must appear. Short words are ignored
    because "of", "the" and roman numerals produce false negatives more often
    than they prevent false positives.
    """
    haystack = _normalise(release_title)
    words = [w for w in _normalise(wanted).split() if len(w) > 2]
    if not words:
        return False
    return all(w in haystack for w in words)


def score(release: Release, wanted: str, platform: Platform | None = None) -> int:
    """Rank a release. Higher is better; negative means "do not take this"."""
    if not is_game_release(release):
        return -1000
    if not title_matches(release.title, wanted):
        return -500

    points = 0
    lowered = release.title.lower()

    # Availability. Usenet has no seeders, so it is not penalised for having none.
    if release.protocol == "torrent":
        if release.seeders <= 0:
            return -400
        points += min(release.seeders, 50) * 4

    for markers, value in _REGION_SCORE:
        if any(m in lowered for m in markers):
            points += value
            break

    if any(marker in lowered for marker in _JUNK_MARKERS):
        points -= 120

    # Reject a release that names a system other than the one requested. Its own
    # platform's aliases are removed from the check first, so asking for a Wii
    # game does not disqualify a title that says "Wii".
    if platform is not None:
        own = {platform.slug.lower(), platform.name.lower(), *platform.aliases}
        own_words = {w for label in own for w in label.split()}
        for marker in FOREIGN_PLATFORM_MARKERS:
            if marker in own or marker in own_words:
                continue
            if _mentions(lowered, marker):
                return -300

    # A cartridge ROM is small. A multi-gigabyte "release" for a cartridge
    # platform is a romset, a PC port, or a disc image -- none of which this
    # pipeline can hand to a browser emulator.
    if platform is not None:
        if release.size > 512 * 1024 * 1024:
            points -= 200
        elif release.size < 4 * 1024:
            points -= 200

    return points


def best_release(releases: list[Release], wanted: str,
                 platform: Platform | None = None) -> Release | None:
    """The highest-scoring usable release, or None if nothing qualifies."""
    ranked = [(score(r, wanted, platform), r) for r in releases]
    ranked = [(s, r) for s, r in ranked if s > 0]
    if not ranked:
        return None
    # Sort by score, then prefer the smaller file: for cartridge ROMs a bigger
    # file is almost always a romset or a bad dump, not a better copy.
    ranked.sort(key=lambda pair: (-pair[0], pair[1].size))
    return ranked[0][1]


def pick_rom_file(filenames: list[str], platform: Platform) -> str | None:
    """Which file in a finished download is the ROM to import.

    Prefers the platform's own extensions in declared order, then falls back to
    any file that is not obviously an extra. Returns None when the download
    holds nothing playable, which is a real outcome worth reporting rather than
    guessing at.
    """
    if not filenames:
        return None

    def is_extra(name: str) -> bool:
        lowered = name.lower()
        return lowered.endswith(
            (".nfo", ".txt", ".diz", ".sfv", ".jpg", ".jpeg", ".png",
             ".url", ".md5", ".sha1", ".par2", ".exe", ".bat")
        )

    for ext in platform.extensions:
        matches = [f for f in filenames if f.lower().endswith(ext) and not is_extra(f)]
        if matches:
            # A multi-dump archive: prefer the region the scorer prefers too.
            matches.sort(key=lambda f: (-_region_rank(f), len(f)))
            return matches[0]
    return None


def _region_rank(name: str) -> int:
    lowered = name.lower()
    for markers, value in _REGION_SCORE:
        if any(m in lowered for m in markers):
            return value
    return 0


def _mentions(haystack: str, marker: str) -> bool:
    """Whether `marker` appears in `haystack` as a whole word or phrase.

    Substring matching is wrong here: "ds" is inside "worlds", and "pc" is
    inside "pcb", so a naive check disqualifies half of every result set.
    """
    padded = f" {re.sub(r'[^a-z0-9 ]+', ' ', haystack)} "
    return f" {marker} " in re.sub(r"\s+", " ", padded)
