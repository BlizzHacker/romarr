"""Proving an import is the right file.

**This is the thing no other *arr can do.** Radarr cannot tell you whether the
file it imported is correct, because there is no canonical hash for a movie --
its last mile is "something arrived, it is probably fine". Every game manager
that scores filenames is making the same guess with a better vocabulary.

ROMs are the exception. No-Intro and Redump publish the CRC32, MD5 and SHA1 of
every correct dump in existence, in the Logiqx DAT format, so "is this the
right file" has an actual answer. That answer is worth more than any amount of
title parsing: a release can be named perfectly and still be a bad dump, and a
release named badly can be byte-for-byte correct.

Three distinctions this module refuses to blur:

  * **Verified** -- the hash is in a DAT. The file is the published dump.
  * **Bad dump** -- a file the exact size of a known ROM whose hash does not
    match. Corrupt, patched, or tampered with. This is the case worth catching.
  * **Unknown** -- no hash matched anything. NOT a bad dump: homebrew,
    translations, new releases and an out-of-date DAT all land here, and
    treating absence as proof of badness would reject good files with total
    confidence.

The copier-header trap is the reason naive implementations of this match
nothing at all. See `header_bytes`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zlib
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

VERIFIED = "verified"
BAD_DUMP = "bad-dump"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Rom:
    """One file inside a DAT entry.

    A cartridge game has one. A Redump disc entry has one per track plus the
    cue sheet, which is why verifying a disc means verifying a set rather than
    a file -- and why `selection.pick_rom_set` exists upstream of this.
    """

    name: str
    size: int = 0
    crc: str = ""
    md5: str = ""
    sha1: str = ""


@dataclass(frozen=True)
class Game:
    name: str
    roms: tuple[Rom, ...] = ()
    #: No-Intro marks regional variants as clones of a parent entry. This is
    #: what makes 1G1R possible at all.
    cloneof: str = ""


@dataclass(frozen=True)
class Match:
    status: str
    game: str = ""
    rom: Rom | None = None
    dat: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == VERIFIED

    def __str__(self) -> str:
        if self.status == VERIFIED:
            return f"verified: {self.game} ({self.dat})"
        if self.status == BAD_DUMP:
            return f"bad dump: {self.detail}"
        return "not in any loaded DAT"


#: Fixed-size headers that some dumps carry and DATs never include.
#:
#: **The single reason a naive implementation matches nothing.** No-Intro
#: hashes the cartridge contents; a copier or emulator header is metadata
#: added afterwards. Hash the file as it sits on disk and you get a checksum
#: that appears in no DAT ever published -- so every NES and SNES import comes
#: back "unknown" and the feature looks useless rather than broken.
_FIXED_HEADERS = {
    ".nes": 16,     # iNES
    ".fds": 16,     # fwNES
    ".lnx": 64,     # Lynx
    ".a78": 128,    # Atari 7800
}

#: A Super Nintendo dump may or may not carry a 512-byte copier header, and
#: the extension does not say. The rule the preservation world uses is
#: arithmetic rather than magic bytes: cartridge data is a whole number of
#: 32KB banks, so a file 512 bytes over a multiple of 32768 is headered.
_SMC_SUFFIXES = (".smc", ".sfc", ".swc", ".fig")
_SMC_HEADER = 512
_SMC_BANK = 32768


def header_size(suffix: str, length: int) -> int:
    """How many leading bytes to skip, given only the file's length.

    Length rather than contents, because the SMC rule is arithmetic and the
    caller may be looking at a four-gigabyte disc image it must not read into
    memory to answer this.
    """
    suffix = str(suffix or "").lower()
    fixed = _FIXED_HEADERS.get(suffix)
    if fixed and length > fixed:
        return fixed
    if suffix in _SMC_SUFFIXES and length % _SMC_BANK == _SMC_HEADER:
        return _SMC_HEADER
    return 0


def header_bytes(suffix: str, data: bytes) -> int:
    """How many leading bytes to skip before hashing `data`."""
    return header_size(suffix, len(data))


def hash_bytes(data: bytes, *, suffix: str = "") -> dict:
    """Every hash a DAT might publish, computed over the ROM proper.

    Returns a dict shaped for `lookup(**hashes)`, so a caller never has to
    know which algorithms a given DAT happens to carry.
    """
    start = header_bytes(suffix, data)
    body = data[start:] if start else data
    return {
        "size": len(body),
        "crc": f"{zlib.crc32(body) & 0xFFFFFFFF:08x}",
        "md5": hashlib.md5(body, usedforsecurity=False).hexdigest(),
        "sha1": hashlib.sha1(body, usedforsecurity=False).hexdigest(),
    }


def hash_file(path, *, chunk: int = 1024 * 1024) -> dict:
    """`hash_bytes` for a file on disk, read in chunks.

    A PS2 image is several gigabytes and holding one in memory to checksum it
    would put an import at the mercy of however much RAM the container was
    given. Only the header -- at most 512 bytes -- is ever buffered.
    """
    from pathlib import Path

    path = Path(path)
    # From the length, never from the contents. Building a buffer the size of
    # the file to decide whether it has a 512-byte header would allocate four
    # gigabytes for a PS2 image -- exactly the thing chunked hashing exists to
    # avoid, reintroduced one line above it.
    start = header_size(path.suffix, path.stat().st_size)
    crc = 0
    md5 = hashlib.md5(usedforsecurity=False)
    sha1 = hashlib.sha1(usedforsecurity=False)
    size = 0
    with open(path, "rb") as handle:
        if start:
            handle.seek(start)
        while True:
            block = handle.read(chunk)
            if not block:
                break
            crc = zlib.crc32(block, crc)
            md5.update(block)
            sha1.update(block)
            size += len(block)
    return {"size": size, "crc": f"{crc & 0xFFFFFFFF:08x}",
            "md5": md5.hexdigest(), "sha1": sha1.hexdigest()}


@dataclass
class Dat:
    """One parsed DAT -- normally one system."""

    name: str = ""
    version: str = ""
    games: dict[str, Game] = field(default_factory=dict)
    _by_crc: dict[tuple[str, int], tuple[str, Rom]] = field(default_factory=dict)
    _by_sha1: dict[str, tuple[str, Rom]] = field(default_factory=dict)
    _by_md5: dict[str, tuple[str, Rom]] = field(default_factory=dict)
    _sizes: dict[int, str] = field(default_factory=dict)

    def lookup(self, *, crc: str = "", md5: str = "", sha1: str = "",
               size: int = 0) -> Match:
        """What this file is, by hash.

        Strongest hash first. CRC32 is 32 bits and collides -- with several
        million entries across a full DAT collection that stops being
        theoretical -- so where the DAT published a SHA1 and the caller
        computed one, that is the answer and CRC is not consulted.
        """
        for key, table in ((sha1, self._by_sha1), (md5, self._by_md5)):
            hit = table.get(str(key or "").lower())
            if hit:
                return Match(VERIFIED, hit[0], hit[1], self.name)

        crc = str(crc or "").lower()
        if crc:
            hit = self._by_crc.get((crc, int(size or 0)))
            if hit is None and not size:
                hit = next((v for (c, _), v in self._by_crc.items() if c == crc),
                           None)
            if hit:
                return Match(VERIFIED, hit[0], hit[1], self.name)

        # Nothing matched. A file the exact size of a known ROM is the case
        # worth separating out: that is a corrupt or altered copy of something
        # this DAT knows, not an unrecognised file.
        if size and int(size) in self._sizes:
            known = self._sizes[int(size)]
            return Match(BAD_DUMP, dat=self.name,
                         detail=f"{size} bytes matches {known!r} but the "
                                f"checksum does not -- corrupt or altered")
        return Match(UNKNOWN, dat=self.name)

    def parent_of(self, game: str) -> str:
        entry = self.games.get(game)
        if entry is None:
            return game
        return entry.cloneof or entry.name

    def one_game_one_rom(self, regions: list[str]) -> dict[str, Game]:
        """One entry per game, choosing by region preference.

        Igir's headline feature and the reason a 10,000-entry set collapses to
        a playable 3,000. The ladder is supplied by the caller so that a 1G1R
        set and a grab of the same title agree about which dump is wanted --
        `store.settings["preferred_regions"]` feeds both.

        A game whose only dumps fall outside the preference is kept anyway.
        Dropping it would silently lose titles from a "cleaned" set, which is
        the kind of loss nobody notices for months.
        """
        groups: dict[str, list[Game]] = {}
        for game in self.games.values():
            groups.setdefault(self.parent_of(game.name), []).append(game)

        order = [r.lower() for r in regions]

        def rank(game: Game) -> tuple[int, int, str]:
            found = _regions_in(game.name)
            best = min((order.index(r) for r in found if r in order),
                       default=len(order))
            # Prefer the parent on a tie: No-Intro makes the canonical dump
            # the parent, so it is the right answer when nothing else decides.
            return (best, 0 if not game.cloneof else 1, game.name)

        return {min(members, key=rank).name: min(members, key=rank)
                for members in groups.values()}


_REGION_IN_NAME = re.compile(r"\(([^)]*)\)")


def _regions_in(name: str) -> set[str]:
    """Region words from a No-Intro style name: `Game (USA, Europe)`."""
    out = set()
    for group in _REGION_IN_NAME.findall(name or ""):
        for part in group.split(","):
            out.add(part.strip().lower())
    return out


def parse_dat(body: str) -> Dat:
    """A Logiqx DAT, as both No-Intro and Redump publish it."""
    import xml.etree.ElementTree as ET

    dat = Dat()
    try:
        root = ET.fromstring(body or "")
    except ET.ParseError:
        log.warning("DAT is not valid XML; ignoring it")
        return dat

    header = root.find("header")
    if header is not None:
        dat.name = (header.findtext("name") or "").strip()
        dat.version = (header.findtext("version") or "").strip()

    for node in root.iter("game"):
        name = (node.get("name") or "").strip()
        if not name:
            continue
        roms = []
        for rom_node in node.findall("rom"):
            rom = Rom(
                name=(rom_node.get("name") or "").strip(),
                size=int(rom_node.get("size") or 0),
                crc=(rom_node.get("crc") or "").strip().lower(),
                md5=(rom_node.get("md5") or "").strip().lower(),
                sha1=(rom_node.get("sha1") or "").strip().lower(),
            )
            roms.append(rom)
            if rom.crc:
                dat._by_crc.setdefault((rom.crc, rom.size), (name, rom))
            if rom.sha1:
                dat._by_sha1.setdefault(rom.sha1, (name, rom))
            if rom.md5:
                dat._by_md5.setdefault(rom.md5, (name, rom))
            if rom.size:
                dat._sizes.setdefault(rom.size, name)
        dat.games[name] = Game(name=name, roms=tuple(roms),
                               cloneof=(node.get("cloneof") or "").strip())
    return dat


class DatIndex:
    """Every loaded DAT, queried as one.

    An operator has one DAT per system and a file arriving from a download does
    not announce which system it belongs to -- so verification asks all of
    them. An index with nothing in it answers `unknown`, because day one is the
    common case and it must not turn every import into an error.
    """

    def __init__(self):
        self.dats: list[Dat] = []

    def add(self, dat: Dat) -> None:
        if dat.games:
            self.dats.append(dat)

    def lookup(self, **hashes) -> Match:
        best = Match(UNKNOWN)
        for dat in self.dats:
            got = dat.lookup(**hashes)
            if got.status == VERIFIED:
                return got
            # A bad-dump verdict from one DAT is only interesting if no other
            # DAT verifies the file -- size collisions across systems are
            # ordinary, so this is held back rather than returned early.
            if got.status == BAD_DUMP and best.status == UNKNOWN:
                best = got
        return best
