"""Proving an import is the right file, which no other *arr can do.

Radarr cannot tell you whether the file it imported is correct. There is no
canonical hash for a movie, so its last mile is "something arrived, it is
probably fine". Every game manager that scores filenames -- gamarr included --
is making the same guess with better vocabulary.

ROMs are the exception: No-Intro and Redump publish the CRC32, MD5 and SHA1 of
every correct dump that exists. So the question "is this the right file" has an
answer, and these tests are that answer.
"""

from __future__ import annotations

import zlib

import pytest

from romarr.dat import (
    DatIndex, Match, VERIFIED, BAD_DUMP, UNKNOWN, header_bytes, parse_dat,
    hash_bytes)

# A Logiqx DAT, the format both No-Intro and Redump publish.
NOINTRO = """<?xml version="1.0"?>
<datafile>
  <header>
    <name>Nintendo - Super Nintendo Entertainment System</name>
    <version>20260101-000000</version>
  </header>
  <game name="Super Metroid (USA)">
    <description>Super Metroid (USA)</description>
    <rom name="Super Metroid (USA).sfc" size="3145728"
         crc="D63ED5F8" md5="21f3e98df4780ee1c667b84e57d88675"
         sha1="da957f0d63d14cb441d215462904d982ec45a7ca"/>
  </game>
  <game name="Super Metroid (Europe)" cloneof="Super Metroid (USA)">
    <description>Super Metroid (Europe)</description>
    <rom name="Super Metroid (Europe).sfc" size="3145728" crc="AAAAAAAA"/>
  </game>
  <game name="Super Metroid (Japan)" cloneof="Super Metroid (USA)">
    <description>Super Metroid (Japan)</description>
    <rom name="Super Metroid (Japan).sfc" size="3145728" crc="BBBBBBBB"/>
  </game>
  <game name="Chrono Trigger (USA)">
    <rom name="Chrono Trigger (USA).sfc" size="4194304" crc="2D206BF7"/>
  </game>
</datafile>"""

# Redump lists every track of a disc as its own <rom>, which is why a cue+bin
# set has to verify per track rather than as one file.
REDUMP = """<?xml version="1.0"?>
<datafile>
  <header><name>Sony - PlayStation</name></header>
  <game name="Silent Hill (USA)">
    <rom name="Silent Hill (USA).cue" size="123" crc="11111111"/>
    <rom name="Silent Hill (USA) (Track 1).bin" size="600000" crc="22222222"/>
    <rom name="Silent Hill (USA) (Track 2).bin" size="300000" crc="33333333"/>
  </game>
</datafile>"""


@pytest.fixture
def snes():
    return parse_dat(NOINTRO)


@pytest.fixture
def psx():
    return parse_dat(REDUMP)


# --- parsing ---------------------------------------------------------------

def test_a_dat_knows_what_it_is(snes):
    assert snes.name.startswith("Nintendo - Super Nintendo")
    assert snes.version == "20260101-000000"


def test_every_game_is_indexed(snes):
    assert len(snes.games) == 4


def test_a_rom_carries_every_hash_the_dat_published(snes):
    rom = snes.games["Super Metroid (USA)"].roms[0]
    assert rom.size == 3145728
    assert rom.crc == "d63ed5f8", "normalised to lower case for comparison"
    assert rom.md5 == "21f3e98df4780ee1c667b84e57d88675"
    assert rom.sha1.startswith("da957f0d")


def test_a_disc_game_has_a_rom_per_track(psx):
    game = psx.games["Silent Hill (USA)"]
    assert len(game.roms) == 3
    assert [r.name.rsplit(".", 1)[1] for r in game.roms] == ["cue", "bin", "bin"]


def test_junk_is_not_an_exception():
    assert parse_dat("<not xml").games == {}
    assert parse_dat("").games == {}


# --- lookup ----------------------------------------------------------------

def test_a_known_crc_is_verified(snes):
    got = snes.lookup(crc="d63ed5f8", size=3145728)
    assert got.status == VERIFIED
    assert got.game == "Super Metroid (USA)"
    assert got.rom.name == "Super Metroid (USA).sfc"


def test_crc_matching_is_case_insensitive(snes):
    assert snes.lookup(crc="D63ED5F8", size=3145728).status == VERIFIED


def test_an_unknown_crc_is_unknown_not_bad(snes):
    """Absence from a DAT is not proof of a bad dump.

    A homebrew title, a translation, a brand-new release, or simply an older
    DAT all produce a hash the file does not contain. Calling that "bad" would
    have ROMarr rejecting perfectly good files with total confidence.
    """
    got = snes.lookup(crc="ffffffff", size=999)
    assert got.status == UNKNOWN
    assert got.game == ""


def test_a_right_size_wrong_hash_is_a_bad_dump(snes):
    """This is the case worth catching. A file the exact size of a known ROM
    whose contents do not match is a corrupt or tampered dump -- not something
    unknown, something wrong."""
    got = snes.lookup(crc="deadbeef", size=3145728)
    assert got.status == BAD_DUMP
    assert "Super Metroid" in got.detail or "3145728" in got.detail


def test_sha1_wins_over_crc_when_both_are_available(snes):
    """CRC32 is 32 bits and collides. Where the DAT published a SHA1 and the
    caller computed one, that is the answer."""
    got = snes.lookup(crc="00000000", sha1="da957f0d63d14cb441d215462904d982ec45a7ca")
    assert got.status == VERIFIED
    assert got.game == "Super Metroid (USA)"


def test_lookup_with_nothing_to_go_on_is_unknown(snes):
    assert snes.lookup().status == UNKNOWN


# --- the copier-header trap ------------------------------------------------
#
# The single reason naive implementations of this never match anything.

