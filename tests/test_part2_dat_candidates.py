"""Part 2 prepublication provenance: never trust the first DAT hit alone.

These tests are deliberately read-only. They do not yet exercise the queue
guard or any archive imports; that integration remains separately required.
"""

from romarr.dat import BAD_DUMP, DatIndex, VERIFIED, parse_dat


def _dat(name: str, game: str, crc: str = "a1b2c3d4"):
    """Build a minimal, independent catalog with a known four-byte ROM."""
    return parse_dat(
        f'<datafile><header><name>{name}</name></header>'
        f'<game name="{game}"><rom name="content.bin" size="4" '
        f'crc="{crc}"/></game></datafile>'
    )


def test_all_verified_catalogs_are_exposed_not_only_the_first():
    index = DatIndex()
    index.add(_dat("Platform A", "Requested Game"))
    index.add(_dat("Platform B", "Different Game"))

    candidates = index.verified_candidates(crc="A1B2C3D4", size=4)

    assert len(candidates) == 2
    assert [(match.game, match.dat) for match in candidates] == [
        ("Requested Game", "Platform A"),
        ("Different Game", "Platform B"),
    ]
    assert all(match.status == VERIFIED for match in candidates)


def test_the_existing_lookup_still_reports_its_first_verification():
    index = DatIndex()
    index.add(_dat("First", "First Game"))
    index.add(_dat("Second", "Second Game"))

    assert index.lookup(crc="a1b2c3d4", size=4).game == "First Game"
    assert len(index.verified_candidates(crc="a1b2c3d4", size=4)) == 2


def test_same_game_across_two_catalogs_preserves_both_provenances():
    index = DatIndex()
    index.add(_dat("System A", "The Game"))
    index.add(_dat("System B", "The Game"))

    assert [match.dat for match in index.verified_candidates(
        crc="a1b2c3d4", size=4
    )] == ["System A", "System B"]


def test_wrong_size_is_not_a_verified_identity():
    index = DatIndex()
    index.add(_dat("First", "Game"))

    assert index.verified_candidates(crc="a1b2c3d4", size=5) == ()


def test_wrong_checksum_with_known_size_is_not_verified():
    index = DatIndex()
    index.add(_dat("First", "Game"))

    assert index.verified_candidates(crc="00000000", size=4) == ()
    assert index.lookup(crc="00000000", size=4).status == BAD_DUMP


def test_empty_index_returns_no_verified_candidates():
    index = DatIndex()

    assert index.verified_candidates(crc="a1b2c3d4", size=4) == ()


def test_only_matching_catalogs_are_returned():
    index = DatIndex()
    index.add(_dat("Matches", "Requested Game"))
    index.add(_dat("Other", "Unrelated Game", crc="ffffffff"))

    matches = index.verified_candidates(crc="a1b2c3d4", size=4)

    assert [(match.game, match.dat) for match in matches] == [
        ("Requested Game", "Matches"),
    ]
