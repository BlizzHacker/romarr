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
from collections.abc import Mapping
from dataclasses import dataclass

from .dat_table import Game, GameTable, Rom

log = logging.getLogger(__name__)

VERIFIED = "verified"
BAD_DUMP = "bad-dump"
UNKNOWN = "unknown"


#: Language codes and names normalised to two-letter ISO 639-1 codes so
#: `language "English / French"` in a DAT, `[T-Fr]` in a filename, and
#: `"French, English"` in settings all speak the same vocabulary.
_LANG_ALIASES = {
    "en": "en", "eng": "en", "english": "en",
    "de": "de", "deu": "de", "ger": "de", "german": "de",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr",
    "es": "es", "spa": "es", "esp": "es", "spanish": "es",
    "it": "it", "ita": "it", "italian": "it",
    "pt": "pt", "por": "pt", "ptb": "pt", "portuguese": "pt",
    "nl": "nl", "dut": "nl", "nla": "nl", "dutch": "nl",
    "sv": "sv", "swe": "sv", "swedish": "sv",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "ja": "ja", "jpn": "ja", "jp": "ja", "japanese": "ja",
    "ko": "ko", "kor": "ko", "korean": "ko",
    "zh": "zh", "chi": "zh", "chn": "zh", "chinese": "zh",
    "pl": "pl", "pol": "pl", "polish": "pl",
    "da": "da", "dan": "da", "danish": "da",
    "fi": "fi", "fin": "fi", "finnish": "fi",
    "no": "no", "nor": "no", "norwegian": "no", "nb": "nb", "nn": "nn",
    "cs": "cs", "cze": "cs", "ces": "cs", "czech": "cs",
    "hu": "hu", "hun": "hu", "hungarian": "hu",
    "el": "el", "gre": "el", "ell": "el", "greek": "el",
    "tr": "tr", "tur": "tr", "turkish": "tr",
    "ar": "ar", "ara": "ar", "arabic": "ar",
    "he": "he", "heb": "he", "hebrew": "he",
    "ca": "ca", "cat": "ca", "catalan": "ca",
    "ro": "ro", "rum": "ro", "ron": "ro", "romanian": "ro",
    "bg": "bg", "bul": "bg", "bulgarian": "bg",
    "hr": "hr", "hrv": "hr", "croatian": "hr",
    "sk": "sk", "slo": "sk", "slk": "sk", "slovak": "sk",
    "sl": "sl", "slv": "sl", "slovenian": "sl",
    "uk": "uk", "ukr": "uk", "ukrainian": "uk",
    "vi": "vi", "vie": "vi", "vietnamese": "vi",
    "th": "th", "tha": "th", "thai": "th",
    "id": "id", "ind": "id", "indonesian": "id",
    "ms": "ms", "may": "ms", "msa": "ms", "malay": "ms",
}

_TWO_LETTER_LANGS = frozenset(_LANG_ALIASES.values())
_PAREN_GROUP = re.compile(r"\(([^)]+)\)")


def normalise_lang(code: str) -> str:
    """Normalise a language code or name to its two-letter form."""
    c = (code or "").strip().lower()
    return _LANG_ALIASES.get(c, c)


def parse_dat_languages(raw: str) -> tuple[str, ...]:
    """Parse a DAT `language` string (e.g. `"English / Japanese"`) into
    deduplicated, normalised language codes, preserving declaration order."""
    text = (raw or "").strip()
    if not text:
        return ()
    tokens = re.split(r"[/,+&;|]+", text)
    seen: list[str] = []
    for tok in tokens:
        code = normalise_lang(tok)
        if code and code not in seen:
            seen.append(code)
    return tuple(seen)


def game_languages(game: Game) -> tuple[str, ...]:
    """Return known language codes for `game`, or `()` when unknown.

    Authoritative DAT `language` metadata wins; otherwise, explicit No-Intro
    language tags like `(En,Fr,De)` in the title are accepted without guessing
    language from region names like `(USA)` or `(Japan)`.
    """
    if game.languages:
        return game.languages
    for group in _PAREN_GROUP.findall(game.name or ""):
        parts = [p.strip().lower() for p in re.split(r"[,/]+", group) if p.strip()]
        if (
            parts
            and all(len(p) == 2 and p.isascii() and p.isalpha() for p in parts)
            and any(p in _TWO_LETTER_LANGS for p in parts)
        ):
            return tuple(dict.fromkeys(parts))
    return ()


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


