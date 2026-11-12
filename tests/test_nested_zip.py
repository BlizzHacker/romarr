"""Regression coverage for bounded ZIP-inside-ZIP import and review."""

import io
import os
import struct
from pathlib import Path
import stat
import zipfile
import zlib

import pytest

from romarr import library, nested_zip
from romarr.import_intent import preview_import
from romarr.library import _DirectorySource, _PathSource, _source_for, import_rom
from romarr.platforms import resolve
from romarr.selection import _digital_set


def zip_bytes(members):
    """Build an in-memory ZIP so tests never need downloaded game data."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_nested_zip_preview_and_import_preserve_original(tmp_path):
    """Import an inner ZIP's ROM without altering the seeded outer archive."""
    original = zip_bytes({"inside.zip": zip_bytes({"Game.gb": b"game bytes"})})
    download = tmp_path / "download.zip"
    download.write_bytes(original)
    library = tmp_path / "library"

    preview = preview_import(download, resolve("gb"), library)
    assert not preview["requires_review"]
    assert preview["items"][0]["primary"] == "inside.zip/Game.gb"
    assert preview["items"][0]["bytes"] == len(b"game bytes")

    result, = import_rom(download, resolve("gb"), library,
                         selected_primaries=["inside.zip/Game.gb"])
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"game bytes"
    assert download.read_bytes() == original
    assert result.import_method == "extracted"


def test_nested_zip_gameboy_ignores_executable_and_imports_rom(tmp_path):
    """Select a Game Boy ROM, never an executable stored beside it."""
    game = b"game bytes"
    original = zip_bytes({"inner.zip": zip_bytes({
        "Game.gb": game,
        "setup.exe": b"not a Game Boy ROM",
    })})
    download = tmp_path / "download.zip"
    download.write_bytes(original)
    library = tmp_path / "library"

    preview = preview_import(download, resolve("gb"), library)
    assert [item["primary"] for item in preview["items"]] == ["inner.zip/Game.gb"]
    assert preview["extras"] == ["inner.zip/setup.exe"]

    result, = import_rom(download, resolve("gb"), library,
                         selected_primaries=["inner.zip/Game.gb"])
    assert result.ok, result.reason
    assert result.destination.name == "Game.gb"
    assert result.destination.read_bytes() == game
    assert not any(path.suffix.lower() == ".exe" for path in library.rglob("*") if path.is_file())
    assert download.read_bytes() == original


def test_nested_zip_gameboy_rejects_executable_only(tmp_path):
    """No approved Game Boy extension means no preview choice or import."""
    original = zip_bytes({"inner.zip": zip_bytes({
        "setup.exe": b"not a Game Boy ROM",
    })})
    download = tmp_path / "download.zip"
    download.write_bytes(original)
    library = tmp_path / "library"

    preview = preview_import(download, resolve("gb"), library)
    assert preview["items"] == []
    assert preview["extras"] == ["inner.zip/setup.exe"]

    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert result.destination is None
    assert "no Game Boy ROM" in result.reason
    assert not library.exists()
    assert download.read_bytes() == original


def test_directory_multiple_nested_zip_trees_have_one_staging_limit(tmp_path, monkeypatch):
    """Reject two individually legal inner ZIPs exceeding one directory budget."""
    inner_a = zip_bytes({"A.gb": b"game"})
    inner_b = zip_bytes({"B.gb": b"game"})
    limit = max(len(inner_a), len(inner_b)) + 1
    assert len(inner_a) + len(inner_b) > limit
    monkeypatch.setattr(nested_zip, "MAX_STAGED_BYTES", limit)
    download = tmp_path / "download"
    download.mkdir()
    a = zip_bytes({"inner.zip": inner_a})
    b = zip_bytes({"inner.zip": inner_b})
    (download / "A.zip").write_bytes(a)
    (download / "B.zip").write_bytes(b)
    library = tmp_path / "library"

    with pytest.raises(ValueError, match="download staging size limit exceeded"):
        preview_import(download, resolve("gb"), library)
    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "download staging size limit exceeded" in result.reason
    assert not library.exists()
    assert (download / "A.zip").read_bytes() == a
    assert (download / "B.zip").read_bytes() == b


def test_directory_nested_staging_closes_on_success_and_failure(tmp_path, monkeypatch):
    """Keep good small multi-ZIP imports and clean temporary ZIPs on all exits."""
    original_temp = nested_zip.tempfile.TemporaryDirectory
    scratch = []

    def tracked_temp(*args, **kwargs):
        temp = original_temp(*args, **kwargs)
        if kwargs.get("prefix") == "romarr-nested-zip-":
            scratch.append(temp)
        return temp

    monkeypatch.setattr(nested_zip.tempfile, "TemporaryDirectory", tracked_temp)
    inner_a = zip_bytes({"A.gb": b"a"})
    inner_b = zip_bytes({"B.gb": b"b"})
    monkeypatch.setattr(nested_zip, "MAX_STAGED_BYTES", len(inner_a) + len(inner_b) + 1)
    download = tmp_path / "download"
    download.mkdir()
    (download / "A.zip").write_bytes(zip_bytes({"inner.zip": inner_a}))
    (download / "B.zip").write_bytes(zip_bytes({"inner.zip": inner_b}))
    library = tmp_path / "library"

    preview = preview_import(download, resolve("gb"), library)
    assert {item["primary"] for item in preview["items"]} == {
        "A.zip/inner.zip/A.gb", "B.zip/inner.zip/B.gb"}
    assert scratch and all(not Path(temp.name).exists() for temp in scratch)
    result, = import_rom(download, resolve("gb"), library,
                         selected_primaries=["B.zip/inner.zip/B.gb"])
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"b"
    assert scratch and all(not Path(temp.name).exists() for temp in scratch)

    monkeypatch.setattr(nested_zip, "MAX_STAGED_BYTES", max(len(inner_a), len(inner_b)) + 1)
    result, = import_rom(download, resolve("gb"), tmp_path / "failed")
    assert not result.ok
    assert "download staging size limit exceeded" in result.reason
    assert all(not Path(temp.name).exists() for temp in scratch)
    assert not (tmp_path / "failed").exists()


