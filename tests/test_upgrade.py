"""Upgrades, tags and manual import."""

from __future__ import annotations

import zlib

import pytest

from romarr.dat import BAD_DUMP, DatIndex, UNKNOWN, VERIFIED, parse_dat
from romarr.platforms import by_slug
from romarr.upgrade import (
    is_upgrade, matches_tags, merge_tags, normalise_tag, scan)


# --- upgrades: the one comparison that is a fact ---------------------------

def test_verified_beats_unknown():
    """Radarr upgrades on a quality ladder, which is taste. This is not: an
    unverified file replaced by a verified one is a statement about the bytes,
    and it is the only upgrade rule in this class of tool that cannot be
    wrong."""
    got = is_upgrade(UNKNOWN, VERIFIED)
    assert got.upgrade and "verified" in got.reason


def test_verified_beats_a_bad_dump():
    assert is_upgrade(BAD_DUMP, VERIFIED).upgrade


def test_unknown_beats_a_bad_dump():
    """A bad dump ranks BELOW unknown deliberately. Unknown is the normal
    state of homebrew and translations; bad-dump means a DAT knows what this
    should be and this is not it. Ranking them equal leaves a corrupt file
    sitting there forever with nothing able to replace it."""
    assert is_upgrade(BAD_DUMP, UNKNOWN).upgrade


def test_nothing_downgrades():
    assert not is_upgrade(VERIFIED, UNKNOWN).upgrade
    assert not is_upgrade(VERIFIED, BAD_DUMP).upgrade
    assert not is_upgrade(UNKNOWN, BAD_DUMP).upgrade


def test_two_verified_copies_are_not_an_upgrade():
    """Re-downloading a byte-identical game is churn, not an upgrade."""
    got = is_upgrade(VERIFIED, VERIFIED, current_size=100, candidate_size=200)
    assert not got.upgrade


def test_a_sidegrade_is_possible_but_must_be_asked_for():
    assert is_upgrade(UNKNOWN, UNKNOWN, current_size=100, candidate_size=200,
                      allow_sidegrade=True).upgrade
    assert not is_upgrade(UNKNOWN, UNKNOWN, current_size=200,
                          candidate_size=100, allow_sidegrade=True).upgrade


def test_an_unrecognised_verdict_is_treated_as_unknown():
    assert is_upgrade("nonsense", VERIFIED).upgrade


def test_every_decision_explains_itself():
    for current, candidate in [(UNKNOWN, VERIFIED), (VERIFIED, UNKNOWN),
                               (VERIFIED, VERIFIED)]:
        assert is_upgrade(current, candidate).reason


# --- tags ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Favourites", "favourites"),
    ("  favourites  ", "favourites"),
    ("FAVOURITES", "favourites"),
    ("co-op games", "co-op games"),
    ("bad!!chars##", "badchars"),
    ("multiple   spaces", "multiple spaces"),
])
def test_tags_are_normalised(raw, expected):
    """A tag is typed by a person. "Favourites", "favourites " and
    "FAVOURITES" are one tag, and treating them as three produces a filter
    list nobody can use."""
    assert normalise_tag(raw) == expected


def test_merging_deduplicates_and_sorts():
    """Sorted because tags render as a row of chips, and a set that reorders
    itself on every edit is unreadable."""
    assert merge_tags(["b", "A"], adding=["a", "c"]) == ["a", "b", "c"]


def test_removing_a_tag():
    assert merge_tags(["a", "b"], removing=["A"]) == ["b"]


def test_empty_tags_are_dropped_rather_than_stored():
    assert merge_tags(["", "  ", "!!"], adding=[""]) == []


def test_matching_requires_every_tag():
    assert matches_tags(["a", "b"], ["a"])
    assert matches_tags(["a", "b"], ["A", "b"])
    assert not matches_tags(["a"], ["a", "b"])


def test_requiring_nothing_matches_everything():
    assert matches_tags([], [])
    assert matches_tags(["a"], None)


# --- manual import ---------------------------------------------------------

PLATFORMS = [by_slug(s) for s in ("snes", "psx", "genesis-slash-megadrive",
                                  "atari2600", "nes")]


