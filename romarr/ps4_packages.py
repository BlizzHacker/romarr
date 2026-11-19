"""PS4 game packages: tell the base game from its update and DLC, and file them.

A PS4 game usually arrives as several ``.pkg`` files: the base game, an update
(patch) and any DLC (additional content). Indexers carry all three kinds as
separate uploads, plus combined packs that hold the game with its update and
DLC. Two decisions depend on telling them apart:

* Search. A request for the game must not settle for a lone update or DLC,
  and a combined pack is the better grab. A request can also ask for the
  update and DLC, in which case the pack that covers the most of what was
  asked for wins. ``release_parts`` reads which parts a release title holds.
* Import. RomM files a multi-file game as one folder per game and reads a
  file's category from the first folder inside it, so the base package goes
  in ``<Title>/``, the update in ``<Title>/update/`` and DLC in
  ``<Title>/dlc/``. ``file_role`` reads the package header where the file is
  on disk, and falls back to its name; ``plan_game_folder`` picks the folder.

PS5 packages use the same names on indexers but a different container, so
PS5 gets the title classification and the folder layout, never the header.
"""
from __future__ import annotations

import functools
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .library_rename import _safe_basename

#: Platforms whose releases and packages are split into base, update and DLC.
PACKAGE_PLATFORMS = ("ps4", "ps5")
#: Platforms whose ``.pkg`` header layout ``read_header`` understands.
HEADER_PLATFORMS = ("ps4",)

BASE, UPDATE, DLC = "base", "update", "dlc"
#: Every part a request may ask for, in display order.
PARTS = (BASE, UPDATE, DLC)
#: What a request wants when it names no parts: the game itself.
DEFAULT_PARTS = (BASE,)
#: RomM's folder for each optional part, inside the game's own folder.
PART_FOLDERS = {UPDATE: "update", DLC: "dlc"}

_LABELS = {BASE: "base game", UPDATE: "update", DLC: "DLC"}

# -- release titles ---------------------------------------------------------

_B = r"(?<![a-z0-9])"   # left word boundary that treats "_" and "." as breaks
_E = r"(?![a-z0-9])"    # right word boundary, same rule
_UPDATE_WORDS = ("update", "updates", "upd", "patch", "patches", "hotfix")
_DLC_WORDS = ("dlc", "dlcs", "addon", "addons", "add-on", "add-ons",
              "add on", "add ons", "additional content", "season pass")
_MARKER_RE = re.compile(
    _B + r"(?P<word>"
    + "|".join(re.escape(w).replace(r"\ ", r"[ ._-]")
               for w in sorted(_UPDATE_WORDS + _DLC_WORDS, key=len, reverse=True))
    + r")" + _E)
#: ``CUSA12345-patch`` / ``-ac`` / ``-app`` suffixes some package tools write.
_ID_SUFFIX_RE = re.compile(r"(?<![a-z0-9])(?:cusa|ppsa)\d{5}[-_ ](?P<kind>app|patch|ac)" + _E)
#: The ``-A0100-V0100`` app/master version pair in package file names.
#: ``A0100-V0100`` is a base game; any other version pair is an update.
_VERSION_PAIR_RE = re.compile(_B + r"a(?P<app>\d{4})[-_ ]v(?P<master>\d{4})" + _E)
_BASE_RE = re.compile(_B + r"(?:base[ ._-]?game|full[ ._-]game)" + _E
                      + r"|" + _B + r"base(?=\s*(?:\+|&))")
#: Words that join an extra part onto the game: "+ Update", "incl. DLC".
_CONNECTOR_RE = re.compile(
    r"(?:\+|&|" + _B + r"(?:and|with|w/|incl|inc|including|includes|included|plus))$")
#: Filler allowed between a connector and the part it joins ("+ all 3 DLC").
#: One separator character per repetition and whole words only, so there is
#: a single way to match any text and no catastrophic backtracking.
_FILLER_RE = re.compile(
    r"(?:[\s._\-:(\[{]|" + _B + r"(?:all|the|latest|every|v?\d+x?)" + _E + r")+$")


def _squash(word: str) -> str:
    """A marker word without its separators: "add-on" -> "addon"."""
    return re.sub(r"[ ._-]", "", word.lower())


_UPDATE_KEYS = frozenset(_squash(w) for w in _UPDATE_WORDS)


def _kind_of(word: str) -> str:
    """Map a matched marker word to UPDATE or DLC."""
    return UPDATE if _squash(word) in _UPDATE_KEYS else DLC


def _connector(text: str):
    """The connector ("+", "incl") ending `text` once filler is skipped, or None."""
    return _CONNECTOR_RE.search(_FILLER_RE.sub("", text.lower()))


def _word_count(text: str, word: str) -> int:
    pattern = _B + re.escape(word.lower()).replace(r"\ ", r"[ ._-]") + _E
    return len(re.findall(pattern, text.lower()))


def _markers(lowered: str, wanted: str):
    """Yield (start, kind, joined) for each update/DLC word not in `wanted`."""
    seen: dict[str, int] = {}
    for match in _MARKER_RE.finditer(lowered):
        word = match.group("word")
        key = re.sub(r"[ ._-]", " ", word)
        seen[key] = seen.get(key, 0) + 1
        if wanted and seen[key] <= _word_count(wanted, key):
            continue
        yield match.start(), _kind_of(word), _connector(lowered[:match.start()]) is not None


