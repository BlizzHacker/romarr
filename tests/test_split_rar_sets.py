"""Split RAR releases import through 7-Zip or unar, one set at a time.

Disc releases often arrive as a RAR split into volumes: `name.part01.rar`,
the older `name.rar` + `name.r00`, or `name.001`. libarchive (bsdtar) reads
one volume at a time, so the importer used to open each `.rar` volume on
its own: a later RAR 4 volume listed the disc again, the first one ran out
of data partway through the disc, and a `.001` set was not seen as an
archive at all. These tests pin the grouping of volumes into sets, the
clear error when no RAR-capable tool is installed, and real imports through
each tool that is.

No free tool writes RAR files, so
`stored_rar_volumes` (RAR 4) and `stored_rar5_volumes` (RAR 5) build the
simplest valid shape by hand: one file, stored without compression, split
across volumes. 7-Zip and unar read both; bsdtar shows the original failure.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

from romarr import library
from romarr.import_intent import preview_import
from romarr.library import import_rom, rar_sets
from romarr.platforms import resolve

#: 9 MiB: the importer refuses a PS2 image under 8 MB. Three volumes of 4 MiB.
DATA = bytes(range(256)) * (9 * 4096)
VOLUME = 4 * 1024 * 1024


def _block(head_type: int, flags: int, body: bytes) -> bytes:
    """One RAR 4 block: CRC16 (low half of CRC32 from HEAD_TYPE on), type, flags, size."""
    head = struct.pack("<BHH", head_type, flags, 7 + len(body)) + body
    return struct.pack("<H", zlib.crc32(head) & 0xFFFF) + head


def stored_rar_volumes(name: str, data: bytes, volume_size: int,
                       part_names: bool = True) -> list[bytes]:
    """RAR 4 volumes holding `name` = `data`, stored, split every `volume_size` bytes.

    `part_names` sets the main-header flag saying the volumes are named
    `name.partN.rar`; without it RAR expects `name.rar`, `name.r00`, ...
    """
    chunks = [data[i:i + volume_size] for i in range(0, len(data), volume_size)]
    encoded = name.encode()
    volumes = []
    for index, chunk in enumerate(chunks):
        first, last = index == 0, index == len(chunks) - 1
        split = len(chunks) > 1
        main_flags = (0x0001 | (0x0010 if part_names else 0) | (0x0100 if first else 0)) if split else 0
        file_flags = 0x8000 | (0 if first else 0x0001) | (0 if last else 0x0002)
        # A part's CRC covers its own data, except the last, which covers the file.
        crc = zlib.crc32(data if last else chunk)
        body = struct.pack("<IIBIIBBHI", len(chunk), len(data), 3, crc, 0x5A210000,
                           29, 0x30, len(encoded), 0o100644 << 16) + encoded
        volumes.append(b"Rar!\x1a\x07\x00" + _block(0x73, main_flags, struct.pack("<HI", 0, 0))
                       + _block(0x74, file_flags, body) + chunk
                       + _block(0x7B, 0 if last else 0x0001, b""))
    return volumes


def _vint(number: int) -> bytes:
    """RAR 5's variable-length integer: 7 bits per byte, low bits first."""
    out = bytearray()
    while True:
        byte, number = number & 0x7F, number >> 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def _rar5_header(fields: bytes) -> bytes:
    """One RAR 5 header: CRC32 over size + fields, the size as a vint, the fields."""
    size = _vint(len(fields))
    return struct.pack("<I", zlib.crc32(size + fields)) + size + fields


def stored_rar5_volumes(name: str, data: bytes, volume_size: int) -> list[bytes]:
    """RAR 5 volumes holding `name` = `data`, stored, split every `volume_size` bytes.

    RAR 5 always names volumes `name.partN.rar`. Per volume: signature, main
    header (volume flag, and the volume number after the first), one file
    header with the continued-from / continues-in flags, the data, end header.
    """
    chunks = [data[i:i + volume_size] for i in range(0, len(data), volume_size)]
    split = len(chunks) > 1
    encoded = name.encode()
    volumes = []
    for index, chunk in enumerate(chunks):
        first, last = index == 0, index == len(chunks) - 1
        archive_flags = (0x0001 | (0 if first else 0x0002)) if split else 0
        main = _rar5_header(_vint(1) + _vint(0) + _vint(archive_flags)
                            + (b"" if first else _vint(index)))
        header_flags = 0x0002 | (0 if first else 0x0008) | (0 if last else 0x0010)
        # As in RAR 4, a part's CRC covers its own data and the last part's the file.
        crc = zlib.crc32(data if last else chunk)
        fields = (_vint(2) + _vint(header_flags) + _vint(len(chunk)) + _vint(0x0004)
                  + _vint(len(data)) + _vint(0o100644) + struct.pack("<I", crc)
                  + _vint(0) + _vint(1) + _vint(len(encoded)) + encoded)
        end = _rar5_header(_vint(5) + _vint(0) + _vint(0 if last else 0x0001))
        volumes.append(b"Rar!\x1a\x07\x01\x00" + main + _rar5_header(fields) + chunk + end)
    return volumes


