import errno
import os
import zipfile
from pathlib import Path
import pytest
from romarr.library import import_rom
from romarr.platforms import resolve


def raw(tmp_path):
    source = tmp_path / "game.gb"
    source.write_bytes(b"x" * 32768)
    source.chmod(0o644)
    return source


def test_raw_link_preserves_bytes_permissions_and_source(tmp_path):
    source = raw(tmp_path)
    before = source.stat()
    result, = import_rom(source, resolve("gb"), tmp_path / "library", prefer_hardlinks=True)
    assert result.ok and result.import_method == "hardlink"
    assert os.path.samefile(source, result.destination)
    assert source.stat().st_nlink >= 2
    assert source.stat().st_mode == before.st_mode
    assert result.destination.read_bytes() == source.read_bytes()


def test_private_inode_copies_without_chmod(tmp_path):
    source = raw(tmp_path)
    source.chmod(0o600)
    result, = import_rom(source, resolve("gb"), tmp_path / "library", prefer_hardlinks=True)
    assert result.ok and result.import_method == "copy"
    assert "permissions" in result.fallback_reason
    assert not os.path.samefile(source, result.destination)
    assert source.stat().st_mode & 0o777 == 0o600


def test_cross_device_fallback_visible(tmp_path, monkeypatch):
    source = raw(tmp_path)
    real_link = os.link
    def link(src, dst, **kw):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(src, dst, **kw)
    monkeypatch.setattr(os, "link", link)
    result, = import_rom(source, resolve("gb"), tmp_path / "library", prefer_hardlinks=True)
    assert result.ok and result.import_method == "copy"
    assert "cross-device" in result.fallback_reason
    assert result.destination.read_bytes() == source.read_bytes()


def test_io_error_not_hidden(tmp_path, monkeypatch):
    from romarr.library import _PathSource
    source = raw(tmp_path)
    reader = _PathSource(source)
    reader.prefer_hardlinks = True
    def fail(*args, **kw):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="I/O error"):
        reader.copy(source.name, tmp_path / "destination.gb")
    assert not (tmp_path / "destination.gb").exists()


def test_archive_extraction_reported(tmp_path):
    source = tmp_path / "game.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("game.gb", b"x" * 32768)
    result, = import_rom(source, resolve("gb"), tmp_path / "library", prefer_hardlinks=True)
    assert result.ok and result.import_method == "extracted"
    assert source.exists()


def test_existing_destination_never_clobbered(tmp_path):
    source = raw(tmp_path)
    destination = tmp_path / "library" / "gb" / source.name
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous")
    result, = import_rom(source, resolve("gb"), tmp_path / "library", prefer_hardlinks=True)
    assert not result.ok
    assert destination.read_bytes() == b"previous"