@pytest.mark.parametrize("suffix,size", [
    (".nes", 16),      # iNES
    (".fds", 16),      # fwNES
    (".lnx", 64),      # Lynx
    (".a78", 128),     # Atari 7800
])
def test_the_known_headers_are_declared(suffix, size):
    assert header_bytes(suffix, b"\x00" * 4096) == size


def test_an_smc_header_is_detected_by_size_not_by_extension():
    """A .smc may or may not carry a 512-byte copier header -- the extension
    does not say. The rule the whole preservation world uses is arithmetic:
    a headered dump is 512 bytes more than a multiple of 32KB.
    """
    assert header_bytes(".smc", b"\x00" * (32768 + 512)) == 512
    assert header_bytes(".smc", b"\x00" * 32768) == 0


def test_a_headered_rom_still_verifies(snes):
    """The trap, end to end.

    No-Intro hashes are headerless. A headered file hashed as-is produces a
    CRC that appears in no DAT ever published, so every single NES and SNES
    import would come back "unknown" and the feature would look useless
    rather than broken.
    """
    body = b"\xA5" * 3145728
    headered = b"\x00" * 512 + body
    crc = f"{zlib.crc32(body) & 0xFFFFFFFF:08x}"
    dat = parse_dat(NOINTRO.replace("D63ED5F8", crc.upper()))

    computed = hash_bytes(headered, suffix=".smc")
    assert computed["size"] == 3145728, "the header is excluded from the size too"
    assert dat.lookup(**computed).status == VERIFIED


def test_a_headerless_rom_is_unaffected():
    body = b"\x5A" * 4096
    plain = hash_bytes(body, suffix=".sfc")
    assert plain["size"] == 4096
    assert plain["crc"] == f"{zlib.crc32(body) & 0xFFFFFFFF:08x}"


def test_hashing_reports_every_algorithm_the_dats_use():
    got = hash_bytes(b"abc", suffix=".bin")
    assert set(got) >= {"crc", "md5", "sha1", "size"}


# --- 1G1R: one game, one ROM ----------------------------------------------

def test_clones_resolve_to_their_parent(snes):
    assert snes.parent_of("Super Metroid (Europe)") == "Super Metroid (USA)"
    assert snes.parent_of("Super Metroid (USA)") == "Super Metroid (USA)"


def test_one_game_one_rom_keeps_the_preferred_region(snes):
    """Igir's headline feature, and the reason a 10,000-entry set collapses to
    a playable 3,000. The regional ladder is the scorer's, so a 1G1R set and a
    grab of the same game agree about which dump is wanted."""
    kept = snes.one_game_one_rom(["USA", "Europe", "Japan"])
    assert "Super Metroid (USA)" in kept
    assert "Super Metroid (Europe)" not in kept
    assert "Super Metroid (Japan)" not in kept
    assert "Chrono Trigger (USA)" in kept, "a game with no clones survives"


def test_one_game_one_rom_falls_back_when_the_preference_is_absent(snes):
    """A game that exists only as a Japanese dump must not vanish because the
    operator prefers USA -- dropping it silently is how a "cleaned" set loses
    titles nobody notices for months."""
    kept = snes.one_game_one_rom(["Brazil"])
    assert len(kept) == 2, kept


# --- the shape callers depend on ------------------------------------------

def test_a_match_is_readable(snes):
    got = snes.lookup(crc="d63ed5f8", size=3145728)
    assert "Super Metroid (USA)" in str(got)
    assert isinstance(got, Match)


def test_an_index_merges_several_dats():
    """An operator has one DAT per system. Verification has to consult all of
    them without the caller knowing which one a file belongs to."""
    index = DatIndex()
    index.add(parse_dat(NOINTRO))
    index.add(parse_dat(REDUMP))
    assert index.lookup(crc="d63ed5f8", size=3145728).status == VERIFIED
    assert index.lookup(crc="22222222", size=600000).status == VERIFIED
    assert index.lookup(crc="0badf00d", size=7).status == UNKNOWN
    assert len(index.dats) == 2


def test_an_empty_index_answers_unknown_rather_than_raising():
    """An operator with no DATs loaded is the common case on day one, and it
    must not turn every import into an error."""
    assert DatIndex().lookup(crc="d63ed5f8").status == UNKNOWN


# --- hashing a real file, which is what an import actually does ------------

def test_hash_file_agrees_with_hash_bytes(tmp_path):
    body = b"\x37" * 100_000
    path = tmp_path / "Game (USA).sfc"
    path.write_bytes(body)
    from romarr.dat import hash_file
    assert hash_file(path) == hash_bytes(body, suffix=".sfc")


def test_hash_file_strips_a_header_the_same_way(tmp_path):
    body = b"\xA5" * (32768 * 4)
    path = tmp_path / "Game (USA).smc"
    path.write_bytes(b"\x00" * 512 + body)
    from romarr.dat import hash_file
    got = hash_file(path)
    assert got["size"] == len(body)
    assert got["crc"] == hash_bytes(body, suffix=".sfc")["crc"]


def test_hash_file_never_buffers_the_whole_file(tmp_path):
    """The header probe used to build a buffer the size of the file to decide
    whether 512 bytes should be skipped -- four gigabytes for a PS2 image,
    reintroducing the exact problem chunked hashing exists to avoid.

    Asserted by reading in tiny chunks: a correct implementation seeks past
    the header and streams, so chunk size cannot change the answer.
    """
    body = b"\x11" * 300_000
    path = tmp_path / "Big (USA).iso"
    path.write_bytes(body)
    from romarr.dat import hash_file
    assert hash_file(path, chunk=997) == hash_file(path, chunk=1024 * 1024)
