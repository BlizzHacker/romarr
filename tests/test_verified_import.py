"""What landed, checked against what should have landed.

The import used to end at "the bytes are in the library". With a DAT loaded
it ends at "the bytes in the library are the published dump", which is a
different and much stronger claim -- and the one thing this class of tool has
never been able to make.
"""

from __future__ import annotations

import zlib
import zipfile
from pathlib import Path

import pytest

from romarr.dat import BAD_DUMP, DatIndex, UNKNOWN, VERIFIED, parse_dat
from romarr.library import import_rom
from romarr.platforms import by_slug

SNES = by_slug("snes")
PSX = by_slug("psx")

ROM = b"\x5A" * 65536
CRC = f"{zlib.crc32(ROM) & 0xFFFFFFFF:08x}"

DAT = f"""<?xml version="1.0"?><datafile>
  <header><name>Nintendo - SNES</name></header>
  <game name="Super Metroid (USA)">
    <rom name="Super Metroid (USA).sfc" size="{len(ROM)}" crc="{CRC.upper()}"/>
  </game>
</datafile>"""


@pytest.fixture
def dats():
    index = DatIndex()
    index.add(parse_dat(DAT))
    return index


def make_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


# --- the claim -------------------------------------------------------------

def test_a_correct_rom_is_reported_verified(tmp_path, dats):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Super Metroid (USA).sfc": ROM})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=dats)

    assert result.ok
    assert result.verification.status == VERIFIED
    assert result.verification.game == "Super Metroid (USA)"


def test_a_corrupt_rom_is_reported_as_a_bad_dump(tmp_path, dats):
    """Same size, different bytes. Every other *arr imports this and tells you
    it succeeded, because filename scoring cannot see inside a file."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    corrupt = b"\x00" + ROM[1:]
    make_zip(downloads / "g.zip", {"Super Metroid (USA).sfc": corrupt})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=dats)

    assert result.verification.status == BAD_DUMP
    assert "checksum" in result.verification.detail


def test_an_unrecognised_rom_is_unknown_and_still_imports(tmp_path, dats):
    """Homebrew, translations and anything newer than the DAT. Refusing these
    would make loading a DAT a downgrade."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Homebrew Demo.sfc": b"\x11" * 4096})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=dats)

    assert result.ok, result.reason
    assert result.verification.status == UNKNOWN


def test_a_bad_dump_still_imports_by_default(tmp_path, dats):
    """Reported, not refused. The operator may have grabbed the only copy that
    exists, and a tool that silently discards it has made that decision for
    them -- the verdict is on the result either way."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Super Metroid (USA).sfc": b"\x00" + ROM[1:]})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=dats)

    assert result.ok
    assert result.destination.exists()


def test_a_bad_dump_can_be_refused_when_asked(tmp_path, dats):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Super Metroid (USA).sfc": b"\x00" + ROM[1:]})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=dats,
                        require_verified=True)

    assert not result.ok
    assert "checksum" in result.reason
    assert not (lib / "snes").exists(), "nothing may be left behind"


# --- headers, through the whole pipeline -----------------------------------

def test_a_headered_import_verifies(tmp_path, dats):
    """The end-to-end version of the copier-header trap. Without the skip,
    every SNES import ever made would report "unknown"."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip",
             {"Super Metroid (USA).smc": b"\x00" * 512 + ROM})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=dats)

    assert result.verification.status == VERIFIED


# --- discs verify per track ------------------------------------------------

def test_a_disc_set_verifies_every_track(tmp_path):
    """Redump lists each track as its own <rom>, so a cue+bin set is verified
    member by member. One good track and one corrupt one is not a good
    import."""
    cue = b'FILE "Game (USA).bin" BINARY\n'
    track = b"\x7E" * 40960
    index = DatIndex()
    index.add(parse_dat(f"""<?xml version="1.0"?><datafile>
      <header><name>Sony - PlayStation</name></header>
      <game name="Game (USA)">
        <rom name="Game (USA).cue" size="{len(cue)}"
             crc="{zlib.crc32(cue) & 0xFFFFFFFF:08x}"/>
        <rom name="Game (USA).bin" size="{len(track)}"
             crc="{zlib.crc32(track) & 0xFFFFFFFF:08x}"/>
      </game></datafile>"""))

    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "d.zip",
             {"Game (USA).cue": cue, "Game (USA).bin": track})

    result = import_rom(downloads / "d.zip", PSX, lib, dats=index)

    assert result.ok, result.reason
    assert result.verification.status == VERIFIED
    assert result.verification.game == "Game (USA)"


def test_one_corrupt_track_fails_the_whole_set(tmp_path):
    cue = b'FILE "Game (USA).bin" BINARY\n'
    track = b"\x7E" * 40960
    index = DatIndex()
    index.add(parse_dat(f"""<?xml version="1.0"?><datafile>
      <header><name>Sony - PlayStation</name></header>
      <game name="Game (USA)">
        <rom name="Game (USA).cue" size="{len(cue)}"
             crc="{zlib.crc32(cue) & 0xFFFFFFFF:08x}"/>
        <rom name="Game (USA).bin" size="{len(track)}"
             crc="{zlib.crc32(track) & 0xFFFFFFFF:08x}"/>
      </game></datafile>"""))

    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "d.zip",
             {"Game (USA).cue": cue, "Game (USA).bin": b"\x00" + track[1:]})

    result = import_rom(downloads / "d.zip", PSX, lib, dats=index)

    assert result.verification.status != VERIFIED


# --- nothing changes for an operator with no DATs -------------------------

def test_without_dats_the_import_behaves_exactly_as_before(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Super Metroid (USA).sfc": ROM})

    result = import_rom(downloads / "g.zip", SNES, lib)

    assert result.ok
    assert result.destination == lib / "snes" / "Super Metroid (USA).sfc"
    assert result.verification.status == UNKNOWN


def test_an_empty_index_does_not_turn_imports_into_errors(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Super Metroid (USA).sfc": ROM})

    result = import_rom(downloads / "g.zip", SNES, lib, dats=DatIndex())

    assert result.ok
    assert result.verification.status == UNKNOWN