class _Digest:
    """crc32 + md5 + sha1 + size, fed a block at a time."""

    def __init__(self) -> None:
        self.crc = 0
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.sha1 = hashlib.sha1(usedforsecurity=False)
        self.size = 0

    def update(self, block: bytes) -> None:
        """Fold one block into every digest."""
        self.crc = zlib.crc32(block, self.crc)
        self.md5.update(block)
        self.sha1.update(block)
        self.size += len(block)

    def result(self) -> dict:
        """The hashes in the shape `lookup(**hashes)` takes."""
        return {"size": self.size, "crc": f"{self.crc & 0xFFFFFFFF:08x}",
                "md5": self.md5.hexdigest(), "sha1": self.sha1.hexdigest()}


def hash_stream(stream, *, suffix: str = "", chunk: int = 1024 * 1024) -> dict:
    """`hash_bytes` for a readable stream, without holding it in memory.

    For an archive member piped out of `bsdtar` the length is not known until
    the stream ends, and `header_size` needs the length (the SMC rule is
    arithmetic). So the body is hashed twice in one pass: once from byte 0 and
    once past the only header this suffix could have. The length then picks
    which of the two is the answer. Memory stays at one chunk either way.
    """
    suffix = str(suffix or "").lower()
    candidate = _FIXED_HEADERS.get(suffix) or (
        _SMC_HEADER if suffix in _SMC_SUFFIXES else 0)
    whole = _Digest()
    past_header = _Digest() if candidate else None
    seen = 0
    while True:
        block = stream.read(chunk)
        if not block:
            break
        whole.update(block)
        if past_header is not None and seen + len(block) > candidate:
            past_header.update(block[max(0, candidate - seen):])
        seen += len(block)
    start = header_size(suffix, seen)
    if start and past_header is not None:
        return past_header.result()
    return whole.result()


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




#: Bracketed groups that identify a *different item* rather than a different
#: version of the same one. A two-disc game needs both discs, so they must not
#: collapse into one title the way (USA) and (Europe) should.
_SEPARATE_ITEM = re.compile(
    r"^(disc|disk|side|tape|cassette|cart|volume|vol)\s*[.\-]?\s*[0-9a-z]+$",
    re.I)

_BRACKETED = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")


def base_title(name: str) -> str:
    """A DAT name reduced to the game it is a dump of.

    "Contra (USA) (Rev 1)" and "Contra (Japan)" both reduce to "Contra", so a
    DAT with no clone data can still be collapsed. Disc and side markers are
    kept, because "Resident Evil 4 (USA) (Disc 1)" and "(Disc 2)" are two
    things you need rather than two versions of one thing -- collapsing them
    would report a complete set as missing half of it.
    """
    kept = []
    for group in _BRACKETED.findall(name or ""):
        for part in group.split(","):
            part = part.strip()
            if part and _SEPARATE_ITEM.match(part):
                kept.append(part)
    stripped = _BRACKETED.sub("", name or "").strip(" -")
    return " ".join([stripped] + [f"({k})" for k in kept]).strip()


