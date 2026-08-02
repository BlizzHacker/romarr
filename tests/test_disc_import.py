"""Importing a disc: the whole set lands, or the import fails saying so.

The rule these tests exist for is one line long: a `.cue` must never arrive
in the library without its `.bin`. Everything else here is in service of it.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from romarr import library
from romarr.library import bsdtar_path, import_rom, list_candidates
from romarr.platforms import by_slug

PSX = by_slug("psx")
DC = by_slug("dc")
SNES = by_slug("snes")

needs_bsdtar = pytest.mark.skipif(
    bsdtar_path() is None,
    reason="bsdtar (libarchive) is not installed on this machine")


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def make_7z(path: Path, members: dict[str, bytes], tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    for name, data in members.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(
        [bsdtar_path(), "--format=7zip", "-cf", str(path), "-C", str(staging)]
        + list(members),
        check=True, capture_output=True)
    return path


CUE = b'FILE "Game (USA).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'


# --- the whole point -------------------------------------------------------

def test_a_cue_and_its_bin_both_land(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "game.zip",
             {"Game (USA).cue": CUE, "Game (USA).bin": b"\x00" * 4096,
              "readme.nfo": b"hi"})

    result = import_rom(downloads / "game.zip", PSX, lib)

    assert result.ok, result.reason
    landed = sorted(p.name for p in result.destination.rglob("*") if p.is_file())
    assert landed == ["Game (USA).bin", "Game (USA).cue"]


def test_a_multi_file_set_lands_as_a_directory(tmp_path):
    """The layout the live library already uses.

    `dc/102 Dalmatians - Puppies to the Rescue (NA, Rev 1.001)/` is a
    directory holding a .gdi and five track files. This produces the same
    shape, so a disc imported by ROMarr is indistinguishable from one filed
    by hand.
    """
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "d.zip", {
        "102 Dalmatians.gdi": b"3\n1 0 4 2352 track01.bin 0\n"
                              b"2 600 0 2352 track02.raw 0\n"
                              b"3 45000 4 2352 track03.bin 0\n",
        "track01.bin": b"a" * 512,
        "track02.raw": b"b" * 512,
        "track03.bin": b"c" * 512,
    })

    result = import_rom(downloads / "d.zip", DC, lib)

    assert result.ok, result.reason
    assert result.destination.is_dir()
    assert result.destination.parent == lib / "dc"
    assert sorted(p.name for p in result.destination.iterdir()) == [
        "102 Dalmatians.gdi", "track01.bin", "track02.raw", "track03.bin"]


def test_a_single_file_image_still_lands_as_a_file(tmp_path):
    """Nothing that worked before becomes a directory."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "s.zip", {"Silent Hill (USA).chd": b"\x00" * 8192})

    result = import_rom(downloads / "s.zip", PSX, lib)

    assert result.ok
    assert result.destination == lib / "psx" / "Silent Hill (USA).chd"
    assert result.destination.is_file()


def test_a_cartridge_import_is_byte_for_byte_what_it_was(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"Zelda.smc": b"rom", "readme.nfo": b"x"})

    result = import_rom(downloads / "g.zip", SNES, lib)

    assert result.ok
    assert result.destination == lib / "snes" / "Zelda.smc"
    assert result.destination.read_bytes() == b"rom"


# --- archives --------------------------------------------------------------

@needs_bsdtar
def test_a_7z_of_a_disc_imports(tmp_path):
    """The live disc library is overwhelmingly .7z -- 2,621 psx entries, and
    the importer could not open any of them."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_7z(downloads / "g.7z",
            {"Game (USA).cue": CUE, "Game (USA).bin": b"\x01" * 4096},
            tmp_path)

    result = import_rom(downloads / "g.7z", PSX, lib)

    assert result.ok, result.reason
    landed = {p.name: p.read_bytes() for p in result.destination.iterdir()}
    assert landed["Game (USA).cue"] == CUE
    assert landed["Game (USA).bin"] == b"\x01" * 4096


@needs_bsdtar
def test_list_candidates_reads_a_7z(tmp_path):
    make_7z(tmp_path / "a.7z", {"x.chd": b"1", "notes.nfo": b"2"}, tmp_path)
    assert sorted(list_candidates(tmp_path / "a.7z")) == ["notes.nfo", "x.chd"]


def test_an_archive_format_with_no_tool_fails_by_name(tmp_path, monkeypatch):
    """Never a bare "no ROM found". An operator who is missing a tool has to
    be told which tool, or the diagnosis is a guess about their download."""
    monkeypatch.setattr(library, "_bsdtar", lambda: None)
    library.bsdtar_path.cache_clear()
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    (downloads / "g.7z").write_bytes(b"not really a 7z")

    result = import_rom(downloads / "g.7z", PSX, lib)

    assert not result.ok
    assert "bsdtar" in result.reason
    library.bsdtar_path.cache_clear()


# --- safety ----------------------------------------------------------------

def test_zip_slip_is_refused(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "evil.zip",
             {"../../escape.chd": b"x", "Game (USA).chd": b"ok"})

    result = import_rom(downloads / "evil.zip", PSX, lib)

    assert result.ok
    assert result.destination.name == "Game (USA).chd"
    assert not (tmp_path / "escape.chd").exists()
    assert not (lib.parent / "escape.chd").exists()


def test_a_traversing_member_cannot_ride_along_as_a_sidecar(tmp_path):
    """The set is where traversal would be easiest to miss: the primary is
    checked, and the tracks arrive because a sheet named them."""
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "e.zip", {
        "Game.cue": b'FILE "../../escape.bin" BINARY\n',
        "Game.bin": b"legit",
    })

    result = import_rom(downloads / "e.zip", PSX, lib)

    assert result.ok, result.reason
    for p in result.destination.rglob("*"):
        assert ".." not in str(p)
    assert not (tmp_path / "escape.bin").exists()


def test_nothing_is_overwritten_silently(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "d.zip",
             {"Game.cue": CUE, "Game (USA).bin": b"new" * 100})
    first = import_rom(downloads / "d.zip", PSX, lib)
    assert first.ok

    again = import_rom(downloads / "d.zip", PSX, lib)
    assert not again.ok
    assert "already in the library" in again.reason

    forced = import_rom(downloads / "d.zip", PSX, lib, overwrite=True)
    assert forced.ok


def test_a_missing_download_still_explains_the_mount(tmp_path):
    result = import_rom(tmp_path / "nope.zip", PSX, tmp_path / "lib")
    assert not result.ok
    assert "does not exist in this container" in result.reason


def test_a_download_with_no_rom_says_so(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    make_zip(downloads / "g.zip", {"readme.nfo": b"x", "cover.png": b"y"})

    result = import_rom(downloads / "g.zip", PSX, lib)

    assert not result.ok
    assert "no Sony PlayStation ROM" in result.reason


# --- a real directory on disk, which is what a torrent client leaves -------

def test_a_finished_torrent_directory_imports_as_a_set(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    folder = downloads / "Game (USA)"
    folder.mkdir(parents=True)
    (folder / "Game (USA).cue").write_bytes(CUE)
    (folder / "Game (USA).bin").write_bytes(b"\x02" * 2048)
    (folder / "readme.nfo").write_bytes(b"x")

    result = import_rom(folder, PSX, lib)

    assert result.ok, result.reason
    assert sorted(p.name for p in result.destination.iterdir()) == [
        "Game (USA).bin", "Game (USA).cue"]
