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



def test_same_catalog_crc_collision_exposes_both_games():
    dat = parse_dat("""
    <datafile><header><name>Single System</name></header>
      <game name="Requested Game"><rom name="one.bin" size="4" crc="a1b2c3d4"/></game>
      <game name="Other Game"><rom name="two.bin" size="4" crc="a1b2c3d4"/></game>
    </datafile>""")
    index = DatIndex()
    index.add(dat)
    matches = index.verified_candidates(crc="a1b2c3d4", size=4)
    assert [(m.dat, m.game) for m in matches] == [
        ("Single System", "Requested Game"),
        ("Single System", "Other Game"),
    ]
    assert index.lookup(crc="a1b2c3d4", size=4).game == "Requested Game"


def test_same_catalog_sha1_collision_exposes_both_games():
    sha1 = "a" * 40
    dat = parse_dat(f"""
    <datafile><header><name>Single System</name></header>
      <game name="Requested Game"><rom name="one.bin" size="4" sha1="{sha1}"/></game>
      <game name="Other Game"><rom name="two.bin" size="4" sha1="{sha1}"/></game>
    </datafile>""")
    index = DatIndex()
    index.add(dat)
    assert [m.game for m in index.verified_candidates(sha1=sha1, size=4)] == [
        "Requested Game", "Other Game",
    ]


def test_same_catalog_md5_collision_exposes_both_games():
    md5 = "b" * 32
    dat = parse_dat(f"""
    <datafile><header><name>Single System</name></header>
      <game name="Requested Game"><rom name="one.bin" size="4" md5="{md5}"/></game>
      <game name="Other Game"><rom name="two.bin" size="4" md5="{md5}"/></game>
    </datafile>""")
    index = DatIndex()
    index.add(dat)
    assert [m.game for m in index.verified_candidates(md5=md5, size=4)] == [
        "Requested Game", "Other Game",
    ]


def test_strongest_hash_first_preserves_legacy_precedence():
    dat = parse_dat("""
    <datafile><header><name>Single System</name></header>
      <game name="CRC Game"><rom name="crc.bin" size="4" crc="a1b2c3d4"/></game>
      <game name="SHA1 Game"><rom name="sha1.bin" size="4" sha1="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/></game>
    </datafile>""")
    index = DatIndex()
    index.add(dat)
    matches = index.verified_candidates(
        sha1="a" * 40, crc="a1b2c3d4", size=4)
    assert [m.game for m in matches] == ["SHA1 Game"]


def test_crc_only_without_size_is_not_approved_as_a_unique_candidate():
    index = DatIndex()
    index.add(_dat("Single System", "Requested Game"))
    assert index.lookup(crc="a1b2c3d4").status == VERIFIED
    assert index.verified_candidates(crc="a1b2c3d4") == ()