def volume_names(stem: str, scheme: str, count: int) -> list[str]:
    """File names for `count` volumes in one of the three scene namings."""
    if scheme == "part":
        return [f"{stem}.part{n:02d}.rar" for n in range(1, count + 1)]
    if scheme == "old":
        return [f"{stem}.rar"] + [f"{stem}.r{n:02d}" for n in range(count - 1)]
    return [f"{stem}.{n:03d}" for n in range(1, count + 1)]


def write_set(folder: Path, scheme: str, member: str = "Game (USA).iso",
              keep: int | None = None, rar5: bool = False) -> list[Path]:
    """Write a split set into `folder`; `keep` drops the volumes after that many."""
    folder.mkdir(parents=True, exist_ok=True)
    volumes = (stored_rar5_volumes(member, DATA, VOLUME) if rar5
               else stored_rar_volumes(member, DATA, VOLUME, part_names=scheme == "part"))
    paths = []
    for name, payload in list(zip(volume_names("game-grp", scheme, len(volumes)), volumes))[:keep]:
        (folder / name).write_bytes(payload)
        paths.append(folder / name)
    return paths


@pytest.fixture(params=["7z", "unar"])
def tool(request, monkeypatch):
    """Force the importer onto one RAR tool, skipping when it is not installed."""
    found = {t.kind: t for t in library.rar_tools()}.get(request.param)
    if found is None:
        pytest.skip({"7z": "7-Zip with RAR support is not installed",
                     "unar": "unar is not installed"}[request.param])
    monkeypatch.setattr(library, "rar_tool", lambda: found)
    return found


@pytest.mark.parametrize("scheme", ["part", "old", "num"])
def test_rar_sets_groups_each_naming(tmp_path, scheme):
    """Each naming groups into one set led by its first volume."""
    paths = write_set(tmp_path, scheme)
    sets, later = rar_sets(sorted(tmp_path.iterdir()))
    assert sets == {paths[0]: paths}
    assert later == set(paths[1:])


def test_rar_sets_leaves_gaps_and_non_rar_numbers_alone(tmp_path):
    """A set missing a middle volume, or a .001 that is not a RAR, is not a set."""
    gap = write_set(tmp_path / "gap", "part")
    gap[1].unlink()
    assert rar_sets(sorted((tmp_path / "gap").iterdir())) == ({}, set())
    split_iso = tmp_path / "iso"
    split_iso.mkdir()
    for n in (1, 2):
        (split_iso / f"disc.iso.{n:03d}").write_bytes(b"CD001" + bytes(100))
    assert rar_sets(sorted(split_iso.iterdir())) == ({}, set())


def test_lone_rar_is_its_own_set(tmp_path):
    """A single .rar is a set of one, so it still opens exactly as before."""
    lone = tmp_path / "game.rar"
    lone.write_bytes(stored_rar_volumes("Game.iso", b"x" * 10, 100)[0])
    assert rar_sets([lone]) == ({lone: [lone]}, set())


def test_split_set_without_a_rar_tool_says_how_to_fix_it(tmp_path, monkeypatch):
    """With only bsdtar, a split set fails with the fix, not 'cannot inspect'."""
    monkeypatch.setattr(library, "rar_tool", lambda: None)
    write_set(tmp_path / "download", "part")
    (tmp_path / "download" / "game-grp.nfo").write_text("nfo")
    result, = import_rom(tmp_path / "download", resolve("ps2"), tmp_path / "library")
    assert not result.ok
    assert "split RAR" in result.reason and "unar" in result.reason
    assert "cannot inspect game-grp.part02.rar" not in result.reason


#: `7z i` lines, as printed by Debian's `7zip` (no `7zip-rar`) and by the official build.
_RAR_FORMAT = " 0  ...F..................  Rar      rar r00       R a r ! 1A 07 00"
_RAR_CODECS = ["     D     40301 Rar1", "     D     40302 Rar2",
               "     D     40303 Rar3", "     D     40305 Rar5"]


