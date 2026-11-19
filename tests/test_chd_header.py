"""A CHD's own header decides which chdman mode decompresses it.

`extractcd` on a DVD CHD fails, and before media kinds were read every CHD
was sent as `extractcd`, so a PS2 DVD or PSP library could never decompress.
These tests build the smallest valid headers by hand (MAME's CHD layout,
versions 4 and 5) and pin each tag to its mode, plus the ways a file can fail
to say: not a CHD, a looping chain, a chain that runs off the end.
"""

from __future__ import annotations

import struct

import pytest

from romarr import chd_header
from romarr.chd_header import chd_media, extract_mode_for_chd
from romarr.compressatorium import mode_for, mode_for_file


def _entry(tag: bytes, next_offset: int, data: bytes = b"x") -> bytes:
    """One metadata entry header: tag, flags+24-bit length, next offset."""
    return (tag + struct.pack(">I", (0x01 << 24) | len(data))
            + struct.pack(">Q", next_offset) + data)


def _chd(tags: list[bytes], version: int = 5, meta_offset: int | None = None) -> bytes:
    """A CHD header followed by a metadata chain holding `tags` in order."""
    header_size = {4: 108, 5: 124}[version]
    header = bytearray(header_size)
    header[0:8] = b"MComprHD"
    header[8:12] = struct.pack(">I", header_size)
    header[12:16] = struct.pack(">I", version)
    chain = bytearray()
    offset = header_size
    for index, tag in enumerate(tags):
        entry_size = 16 + 1
        nxt = offset + entry_size if index < len(tags) - 1 else 0
        chain += _entry(tag, nxt)
        offset += entry_size
    first = header_size if tags else 0
    if meta_offset is not None:
        first = meta_offset
    at = 48 if version == 5 else 36
    header[at:at + 8] = struct.pack(">Q", first)
    return bytes(header) + bytes(chain)


@pytest.mark.parametrize("tag, kind, mode", [
    (b"CHT2", chd_header.CD, "extractcd"),
    (b"CHTR", chd_header.CD, "extractcd"),
    (b"CHCD", chd_header.CD, "extractcd"),
    (b"CHGD", chd_header.GDROM, "extractcd"),
    (b"DVD ", chd_header.DVD, "extractdvd"),
    (b"GDDD", chd_header.HARD_DISK, "extracthd"),
    (b"AVAV", chd_header.LASERDISC, "extractld"),
])
def test_each_media_tag_picks_its_mode(tmp_path, tag, kind, mode):
    path = tmp_path / "game.chd"
    path.write_bytes(_chd([tag]))

    assert chd_media(path) == kind
    assert extract_mode_for_chd(path) == mode


def test_version_4_headers_are_read_too(tmp_path):
    path = tmp_path / "old.chd"
    path.write_bytes(_chd([b"DVD "], version=4))

    assert extract_mode_for_chd(path) == "extractdvd"


def test_a_descriptive_tag_before_the_media_tag_is_walked_past(tmp_path):
    """Hard-disk CHDs can carry an IDENT entry before their geometry."""
    path = tmp_path / "disk.chd"
    path.write_bytes(_chd([b"IDNT", b"GDDD"]))

    assert chd_media(path) == chd_header.HARD_DISK


def test_no_metadata_at_all_is_a_raw_chd(tmp_path):
    path = tmp_path / "raw.chd"
    path.write_bytes(_chd([]))

    assert chd_media(path) == chd_header.RAW
    assert extract_mode_for_chd(path) == "extractraw"


def test_a_file_that_is_not_a_chd_keeps_the_old_default(tmp_path):
    """Unreadable means "let the service say why", not "drop it here"."""
    path = tmp_path / "fake.chd"
    path.write_bytes(b"x")

    assert chd_media(path) == ""
    assert extract_mode_for_chd(path) == chd_header.DEFAULT_EXTRACT_MODE


def test_a_missing_file_is_not_an_exception(tmp_path):
    assert chd_media(tmp_path / "gone.chd") == ""


def test_a_chain_that_runs_off_the_end_tells_nothing(tmp_path):
    path = tmp_path / "short.chd"
    path.write_bytes(_chd([b"DVD "], meta_offset=10_000))

    assert chd_media(path) == ""


def test_a_looping_chain_stops(tmp_path):
    """A hostile chain pointing back at itself must not spin forever."""
    header = bytearray(_chd([]))
    header[48:56] = struct.pack(">Q", 124)
    loop = _entry(b"IDNT", 124)
    path = tmp_path / "loop.chd"
    path.write_bytes(bytes(header) + loop)

    assert chd_media(path) == ""


def test_unknown_version_tells_nothing(tmp_path):
    data = bytearray(_chd([b"DVD "]))
    data[12:16] = struct.pack(">I", 9)
    path = tmp_path / "future.chd"
    path.write_bytes(bytes(data))

    assert chd_media(path) == ""


def test_mode_for_file_reads_chd_headers_and_only_chd_headers(tmp_path):
    dvd = tmp_path / "Game (USA).chd"
    dvd.write_bytes(_chd([b"DVD "]))
    rvz = tmp_path / "Game (USA).rvz"
    rvz.write_bytes(b"not read")

    assert mode_for(dvd.name) == "extractcd", "the name alone cannot tell"
    assert mode_for_file(dvd) == "extractdvd"
    assert mode_for_file(rvz) == "dolphin_iso"