def test_a_scan_finds_roms(tmp_path):
    (tmp_path / "Super Metroid (USA).sfc").write_bytes(b"\x00" * 1024)
    (tmp_path / "Contra (USA).nes").write_bytes(b"\x00" * 512)
    got = scan(tmp_path, PLATFORMS)
    assert {c.filename for c in got.candidates} == {
        "Super Metroid (USA).sfc", "Contra (USA).nes"}
    assert {c.platform for c in got.candidates} == {"snes", "nes"}


def test_extras_are_skipped_not_listed(tmp_path):
    """Listing twenty rows of readmes makes an operator read past them to find
    the four that matter."""
    (tmp_path / "Game.sfc").write_bytes(b"\x00")
    for junk in ("readme.nfo", "cover.png", "notes.txt", "hashes.md5"):
        (tmp_path / junk).write_bytes(b"x")
    got = scan(tmp_path, PLATFORMS)
    assert [c.filename for c in got.candidates] == ["Game.sfc"]
    assert got.skipped == 4


def test_the_parent_directory_settles_an_ambiguous_extension(tmp_path):
    """`.bin` is Mega Drive, Atari 2600 and a PlayStation track. A file in a
    folder called `psx` is taken at its word -- that folder name is a
    deliberate statement by whoever laid the library out."""
    folder = tmp_path / "psx"
    folder.mkdir()
    (folder / "Game (Track 1).bin").write_bytes(b"\x00" * 2048)
    got = scan(tmp_path, PLATFORMS)
    assert got.candidates[0].platform == "psx"
    assert "folder" in got.candidates[0].reason


def test_an_ambiguous_extension_with_no_hint_says_so(tmp_path):
    """It still offers a guess -- refusing to list the file would hide it --
    but the reason tells the operator to confirm."""
    (tmp_path / "Mystery.bin").write_bytes(b"\x00" * 2048)
    got = scan(tmp_path, PLATFORMS)
    assert len(got.candidates) == 1
    assert "confirm" in got.candidates[0].reason


def test_an_unknown_extension_is_skipped(tmp_path):
    (tmp_path / "thing.qqq").write_bytes(b"\x00")
    got = scan(tmp_path, PLATFORMS)
    assert got.candidates == [] and got.skipped == 1


def test_a_scan_recurses(tmp_path):
    deep = tmp_path / "a" / "b" / "snes"
    deep.mkdir(parents=True)
    (deep / "Game.sfc").write_bytes(b"\x00")
    assert len(scan(tmp_path, PLATFORMS).candidates) == 1


def test_scanning_something_that_is_not_a_directory_is_reported(tmp_path):
    got = scan(tmp_path / "nope", PLATFORMS)
    assert got.candidates == [] and "not a directory" in got.error


def test_a_scan_is_bounded(tmp_path):
    """A pointed-at-root scan must not try to list a million files."""
    for i in range(20):
        (tmp_path / f"g{i}.sfc").write_bytes(b"\x00")
    assert len(scan(tmp_path, PLATFORMS, limit=5).candidates) == 5


def test_a_scan_verifies_against_a_dat_when_one_is_loaded(tmp_path):
    """The strongest statement available about a found file, and it names the
    actual game rather than the filename somebody typed."""
    body = b"\x5A" * 4096
    crc = f"{zlib.crc32(body) & 0xFFFFFFFF:08x}"
    index = DatIndex()
    index.add(parse_dat(f"""<?xml version="1.0"?><datafile>
      <header><name>SNES</name></header>
      <game name="Super Metroid (USA)">
        <rom name="x.sfc" size="{len(body)}" crc="{crc.upper()}"/>
      </game></datafile>"""))

    (tmp_path / "badly named file.sfc").write_bytes(body)
    got = scan(tmp_path, PLATFORMS, dats=index)

    assert got.candidates[0].verdict == VERIFIED
    assert got.candidates[0].game == "Super Metroid (USA)"
    assert "Super Metroid (USA)" in got.candidates[0].reason


def test_a_scan_without_dats_still_works(tmp_path):
    (tmp_path / "Game.sfc").write_bytes(b"\x00")
    got = scan(tmp_path, PLATFORMS)
    assert got.candidates[0].verdict == UNKNOWN