class Dat:
    """One parsed DAT -- normally one system.

    `games` is a read-only mapping of game name to `Game`, packed (see
    `GameTable`). Build one from a parsed record with `dat_from_record`, or
    pass `games` as a dict of `Game`s; either way the hash indexes are built
    from the same rows, in the same order.
    """

    def __init__(self, name: str = "", version: str = "",
                 games: Mapping[str, Game] | None = None) -> None:
        self.name = name
        self.version = version
        self.games = GameTable(
            (key, game.cloneof, game.languages,
             [(rom.name, rom.size, rom.crc, rom.md5, rom.sha1) for rom in game.roms])
            for key, game in (games or {}).items())

    def __repr__(self) -> str:
        return f"Dat(name={self.name!r}, version={self.version!r}, games={self.games!r})"

    def _verified(self, row: int, sha1: str) -> Match | None:
        """A verified match for `row`, unless its declared SHA1 contradicts `sha1`."""
        game, rom = self.games.hit(row)
        if sha1 and rom.sha1 and sha1 != rom.sha1:
            return None
        return Match(VERIFIED, game, rom, self.name)

    def lookup(self, *, crc: str = "", md5: str = "", sha1: str = "",
               size: int = 0) -> Match:
        """What this file is, by hash.

        Strongest hash first. CRC32 is 32 bits and collides -- with several
        million entries across a full DAT collection that stops being
        theoretical -- so where the DAT published a SHA1 and the caller
        computed one, that is the answer and CRC is not consulted.
        """
        table = self.games
        sha1 = str(sha1 or "").lower()
        row = table.sha1_row(sha1)
        if row is not None:
            return self._verified(row, "")

        row = table.md5_row(str(md5 or "").lower())
        if row is not None and (match := self._verified(row, sha1)):
            return match

        crc = str(crc or "").lower()
        if crc:
            row = table.crc_row(crc, int(size or 0))
            if row is None and not size:
                row = table.crc_any_size(crc)
            if row is not None and (match := self._verified(row, sha1)):
                return match

        # Nothing matched. A file the exact size of a known ROM is the case
        # worth separating out: that is a corrupt or altered copy of something
        # this DAT knows, not an unrecognised file.
        if size and (row := table.size_row(int(size))) is not None:
            known = table.name_of(table.entry_of(row))
            return Match(BAD_DUMP, dat=self.name,
                         detail=f"{size} bytes matches {known!r} but the "
                                f"checksum does not -- corrupt or altered")
        return Match(UNKNOWN, dat=self.name)

    def parent_of(self, game: str) -> str:
        """The entry this one is a clone of, or itself.

        Answers a question about the DAT's own declarations. `group_key` is
        what decides which dumps are versions of one game.
        """
        entry = self.games.entry(game)
        if entry is None:
            return game
        return self.games.clone_of(entry) or self.games.name_of(entry)

    @property
    def declares_clones(self) -> bool:
        """Whether this DAT carries any parent/clone relationship at all."""
        return self.games.has_clones

    def group_key(self, game: str) -> str:
        """Which game a dump belongs to, for collapsing versions.

        A declared `cloneof` is authoritative and used. Many real DATs do not
        declare them: the No-Intro GameCube datfile on the install this was
        found against carries exactly one across 1,907 entries, and Redump-style
        DATs generally carry none. Without a fallback every dump is its own
        group and `one_game_one_rom` quietly does nothing -- it returned 1,906
        "titles" for that GameCube set, listing "007 - Agent Under Fire (USA)",
        "(USA) (Rev 1)" and "(Europe)" separately. It did not fail; it just was
        not 1G1R.

        So anything without a declared parent is grouped by its normalised
        title, which is what Igir and RomVault do. Gating that on "the DAT
        declares nothing at all" was the obvious conservative rule and the real
        data disproved it: one stray cloneof was enough to switch inference off
        for the whole file.

        Both sides are normalised, or the key a clone produces -- its parent's
        full name -- would never equal the key its parent produces, and the two
        would land in different groups.
        """
        return base_title(self.parent_of(game))

    def one_game_one_rom(
        self,
        regions: list[str],
        languages: list[str] | None = None,
    ) -> dict[str, Game]:
        """One entry per game, choosing by optional language then region preference.

        Igir's headline feature and the reason a 10,000-entry set collapses to
        a playable 3,000. The ladder is supplied by the caller so that a 1G1R
        set and a grab of the same title agree about which dump is wanted --
        `store.settings["preferred_regions"]` feeds both.

        A game whose only dumps fall outside the preference is kept anyway.
        Dropping it would silently lose titles from a "cleaned" set, which is
        the kind of loss nobody notices for months.
        """
        # Rank ROM-less summaries and build full Games only for the winners:
        # the table makes Game and Rom objects on demand.
        table = self.games
        groups: dict[str, list[tuple[Game, int]]] = {}
        for entry in table.entries():
            game = table.summary(entry)
            groups.setdefault(base_title(game.cloneof or game.name), []).append((game, entry))

        order = [r.lower() for r in regions]
        lang_order = [normalise_lang(l) for l in (languages or ()) if str(l).strip()]

        def rank(game: Game) -> tuple[int, int, int, int, str]:
            if lang_order:
                g_langs = game_languages(game)
                lang_best = min(
                    (lang_order.index(l) for l in g_langs if l in lang_order),
                    default=len(lang_order),
                )
            else:
                lang_best = 0
            found = _regions_in(game.name)
            best = min((order.index(r) for r in found if r in order),
                       default=len(order))
            # Prefer the parent on a tie: No-Intro makes the canonical dump
            # the parent, so it is the right answer when nothing else decides.
            # Non-retail variants must not win an alphabetical tie in DATs
            # without cloneof relationships. Keep promo/debug-only games.
            variant = bool(re.search(r"[\(\[][^)\]]*\b(debug|promo)\b",
                                     game.name, re.IGNORECASE))
            return (lang_best, best, int(variant), 0 if not game.cloneof else 1, game.name)

        winners = (min(members, key=lambda member: rank(member[0]))
                   for members in groups.values())
        return {game.name: table.game(entry) for game, entry in winners}


