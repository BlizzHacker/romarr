"""Disc images, which is what most of a 7z or rar in a ROM library contains.

Cartridge signatures alone had nothing to say about any of it. Measured on a
real library: twenty consecutive .7z archives, every one a disc image or an
emulator build, and every one "no opinion". With these, eleven are identified
and the remaining nine are archives that genuinely are not ROMs -- Dolphin
builds, a Minecraft pack -- where silence is the right answer.
"""

from __future__ import annotations

import pytest

from romarr.sniff import HEAD_BYTES, disagrees_with, identify


def disc(size=0x9000, **patches) -> bytes:
    blob = bytearray(b"\x00" * size)
    for offset, data in patches.items():
        at = int(offset)
        blob[at:at + len(data)] = data
    return bytes(blob)


# --- Nintendo optical ------------------------------------------------------

def test_gamecube_disc_magic():
    got = identify(disc(**{str(0x1C): b"\xc2\x33\x9f\x3d"}))
    assert got.platform == "ngc"


def test_wii_is_checked_before_gamecube():
    """A Wii disc carries both magics. Testing GameCube first would report
    every Wii disc as a GameCube one."""
    both = disc(**{str(0x18): b"\x5d\x1c\x9e\xa3",
                   str(0x1C): b"\xc2\x33\x9f\x3d"})
    assert identify(both).platform == "wii"


@pytest.mark.parametrize("magic,fmt", [
    (b"CISO", "CISO"), (b"RVZ\x01", "RVZ"),
    (b"WIA\x01", "WIA"), (b"\x01\xc0\x0b\xb1", "GCZ"),
])
def test_compressed_containers_are_named_but_not_over_claimed(magic, fmt):
    """The console lives in the payload, not the wrapper. Reporting the
    container is useful; pretending to know which machine is not."""
    got = identify(disc(**{"0": magic}))
    assert got.platform == "ngc"
    assert fmt in got.detail
    assert "GameCube or Wii" in got.detail


def test_a_compressed_container_never_accuses_wii_of_being_gamecube(tmp_path):
    path = tmp_path / "game.ciso"
    path.write_bytes(disc(**{"0": b"CISO"}))
    assert disagrees_with(path, "wii") is None


# --- Sega optical ----------------------------------------------------------

@pytest.mark.parametrize("offset", [0x00, 0x10])
def test_sega_cd_at_both_sector_layouts(offset):
    """0x00 for a 2048-byte user-data rip, 0x10 for a 2352-byte raw one."""
    assert identify(disc(**{str(offset): b"SEGADISCSYSTEM"})).platform == "segacd"


def test_sega_cd_alternative_boot_string():
    assert identify(disc(**{"0": b"SEGABOOTDISC"})).platform == "segacd"


def test_saturn_and_dreamcast():
    assert identify(disc(**{"0": b"SEGA SEGASATURN"})).platform == "saturn"
    assert identify(disc(**{str(0x10): b"SEGA SEGAKATANA"})).platform == "dc"


def test_3do_opera_header():
    assert identify(disc(**{"0": b"\x01\x5a\x5a\x5a\x5a\x5a"})).platform == "3do"


# --- Sony ------------------------------------------------------------------

def test_playstation_is_iso9660_plus_a_sony_identifier():
    got = identify(disc(**{str(0x8001): b"CD001",
                           str(0x8028): b"PLAYSTATION           "}))
    assert got.platform == "psx"
    assert "PlayStation or PS2" in got.detail


def test_psx_and_ps2_are_never_reported_against_each_other(tmp_path):
    """They share the identifier, so a PS2 image must not be called a
    mislabelled PSX one."""
    path = tmp_path / "game.iso"
    path.write_bytes(disc(**{str(0x8001): b"CD001",
                             str(0x8028): b"PLAYSTATION           "}))
    assert disagrees_with(path, "ps2") is None


def test_plain_iso9660_without_sony_is_not_claimed():
    """A data CD is not a PlayStation game."""
    assert identify(disc(**{str(0x8001): b"CD001"})) is None


def test_the_window_reaches_the_iso9660_descriptor():
    assert HEAD_BYTES > 0x8001