def title_before_parts(title: str, wanted: str = "") -> str:
    """The game's name in a release title, before any update or DLC words.

    "Game + Update 1.05 + 3 DLC" and "Game.Update.v1.05.PS4" both name
    "Game"; the title matcher reads that part, so a pack or an update still
    matches the request it belongs to.
    """
    title = str(title or "")
    first = next(_markers(title.lower(), wanted), None)
    if first is None:
        return title
    core = title[:first[0]]
    while True:
        # Filler words ("all", "3", "v1.05") are skipped only to reach a
        # connector behind them; a number that is part of the name stays.
        connector = _connector(core)
        if connector is None:
            return core.rstrip(" ._-:([{")
        core = core[:connector.start()]


@functools.lru_cache(maxsize=4096)
def release_parts(title: str, wanted: str = "") -> frozenset[str]:
    """Which parts of a PS4 game a release title says it holds.

    ``{"base"}`` is a plain game upload, ``{"update"}`` or ``{"dlc"}`` an
    extra on its own, and a mix such as ``{"base", "update", "dlc"}`` a
    combined pack. Words that are part of the requested title (``wanted``)
    are not read as markers, so "Patch Quest" stays a game.

    The rule: an update or DLC word joined to what comes before it by "+",
    "&", "and", "with" or "incl" is included alongside the game. The first
    such word standing on its own means the release is that extra, not the
    game: "Game Update 1.05" is an update, "Game + Update 1.05" a pack.
    """
    lowered = str(title or "").lower()
    markers = [(kind, joined) for _, kind, joined in _markers(lowered, wanted)]
    parts: set[str] = set()
    suffix = _ID_SUFFIX_RE.search(lowered)
    if suffix:
        parts.add({"app": BASE, "patch": UPDATE, "ac": DLC}[suffix.group("kind")])
    pair = _VERSION_PAIR_RE.search(lowered)
    if pair:
        parts.add(BASE if (pair.group("app"), pair.group("master")) == ("0100", "0100") else UPDATE)
    if _BASE_RE.search(lowered):
        parts.add(BASE)
    if markers:
        parts.update(kind for kind, _ in markers)
        if markers[0][1]:
            parts.add(BASE)
    return frozenset(parts or {BASE})


def describe_parts(parts) -> str:
    """Readable name for a set of parts, e.g. "base game + update + DLC"."""
    return " + ".join(_LABELS[p] for p in PARTS if p in set(parts))


def normalise_parts(value) -> tuple[str, ...]:
    """Validate the parts a request asks for; nothing named means the game.

    Accepts a list of part names (or one name). Raises ValueError on anything
    else, so a typo is refused instead of silently becoming "game only".
    """
    if value is None or value == "" or value == []:
        return DEFAULT_PARTS
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError("parts must be a list of: " + ", ".join(PARTS))
    chosen = {v.strip().lower() for v in value}
    unknown = sorted(chosen - set(PARTS))
    if unknown:
        raise ValueError(f"unknown part(s) {', '.join(unknown)}; choose from " + ", ".join(PARTS))
    return tuple(p for p in PARTS if p in chosen) or DEFAULT_PARTS


# -- package files ----------------------------------------------------------

_MAGIC = b"\x7fCNT"
#: Bytes at the start of a package that hold every field `parse_header` reads.
HEADER_SIZE = 0x80
_CONTENT_ID = slice(0x40, 0x40 + 36)
_CONTENT_TYPE = slice(0x74, 0x78)
_CONTENT_FLAGS = slice(0x78, 0x7C)
#: Content types, from LibOrbisPkg's ``ContentType``.
_TYPE_GAME, _TYPE_ADDON, _TYPE_ADDON_LICENSE, _TYPE_DELTA_PATCH = 0x1A, 0x1B, 0x1C, 0x1E
#: ``FIRST_PATCH`` and ``SUBSEQUENT_PATCH`` content flags; delta and
#: cumulative patches set the subsequent bit as well.
_PATCH_FLAGS = 0x00100000 | 0x40000000
_CONTENT_ID_RE = re.compile(r"[A-Z]{2}\d{4}-(?P<tid>[A-Z]{4}\d{5})_\d{2}-[A-Z0-9_]{16}")
_TITLE_ID_RE = re.compile(r"(?<![A-Za-z0-9])(?P<tid>(?:CUSA|PPSA|PCAS|PCJS|PCKS|PLAS|PLJS|PLJM)\d{5})(?![0-9])",
                          re.IGNORECASE)


@dataclass(frozen=True)
class PackageHeader:
    """The few PS4 package header fields that say what a package is."""

    content_id: str
    content_type: int
    content_flags: int

    @property
    def role(self) -> str:
        """BASE, UPDATE or DLC, or "" for a content type this does not know."""
        if self.content_type in (_TYPE_ADDON, _TYPE_ADDON_LICENSE):
            return DLC
        if self.content_type == _TYPE_DELTA_PATCH:
            return UPDATE
        if self.content_type == _TYPE_GAME:
            return UPDATE if self.content_flags & _PATCH_FLAGS else BASE
        return ""


