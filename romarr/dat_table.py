"""The packed storage behind `dat.Dat`: one DAT's games and hash indexes.

`dat.py` is what DAT verification means -- verified, bad dump, unknown, and
the copier-header trap. This module is only how a parsed DAT is held in
memory, kept apart so the semantics read without the bit-packing. See
`GameTable` for why it is packed at all.
"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import ItemsView, Mapping, ValuesView
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Game:
    name: str
    roms: tuple[Rom, ...] = ()
    #: No-Intro marks regional variants as clones of a parent entry. This is
    #: what makes 1G1R possible at all.
    cloneof: str = ""
    #: Normalised two-letter language codes declared by the DAT's `language`
    #: field (e.g. `("en", "fr")` from `language "English / French"`).
    languages: tuple[str, ...] = ()



#: Row flags in `GameTable._flags`. A hash bit means the value is canonical
#: lowercase hex, lives in its packed column and is in that hash's sorted
#: index; `_CRC` also requires a size that fits the signed 64-bit column.
_CRC, _MD5, _SHA1, _SIZE, _SUFFIX, _ODD = 1, 2, 4, 8, 16, 32
_INT64 = (-(1 << 63), (1 << 63) - 1)
_HEX = {8: re.compile(r"[0-9a-f]{8}"), 32: re.compile(r"[0-9a-f]{32}"),
        40: re.compile(r"[0-9a-f]{40}")}


def _is_hex(value, width: int) -> bool:
    """Whether `value` is exactly `width` lowercase hex digits."""
    return (isinstance(value, str) and len(value) == width
            and _HEX[width].fullmatch(value) is not None)


def _fits(size) -> bool:
    """Whether `size` is an int the packed signed 64-bit size column can hold."""
    return isinstance(size, int) and _INT64[0] <= size <= _INT64[1]


def _prefix(digest: bytes) -> int:
    """A digest's first four bytes as an int, for native-speed bisects."""
    return int.from_bytes(digest[:4], "big")


@lru_cache(maxsize=64)
def _hex_query(value: str, width: int) -> bytes | None:
    """A looked-up hash as raw bytes when it is canonical hex, else None.

    Cached because one lookup asks every loaded DAT the same question.
    """
    return bytes.fromhex(value) if _is_hex(value, width) else None


_ZERO16, _ZERO20 = bytes(16), bytes(20)


