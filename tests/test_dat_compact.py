"""The packed DAT index answers exactly what the dict-based one did.

`dat._GameTable` replaced one `Rom` object per ROM and three dicts of hex
strings with flat columns and sorted row arrays, to cut resident memory on
small NAS boxes. That is only safe if every answer is unchanged, so this file
keeps a frozen copy of the 1.13.2 dict-based index as an oracle and compares
the two on DATs built to hit every awkward path: repeated game names whose
later entry shadows the earlier one, hashes shared across games, CRC32
collisions across sizes, ROMs without a size, sizes without a hash, and
values that are not canonical lowercase hex.
"""

import random
import re
from array import array

import pytest

from romarr.dat import (
    BAD_DUMP,
    UNKNOWN,
    VERIFIED,
    Dat,
    DatIndex,
    Game,
    Match,
    Rom,
    _regions_in,
    base_title,
    dat_from_record,
    game_languages,
    normalise_lang,
    parse_dat,
)

# -- the oracle: dat.py's index as of 1.13.2, trimmed to what it answers ----

class LegacyDat:
    """Frozen copy of the 1.13.2 `Dat` index (games dict + per-hash dicts)."""

    def __init__(self, record):
        self.name, self.version, entries = record
        self.games, self.ambiguous = {}, set()
        self.by_crc, self.by_sha1, self.by_md5, self.sizes = {}, {}, {}, {}
        for game, clone, langs, rom_rows in entries:
            roms = []
            for rom_name, size, crc, md5, sha1 in rom_rows:
                rom = Rom(name=rom_name, size=size, crc=crc, md5=md5, sha1=sha1)
                roms.append(rom)
                if crc:
                    self.by_crc.setdefault((crc, size), (game, rom))
                if sha1:
                    self.by_sha1.setdefault(sha1, (game, rom))
                if md5:
                    self.by_md5.setdefault(md5, (game, rom))
                if size:
                    self.sizes.setdefault(size, game)
            if game in self.games:
                self.ambiguous.add(game)
            self.games[game] = Game(name=game, roms=tuple(roms), cloneof=clone,
                                    languages=tuple(langs))

    def lookup(self, *, crc="", md5="", sha1="", size=0):
        sha1 = str(sha1 or "").lower()
        hit = self.by_sha1.get(sha1)
        if hit:
            return Match(VERIFIED, hit[0], hit[1], self.name)
        hit = self.by_md5.get(str(md5 or "").lower())
        if hit and not (sha1 and hit[1].sha1 and sha1 != hit[1].sha1):
            return Match(VERIFIED, hit[0], hit[1], self.name)
        crc = str(crc or "").lower()
        if crc:
            hit = self.by_crc.get((crc, int(size or 0)))
            if hit is None and not size:
                hit = next((v for (c, _), v in self.by_crc.items() if c == crc), None)
            if hit and not (sha1 and hit[1].sha1 and sha1 != hit[1].sha1):
                return Match(VERIFIED, hit[0], hit[1], self.name)
        if size and int(size) in self.sizes:
            known = self.sizes[int(size)]
            return Match(BAD_DUMP, dat=self.name,
                         detail=f"{size} bytes matches {known!r} but the "
                                f"checksum does not -- corrupt or altered")
        return Match(UNKNOWN, dat=self.name)


class LegacyIndex:
    """Frozen copy of the 1.13.2 `DatIndex` collision and candidate logic."""

    def __init__(self, dats):
        self.dats, self.extras = [], []
        for dat in dats:
            if not dat.games:
                continue
            extra = ({}, {}, {})
            for game_name, game in dat.games.items():
                for rom in game.roms:
                    identity = (game_name, rom)
                    for key, first, out in (
                            ((rom.crc, rom.size) if rom.crc else None, dat.by_crc, extra[0]),
                            (rom.md5 or None, dat.by_md5, extra[1]),
                            (rom.sha1 or None, dat.by_sha1, extra[2])):
                        if key is not None and key in first and first[key] != identity:
                            hits = out.setdefault(key, [])
                            if identity not in hits:
                                hits.append(identity)
            self.dats.append(dat)
            self.extras.append(extra)

    def lookup(self, **hashes):
        best = Match(UNKNOWN)
        for dat in self.dats:
            got = dat.lookup(**hashes)
            if got.status == VERIFIED:
                return got
            if got.status == BAD_DUMP and best.status == UNKNOWN:
                best = got
        return best

    def verified_candidates(self, **hashes):
        out = []
        sha1 = str(hashes.get("sha1") or "").lower()
        md5 = str(hashes.get("md5") or "").lower()
        crc = str(hashes.get("crc") or "").lower()
        size = int(hashes.get("size") or 0)

        def agree(rom):
            return not ((size and rom.size and size != rom.size)
                        or (sha1 and rom.sha1 and sha1 != rom.sha1)
                        or (md5 and rom.md5 and md5 != rom.md5))

        for dat, (e_crc, e_md5, e_sha1) in zip(self.dats, self.extras):
            match = dat.lookup(**hashes)
            if match.status != VERIFIED:
                continue
            if sha1 and sha1 in dat.by_sha1:
                extra = e_sha1.get(sha1, ())
            elif md5 and md5 in dat.by_md5:
                extra = e_md5.get(md5, ())
            elif crc and size and (crc, size) in dat.by_crc:
                extra = e_crc.get((crc, size), ())
            else:
                continue
            if match.rom is not None and agree(match.rom):
                out.append(match)
            out.extend(Match(VERIFIED, n, r, dat.name) for n, r in extra if agree(r))
        return tuple(out)

    def game_rom_count(self, match):
        if (match.status != VERIFIED or match.rom is None
                or not match.dat or not match.game):
            return None
        if any(d.name == match.dat and match.game in d.ambiguous for d in self.dats):
            return None
        games = [g for d in self.dats if d.name == match.dat
                 if (g := d.games.get(match.game)) is not None and match.rom in g.roms]
        return len(games[0].roms) if len(games) == 1 else None


