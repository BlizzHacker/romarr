"""#225: never record an import that did not land in the library."""

from __future__ import annotations

from pathlib import Path

from romarr.app import confirm_in_library
from romarr.library import ImportResult


def test_a_destination_inside_the_library_stays_ok(tmp_path):
    lib = tmp_path / "library"
    (lib / "snes").mkdir(parents=True)
    rom = lib / "snes" / "Game.sfc"
    rom.write_bytes(b"x")
    out = confirm_in_library([ImportResult(True, rom)], lib)
    assert out[0].ok


def test_a_destination_in_the_download_folder_is_a_failure(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    downloads = tmp_path / "downloads" / "games"
    downloads.mkdir(parents=True)
    staged = downloads / "Funpak.gb"
    staged.write_bytes(b"x")
    out = confirm_in_library([ImportResult(True, staged)], lib)
    assert not out[0].ok and "outside the library" in out[0].reason


def test_a_missing_destination_is_a_failure(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    out = confirm_in_library([ImportResult(True, lib / "gone.sfc")], lib)
    assert not out[0].ok and "not found" in out[0].reason


def test_failures_pass_through_untouched(tmp_path):
    out = confirm_in_library([ImportResult(False, None, reason="bad dump")], tmp_path)
    assert out[0].reason == "bad dump"
