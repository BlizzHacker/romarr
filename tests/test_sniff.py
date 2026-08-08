"""Reading a ROM's header instead of trusting its name.

The case that motivates all of this is the one Yarr.It's own test suite names:
"a Mega Drive rom that somebody renamed .nes". Yarr.It has to read the header
because handing EmulatorJS the wrong core gives a black screen rather than an
error. ROMarr files the same files, and filing one under the wrong platform
surfaces much later as a game that will not boot.

The rule these enforce is that silence is allowed and guessing is not.
"""

from __future__ import annotations

import pytest

from romarr.sniff import HEAD_BYTES, disagrees_with, identify, identify_file

GB_LOGO = bytes.fromhex("CEED6666CC0D000B03730083")
GBA_LOGO = bytes.fromhex("24FFAE51699AA2213D84820A")


def rom(*, size=0x8000, **patches) -> bytes:
    blob = bytearray(b"\x00" * size)
    for offset, data in patches.items():
        at = int(offset)
        blob[at:at + len(data)] = data
    return bytes(blob)


# --- the signatures --------------------------------------------------------

def test_ines_header():
    assert identify(rom(**{"0": b"NES\x1a"})).platform == "nes"


@pytest.mark.parametrize("magic,order", [
    (b"\x80\x37\x12\x40", "big-endian"),
    (b"\x37\x80\x40\x12", "byte-swapped"),
    (b"\x40\x12\x37\x80", "little-endian"),
])
def test_n64_in_every_byte_order(magic, order):
    """The same word shuffled, which is exactly why .n64/.v64/.z64 get mixed
    up in the first place."""
    got = identify(rom(**{"0": magic}))
    assert got.platform == "n64"
    assert order in got.detail


def test_game_boy_and_colour_are_separated_by_the_cgb_flag():
    plain = identify(rom(**{str(0x104): GB_LOGO}))
    assert plain.platform == "gb"
    colour = identify(rom(**{str(0x104): GB_LOGO, str(0x143): b"\xc0"}))
    assert colour.platform == "gbc"


def test_gba_logo_is_not_confused_with_game_boy():
    assert identify(rom(**{str(0x04): GBA_LOGO})).platform == "gba"


def test_mega_drive_console_name():
    got = identify(rom(size=0x400, **{str(0x100): b"SEGA MEGA DRIVE "}))
    assert got.platform == "genesis-slash-megadrive"


@pytest.mark.parametrize("offset", [0x1FF0, 0x3FF0, 0x7FF0])
def test_master_system_signature_at_each_legal_offset(offset):
    got = identify(rom(**{str(offset): b"TMR SEGA"}))
    assert got.platform == "sms"
    # The offset depends on ROM size, not on the machine, so it cannot tell
    # Master System from Game Gear and the detail says so.
    assert "Game Gear" in got.detail


def test_atari_7800_and_lynx():
    assert identify(rom(**{"1": b"ATARI7800"})).platform == "atari7800"
    assert identify(rom(**{str(0x40): b"LYNX"})).platform == "lynx"


# --- silence rather than guessing ------------------------------------------

def test_an_unrecognised_rom_gets_no_opinion():
    assert identify(rom()) is None
    assert identify(b"") is None


def test_a_truncated_file_does_not_raise():
    assert identify(b"NE") is None


def test_a_missing_file_is_not_an_error(tmp_path):
    assert identify_file(tmp_path / "nope.nes") is None


# --- the case this exists for ----------------------------------------------

def test_a_mega_drive_rom_renamed_nes_is_caught(tmp_path):
    """Yarr.It's test names this exact scenario. ROMarr had no answer to it."""
    path = tmp_path / "Sonic (USA).nes"
    path.write_bytes(rom(size=0x400, **{str(0x100): b"SEGA GENESIS    "}))

    got = disagrees_with(path, "nes")

    assert got is not None
    assert got.platform == "genesis-slash-megadrive"


def test_a_correctly_named_rom_raises_no_objection(tmp_path):
    path = tmp_path / "Mario (USA).nes"
    path.write_bytes(rom(**{"0": b"NES\x1a"}))
    assert disagrees_with(path, "nes") is None


def test_an_unrecognised_rom_never_accuses(tmp_path):
    """Most of a real library is formats with no distinctive header. They
    must not all be reported as mislabelled."""
    path = tmp_path / "Something (USA).sfc"
    path.write_bytes(rom())
    assert disagrees_with(path, "snes") is None


@pytest.mark.parametrize("claimed,header_platform", [
    ("gamegear", "sms"),     # share TMR SEGA
    ("gbc", "gb"),           # share the logo
    ("famicom", "nes"),      # same machine
    # ("sfam", "snes") is absent: no reliable SNES signature is implemented,
    # so there is no header to write and the pairing cannot be exercised. The
    # rule stays in sniff.py for whenever one is added.
])
def test_families_this_cannot_separate_are_not_accused(tmp_path, claimed,
                                                       header_platform):
    path = tmp_path / "game.bin"
    if header_platform == "sms":
        path.write_bytes(rom(**{str(0x1FF0): b"TMR SEGA"}))
    elif header_platform == "gb":
        path.write_bytes(rom(**{str(0x104): GB_LOGO}))
    else:
        path.write_bytes(rom(**{"0": b"NES\x1a"}))
    assert disagrees_with(path, claimed) is None, (
        f"{claimed} was wrongly reported as {header_platform}")


def test_it_reads_a_window_not_the_whole_file(tmp_path):
    """A PS2 image is gigabytes. Reading it to check a header would make
    Manual Import unusable on a real library."""
    path = tmp_path / "big.iso"
    path.write_bytes(rom(**{"0": b"NES\x1a"}) + b"\x00" * (HEAD_BYTES * 4))
    assert HEAD_BYTES <= 0x8000
    assert identify_file(path).platform == "nes"


def test_no_platform_claimed_means_nothing_to_contradict(tmp_path):
    path = tmp_path / "game.nes"
    path.write_bytes(rom(**{"0": b"NES\x1a"}))
    assert disagrees_with(path, "") is None