def parse_header(data: bytes | None) -> PackageHeader | None:
    """Decode the first 128 bytes of a PS4 ``.pkg``; None if it is not one."""
    if not data or len(data) < HEADER_SIZE or data[:4] != _MAGIC:
        return None
    content_id = data[_CONTENT_ID].split(b"\0", 1)[0].decode("ascii", "replace")
    return PackageHeader(content_id,
                         int.from_bytes(data[_CONTENT_TYPE], "big"),
                         int.from_bytes(data[_CONTENT_FLAGS], "big"))


def read_head(path: Path, size: int) -> bytes | None:
    """The first `size` bytes of a regular file, never through a link."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return None


def read_header(path: Path) -> PackageHeader | None:
    """Read a package header from a regular file, never following a link."""
    return parse_header(read_head(path, HEADER_SIZE))


def header_of(source, platform_slug: str, name: str) -> PackageHeader | None:
    """A download member's package header, when its platform has one and the
    source can read it cheaply (see ``library._Source.head``)."""
    if platform_slug not in HEADER_PLATFORMS or not name.lower().endswith(".pkg"):
        return None
    return parse_header(source.head(name, HEADER_SIZE))


def title_id(text: str) -> str:
    """The PS4/PS5 title ID (``CUSA12345``) in a content ID or file name, or ""."""
    text = str(text or "")
    found = _CONTENT_ID_RE.search(text.upper()) or _TITLE_ID_RE.search(text)
    return found.group("tid").upper() if found else ""


def file_role(relative: str, header: PackageHeader | None = None) -> str:
    """BASE, UPDATE or DLC for one package file inside a download.

    The header decides when there is one. Otherwise the file's folders count
    first ("Update/", "DLC/"), then its name, read the same way as a release
    title. A merged package ("Game + Update") holds the game, so it is BASE.
    """
    if header is not None and header.role:
        return header.role
    parts = [p for p in re.split(r"[\\/]+", str(relative or "")) if p]
    for folder in reversed(parts[:-1]):
        found = release_parts(folder)
        if found in ({UPDATE}, {DLC}):
            return next(iter(found))
    name = parts[-1] if parts else ""
    found = release_parts(Path(name).stem)
    return BASE if BASE in found else (UPDATE if UPDATE in found else DLC)


# -- library layout ---------------------------------------------------------

_MAX_FOLDER_LEN = 120
#: Bound the search for an existing game folder; a huge library still answers fast.
MAX_SCAN_FOLDERS = 20_000


def game_folder_name(title: str) -> str:
    """A safe single folder name for a game title, or "" if none can be made."""
    name = "".join(" " if ord(c) < 32 or c in '\\/:*?"<>|' else c for c in str(title or ""))
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")[:_MAX_FOLDER_LEN].rstrip(". ")
    try:
        return _safe_basename(name)   # the library's own device-name rules
    except ValueError:
        return ""


def existing_game_folder(platform_dir: Path, tid: str) -> str:
    """The one game folder whose base package carries title ID ``tid``, or "".

    Looks only at packages directly inside each folder (where a base lives),
    skips links, and reads headers before names. Two matching folders are as
    good as none: guessing would scatter the game.
    """
    if not tid:
        return ""
    found: set[str] = set()
    try:
        folders = sorted(os.scandir(platform_dir), key=lambda e: e.name)[:MAX_SCAN_FOLDERS]
    except OSError:
        return ""
    for entry in folders:
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            files = [f for f in os.scandir(entry.path)
                     if f.name.lower().endswith(".pkg") and f.is_file(follow_symlinks=False)]
        except OSError:
            continue
        for package in files:
            header = read_header(Path(package.path))
            role = file_role(package.name, header)
            if role == BASE and title_id(header.content_id if header else package.name) == tid:
                found.add(entry.name)
                break
        if len(found) > 1:
            return ""
    return found.pop() if found else ""


def plan_game_folder(packages: list[tuple[str, str, str]], platform_dir: Path,
                     title: str) -> dict[str, str] | None:
    """Relative folder (under the platform folder) for each package, or None.

    ``packages`` holds ``(primary, role, title_id)`` for every package in one
    download. The base goes in ``<Title>``, updates in ``<Title>/update`` and
    DLC in ``<Title>/dlc``. ``<Title>`` is the existing folder whose base has
    the same title ID when there is exactly one, else the request's title.

    None means "keep the normal flat destination": no packages, more than one
    base (a bundle of games is not one game), or no usable folder name.
    """
    if not packages:
        return None
    bases = [p for p in packages if p[1] == BASE]
    if len(bases) > 1:
        return None
    ids = {tid for _, _, tid in packages if tid}
    folder = existing_game_folder(Path(platform_dir), ids.pop()) if len(ids) == 1 else ""
    folder = folder or game_folder_name(title)
    if not folder:
        return None
    return {primary: (folder if role == BASE else f"{folder}/{PART_FOLDERS[role]}")
            for primary, role, _ in packages}