_REGION_IN_NAME = re.compile(r"\(([^)]*)\)")


def _regions_in(name: str) -> set[str]:
    """Region words from a No-Intro style name: `Game (USA, Europe)`."""
    out = set()
    for group in _REGION_IN_NAME.findall(name or ""):
        for part in group.split(","):
            out.add(part.strip().lower())
    return out


#: A ClrMamePro block header: `game (` or `machine (` at the start of a line.
_CMP_BLOCK = re.compile(r"^\s*(game|machine|resource)\s*\(\s*$", re.M)
#: `name "Some Game (USA)"` or `name Some_Game` -- quoted or bare.
_CMP_FIELD = re.compile(r'(\w+)\s+(?:"([^"]*)"|(\S+))')
# Compiled once: these run per block across hundreds of thousands of blocks,
# and re's compile cache lookup alone was a measurable share of startup.
_CMP_HEADER = re.compile(r"clrmamepro\s*\((.*?)\n\)", re.S)
_CMP_NAME = re.compile(r'^\s*name\s+(?:"([^"]*)"|(\S+))', re.M)
_CMP_CLONE = re.compile(r'^\s*cloneof\s+(?:"([^"]*)"|(\S+))', re.M)
_CMP_LANG = re.compile(r'^\s*languages?\s+(?:"([^"]*)"|(\S+))', re.M)
_CMP_ROM = re.compile(r"^\s*rom\s*\((.*?)\)\s*$", re.M)

#: Bumped whenever the record layout below changes, so an old cache is
#: re-parsed rather than misread.
DAT_RECORD_FORMAT = 1


def _cmp_fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _CMP_FIELD.finditer(text):
        value = m.group(2)
        out[m.group(1)] = value if value is not None else m.group(3)
    return out


def _cmp_value(pattern, block: str) -> str:
    m = pattern.search(block)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2)).strip()


def _rom_tuple(name: str, size, crc: str, md5: str, sha1: str) -> tuple:
    try:
        size = int(size or 0)
    except ValueError:
        size = 0
    return ((name or "").strip(), size, (crc or "").strip().lower(),
            (md5 or "").strip().lower(), (sha1 or "").strip().lower())