# -- generated DATs ------------------------------------------------------------

def _hex(rng, width, pool):
    """A hash, often reused from `pool` so collisions are common."""
    if pool and rng.random() < 0.3:
        return rng.choice(pool)
    value = "".join(rng.choice("0123456789abcdef") for _ in range(width))
    pool.append(value)
    return value


def _odd(rng, width, pool):
    """Mostly canonical hex; sometimes empty, short, or not hex at all."""
    roll = rng.random()
    if roll < 0.12:
        return ""
    if roll < 0.18:
        return rng.choice(["0f", "11", "xyz", "abc", "-", "n/a"])
    return _hex(rng, width, pool)


def _record(seed, *, games=60, odd=True):
    rng = random.Random(seed)
    names = [f"Game {i} ({rng.choice(['USA', 'Europe', 'Japan'])})" for i in range(games // 2)]
    pools = ([], [], [])
    sizes = [0, 1, 512, 32768, 32768, 1 << 20, 1 << 33, -3]
    entries = []
    for _ in range(games):
        game = rng.choice(names)  # repeats: shadowed entries and ambiguity
        clone = rng.choice(["", "", rng.choice(names), "Missing Parent"])
        langs = rng.choice([(), ("en",), ("en", "fr")])
        roms = []
        for _ in range(rng.choice([0, 1, 1, 1, 2, 3])):
            make = _odd if odd else _hex
            crc = make(rng, 8, pools[0])
            md5 = make(rng, 32, pools[1])
            sha1 = make(rng, 40, pools[2])
            size = rng.choice(sizes + ([1 << 70] if odd else []))
            rom_name = rng.choice([game + ".bin", game + " (Track 1).bin", "other.bin", game])
            roms.append((rom_name, size, crc, md5, sha1))
        entries.append((game, clone, langs, tuple(roms)))
    # an exact duplicate row under one name: identities must dedupe
    entries.append((names[0], "", (), (("dup.bin", 4, "0badf00d", "", ""),) * 2))
    return (f"DAT {seed}", "1", entries)


def _queries(record, seed):
    rng = random.Random(seed)
    out = [{}, {"size": 4}, {"crc": "0BADF00D"}, {"crc": "0badf00d", "size": 4},
           {"sha1": "f" * 40}, {"md5": "e" * 32, "size": 512}, {"crc": "xyz"},
           {"crc": "abc", "size": 1 << 70}, {"size": 1 << 70}, {"size": -3}]
    for _game, _clone, _langs, roms in record[2]:
        for _name, size, crc, md5, sha1 in roms:
            out += [{"sha1": sha1}, {"sha1": sha1.upper(), "size": size},
                    {"md5": md5}, {"md5": md5, "sha1": "0" * 40},
                    {"crc": crc, "size": size}, {"crc": crc}, {"crc": crc.upper(), "size": 0},
                    {"crc": crc, "size": size + 1}, {"size": size},
                    {"crc": crc, "md5": md5, "sha1": sha1, "size": size},
                    {"crc": crc, "sha1": rng.choice([sha1, "1" * 40])}]
    return out


SEEDS = range(40)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_lookup_matches_the_dict_index(seed):
    record = _record(seed)
    new, old = dat_from_record(record), LegacyDat(record)
    for query in _queries(record, seed):
        assert new.lookup(**query) == old.lookup(**query), query


@pytest.mark.parametrize("seed", SEEDS)
def test_games_mapping_matches_the_dict(seed):
    record = _record(seed)
    new, old = dat_from_record(record), LegacyDat(record)
    assert list(new.games) == list(old.games)
    assert list(new.games.items()) == list(old.games.items())
    assert list(new.games.values()) == list(old.games.values())
    assert len(new.games) == len(old.games)
    assert new.games.ambiguous == old.ambiguous
    for name in list(old.games) + ["Missing Parent", "", None]:
        assert (name in new.games) == (name in old.games)
        assert new.games.get(name) == old.games.get(name)
        assert new.parent_of(name or "") == (
            (old.games[name].cloneof or name) if name in old.games else (name or ""))
    assert new.declares_clones == any(g.cloneof for g in old.games.values())


@pytest.mark.parametrize("seed", range(0, 40, 2))
def test_candidates_counts_and_index_lookup_match(seed):
    records = [_record(seed), _record(seed + 1), _record(seed, games=30), ("Empty", "", [])]
    new = DatIndex()
    for record in records:
        new.add(dat_from_record(record))
    old = LegacyIndex([LegacyDat(r) for r in records])
    for query in _queries(records[0], seed) + _queries(records[1], seed):
        assert new.lookup(**query) == old.lookup(**query), query
        got = new.verified_candidates(**query)
        assert got == old.verified_candidates(**query), query
        for match in got:
            assert new.game_rom_count(match) == old.game_rom_count(match), match


@pytest.mark.parametrize("seed", range(5))
def test_group_keys_and_1g1r_match_the_dict_index(seed):
    record = _record(seed, games=200)
    new, old = dat_from_record(record), LegacyDat(record)
    for name in list(old.games) + ["Missing Parent (USA) (Disc 2)"]:
        entry = old.games.get(name)
        assert new.group_key(name) == base_title(
            (entry.cloneof or entry.name) if entry else name)
    for regions, langs in ((["usa", "europe"], ["en"]), (["japan"], None), ([], ["fr", "en"])):
        got = new.one_game_one_rom(regions, langs)
        assert list(got.items()) == list(_legacy_1g1r(old, regions, langs).items())


def _legacy_1g1r(dat, regions, languages):
    """1.13.2's one_game_one_rom over the oracle's games dict."""
    groups = {}
    for game in dat.games.values():
        entry = dat.games.get(game.name)
        groups.setdefault(base_title(entry.cloneof or entry.name), []).append(game)
    order = [r.lower() for r in regions]
    lang_order = [normalise_lang(c) for c in (languages or ()) if str(c).strip()]

    def rank(game):
        lang_best = min((lang_order.index(c) for c in game_languages(game) if c in lang_order),
                        default=len(lang_order)) if lang_order else 0
        best = min((order.index(r) for r in _regions_in(game.name) if r in order),
                   default=len(order))
        variant = bool(re.search(r"[\(\[][^)\]]*\b(debug|promo)\b", game.name, re.IGNORECASE))
        return (lang_best, best, int(variant), 0 if not game.cloneof else 1, game.name)

    return {min(m, key=rank).name: min(m, key=rank) for m in groups.values()}


def test_a_dat_built_from_games_indexes_them_like_a_parsed_one():
    rom = Rom(name="Sonic (USA).md", size=524288, crc="0011aabb",
              md5="a" * 32, sha1="b" * 40)
    built = Dat(name="Sega", games={"Sonic (USA)": Game(name="Sonic (USA)", roms=(rom,))})
    parsed = parse_dat('clrmamepro (\n\tname "Sega"\n)\n\ngame (\n\tname "Sonic (USA)"\n'
                       f'\trom ( name "Sonic (USA).md" size 524288 crc 0011aabb '
                       f'md5 {"a" * 32} sha1 {"b" * 40} )\n)\n')
    for query in ({"sha1": "B" * 40}, {"md5": "a" * 32}, {"crc": "0011AABB", "size": 524288},
                  {"size": 524288}):
        assert built.lookup(**query) == parsed.lookup(**query)
        assert built.lookup(**query).status in (VERIFIED, BAD_DUMP)
    assert built.games["Sonic (USA)"].roms == (rom,)


def test_rom_names_and_odd_values_come_back_verbatim():
    record = ("D", "", [
        ("Game (USA)", "", (), (("Game (USA).nes", 40976, "AbC", "0F", "11"),
                                ("Game (USA) (Track 01).bin", 1 << 70, "0011aabb", "", ""),
                                ("Unrelated.bin", 7, "00000001", "", ""))),
    ])
    roms = dat_from_record(record).games["Game (USA)"].roms
    assert [(r.name, r.size, r.crc, r.md5, r.sha1) for r in roms] == [
        rom for rom in record[2][0][3]]


def test_the_table_is_packed_not_a_rom_object_per_row():
    table = dat_from_record(_record(3, games=400, odd=False)).games
    for attr in ("_sha1_rows", "_md5_rows", "_crc_rows", "_size_rows", "_order", "_by_name"):
        assert isinstance(getattr(table, attr), array), attr
    assert not table._odd, "canonical hex never takes the per-row fallback"
    assert not any(isinstance(v, (list, tuple, dict)) and v
                   and isinstance(next(iter(v)), Rom) for v in vars(table).values())