class GameTable(Mapping):
    """A DAT's games and its hash indexes, packed into flat columns.

    This is `Dat.games`: a read-only mapping from game name to `Game`, in the
    order a dict built from the DAT would have (first appearance of a name,
    last entry with that name wins). `Game` and `Rom` objects are made on
    access rather than kept, because one object per ROM plus three hash dicts
    holding hex strings cost about 900 bytes a ROM -- 237 MB resident for the
    111 libretro DATs, enough to OOM a 192 MB NAS container while loading.

    Each ROM row is a few array slots: CRC32 as a 32-bit int, MD5 and SHA1 as
    raw bytes, size as a 64-bit int, and the name as UTF-8 in one shared
    blob -- for the 70% of ROMs named after their game, only the suffix
    (".nes", " (Track 01).bin"). Game names live in a blob the same way, a
    clone points at its parent's entry number, and languages index a short
    table of distinct tuples. No string or object is kept per game or per
    ROM, which matters as much as the bytes saved: thousands of small
    survivors from parsing would pin the allocator's arenas, and the
    process would keep most of the parse's peak resident.

    Every hash index is an array of row numbers sorted by (hash, row) and
    searched with `bisect`, so the first hit for a hash is the earliest row
    in DAT order -- exactly what `dict.setdefault` used to keep. A value that
    is not canonical lowercase hex (or a size outside 64 bits) is never
    rewritten: that row keeps its original strings, and those keys go into
    small dicts with the old string semantics instead.
    """

    def __init__(self, entries=()):
        self._name_blob = bytearray()
        self._name_off = array("I", [0])
        self._rom_blob = bytearray()
        self._rom_off = array("I", [0])
        self._starts = array("I")
        self._lang_ids = array("I")
        self._flags = bytearray()
        self._sizes = array("q")
        self._crcs = array("I")
        self._md5s = bytearray()
        self._sha1s = bytearray()
        self._odd: dict[int, tuple] = {}
        self._odd_crc: dict[tuple, list[int]] = {}
        self._odd_md5: dict[str, list[int]] = {}
        self._odd_sha1: dict[str, list[int]] = {}
        self._odd_sizes: dict[int, list[int]] = {}
        clones, lang_table = self._pack(entries)
        self._starts.append(len(self._flags))
        self._name_blob, self._rom_blob = bytes(self._name_blob), bytes(self._rom_blob)
        self._flags = bytes(self._flags)
        self._md5s, self._sha1s = bytes(self._md5s), bytes(self._sha1s)
        self._langs = tuple(lang_table)
        # Each index is rows sorted by (key, row), plus the sorted keys (or a
        # 4-byte digest prefix) beside them so `bisect` compares native ints.
        self._sha1_rows = self._sorted(_SHA1, self._sha1_key)
        self._sha1_pre = array("I", (_prefix(self._sha1_key(r)) for r in self._sha1_rows))
        self._md5_rows = self._sorted(_MD5, self._md5_key)
        self._md5_pre = array("I", (_prefix(self._md5_key(r)) for r in self._md5_rows))
        self._crc_rows = self._sorted(_CRC, self._crc_key)
        self._crc_keys = array("I", (self._crcs[r] for r in self._crc_rows))
        self._size_rows = self._sorted(_SIZE, self._sizes.__getitem__)
        self._size_keys = array("q", (self._sizes[r] for r in self._size_rows))
        self._dict_order(clones)

    def _pack(self, entries) -> tuple[list[str], dict]:
        """Append every entry and its ROM rows to the columns.

        Returns the raw cloneof names (resolved to entries afterwards, since
        a clone may name a parent that comes later) and the language table.
        """
        clones: list[str] = []
        lang_table: dict[tuple, int] = {}
        for game, clone, game_langs, rom_rows in entries:
            self._name_blob += game.encode("utf-8", "surrogatepass")
            self._name_off.append(len(self._name_blob))
            clones.append(clone)
            self._lang_ids.append(lang_table.setdefault(tuple(game_langs), len(lang_table)))
            self._starts.append(len(self._flags))
            for rom_name, size, crc, md5, sha1 in rom_rows:
                flag = 0
                if rom_name.startswith(game):
                    rom_name = rom_name[len(game):]
                    flag = _SUFFIX
                self._rom_blob += rom_name.encode("utf-8", "surrogatepass")
                self._rom_off.append(len(self._rom_blob))
                self._flags.append(self._pack_hashes(flag, size, crc, md5, sha1))
        return clones, lang_table

    def _pack_hashes(self, flag: int, size, crc, md5, sha1) -> int:
        """Append one row's size and hashes; return its flags."""
        row = len(self._flags)
        size_ok = _fits(size)
        if not size_ok:
            flag |= _ODD
        self._sizes.append(size if size_ok else 0)
        if crc:
            if size_ok and _is_hex(crc, 8):
                flag |= _CRC
            else:
                flag |= _ODD
                self._odd_crc.setdefault((crc, size), []).append(row)
        self._crcs.append(int(crc, 16) if flag & _CRC else 0)
        if md5:
            if _is_hex(md5, 32):
                flag |= _MD5
            else:
                flag |= _ODD
                self._odd_md5.setdefault(md5, []).append(row)
        self._md5s += bytes.fromhex(md5) if flag & _MD5 else _ZERO16
        if sha1:
            if _is_hex(sha1, 40):
                flag |= _SHA1
            else:
                flag |= _ODD
                self._odd_sha1.setdefault(sha1, []).append(row)
        self._sha1s += bytes.fromhex(sha1) if flag & _SHA1 else _ZERO20
        if size:
            if size_ok:
                flag |= _SIZE
            else:
                self._odd_sizes.setdefault(size, []).append(row)
        if flag & _ODD:
            self._odd[row] = (crc, md5, sha1, size)
        return flag

    def _dict_order(self, clones: list[str]) -> None:
        """Games as a dict would hold them: first appearance, last entry wins.

        Also resolves each cloneof to the first entry with that name, keeping
        the few that name no entry as strings.
        """
        position: dict[bytes, int] = {}
        first: dict[bytes, int] = {}
        order: list[int] = []
        ambiguous: set[int] = set()
        for entry in range(len(self._starts) - 1):
            name = self._name_key(entry)
            first.setdefault(name, entry)
            at = position.get(name)
            if at is None:
                position[name] = len(order)
                order.append(entry)
            else:
                order[at] = entry
                # Duplicate labels make request completeness ambiguous,
                # even when hashes match.
                ambiguous.add(at)
        self._clone_ids = array("i", [-1]) * len(clones)
        self._clone_names: dict[int, str] = {}
        for entry, clone in enumerate(clones):
            if clone:
                parent = first.get(clone.encode("utf-8", "surrogatepass"))
                if parent is None:
                    self._clone_names[entry] = clone
                else:
                    self._clone_ids[entry] = parent
        del position, first
        self._order = array("I", order)
        #: Whether any game in the mapping declares a parent.
        self.has_clones = any(self._clone_ids[e] >= 0 or e in self._clone_names
                              for e in order)
        self._by_name = array("I", sorted(order, key=self._name_key))
        self._ambiguous = array("I", sorted((order[at] for at in ambiguous),
                                            key=self._name_key))

    def _name_key(self, entry: int) -> bytes:
        """An entry's name as UTF-8; byte order is code point order."""
        return self._name_blob[self._name_off[entry]:self._name_off[entry + 1]]

    def _sorted(self, bit: int, key) -> array:
        """Rows carrying `bit`, sorted by (key, row) for first-hit bisects."""
        flags = self._flags
        rows = [row for row in range(len(flags)) if flags[row] & bit]
        rows.sort(key=key)  # stable: equal keys stay in DAT order
        return array("I", rows)

    def _md5_key(self, row: int) -> bytes:
        return self._md5s[16 * row:16 * row + 16]

    def _sha1_key(self, row: int) -> bytes:
        return self._sha1s[20 * row:20 * row + 20]

    def _crc_key(self, row: int) -> tuple[int, int]:
        return (self._crcs[row], self._sizes[row])

    @staticmethod
    def _first(rows: array, key, want):
        """The first row whose key equals `want`, or None."""
        at = bisect_left(rows, want, key=key)
        if at < len(rows) and key(rows[at]) == want:
            return rows[at]
        return None

    @staticmethod
    def _digest_row(value: str, width: int, rows: array, prefixes: array,
                    key, odd: dict) -> int | None:
        """The first row whose hash is `value`: by 4-byte prefix, else verbatim."""
        want = _hex_query(value, width)
        if want is None:
            hits = odd.get(value)
            return hits[0] if hits else None
        prefix = _prefix(want)
        at = bisect_left(prefixes, prefix)
        while at < len(rows) and prefixes[at] == prefix:
            if key(rows[at]) == want:
                return rows[at]
            at += 1
        return None

    def _crc_range(self, crc: int) -> tuple[int, int]:
        """The slice of `_crc_rows` whose CRC32 is `crc`."""
        lo = bisect_left(self._crc_keys, crc)
        return lo, bisect_right(self._crc_keys, crc, lo)

    # -- the hash indexes ------------------------------------------------

    def sha1_row(self, sha1: str) -> int | None:
        """The first row whose SHA1 is `sha1` (already lowercased)."""
        return self._digest_row(sha1, 40, self._sha1_rows, self._sha1_pre,
                                self._sha1_key, self._odd_sha1)

    def md5_row(self, md5: str) -> int | None:
        """The first row whose MD5 is `md5` (already lowercased)."""
        return self._digest_row(md5, 32, self._md5_rows, self._md5_pre,
                                self._md5_key, self._odd_md5)

    def crc_row(self, crc: str, size: int) -> int | None:
        """The first row whose (CRC32, size) is exactly (`crc`, `size`)."""
        if (want := _hex_query(crc, 8)) is not None and _fits(size):
            lo, hi = self._crc_range(int.from_bytes(want, "big"))
            # Within one CRC the rows are sorted by (size, row).
            at = bisect_left(self._crc_rows, size, lo, hi, key=self._sizes.__getitem__)
            if at < hi and self._sizes[self._crc_rows[at]] == size:
                return self._crc_rows[at]
            return None
        hits = self._odd_crc.get((crc, size))
        return hits[0] if hits else None

    def crc_any_size(self, crc: str) -> int | None:
        """The earliest row with CRC32 `crc`, whatever its size."""
        best = None
        if (want := _hex_query(crc, 8)) is not None:
            lo, hi = self._crc_range(int.from_bytes(want, "big"))
            if lo < hi:
                best = min(self._crc_rows[lo:hi])
        for (value, _), hits in self._odd_crc.items():
            if value == crc and (best is None or hits[0] < best):
                best = hits[0]
        return best

    def size_row(self, size: int) -> int | None:
        """The first row whose size is `size`."""
        if _fits(size):
            at = bisect_left(self._size_keys, size)
            if at < len(self._size_keys) and self._size_keys[at] == size:
                return self._size_rows[at]
            return None
        hits = self._odd_sizes.get(size)
        return hits[0] if hits else None

    # -- rows and entries --------------------------------------------------

    def entry_of(self, row: int) -> int:
        """The DAT entry (game block) a ROM row belongs to."""
        return bisect_right(self._starts, row) - 1

    def entry(self, name) -> int | None:
        """The entry `self[name]` reads, or None when there is none."""
        if not isinstance(name, str):
            return None
        return self._first(self._by_name, self._name_key,
                           name.encode("utf-8", "surrogatepass"))

    def is_ambiguous(self, name) -> bool:
        """Whether more than one entry declares the game name `name`."""
        return isinstance(name, str) and self._first(
            self._ambiguous, self._name_key,
            name.encode("utf-8", "surrogatepass")) is not None

    @property
    def ambiguous(self) -> set[str]:
        """Every game name more than one entry declares."""
        return {self.name_of(entry) for entry in self._ambiguous}

    def rom(self, row: int, entry: int | None = None) -> Rom:
        """The ROM in `row`, rebuilt with its original strings."""
        flag = self._flags[row]
        name = self._rom_blob[self._rom_off[row]:self._rom_off[row + 1]].decode(
            "utf-8", "surrogatepass")
        if flag & _SUFFIX:
            name = self.name_of(self.entry_of(row) if entry is None else entry) + name
        if flag & _ODD:
            crc, md5, sha1, size = self._odd[row]
        else:
            size = self._sizes[row]
            crc = f"{self._crcs[row]:08x}" if flag & _CRC else ""
            md5 = self._md5_key(row).hex() if flag & _MD5 else ""
            sha1 = self._sha1_key(row).hex() if flag & _SHA1 else ""
        return Rom(name=name, size=size, crc=crc, md5=md5, sha1=sha1)

    def hit(self, row: int) -> tuple[str, Rom]:
        """(game name, ROM) for a row, as the old per-hash dicts held it."""
        entry = self.entry_of(row)
        return self.name_of(entry), self.rom(row, entry)

    def game(self, entry: int) -> Game:
        """The `Game` for one entry, ROMs included."""
        start, end = self._starts[entry], self._starts[entry + 1]
        return Game(name=self.name_of(entry),
                    roms=tuple(self.rom(row, entry) for row in range(start, end)),
                    cloneof=self.clone_of(entry),
                    languages=self._langs[self._lang_ids[entry]])

    def clone_of(self, entry: int) -> str:
        """The entry's declared cloneof, or ""."""
        parent = self._clone_ids[entry]
        if parent >= 0:
            return self.name_of(parent)
        return self._clone_names.get(entry, "")

    def entries(self):
        """Entry numbers of the mapping's games, in mapping order."""
        return iter(self._order)

    def summary(self, entry: int) -> Game:
        """The entry's `Game` without its ROMs, for ranking and grouping."""
        return Game(name=self.name_of(entry), cloneof=self.clone_of(entry),
                    languages=self._langs[self._lang_ids[entry]])

    def name_of(self, entry: int) -> str:
        """The entry's game name."""
        return self._name_key(entry).decode("utf-8", "surrogatepass")

    def collisions(self) -> tuple[dict, dict, dict]:
        """Sparse (crc, md5, sha1) maps: first-hit row -> other rows sharing it.

        Mirrors what `DatIndex.add` always computed: walking the games in
        mapping order, every ROM whose (game, ROM) differs from the first
        hit for one of its hashes is kept once, in walk order.
        """
        groups = (self._groups(self._crc_rows, self._crc_key, self._odd_crc),
                  self._groups(self._md5_rows, self._md5_key, self._odd_md5),
                  self._groups(self._sha1_rows, self._sha1_key, self._odd_sha1))
        extras: tuple[dict, dict, dict] = ({}, {}, {})
        seen: tuple[dict, dict, dict] = ({}, {}, {})
        shared = set().union(*groups)
        if not shared:
            return extras
        identity: dict[int, tuple] = {}

        def ident(row: int) -> tuple:
            if row not in identity:
                identity[row] = self.hit(row)
            return identity[row]

        starts = self._starts
        for entry in self._order:
            for row in range(starts[entry], starts[entry + 1]):
                if row not in shared:
                    continue
                for group, extra, kept in zip(groups, extras, seen):
                    first = group.get(row)
                    if first is None or first == row:
                        continue
                    mine = ident(row)
                    if mine == ident(first):
                        continue
                    if mine not in kept.setdefault(first, set()):
                        kept[first].add(mine)
                        extra.setdefault(first, []).append(row)
        return tuple({first: tuple(rows) for first, rows in extra.items()}
                     for extra in extras)

    @staticmethod
    def _groups(rows: array, key, odd: dict) -> dict[int, int]:
        """row -> first row of its key, for keys more than one row shares."""
        out: dict[int, int] = {}
        at, total = 0, len(rows)
        while at < total:
            want = key(rows[at])
            end = at + 1
            while end < total and key(rows[end]) == want:
                end += 1
            if end - at > 1:
                for i in range(at, end):
                    out[rows[i]] = rows[at]
            at = end
        for hits in odd.values():
            if len(hits) > 1:
                for row in hits:
                    out[row] = hits[0]
        return out

    # -- Mapping -----------------------------------------------------------

    def __getitem__(self, name) -> Game:
        entry = self.entry(name)
        if entry is None:
            raise KeyError(name)
        return self.game(entry)

    def __contains__(self, name) -> bool:
        return self.entry(name) is not None

    def __iter__(self):
        return (self.name_of(entry) for entry in self._order)

    def __len__(self) -> int:
        return len(self._order)

    def values(self):
        return _GameValues(self)

    def items(self):
        return _GameItems(self)

    def __repr__(self) -> str:
        return f"<{len(self)} games>"


class _GameValues(ValuesView):
    """`games.values()` without a name lookup per game."""

    def __iter__(self):
        table = self._mapping
        return (table.game(entry) for entry in table._order)


class _GameItems(ItemsView):
    """`games.items()` without a name lookup per game."""

    def __iter__(self):
        table = self._mapping
        return ((table.name_of(entry), table.game(entry)) for entry in table._order)
