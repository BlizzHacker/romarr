"""Compressed TAR downloads import like any other archive, and refuse tricks."""
import io
import tarfile

import pytest

from romarr.import_intent import preview_import
from romarr.library import _digital_set_name, _source_for, import_rom, is_archive_name
from romarr.platforms import resolve
from romarr.tar_archive import TarSource, is_tar_name

DISC = {"Game.cue": b'FILE "track.bin" BINARY\n', "track.bin": b"track bytes"}


def make_tar(path, members, mode="w:gz", extra=()):
    """Write regular members, then any hand-built TarInfo entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, mode) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra:
            archive.addfile(info)
    return path


@pytest.mark.parametrize("suffix,mode", [
    (".tar.gz", "w:gz"), (".tgz", "w:gz"), (".tar.bz2", "w:bz2"),
    (".tar.xz", "w:xz"), (".tar", "w")])
def test_tar_family_disc_imports_and_preserves_download(tmp_path, suffix, mode):
    download = tmp_path / "download"
    archive = make_tar(download / ("game" + suffix), DISC, mode)
    original = archive.read_bytes()
    preview = preview_import(download, resolve("dc"), tmp_path / "library")
    assert not preview["requires_review"]
    primary = preview["items"][0]["primary"]
    assert primary.endswith("Game.cue")
    result, = import_rom(download, resolve("dc"), tmp_path / "library",
                         selected_primaries=[primary])
    assert result.ok, result.reason
    assert (result.destination / "track.bin").read_bytes() == b"track bytes"
    assert archive.read_bytes() == original


def test_single_tar_file_is_read_as_an_archive(tmp_path):
    archive = make_tar(tmp_path / "Game (USA).tar.gz", {"Game (USA).gba": b"rom"})
    source = _source_for(archive, resolve("gba"))
    try:
        assert isinstance(source, TarSource)
        assert source.names() == ["Game (USA).gba"]
        assert source.hash("Game (USA).gba")["sha1"]
        out = tmp_path / "out.gba"
        source.copy("Game (USA).gba", out)
        assert out.read_bytes() == b"rom"
    finally:
        source.close()


def test_archive_is_the_rom_platforms_still_open_tar_wrappers(tmp_path):
    """A MAME zip is the ROM, but nothing loads a .tar.gz -- unwrap it."""
    archive = make_tar(tmp_path / "pacman.tar.gz", {"pacman.zip": b"PK\x05\x06" + b"\0" * 18})
    source = _source_for(archive, resolve("arcade"))
    try:
        assert isinstance(source, TarSource)
        assert source.names() == ["pacman.zip"]
    finally:
        source.close()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_links_are_refused(tmp_path, kind):
    link = tarfile.TarInfo("Game.gba")
    link.type = kind
    link.linkname = "/etc/passwd"
    archive = make_tar(tmp_path / "evil.tar.gz", {}, extra=[link])
    with pytest.raises(ValueError):
        TarSource(archive)


def test_traversal_members_are_never_exposed(tmp_path):
    archive = make_tar(tmp_path / "game.tar", {
        "../escape.gba": b"x", "/abs.gba": b"y", "Game.gba": b"rom"}, "w")
    source = TarSource(archive)
    try:
        assert source.names() == ["Game.gba"]
    finally:
        source.close()


def test_case_aliased_members_are_refused(tmp_path):
    archive = make_tar(tmp_path / "game.tar", {"Game.gba": b"a", "game.GBA": b"b"}, "w")
    with pytest.raises(ValueError):
        TarSource(archive)


def test_declared_size_beyond_member_limit_is_refused(tmp_path, monkeypatch):
    from romarr import nested_zip
    monkeypatch.setattr(nested_zip, "MAX_FINAL_MEMBER_BYTES", 8)
    archive = make_tar(tmp_path / "big.tar", {"Game.gba": b"0123456789"}, "w")
    with pytest.raises(ValueError):
        TarSource(archive)


def test_compound_extensions_are_stripped_whole(tmp_path):
    for name in ("Call.of.Duty.[DODI].tar.gz", "Call.of.Duty.[DODI].tgz",
                 "Call.of.Duty.[DODI].tar.xz"):
        path = tmp_path / name
        path.write_bytes(b"")
        assert _digital_set_name(path) == "Call.of.Duty.[DODI]"
    assert is_archive_name("x.TAR.BZ2") and is_tar_name("x.txz")
    assert not is_archive_name("Game.gz") and not is_tar_name("Game.tar.zst")