def test_directory_unexpected_inspection_exception_closes_scratch(tmp_path, monkeypatch):
    """An unexpected decompression error must not retain staged inner archives."""
    original_temp = nested_zip.tempfile.TemporaryDirectory
    owned = []

    def tracked_temp(*args, **kwargs):
        temp = original_temp(*args, **kwargs)
        if kwargs.get("prefix") == "romarr-nested-zip-":
            owned.append(temp)
        return temp

    monkeypatch.setattr(nested_zip.tempfile, "TemporaryDirectory", tracked_temp)
    download = tmp_path / "download"
    download.mkdir()
    (download / "A.zip").write_bytes(zip_bytes({
        "inner.zip": zip_bytes({"A.gb": b"a"}),
    }))
    # Prevent the one-archive directory shortcut from bypassing _DirectorySource.
    (download / "notes.txt").write_bytes(b"fixture companion")
    library = tmp_path / "library"

    def fail_during_inspection(_source, _platform):
        raise zlib.error("synthetic compressed member failure")

    monkeypatch.setattr(_DirectorySource, "rom_sets", fail_during_inspection)
    with pytest.raises(zlib.error, match="synthetic compressed member failure"):
        import_rom(download, resolve("gb"), library)

    assert owned and all(not Path(temp.name).exists() for temp in owned)
    assert not library.exists()


def test_nested_zip_disc_in_directory_preserves_tracks_and_art(tmp_path):
    """Retain a disc's CUE/BIN companions when ZIPs are nested in a folder."""
    download = tmp_path / "download"
    download.mkdir()
    outer = download / "outer.zip"
    original = zip_bytes({"inner.zip": zip_bytes({
        "Game.cue": b'FILE "track.bin" BINARY\n',
        "track.bin": b"track payload",
    })})
    outer.write_bytes(original)
    (download / "art.jpg").write_bytes(b"art")

    primary = "outer.zip/inner.zip/Game.cue"
    preview = preview_import(download, resolve("dc"), tmp_path / "library")
    assert [item["primary"] for item in preview["items"]] == [primary]
    result, = import_rom(download, resolve("dc"), tmp_path / "library",
                         selected_primaries=[primary])
    assert result.ok, result.reason
    assert (result.destination / "Game.cue").read_bytes() == b'FILE "track.bin" BINARY\n'
    assert (result.destination / "track.bin").read_bytes() == b"track payload"
    assert outer.read_bytes() == original
    assert (download / "art.jpg").read_bytes() == b"art"


def test_two_inner_zip_sets_remain_separate_under_review(tmp_path):
    """Selecting one disc must not pick the other ZIP's identically named track."""
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({
        "A.zip": zip_bytes({"Game.cue": b'FILE "track.bin" BINARY\n', "track.bin": b"a"}),
        "B.zip": zip_bytes({"Game.cue": b'FILE "track.bin" BINARY\n', "track.bin": b"b"}),
    }))
    preview = preview_import(download, resolve("dc"), tmp_path / "library")
    assert preview["requires_review"]
    assert {item["primary"] for item in preview["items"]} == {
        "A.zip/Game.cue", "B.zip/Game.cue"}
    result, = import_rom(download, resolve("dc"), tmp_path / "library",
                         selected_primaries=["B.zip/Game.cue"])
    assert result.ok, result.reason
    assert (result.destination / "track.bin").read_bytes() == b"b"


@pytest.mark.parametrize("violation", ["depth", "corruption", "count", "size", "symlink"])
def test_nested_zip_fails_before_library_publication(tmp_path, monkeypatch, violation):
    """Reject unsafe or excessive archive trees without publishing any ROM."""
    inner = zip_bytes({"Game.gb": b"good"})
    if violation == "depth":
        for number in range(nested_zip.MAX_ZIP_LAYERS):
            inner = zip_bytes({f"layer{number}.zip": inner})
    elif violation == "corruption":
        inner = b"invalid zip content"
    elif violation == "count":
        monkeypatch.setattr(nested_zip, "MAX_ZIP_MEMBERS", 1)
    elif violation == "size":
        monkeypatch.setattr(nested_zip, "MAX_INNER_BYTES", 1)
    elif violation == "symlink":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("Game.gb")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"bad-link")
        inner = buffer.getvalue()
    if violation != "depth":
        inner = zip_bytes({"inner.zip": inner})
    download = tmp_path / "outer.zip"
    download.write_bytes(inner)
    library = tmp_path / "library"

    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "source inspection failed" in result.reason
    assert not library.exists()