@pytest.mark.parametrize("lines,reads", [
    (["Formats:", "  ...  7z  7z"], False),            # Alpine: built without RAR
    (["Formats:", _RAR_FORMAT, "Codecs:"], False),     # Debian: format listed, no decoders
    (["Formats:", _RAR_FORMAT, "Codecs:", *_RAR_CODECS], True),
])
def test_7zip_without_rar_decoders_is_not_used(tmp_path, lines, reads):
    """Only a 7-Zip that lists the RAR decoders counts as a RAR reader."""
    if os.name == "nt":
        pytest.skip("uses a POSIX shell script as the fake 7-Zip")
    fake = tmp_path / "7zz"
    fake.write_text("#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in lines))
    fake.chmod(0o755)
    assert library._reads_rar(str(fake)) is reads


@pytest.mark.parametrize("scheme,rar5", [
    ("part", False), ("old", False), ("num", False), ("part", True), ("num", True)])
def test_split_set_imports_the_whole_disc(tmp_path, tool, scheme, rar5):
    """Every naming, RAR 4 and RAR 5, imports the joined image; later volumes are not offered."""
    download = tmp_path / "download"
    paths = write_set(download, scheme, rar5=rar5)
    (download / "game-grp.nfo").write_text("nfo")
    preview = preview_import(download, resolve("ps2"), tmp_path / "library")
    primaries = [item["primary"] for item in preview["items"]]
    assert primaries == [paths[0].name + "/Game (USA).iso"]
    result, = import_rom(download, resolve("ps2"), tmp_path / "library")
    assert result.ok, result.reason
    assert result.destination.read_bytes() == DATA
    assert result.import_method == "extracted"
    assert sorted(p.name for p in download.iterdir()) == sorted(
        [p.name for p in paths] + ["game-grp.nfo"])


def test_missing_last_volume_fails_instead_of_importing_a_short_image(tmp_path, tool):
    """A set missing its last volume fails with either tool, never imports short.

    unar exits 0 in this case, so the size check is what stops it there.
    """
    download = tmp_path / "download"
    write_set(download, "part", keep=2)
    result, = import_rom(download, resolve("ps2"), tmp_path / "library")
    assert not result.ok
    assert not any((tmp_path / "library").rglob("*.iso"))


def test_bracketed_member_name_is_matched_literally(tmp_path, tool):
    """Scene names full of brackets reach the tool as literal names."""
    download = tmp_path / "download"
    write_set(download, "part", member="Game [v1] [US].iso")
    result, = import_rom(download, resolve("ps2"), tmp_path / "library")
    assert result.ok, result.reason
    assert result.destination.read_bytes() == DATA


def test_single_rar_download_uses_the_rar_tool(tmp_path, tool):
    """A download that is one .rar file goes to the RAR tool when present."""
    archive = tmp_path / "game.rar"
    archive.write_bytes(stored_rar_volumes("Game.iso", DATA, len(DATA))[0])
    source = library._source_for(archive, resolve("ps2"))
    try:
        assert isinstance(source, library._RarToolSource)
        assert source.names() == ["Game.iso"]
        assert source.read("Game.iso") == DATA
    finally:
        source.close()


def test_unar_links_for_a_numbered_set_are_removed_on_close(tmp_path):
    """The temporary RAR-named links unar needs for `.001` sets go away with the source."""
    if os.name == "nt":
        pytest.skip("symlinks need extra rights on Windows")
    paths = write_set(tmp_path, "num")
    source = library._RarToolSource(paths[0], library.RarTool("unar", "unar", "lsar"), paths)
    staged = Path(source.archive).parent
    assert source.archive.name == "set.rar"
    assert sorted(p.name for p in staged.iterdir()) == ["set.r00", "set.r01", "set.rar"]
    source.close()
    assert not staged.exists()


def test_a_volume_linked_outside_the_download_is_refused_before_reading(tmp_path, monkeypatch):
    """A later volume that is a symlink out of the download stops the import before any tool runs."""
    if os.name == "nt":
        pytest.skip("symlinks need extra rights on Windows")
    outside = tmp_path / "outside.part02.rar"
    paths = write_set(tmp_path / "download", "part")
    outside.write_bytes(paths[1].read_bytes())
    paths[1].unlink()
    paths[1].symlink_to(outside)
    opened = []
    monkeypatch.setattr(library, "rar_sets", lambda files: opened.append(files) or ({}, set()))
    with pytest.raises(ValueError, match="escapes download directory"):
        library._DirectorySource(tmp_path / "download")
    assert opened == []


def test_a_one_part_numbered_set_opens_with_either_tool(tmp_path, tool):
    """A lone RAR named `.001` is a set of one and imports with either tool."""
    folder = tmp_path / "download"
    folder.mkdir()
    lone = folder / "game-grp.001"
    lone.write_bytes(stored_rar_volumes("Game (USA).iso", DATA, len(DATA))[0])
    result, = import_rom(folder, resolve("ps2"), tmp_path / "library")
    assert result.ok, result.reason
    assert result.destination.read_bytes() == DATA


def test_a_one_part_numbered_file_opens_directly_with_either_tool(tmp_path, tool):
    """Passing a single `.001` RAR file directly opens it as an archive, not a raw file."""
    lone = tmp_path / "game-grp.001"
    lone.write_bytes(stored_rar_volumes("Game (USA).iso", DATA, len(DATA))[0])
    result, = import_rom(lone, resolve("ps2"), tmp_path / "library")
    assert result.ok, result.reason
    assert result.destination.read_bytes() == DATA
