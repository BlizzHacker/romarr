"""Reading the header of a ROM that is inside a zip.

A zipped ROM is the normal shipping form, and `.zip` is the most ambiguous
extension ROMarr has -- twenty platforms claim it, so the extension carries no
information at all. Against the library this was measured on, 3,319 of 3,347
NES files and 3,833 of 3,873 GBA files are zipped; without opening them the
header check confirmed 23% of files, and with it 76%, in the same wall time
and with no false positives either way.
"""

from __future__ import annotations

import zipfile

import pytest

from romarr.sniff import identify_archive, identify_file


def rom(size=0x400, **patches) -> bytes:
    blob = bytearray(b"\x00" * size)
    for offset, data in patches.items():
        at = int(offset)
        blob[at:at + len(data)] = data
    return bytes(blob)


def zipped(path, members: dict) -> object:
    with zipfile.ZipFile(path, "w") as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)
    return path


def test_a_zipped_rom_is_identified_by_its_contents(tmp_path):
    path = zipped(tmp_path / "Sonic (USA).zip",
                  {"Sonic (USA).bin": rom(**{str(0x100): b"SEGA GENESIS    "})})
    got = identify_file(path)
    assert got.platform == "genesis-slash-megadrive"
    assert "inside .zip" in got.detail


def test_the_zip_extension_alone_says_nothing(tmp_path):
    """Twenty platforms claim .zip. Only the member can settle it."""
    nes = zipped(tmp_path / "a.zip", {"game.nes": b"NES\x1a" + b"\x00" * 512})
    n64 = zipped(tmp_path / "b.zip", {"game.z64": rom(**{"0": b"\x80\x37\x12\x40"})})
    assert identify_file(nes).platform == "nes"
    assert identify_file(n64).platform == "n64"


def test_scene_junk_beside_the_rom_is_skipped(tmp_path):
    """Releases ship a .nfo and a .sfv next to the game."""
    path = zipped(tmp_path / "release.zip", {
        "file_id.diz": b"scene notes",
        "release.nfo": b"more notes",
        "Game (USA).nes": b"NES\x1a" + b"\x00" * 512,
    })
    assert identify_file(path).platform == "nes"


def test_an_archive_with_nothing_recognisable_gives_no_opinion(tmp_path):
    path = zipped(tmp_path / "quiet.zip", {"game.sfc": rom()})
    assert identify_file(path) is None


def test_an_empty_archive_is_not_an_error(tmp_path):
    assert identify_file(zipped(tmp_path / "empty.zip", {})) is None


def test_a_corrupt_archive_is_not_an_error(tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04 this is not really a zip")
    assert identify_file(path) is None


def test_a_directory_entry_does_not_confuse_it(tmp_path):
    path = tmp_path / "nested.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("roms/", b"")
        archive.writestr("roms/Game (USA).nes", b"NES\x1a" + b"\x00" * 512)
    assert identify_file(path).platform == "nes"


def test_only_zip_is_opened_here(tmp_path):
    """7z and rar need an external tool. Returning None is honest about what
    was actually checked rather than pretending to have looked."""
    path = tmp_path / "game.7z"
    path.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 256)
    assert identify_archive(path) is None
    assert identify_file(path) is None


def test_it_reads_one_member_not_the_whole_archive(tmp_path):
    """A scan must not turn into a decompression."""
    import time
    big = b"NES\x1a" + b"\x00" * (2 * 1024 * 1024)
    path = zipped(tmp_path / "many.zip",
                  {f"Game {n}.nes": big for n in range(20)})
    started = time.monotonic()
    assert identify_file(path).platform == "nes"
    assert time.monotonic() - started < 5