def test_duplicate_inner_zip_container_names_fail_closed(tmp_path):
    """Two containers with the same name must not merge unrelated ROM sources."""
    download = tmp_path / "outer.zip"
    original_inner = zip_bytes({"Game.gb": b"game"})
    with zipfile.ZipFile(download, "w") as outer:
        outer.writestr("inner.zip", original_inner)
        with pytest.warns(UserWarning, match="Duplicate name"):
            outer.writestr("inner.zip", zip_bytes({"Other.gb": b"other"}))
    library = tmp_path / "library"

    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "duplicate nested ZIP member" in result.reason
    assert not library.exists()


@pytest.mark.parametrize("directory_download", [False, True])
def test_missing_nested_playlist_disc_never_publishes(tmp_path, directory_download):
    """A nested playlist lacking any referenced disc is not a valid game."""
    inner = zip_bytes({
        "Game.m3u": b"Disc One.chd\nDisc Two.chd\n",
        "Disc One.chd": b"one",
    })
    outer = zip_bytes({"inner.zip": inner})
    library = tmp_path / "library"
    if directory_download:
        download = tmp_path / "download"
        download.mkdir()
        (download / "outer.zip").write_bytes(outer)
        (download / "art.jpg").write_bytes(b"art")
    else:
        download = tmp_path / "outer.zip"
        download.write_bytes(outer)

    preview = preview_import(download, resolve("psx"), library)
    assert not preview["items"]
    result, = import_rom(download, resolve("psx"), library)
    assert not result.ok
    assert "no " in result.reason
    assert not library.exists()


@pytest.mark.parametrize("directory_download", [False, True])
def test_complete_nested_playlist_keeps_both_discs(tmp_path, directory_download):
    """A complete same-archive playlist remains selectable and playable."""
    inner = zip_bytes({
        "Game.m3u": b"Disc One.chd\nDisc Two.chd\n",
        "Disc One.chd": b"disc one",
        "Disc Two.chd": b"disc two",
    })
    outer = zip_bytes({"inner.zip": inner})
    library = tmp_path / "library"
    if directory_download:
        download = tmp_path / "download"
        download.mkdir()
        (download / "outer.zip").write_bytes(outer)
        (download / "art.jpg").write_bytes(b"art")
        primary = "outer.zip/inner.zip/Game.m3u"
    else:
        download = tmp_path / "outer.zip"
        download.write_bytes(outer)
        primary = "inner.zip/Game.m3u"

    preview = preview_import(download, resolve("psx"), library)
    assert [item["primary"] for item in preview["items"]] == [primary]
    result, = import_rom(download, resolve("psx"), library,
                         selected_primaries=[primary])
    assert result.ok, result.reason
    assert (result.destination / "Game.m3u").read_bytes() == b"Disc One.chd\nDisc Two.chd\n"
    assert (result.destination / "Disc One.chd").read_bytes() == b"disc one"
    assert (result.destination / "Disc Two.chd").read_bytes() == b"disc two"


def test_nested_zip_directory_entries_are_counted(tmp_path, monkeypatch):
    """Directories count toward the ZIP entry limit, not only files."""
    monkeypatch.setattr(nested_zip, "MAX_ZIP_MEMBERS", 3)
    inner = zip_bytes({"a/": b"", "b/": b"", "Game.gb": b"game"})
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": inner}))
    # The outer ZIP entry adds one; the inner ZIP adds three more.
    result, = import_rom(download, resolve("gb"), tmp_path / "library")
    assert not result.ok
    assert "member-count limit" in result.reason
    assert not (tmp_path / "library").exists()


def test_nested_zip_central_directory_byte_limit_before_parse(tmp_path, monkeypatch):
    """Reject oversized directory bytes before ZipFile builds member records."""
    monkeypatch.setattr(nested_zip, "MAX_CENTRAL_DIRECTORY_BYTES", 32)
    inner = zip_bytes({"Game.gb": b"game"})
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": inner}))
    result, = import_rom(download, resolve("gb"), tmp_path / "library")
    assert not result.ok
    assert "central-directory byte limit" in result.reason
    assert not (tmp_path / "library").exists()


def test_outer_playlist_cannot_borrow_discs_from_inner_zip(tmp_path):
    """A playlist outside the inner archive cannot claim its child discs."""
    contents = zip_bytes({
        "Game.m3u": b"Disc One.chd\n",
        "inside.zip": zip_bytes({"Disc One.chd": b"disc one"}),
    })
    download = tmp_path / "outer.zip"
    download.write_bytes(contents)
    library = tmp_path / "library"
    preview = preview_import(download, resolve("psx"), library)
    assert [item["primary"] for item in preview["items"]] == [
        "inside.zip/Disc One.chd"]
    # The standalone disc remains selectable. Only the incomplete playlist is
    # refused; do not mislabel that valid disc as a failed import.
    result, = import_rom(download, resolve("psx"), library,
                         selected_primaries=["Game.m3u"])
    assert not result.ok
    assert "selected import members changed" in result.reason
    assert not library.exists()


def test_zip64_locator_without_classic_sentinels_obeys_entry_limit(tmp_path):
    """ZIP64 counts override ordinary EOCD even when classic values look small."""
    inner = bytearray(zip_bytes({"Game.gb": b"game"}))
    end = inner.rfind(b"PK\x05\x06")
    assert end == len(inner) - 22
    size = struct.unpack_from("<I", inner, end + 12)[0]
    offset = struct.unpack_from("<I", inner, end + 16)[0]
    zip64 = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45,
                        0, 0, nested_zip.MAX_ZIP_MEMBERS + 1,
                        nested_zip.MAX_ZIP_MEMBERS + 1, size, offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
    crafted = inner[:end] + zip64 + locator + inner[end:]
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": crafted}))
    library = tmp_path / "library"
    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "member-count limit" in result.reason
    assert not library.exists()


