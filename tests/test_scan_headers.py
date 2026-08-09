"""Detection when the extension is shared, and when the name simply lies.

`.bin` is claimed by twenty of ROMarr's platforms and `.rom` by five, so
picking the first-listed claimant is barely better than a coin toss. The header
usually knows, and reading it turns a guess into a fact.

Two separate behaviours, and the difference is deliberate:

  * the header names a platform that *does* claim this extension -> correct it,
    because the extension was never evidence for one claimant over another;
  * the header names a platform that does *not* claim it -> flag it and leave
    the guess alone, because now the filename is wrong and overriding on that
    basis would be ROMarr deciding it knows better than the operator.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from romarr.app import ROMarr


def rom(size=0x400, **patches) -> bytes:
    blob = bytearray(b"\x00" * size)
    for offset, data in patches.items():
        at = int(offset)
        blob[at:at + len(data)] = data
    return bytes(blob)


@pytest.fixture
def scan(tmp_path):
    src = tmp_path / "incoming"
    src.mkdir()
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})

    def run(name: str, blob: bytes) -> dict:
        (src / name).write_bytes(blob)
        rows = service.scan(str(src))["candidates"]
        return next(r for r in rows if r["filename"] == name)

    return run


# --- a shared extension the header can settle ------------------------------

def test_the_header_confirms_a_lucky_guess(scan):
    """Mega Drive is the first claimant of .bin, so this was already right --
    but only by ordering. Saying what confirmed it removes a confirmation
    prompt the operator no longer needs."""
    row = scan("Sonic (USA).bin", rom(**{str(0x100): b"SEGA GENESIS    "}))
    assert row["platform"] == "genesis-slash-megadrive"
    assert row["header_chose"] == "genesis-slash-megadrive"
    assert "confirmed by the header" in row["reason"]


def test_a_shared_extension_is_corrected_to_what_the_bytes_say(scan):
    """.nes is claimed by nes and famicom; an iNES header settles it without
    the operator confirming anything."""
    row = scan("Mario (USA).nes", b"NES\x1a" + b"\x00" * 2048)
    assert row["platform"] in ("nes", "famicom")
    assert row["header_chose"]


# --- a name that is simply wrong -------------------------------------------

def test_a_mega_drive_rom_named_nes_is_flagged_not_silently_moved(scan):
    row = scan("Liar (USA).nes", rom(**{str(0x100): b"SEGA MEGA DRIVE "}))
    assert row["platform"] == "nes", "the guess must not be overridden"
    assert row["header_says"] == "genesis-slash-megadrive"
    assert row["header_detail"]


def test_a_header_for_a_platform_that_does_not_claim_the_extension(scan):
    """.bin is not an Atari 7800 extension, so this is a wrong name rather
    than an ambiguous one -- flagged, and the guess left for the operator."""
    row = scan("Centipede (USA).bin", rom(**{"1": b"ATARI7800"}))
    assert row["header_says"] == "atari7800"
    assert row["platform"] != "atari7800"


# --- silence where silence is right ----------------------------------------

def test_a_file_with_no_recognisable_header_is_left_alone(scan):
    """Most of a real library. It must not all be reported as suspect."""
    row = scan("Mystery.rom", rom())
    assert "header_says" not in row
    assert "header_chose" not in row
    assert "confirm before importing" in row["reason"]


def test_families_the_header_cannot_separate_are_not_flagged(scan):
    """gb and gbc share the Nintendo logo; a .gbc must not be accused of
    being a .gb."""
    logo = bytes.fromhex("CEED6666CC0D000B03730083")
    row = scan("Game (USA).gbc", rom(size=0x200, **{str(0x104): logo}))
    assert "header_says" not in row


def test_reading_a_header_does_not_slow_a_scan_to_a_crawl(scan, tmp_path):
    """Only the opening window is read, so a directory of large images stays
    usable."""
    import time
    src = tmp_path / "incoming"
    big = rom(**{"0": b"NES\x1a"}) + b"\x00" * (4 * 1024 * 1024)
    started = time.monotonic()
    for n in range(12):
        scan(f"Big {n}.nes", big)
    assert time.monotonic() - started < 20
