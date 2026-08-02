"""A disc game is a set of files, and importing the sheet alone is silent death.

`pick_rom_file` answered with one filename. For a cartridge that is the whole
game. For a disc it is often a `.cue` -- a few hundred bytes of text naming
tracks that were left behind -- and the resulting library entry has a cover,
a title, a size, and boots nothing. Nothing downstream can detect that.

These tests pin the set, not the file.
"""

from __future__ import annotations

import pytest

from romarr.platforms import by_slug
from romarr.selection import RomSet, pick_rom_file, pick_rom_set

PSX = by_slug("psx")
DC = by_slug("dc")
SNES = by_slug("snes")
PS2 = by_slug("ps2")
WII = by_slug("wii")


def reader(contents: dict[str, str]):
    """A `read` callable over an in-memory archive listing."""
    def read(name: str) -> bytes | None:
        text = contents.get(name)
        return text.encode() if text is not None else None
    return read


# --- cartridges are unchanged ----------------------------------------------

def test_a_cartridge_is_a_set_of_one():
    got = pick_rom_set(["Super Metroid (USA).smc", "readme.nfo"], SNES)
    assert got.primary == "Super Metroid (USA).smc"
    assert got.members == ("Super Metroid (USA).smc",)
    assert not got.is_multi_file


def test_pick_rom_file_still_answers_for_callers_that_want_one_name():
    """The old entry point keeps working, on top of the new one.

    Two implementations of "which file is the ROM" would drift, and the one
    that drifted would be the one nobody was testing.
    """
    assert pick_rom_file(["Zelda.smc", "box.jpg"], SNES) == "Zelda.smc"
    assert pick_rom_file(["nothing.txt"], SNES) is None


def test_nothing_playable_is_still_none():
    assert pick_rom_set(["readme.nfo", "cover.png"], PSX) is None


# --- the failure this exists to prevent ------------------------------------

def test_a_cue_brings_its_bin():
    files = ["Final Fantasy VII (USA) (Disc 1).cue",
             "Final Fantasy VII (USA) (Disc 1).bin",
             "readme.nfo"]
    sheet = 'FILE "Final Fantasy VII (USA) (Disc 1).bin" BINARY\n' \
            '  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    got = pick_rom_set(files, PSX,
                       read=reader({"Final Fantasy VII (USA) (Disc 1).cue": sheet}))
    assert got.primary.endswith(".cue")
    assert "Final Fantasy VII (USA) (Disc 1).bin" in got.members
    assert got.is_multi_file
    assert "readme.nfo" not in got.members


def test_a_cue_with_many_audio_tracks_brings_all_of_them():
    files = ["game.cue"] + [f"game (Track {i:02d}).bin" for i in range(1, 8)]
    sheet = "".join(
        f'FILE "game (Track {i:02d}).bin" BINARY\n  TRACK {i:02d} AUDIO\n'
        for i in range(1, 8))
    got = pick_rom_set(files, PSX, read=reader({"game.cue": sheet}))
    assert len(got.members) == 8
    for i in range(1, 8):
        assert f"game (Track {i:02d}).bin" in got.members


def test_a_gdi_brings_its_tracks():
    """The live library's Dreamcast layout, verbatim.

    `dc/102 Dalmatians - Puppies to the Rescue (NA, Rev 1.001)/` holds a
    149-byte .gdi and five track files whose names share nothing with it --
    which is exactly why sidecars are read out of the sheet rather than
    guessed from the stem.
    """
    files = ["102 Dalmatians v1.001 (2000)(EIDOS)(NTSC)(US)[!].gdi",
             "track01.bin", "track02.raw", "track03.bin", "track04.raw",
             "track05.bin"]
    sheet = ("5\n"
             "1 0 4 2352 track01.bin 0\n"
             "2 600 0 2352 track02.raw 0\n"
             "3 45000 4 2352 track03.bin 0\n"
             "4 250000 0 2352 track04.raw 0\n"
             "5 300000 4 2352 track05.bin 0\n")
    got = pick_rom_set(files, DC, read=reader({files[0]: sheet}))
    assert got.primary.endswith(".gdi")
    assert set(got.members) == set(files)


def test_an_unquoted_cue_filename_is_read():
    """Not every cue quotes its FILE argument, and an unread sheet is a
    silently incomplete import rather than an error."""
    files = ["game.cue", "game.bin"]
    got = pick_rom_set(files, PSX,
                       read=reader({"game.cue": "FILE game.bin BINARY\n"}))
    assert "game.bin" in got.members


# --- whole-disc images need no sidecars ------------------------------------

def test_a_chd_is_a_set_of_one():
    got = pick_rom_set(["Silent Hill (USA).chd", "cover.png"], PSX)
    assert got.members == ("Silent Hill (USA).chd",)
    assert not got.is_multi_file