def test_nested_playlist_accepts_safe_dot_slash_reference(tmp_path):
    """A leading ./ must resolve to a companion in the same inner ZIP."""
    inner = zip_bytes({
        "Game.m3u": b"./Disc One.chd\n",
        "Disc One.chd": b"disc one",
    })
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": inner}))
    library = tmp_path / "library"
    primary = "inner.zip/Game.m3u"

    preview = preview_import(download, resolve("psx"), library)
    assert [item["primary"] for item in preview["items"]] == [primary]
    result, = import_rom(download, resolve("psx"), library,
                         selected_primaries=[primary])
    assert result.ok, result.reason
    assert (result.destination / "Game.m3u").read_bytes() == b"./Disc One.chd\n"
    assert (result.destination / "Disc One.chd").read_bytes() == b"disc one"



def test_zip64_valid_adjacent_locator_preserves_import(tmp_path):
    """A standard adjacent ZIP64 record remains importable after preflight."""
    inner = bytearray(zip_bytes({"Game.gb": b"game"}))
    end = inner.rfind(b"PK\x05\x06")
    assert end == len(inner) - 22
    central_bytes, central_offset = struct.unpack_from("<II", inner, end + 12)
    record = struct.pack(
        "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
        1, 1, central_bytes, central_offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
    nested = inner[:end] + record + locator + inner[end:]
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": nested}))

    result, = import_rom(download, resolve("gb"), tmp_path / "library")
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"game"



def test_zip64_with_prepended_data_preserves_import(tmp_path):
    """Accept only an unambiguous prefixed ZIP64 using the adjacent record."""
    inner = bytearray(zip_bytes({"Game.gb": b"game"}))
    end = inner.rfind(b"PK\x05\x06")
    assert end == len(inner) - 22
    central_bytes, central_offset = struct.unpack_from("<II", inner, end + 12)
    record = struct.pack(
        "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
        1, 1, central_bytes, central_offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
    inner_with_zip64 = inner[:end] + record + locator + inner[end:]
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": b"PREFIX" + inner_with_zip64}))

    result, = import_rom(download, resolve("gb"), tmp_path / "library")
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"game"


def test_zip64_prefixed_locator_rejects_competing_record(tmp_path):
    """Do not accept a prefix that looks like a second locator-target record."""
    inner = bytearray(zip_bytes({"Game.gb": b"game"}))
    end = inner.rfind(b"PK\x05\x06")
    central_bytes, central_offset = struct.unpack_from("<II", inner, end + 12)
    record = struct.pack(
        "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
        1, 1, central_bytes, central_offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end, 1)
    prefix = bytearray(end + 64)
    prefix[end:end + 4] = b"PK\x06\x06"
    inner_with_zip64 = inner[:end] + record + locator + inner[end:]
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": bytes(prefix) + inner_with_zip64}))
    library = tmp_path / "library"

    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "ambiguous ZIP64 locator offset" in result.reason
    assert not library.exists()


@pytest.mark.parametrize("zip64_entries", [1, nested_zip.MAX_ZIP_MEMBERS + 1])
def test_zip64_locator_offset_mismatch_fails_closed(tmp_path, zip64_entries):
    """Refuse a forged ZIP64 pointer that different Python versions may follow."""
    inner = bytearray(zip_bytes({"Game.gb": b"game"}))
    end = inner.rfind(b"PK\x05\x06")
    central_bytes, central_offset = struct.unpack_from("<II", inner, end + 12)
    record = struct.pack(
        "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
        zip64_entries, zip64_entries, central_bytes, central_offset)
    # A zero record offset may trigger fallback to the adjacent record
    # in some Python versions. Refuse the mismatch across all supported runtimes.
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
    nested = inner[:end] + record + locator + inner[end:]
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": nested}))
    library = tmp_path / "library"

    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "ZIP64 locator offset" in result.reason
    assert not library.exists()



def test_ambiguous_zip_eocd_in_comment_fails_before_parse(tmp_path):
    """Reject a fake EOCD in a comment that ZipFile would select instead."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("Game.gb", b"game")
        archive.comment = b"comment " + b"PK\x05\x06" + b"\0" * 18 + b"tail"
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"inner.zip": inner.getvalue()}))
    library = tmp_path / "library"
    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "ambiguous ZIP end-of-central-directory" in result.reason
    assert not library.exists()


def test_nested_windows_installer_is_one_complete_set(tmp_path):
    """Retain installer ZIP payloads and nested file paths, not extractions."""
    payload_zip = zip_bytes({
        "helper.exe": b"not the primary",
        "game-data1.bin": b"payload one",
        "game-data2.bin": b"payload two",
    })
    outer = zip_bytes({
        "setup-data.zip": payload_zip,
        "setup.exe": b"installer executable",
        "assets/config.dat": b"keep this subdirectory",
    })
    download = tmp_path / "Installer.zip"
    download.write_bytes(outer)
    library = tmp_path / "library"
    preview = preview_import(download, resolve("win"), library)

    assert len(preview["items"]) == 1
    item = preview["items"][0]
    assert item["primary"] == "setup.exe"
    assert set(item["members"]) == {
        "setup-data.zip", "setup.exe", "assets/config.dat"}
    assert item["selected"]
    result, = import_rom(download, resolve("win"), library,
                         selected_primaries=["setup.exe"])
    assert result.ok, result.reason
    assert (result.destination / "setup.exe").read_bytes() == b"installer executable"
    assert (result.destination / "setup-data.zip").read_bytes() == payload_zip
    assert (result.destination / "assets/config.dat").read_bytes() == b"keep this subdirectory"
    assert not (result.destination / "helper.exe").exists()
    assert download.read_bytes() == outer


def test_digital_installer_selection_ignores_parent_archive_name():
    """A directory containing 'setup' cannot promote an unrelated helper."""
    selected = _digital_set(["setup-data.zip/helper.exe", "setup.exe"])
    assert selected is not None
    assert selected.primary == "setup.exe"


@pytest.mark.parametrize("filenames", [
    ["_CommonRedist/DirectX/setup.exe", "setup.exe", "data.bin"],
    ["setup.exe", "_CommonRedist/DirectX/setup.exe", "data.bin"],
    ["install/setup.exe", "install/redist/setup.exe", "data.bin"],
])
def test_digital_installer_prefers_shallowest_exact_setup(filenames):
    """A redistributable's setup.exe never outranks the main installer."""
    selected = _digital_set(filenames)
    assert selected is not None
    assert selected.primary == next(
        path for path in filenames
        if path.lower() in ("setup.exe", "install/setup.exe"))


@pytest.mark.parametrize("filenames", [
    ["_CommonRedist/DirectX/setup.exe", "install/game/setup.exe"],
    ["install/game/setup.exe", "_CommonRedist/DirectX/setup.exe"],
    ["redist/DirectX/setup.exe", "install/game/setup.exe"],
    [r"_CommonRedist\DirectX\setup.exe", "install/game/setup.exe"],
])
def test_digital_equal_depth_prefers_game_setup_over_redist(filenames):
    """Equal-depth redistributables are not the game's actual installer."""
    selected = _digital_set(filenames)
    assert selected is not None
    assert selected.primary == "install/game/setup.exe"


def test_digital_exact_setup_outranks_asset_updater():
    """'AssetUpdater' embeds the substring 'setup' but is not the installer."""
    selected = _digital_set(["AssetUpdater.exe", "setup.exe"])
    assert selected is not None
    assert selected.primary == "setup.exe"


def test_digital_delimited_setup_fallback():
    """A delimited setup variant can outrank an unrelated helper executable."""
    selected = _digital_set(["AssetUpdater.exe", "install/setup-01.exe"])
    assert selected is not None
    assert selected.primary == "install/setup-01.exe"



def test_digital_numbered_setup_outranks_uninstaller():
    """Recognize setup32/setup64 filenames without preferring an uninstaller."""
    for candidate in ("setup32.exe", "setup64.exe", "install/setup64.exe"):
        selected = _digital_set(["uninstall.exe", "AssetUpdater.exe", candidate])
        assert selected is not None
        assert selected.primary == candidate


def test_digital_numbered_setup_keeps_delimiter_boundary():
    """A setup token inside another executable name is not the installer."""
    selected = _digital_set(["AssetUpdater.exe", "game-setup64.exe"])
    assert selected is not None
    assert selected.primary == "game-setup64.exe"



def test_digital_architecture_suffix_setup_outranks_uninstaller():
    """Windows setupx86/setupx64 should beat unrelated executables."""
    for candidate in ("setupx86.exe", "setupx64.exe", "install/setupx64.exe"):
        selected = _digital_set(["uninstall.exe", "AssetUpdater.exe", candidate])
        assert selected is not None
        assert selected.primary == candidate


def test_digital_architecture_setup_suffix_requires_name_boundary():
    """Do not treat setupx64helper.exe as a setupx64 installer."""
    selected = _digital_set(["setupx64helper.exe", "install/setup-01.exe"])
    assert selected is not None
    assert selected.primary == "install/setup-01.exe"


def test_digital_relative_path_collision_fails_closed(tmp_path):
    """A file cannot also be a directory; preserve the original source."""
    outer = zip_bytes({
        "setup.exe": b"installer",
        "assets": b"file not folder",
        "assets/config.dat": b"config",
    })
    download = tmp_path / "Installer.zip"
    download.write_bytes(outer)
    library = tmp_path / "library"
    result, = import_rom(download, resolve("win"), library)
    assert not result.ok
    assert "colliding set member paths" in result.reason
    assert download.read_bytes() == outer
    assert not (library / "win" / "Installer" / "setup.exe").exists()


def test_digital_equivalent_relative_paths_fail_before_copy(tmp_path):
    """Aliases such as assets/. must never overwrite one staged file."""
    outer = zip_bytes({
        "setup.exe": b"installer",
        "assets/config.dat": b"expected config",
        "assets/./config.dat": b"malicious alias",
    })
    download = tmp_path / "Installer.zip"
    download.write_bytes(outer)
    library = tmp_path / "library"
    result, = import_rom(download, resolve("win"), library)
    assert not result.ok
    assert "colliding set member paths" in result.reason
    assert download.read_bytes() == outer
    assert not (library / "win" / "Installer" / "setup.exe").exists()


@pytest.mark.parametrize("first, second", [
    ("assets/config.dat", "assets"),
    ("ASSETS/CONFIG.dat", "assets"),
])
def test_digital_descendant_before_file_collision_fails_closed(tmp_path, first, second):
    """The path collision guard rejects files that alias earlier directories."""
    outer = zip_bytes({
        "setup.exe": b"installer",
        first: b"file beneath assets",
        second: b"cannot also be a directory",
    })
    download = tmp_path / "Installer.zip"
    download.write_bytes(outer)
    library = tmp_path / "library"
    result, = import_rom(download, resolve("win"), library)
    assert not result.ok
    assert "colliding set member paths" in result.reason
    assert download.read_bytes() == outer
    assert not (library / "win" / "Installer" / "setup.exe").exists()


def test_digital_relative_path_normalizes_without_collision(tmp_path):
    """A single dot-segment member keeps its directory, with no alias."""
    outer = zip_bytes({
        "setup.exe": b"installer",
        "assets/./config.dat": b"config",
    })
    download = tmp_path / "Installer.zip"
    download.write_bytes(outer)
    library = tmp_path / "library"
    result, = import_rom(download, resolve("win"), library)
    assert result.ok, result.reason
    assert (result.destination / "assets/config.dat").read_bytes() == b"config"
    assert download.read_bytes() == outer


def test_flat_zip_not_rejected_by_nested_entry_limit(tmp_path, monkeypatch):
    """Two flat ZIP members do not inherit a one-entry nested ZIP ceiling."""
    original = zip_bytes({"Game.gb": b"game", "readme.txt": b"notes"})
    download = tmp_path / "Flat.zip"
    download.write_bytes(original)
    library = tmp_path / "library"
    monkeypatch.setattr(nested_zip, "MAX_ZIP_MEMBERS", 1)

    result, = import_rom(download, resolve("gb"), library)
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"game"
    assert download.read_bytes() == original


def test_flat_outer_zip_has_separate_finite_metadata_cap(tmp_path, monkeypatch):
    """Flat ZIPs have a higher cap, but reject excessive metadata before import."""
    original = zip_bytes({"Game.gb": b"game", "readme.txt": b"notes"})
    download = tmp_path / "Flat.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    monkeypatch.setattr(library, "MAX_OUTER_ZIP_MEMBERS", 1)

    result, = import_rom(download, resolve("gb"), library_root)
    assert not result.ok
    assert "ZIP member-count limit exceeded" in result.reason
    assert not library_root.exists()
    assert download.read_bytes() == original


def test_flat_outer_zip_central_byte_bound_before_import(tmp_path, monkeypatch):
    """Outer flat ZIP metadata still has a pre-open byte-size safeguard."""
    original = zip_bytes({"Game.gb": b"game", "readme.txt": b"notes"})
    download = tmp_path / "Flat.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    monkeypatch.setattr(nested_zip, "MAX_CENTRAL_DIRECTORY_BYTES", 48)

    result, = import_rom(download, resolve("gb"), library_root)
    assert not result.ok
    assert "ZIP central-directory byte limit exceeded" in result.reason
    assert not library_root.exists()
    assert download.read_bytes() == original


def test_nested_zip_still_rejects_strict_member_limit(tmp_path, monkeypatch):
    """The nested index retains entry limits despite flat-ZIP compatibility."""
    inner = zip_bytes({"Game.gb": b"game", "Another.gb": b"more"})
    outer = zip_bytes({"inner.zip": inner})
    download = tmp_path / "Nested.zip"
    download.write_bytes(outer)
    library = tmp_path / "library"
    monkeypatch.setattr(nested_zip, "MAX_ZIP_MEMBERS", 1)

    result, = import_rom(download, resolve("gb"), library)
    assert not result.ok
    assert "nested ZIP member-count limit" in result.reason
    assert not library.exists()
    assert download.read_bytes() == outer


def test_mame_archive_remains_the_playable_rom(tmp_path):
    """MAME's archive-is-the-ROM policy wins over nested ZIP discovery."""
    platform = resolve("mame")
    assert platform.archive_is_the_rom
    download = tmp_path / "arcade.zip"
    download.write_bytes(zip_bytes({"nested.zip": zip_bytes({"chip.bin": b"chip"})}))
    assert isinstance(_source_for(download, platform), _PathSource)


@pytest.mark.parametrize("sheet, contents", [
    ("Game.cue", b'FILE "track01.bin" BINARY\n'),
    ("Game.gdi", b'1\n1 0 4 2352 track01.bin 0\n'),
])
def test_nested_missing_disc_track_is_not_previewed(tmp_path, sheet, contents):
    """A track in another ZIP cannot make an incomplete disc playable."""
    original = zip_bytes({
        "disc.zip": zip_bytes({sheet: contents}),
        "other.zip": zip_bytes({"track01.bin": b"other disc"}),
    })
    download = tmp_path / "outer.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    preview = preview_import(download, resolve("dc"), library_root)
    assert all(item["primary"] != f"disc.zip/{sheet}" for item in preview["items"])
    assert not library_root.exists()
    assert download.read_bytes() == original


def test_nested_playlist_rejects_cue_missing_track(tmp_path):
    """A valid M3U reference does not excuse a missing CUE track."""
    download = tmp_path / "outer.zip"
    download.write_bytes(zip_bytes({"disc.zip": zip_bytes({
        "Game.m3u": b"Game.cue\n",
        "Game.cue": b'FILE "missing.bin" BINARY\n',
    })}))
    preview = preview_import(download, resolve("dc"), tmp_path / "library")
    assert all(not i["primary"].endswith("Game.m3u") for i in preview["items"])


def test_nested_skips_traversal_but_keeps_safe_sibling(tmp_path):
    """Skip the traversal entry without rejecting the valid ROM."""
    original = zip_bytes({"inner.zip": zip_bytes({
        "../skip.gb": b"unsafe", "Game.gb": b"valid game",
    })})
    download = tmp_path / "outer.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    preview = preview_import(download, resolve("gb"), library_root)
    assert [i["primary"] for i in preview["items"]] == ["inner.zip/Game.gb"]
    result, = import_rom(download, resolve("gb"), library_root)
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"valid game"
    assert download.read_bytes() == original


def test_existing_destination_parent_file_does_not_partially_publish(tmp_path):
    """Reject a path conflict before writing setup.exe on overwrite."""
    original = zip_bytes({"setup.exe": b"installer",
                          "assets/config.dat": b"config"})
    download = tmp_path / "Installer.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    target = library_root / "win" / "Installer"
    target.mkdir(parents=True)
    (target / "assets").write_bytes(b"existing file")

    result, = import_rom(download, resolve("win"), library_root, overwrite=True)
    assert not result.ok
    assert "destination ancestor is not a directory" in result.reason
    assert not (target / "setup.exe").exists()
    assert (target / "assets").read_bytes() == b"existing file"
    assert download.read_bytes() == original


@pytest.mark.parametrize("filenames, expected", [
    (["Game/install/setup.exe", "redist/setup.exe"], "Game/install/setup.exe"),
    (["redist/setup.exe", "Game/install/setup.exe"], "Game/install/setup.exe"),
    (["setup64.exe", "redist/setup.exe"], "setup64.exe"),
    (["redist/setup.exe", "setup64.exe"], "setup64.exe"),
])
def test_game_installer_outranks_redist_across_depths(filenames, expected):
    """Game install setup variants take precedence over redistributables."""
    selected = _digital_set(filenames)
    assert selected is not None
    assert selected.primary == expected


def test_nested_disc_lookup_reuses_member_index(monkeypatch):
    """Checking many tracks must not rescan all members for each reference."""
    from romarr.selection import RomSet

    count = 160
    sheet = "inner.zip/Game.cue"
    tracks = tuple(f"inner.zip/Track{index:03}.bin" for index in range(count))
    game = RomSet(sheet, (sheet, *tracks))
    cue = "\n".join(
        f'FILE "Track{index:03}.bin" BINARY'
        for index in range(count)
    ).encode()

    original_parent = library._parent
    parent_calls = 0

    def counted_parent(name):
        """Count directory lookup work for a representative disc set."""
        nonlocal parent_calls
        parent_calls += 1
        return original_parent(name)

    monkeypatch.setattr(library, "_parent", counted_parent)
    assert library._complete_nested_zip_sets([game], lambda _name: cue) == [game]
    assert parent_calls <= len(game.members) + 2


@pytest.mark.parametrize("sheet, contents", [
    ("Game.cue", b"REM no track names\n"),
    ("Game.gdi", b"0\n"),
])
def test_nested_disc_sheet_with_no_track_references_is_rejected(
        tmp_path, sheet, contents):
    """A readable but empty disc sheet must not import orphan tracks."""
    inner = zip_bytes({sheet: contents, "track.bin": b"orphan"})
    download = tmp_path / "outer.zip"
    original = zip_bytes({"disc.zip": inner})
    download.write_bytes(original)
    library_root = tmp_path / "library"

    preview = preview_import(download, resolve("dc"), library_root)
    assert all(not item["primary"].endswith(sheet) for item in preview["items"])
    result, = import_rom(download, resolve("dc"), library_root,
                         selected_primaries=[f"disc.zip/{sheet}"])
    assert not result.ok
    assert not library_root.exists()
    assert download.read_bytes() == original


@pytest.mark.parametrize("sheet, contents", [
    ("Game.cue", b'FILE "tracks/track.bin" BINARY\n'),
    ("Game.gdi", b'1\n1 0 4 2352 tracks/track.bin 0\n'),
])
def test_nested_disc_reference_cannot_match_wrong_directory(
        tmp_path, sheet, contents):
    """A root-level basename does not satisfy a subdirectory reference."""
    original = zip_bytes({"disc.zip": zip_bytes({
        sheet: contents, "track.bin": b"wrong location",
    })})
    download = tmp_path / "outer.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    preview = preview_import(download, resolve("dc"), library_root)
    assert all(not item["primary"].endswith(sheet) for item in preview["items"])
    result, = import_rom(download, resolve("dc"), library_root,
                         selected_primaries=[f"disc.zip/{sheet}"])
    assert not result.ok
    assert not library_root.exists()
    assert download.read_bytes() == original


@pytest.mark.parametrize("reference", ["./track.bin", ".\\track.bin"])
def test_nested_disc_accepts_same_directory_reference_prefix(tmp_path, reference):
    """An explicit current-directory prefix still resolves a real sidecar."""
    cue = f'FILE "{reference}" BINARY\n'.encode()
    original = zip_bytes({"disc.zip": zip_bytes({
        "Game.cue": cue, "track.bin": b"correct track",
    })})
    download = tmp_path / "outer.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    preview = preview_import(download, resolve("dc"), library_root)
    assert [i["primary"] for i in preview["items"]] == ["disc.zip/Game.cue"]
    result, = import_rom(download, resolve("dc"), library_root)
    assert result.ok, result.reason
    assert (result.destination / "track.bin").read_bytes() == b"correct track"
    assert download.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory symlinks")
def test_digital_symlink_alias_rejected_before_first_publication(tmp_path):
    """Existing directory aliases cannot publish two files to one target."""
    original = zip_bytes({
        "setup.exe": b"game setup",
        "assets/setup.exe": b"second installer",
    })
    download = tmp_path / "Installer.zip"
    download.write_bytes(original)
    library_root = tmp_path / "library"
    target = library_root / "win" / "Installer"
    target.mkdir(parents=True)
    (target / "assets").symlink_to(".", target_is_directory=True)

    result, = import_rom(download, resolve("win"), library_root, overwrite=True)
    assert not result.ok
    assert "colliding destination paths" in result.reason
    assert not (target / "setup.exe").exists()
    assert (target / "assets").is_symlink()
    assert download.read_bytes() == original


def test_classic_zip_with_exactly_65535_members_has_no_zip64_locator(tmp_path):
    """A classic maximum-count archive is legal without a ZIP64 locator."""
    archive = tmp_path / "classic.zip"
    with zipfile.ZipFile(archive, "w", allowZip64=False) as out:
        for index in range(65_535):
            out.writestr(f"{index:05}.txt", b"")
    data = archive.read_bytes()
    end = data.rfind(b"PK\x05\x06")
    assert end >= 20
    assert data[end - 20:end - 16] != b"PK\x06\x07"
    nested_zip.check_zip_central_directory(
        archive, max_members=nested_zip.MAX_OUTER_ZIP_MEMBERS)
    with pytest.raises(ValueError, match="member-count limit exceeded"):
        nested_zip.check_zip_central_directory(archive, max_members=65_534)


@pytest.mark.parametrize("version, expected", [
    ((3, 10, 21), False), ((3, 10, 22), True),
    ((3, 11, 16), False), ((3, 11, 17), True),
    ((3, 12, 14), False), ((3, 12, 15), True),
    ((3, 13, 15), False), ((3, 13, 16), True),
    ((3, 14, 7), False), ((3, 14, 8), True),
    ((3, 15, 0), True), ((3, 9, 25), False),
    ((3, 13, 16, "candidate", 1), False),
    ((3, 14, 8, "candidate", 1), False),
    ((3, 15, 0, "alpha", 1), False),
    ((3, 15, 0, "candidate", 2), False),
    ((3, 15, 0, "final", 0), True),
])
def test_zipfile_security_release_floor(version, expected):
    """Allow alternate ZIP compression only on patched CPython versions."""
    assert nested_zip._bounded_alt_zipfile_reads(version) is expected


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_nested_alt_compression_requires_patched_python(
        tmp_path, monkeypatch, method):
    """Do not expand bzip2/LZMA ZIP containers on a vulnerable runtime."""
    inner = zip_bytes({"Game.gb": b"game"})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=method) as archive:
        archive.writestr("inner.zip", inner)
    download = tmp_path / "outer.zip"
    original = data.getvalue()
    download.write_bytes(original)
    library_root = tmp_path / "library"

    monkeypatch.setattr(nested_zip, "_bounded_alt_zipfile_reads", lambda: False)
    result, = import_rom(download, resolve("gb"), library_root)
    assert not result.ok
    assert "requires a patched Python runtime" in result.reason
    assert not library_root.exists()
    assert download.read_bytes() == original

    monkeypatch.setattr(nested_zip, "_bounded_alt_zipfile_reads", lambda: True)
    result, = import_rom(download, resolve("gb"), library_root)
    assert result.ok, result.reason
    assert result.destination.read_bytes() == b"game"
    assert download.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory symlinks")
@pytest.mark.parametrize("members", [
    ("setup.exe", "assets/setup.exe/config.ini"),
    ("assets/setup.exe/config.ini", "setup.exe"),
])
def test_digital_aliased_file_directory_conflict_is_preflighted(tmp_path, members):
    """A file cannot also be an ancestor of a resolved installer member."""
    download = tmp_path / "Installer.zip"
    original = zip_bytes({name: b"data" for name in members})
    download.write_bytes(original)
    library_root = tmp_path / "library"
    target = library_root / "win" / "Installer"
    target.mkdir(parents=True)
    (target / "assets").symlink_to(".", target_is_directory=True)

    result, = import_rom(download, resolve("win"), library_root, overwrite=True)
    assert not result.ok
    assert "colliding destination paths" in result.reason
    assert not (target / "setup.exe").exists()
    assert (target / "assets").is_symlink()
    assert download.read_bytes() == original


@pytest.mark.parametrize("method", [20, 93])
def test_nested_zstandard_method_requires_patched_python(
        tmp_path, monkeypatch, method):
    """Reject both ZIP Zstandard identifiers before unsafe decompression."""
    payload = bytearray(zip_bytes({"inner.zip": zip_bytes({"Game.gb": b"game"})}))
    local_header = payload.find(b"PK\x03\x04")
    central_header = payload.rfind(b"PK\x01\x02")
    assert local_header >= 0 and central_header > local_header
    struct.pack_into("<H", payload, local_header + 8, method)
    struct.pack_into("<H", payload, central_header + 10, method)
    with zipfile.ZipFile(io.BytesIO(payload)) as mutated:
        assert [(member.filename, member.compress_type)
                for member in mutated.infolist()] == [("inner.zip", method)]
    download = tmp_path / "outer.zip"
    download.write_bytes(payload)
    library_root = tmp_path / "library"

    monkeypatch.setattr(nested_zip, "_bounded_alt_zipfile_reads", lambda: False)
    result, = import_rom(download, resolve("gb"), library_root)
    assert not result.ok
    assert "requires a patched Python runtime" in result.reason
    assert not library_root.exists()
    assert download.read_bytes() == payload