def _clrmamepro_record(body: str) -> tuple:
    """The other DAT format, which is not XML at all.

    libretro-database ships the largest freely downloadable corpus of
    No-Intro-derived DATs, and every one of them is ClrMamePro rather than
    Logiqx -- No-Intro's own XML is behind a captcha. Feeding those to an XML
    parser produced a ParseError per file and an index of nothing, so
    verification answered UNKNOWN for the whole library and looked like it
    was working.
    """
    body = body or ""
    name = version = ""
    header = _CMP_HEADER.search(body)
    if header:
        fields = _cmp_fields(header.group(1))
        name = (fields.get("name") or "").strip()
        version = (fields.get("version") or "").strip()

    entries = []
    for match in _CMP_BLOCK.finditer(body):
        # A block runs to the first `)` alone on a line. Nested `rom (...)`
        # entries close on the same line, so they never end the block early.
        end = body.find("\n)", match.end())
        if end == -1:
            continue
        block = body[match.end():end]
        game = _cmp_value(_CMP_NAME, block)
        if not game:
            continue
        roms = []
        for line in _CMP_ROM.finditer(block):
            f = _cmp_fields(line.group(1))
            roms.append(_rom_tuple(f.get("name"), f.get("size"), f.get("crc"),
                                   f.get("md5"), f.get("sha1")))
        entries.append((game, _cmp_value(_CMP_CLONE, block),
                        parse_dat_languages(_cmp_value(_CMP_LANG, block)),
                        tuple(roms)))
    return (name, version, entries)


def _parse_clrmamepro(body: str) -> Dat:
    return dat_from_record(_clrmamepro_record(body))


def _logiqx_record(xml_text: str):
    """Logiqx XML as a record, streamed rather than built as one tree.

    A full DAT tree is several times the size of its text; each `game` is
    read and released as it closes, so peak memory follows the largest game,
    not the whole file. Returns None when the text is not well-formed XML.
    """
    import xml.etree.ElementTree as ET

    parser = ET.XMLPullParser(events=("start", "end"))
    name = version = ""
    raw = []  # (name, cloneof, cloneofid, languages, roms)
    ids: dict[str, str] = {}
    root = None
    chunk = 1 << 20
    try:
        for offset in range(0, len(xml_text), chunk):
            parser.feed(xml_text[offset:offset + chunk])
            for event, node in parser.read_events():
                if event == "start":
                    if root is None:
                        root = node
                    continue
                if node.tag == "header" and not name and not version:
                    name = (node.findtext("name") or "").strip()
                    version = (node.findtext("version") or "").strip()
                elif node.tag == "game":
                    game = (node.get("name") or "").strip()
                    node_id = (node.get("id") or "").strip()
                    if node_id and game:
                        ids[node_id] = game
                    if game:
                        roms = tuple(
                            _rom_tuple(r.get("name"), r.get("size"), r.get("crc"),
                                       r.get("md5"), r.get("sha1"))
                            for r in node.findall("rom"))
                        raw_lang = (node.get("language") or node.findtext("language")
                                    or node.get("languages") or node.findtext("languages")
                                    or "").strip()
                        raw.append((game, (node.get("cloneof") or "").strip(),
                                    (node.get("cloneofid") or "").strip(),
                                    parse_dat_languages(raw_lang), roms))
                    if root is not None:
                        root.clear()  # drop every finished child
        parser.close()
    except (ET.ParseError, ValueError):
        return None
    if root is None:
        return None
    entries = [(game, parent or ids.get(parent_id, ""), langs, roms)
               for game, parent, parent_id, langs, roms in raw]
    return (name, version, entries)


def dat_from_record(record) -> Dat:
    """Build a Dat from its record: (name, version, entries).

    Each entry is (game, cloneof, languages, roms) with roms as
    (name, size, crc, md5, sha1) tuples, in DAT order and including
    duplicate game names -- the indexes keep the first hit for every hash,
    so rebuilding from the record gives exactly what parsing gave.
    """
    name, version, entries = record
    dat = Dat(name=name, version=version)
    dat.games = GameTable(entries)
    return dat


def parse_dat_record(body: str):
    """The record for a DAT in either format publishers actually use.

    Logiqx XML is what No-Intro and Redump publish directly. ClrMamePro is
    what most mirrors carry, including libretro-database -- so accepting only
    the first meant the DATs an operator can actually download without
    solving a captcha all parsed to nothing.
    """
    text = body or ""
    xml_text = text.lstrip("﻿").lstrip()
    looks_xml = (xml_text.startswith("<?xml")
                 or re.search(r"<datafile(?:\s|>)", xml_text[:4096], re.I))
    record = _logiqx_record(xml_text) if looks_xml else None
    if record is None:
        record = _clrmamepro_record(text)
        if record[2]:
            return record
        record = _logiqx_record(xml_text)
        if record is None:
            log.warning("DAT is neither valid XML nor ClrMamePro; ignoring it")
            return ("", "", [])
    return record