def test_a_chd_is_preferred_over_a_cue_bin_pair():
    """One file that cannot be separated from its tracks beats two that can."""
    files = ["game.chd", "game.cue", "game.bin"]
    got = pick_rom_set(files, PSX, read=reader({"game.cue": 'FILE "game.bin" BINARY\n'}))
    assert got.primary == "game.chd"
    assert not got.is_multi_file


def test_an_rvz_is_preferred_for_wii():
    got = pick_rom_set(["Metroid Prime 3 (USA).rvz", "Metroid Prime 3 (USA).iso"], WII)
    assert got.primary.endswith(".rvz")


# --- multi-disc ------------------------------------------------------------

def test_an_m3u_binds_every_disc_it_lists():
    """A playlist is what makes a multi-disc game work in RetroArch and
    EmulatorJS alike. Importing one disc of three is not importing the game."""
    files = ["Final Fantasy VII.m3u",
             "Final Fantasy VII (Disc 1).chd",
             "Final Fantasy VII (Disc 2).chd",
             "Final Fantasy VII (Disc 3).chd"]
    playlist = "\n".join(files[1:]) + "\n"
    got = pick_rom_set(files, PSX, read=reader({"Final Fantasy VII.m3u": playlist}))
    assert got.primary.endswith(".m3u")
    assert set(got.members) == set(files)
    assert got.is_multi_file


def test_two_discs_in_one_directory_do_not_bleed_into_each_other():
    """Each cue takes its own tracks. A glob for "every .bin nearby" would
    hand disc 1 both discs' data and produce a set that boots the wrong one."""
    files = ["Disc 1/game.cue", "Disc 1/game.bin",
             "Disc 2/game.cue", "Disc 2/game.bin"]
    sheets = {"Disc 1/game.cue": 'FILE "game.bin" BINARY\n',
              "Disc 2/game.cue": 'FILE "game.bin" BINARY\n'}
    got = pick_rom_set(files, PSX, read=reader(sheets))
    assert len(got.members) == 2
    parents = {m.rsplit("/", 1)[0] for m in got.members}
    assert len(parents) == 1, f"set spans two discs: {got.members}"


# --- the fallback, when the sheet cannot be read ---------------------------

def test_an_unreadable_sheet_falls_back_to_every_track_beside_it():
    """Over-inclusive on purpose.

    Taking a file too many costs disk. Taking one too few costs a game that
    imports, displays, and does not boot -- and nothing downstream can tell.
    """
    files = ["game.gdi", "track01.bin", "track02.raw", "track03.bin"]
    got = pick_rom_set(files, DC, read=None)
    assert set(got.members) == set(files)


def test_the_fallback_prefers_a_shared_stem_when_there_is_one():
    files = ["Game A.cue", "Game A (Track 01).bin", "Game A (Track 02).bin",
             "Game B.cue", "Game B (Track 01).bin"]
    got = pick_rom_set(files, PSX, read=None)
    assert all(m.startswith("Game A") for m in got.members), got.members


def test_a_read_that_raises_is_treated_as_unreadable_not_fatal():
    def boom(name):
        raise OSError("archive member unreadable")
    files = ["game.cue", "game.bin"]
    got = pick_rom_set(files, PSX, read=boom)
    assert "game.bin" in got.members


# --- sanity ----------------------------------------------------------------

def test_the_primary_is_always_a_member():
    for files, platform, read in [
        (["a.smc"], SNES, None),
        (["a.chd"], PSX, None),
        (["a.cue", "a.bin"], PSX, reader({"a.cue": 'FILE "a.bin" BINARY\n'})),
    ]:
        got = pick_rom_set(files, platform, read=read)
        assert got.primary in got.members


def test_members_never_include_an_extra():
    files = ["game.cue", "game.bin", "game.nfo", "cover.jpg", "game.sfv"]
    got = pick_rom_set(files, PSX, read=reader({"game.cue": 'FILE "game.bin" BINARY\n'}))
    assert set(got.members) == {"game.cue", "game.bin"}


def test_a_ps2_iso_is_a_set_of_one():
    got = pick_rom_set(["Shadow of the Colossus (USA).iso"], PS2)
    assert got.members == ("Shadow of the Colossus (USA).iso",)


def test_region_preference_still_decides_between_two_dumps():
    files = ["Game (Japan).chd", "Game (USA).chd"]
    got = pick_rom_set(files, PSX)
    assert got.primary == "Game (USA).chd"


def test_rom_set_is_frozen():
    got = pick_rom_set(["a.smc"], SNES)
    assert isinstance(got, RomSet)
    with pytest.raises(Exception):
        got.primary = "b.smc"