def parse_dat(body: str) -> Dat:
    """A DAT in either format publishers actually use."""
    return dat_from_record(parse_dat_record(body))


class DatIndex:
    """Every loaded DAT, queried as one.

    An operator has one DAT per system and a file arriving from a download does
    not announce which system it belongs to -- so verification asks all of
    them. An index with nothing in it answers `unknown`, because day one is the
    common case and it must not turn every import into an error.
    """

    def __init__(self):
        self.dats: list[Dat] = []
        # Sparse collision indexes, keyed by first-hit row: the per-hash
        # indexes intentionally return their first entry. Request approval
        # must retain other entries.
        self._duplicate_hits: list[tuple[dict, dict, dict]] = []

    def add(self, dat: Dat) -> None:
        """Index one DAT, recording which hashes it declares more than once."""
        if not dat.games:
            return
        self.dats.append(dat)
        self._duplicate_hits.append(dat.games.collisions())

    def verified_candidates(self, **hashes) -> tuple[Match, ...]:
        """Expose every matching DAT identity, including same-catalog collisions.

        Preserve legacy lookup's first-match semantics for existing callers.
        Collision maps are built only once on catalog addition, rather than
        scanning millions of DAT records for every imported ROM.
        """
        candidates = []
        sha1 = str(hashes.get("sha1") or "").lower()
        md5 = str(hashes.get("md5") or "").lower()
        crc = str(hashes.get("crc") or "").lower()
        size = int(hashes.get("size") or 0)

        def hashes_agree(rom: Rom) -> bool:
            # Legacy lookup can fall back to CRC, but requests must not trust
            # a candidate whose stronger declared hashes disagree.
            return not ((size and rom.size and size != rom.size)
                        or (sha1 and rom.sha1 and sha1 != rom.sha1)
                        or (md5 and rom.md5 and md5 != rom.md5))

        for dat, (extra_crc, extra_md5, extra_sha1) in zip(self.dats, self._duplicate_hits):
            match = dat.lookup(**hashes)
            if match.status != VERIFIED:
                continue
            table = dat.games
            if sha1 and (first := table.sha1_row(sha1)) is not None:
                extra = extra_sha1.get(first, ())
            elif md5 and (first := table.md5_row(md5)) is not None:
                extra = extra_md5.get(first, ())
            elif crc and size and (first := table.crc_row(crc, size)) is not None:
                extra = extra_crc.get(first, ())
            else:
                # A CRC without file size is not enough to establish unique
                # identity when distinct sizes may share that CRC.
                continue
            if match.rom is not None and hashes_agree(match.rom):
                candidates.append(match)
            for row in extra:
                name, rom = table.hit(row)
                if hashes_agree(rom):
                    candidates.append(Match(VERIFIED, name, rom, dat.name))
        return tuple(candidates)

    def game_rom_count(self, match: Match) -> int | None:
        """Count all declared ROMs for a uniquely identified, verified DAT game.

        A Match carries a display catalog name, not a stable catalog ID.
        Missing or duplicate matching catalogs must not certify completeness.
        Preserve first-match lookup compatibility for legacy import callers.
        """
        if (match.status != VERIFIED or match.rom is None
                or not match.dat or not match.game):
            return None
        # A repeated game label can hide members in either DAT parser. Do not
        # certify a request from the surviving record, regardless of hash hit.
        if any(dat.name == match.dat and dat.games.is_ambiguous(match.game)
               for dat in self.dats):
            return None
        games = [game for dat in self.dats if dat.name == match.dat
                 if (game := dat.games.get(match.game)) is not None
                 and match.rom in game.roms]
        if len(games) != 1:
            return None
        return len(games[0].roms)

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
