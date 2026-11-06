"""Import publication and request completion must preserve failed payloads (#124)."""
import errno
import zipfile

import pytest

from romarr import library
from romarr.library import ImportResult, import_rom
from romarr.platforms import resolve


@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("overwrite", [False, True])
def test_failed_write_has_no_partial_destination_and_preserves_existing(tmp_path, monkeypatch, archive, overwrite):
    source = tmp_path / ("game.zip" if archive else "game.gb")
    if archive:
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("game.gb", b"complete" * 4096)
        cls = library._ZipSource
    else:
        source.write_bytes(b"complete" * 4096)
        cls = library._PathSource
    dest = tmp_path / "lib" / "gb" / "game.gb"
    if overwrite:
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"previous valid payload")
    original = cls.copy

    def fail(self, name, destination):
        destination.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr(cls, "copy", fail)
    outcome = import_rom(source, resolve("gb"), tmp_path / "lib", overwrite=overwrite)[0]
    assert not outcome.ok and "injected disk full" in outcome.reason
    assert dest.read_bytes() == b"previous valid payload" if overwrite else not dest.exists()
    assert source.exists()
    assert not list(dest.parent.glob(".romarr-import-*"))
    monkeypatch.setattr(cls, "copy", original)
    assert import_rom(source, resolve("gb"), tmp_path / "lib", overwrite=overwrite)[0].ok
    assert dest.read_bytes() == b"complete" * 4096


def test_racing_destination_is_never_clobbered(tmp_path, monkeypatch):
    source = tmp_path / "game.gb"
    source.write_bytes(b"new" * 100)
    dest = tmp_path / "lib" / "gb" / "game.gb"
    original = library._PathSource.copy

    def race(self, name, staged):
        original(self, name, staged)
        dest.write_bytes(b"racing writer")

    monkeypatch.setattr(library._PathSource, "copy", race)
    result = import_rom(source, resolve("gb"), tmp_path / "lib")[0]
    assert not result.ok
    assert dest.read_bytes() == b"racing writer"


@pytest.mark.parametrize("overwrite", [False, True])
def test_existing_empty_destination_needs_inspection(tmp_path, overwrite):
    source = tmp_path / "game.gb"
    source.write_bytes(b"payload")
    dest = tmp_path / "lib" / "gb" / "game.gb"
    dest.parent.mkdir(parents=True)
    dest.touch()
    result = import_rom(source, resolve("gb"), tmp_path / "lib", overwrite=overwrite)[0]
    assert not result.ok and "zero-byte" in result.reason
    assert dest.stat().st_size == 0


def test_failed_set_member_never_publishes_incomplete_set(tmp_path, monkeypatch):
    source = tmp_path / "download"
    source.mkdir()
    (source / "game.cue").write_text('FILE "game.bin" BINARY\n')
    (source / "game.bin").write_bytes(b"payload")
    original = library._PathSource.copy

    def fail(self, name, staged):
        if name.endswith(".bin"):
            staged.write_bytes(b"part")
            raise OSError("injected track failure")
        original(self, name, staged)

    monkeypatch.setattr(library._PathSource, "copy", fail)
    result = import_rom(source, resolve("psx"), tmp_path / "lib")[0]
    assert not result.ok and "track failure" in result.reason
    assert not result.destination.exists()
    assert (source / "game.bin").read_bytes() == b"payload"


def test_partial_import_never_cleans_source_or_fulfils_request(tmp_path, monkeypatch):
    import romarr.app as app
    from romarr.app import ROMarr, QueueItem

    root = tmp_path / "library"
    root.mkdir()
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                       "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(root)})
    service.store.put_item("libraries", {"type": "folder", "name": "Shelf", "enable": True,
                                         "path": str(root), "is_default": True})
    service.reload_libraries()
    source = tmp_path / "download.gb"
    source.write_bytes(b"payload")
    item = QueueItem("Game", "gb", "bundle", 1, "grabbed", download_id="qbit-tag:job",
                     external_request_id="external-124")
    service.queue.append(item)
    removed, updated, rescanned, reconciled = [], [], [], []

    class Client:
        configured = True
        def completed(self):
            return [{"id": "qbit-tag:job", "name": "bundle", "content_path": str(source)}]
        def remove_download(self, *args, **kwargs):
            removed.append((args, kwargs))

    service.clients = [Client()]
    monkeypatch.setattr(app, "import_rom", lambda *a, **kw: [
        ImportResult(True, root / "first.gb"), ImportResult(False, None, "failed second ROM")])
    monkeypatch.setattr(service.store, "update_seerr_request", lambda *a, **kw: updated.append(kw))
    monkeypatch.setattr(service, "reconcile_platform", lambda *a: reconciled.append(a))
    monkeypatch.setattr(service.library_for("gb")[1], "rescan", lambda *a: rescanned.append(a))
    result = service.import_finished()
    assert not result[0]["ok"] and result[0]["reason"] == "failed second ROM"
    assert item.state == "import-failed"
    assert not removed and not updated and not rescanned and not reconciled
    assert not item.imported_paths
    assert source.read_bytes() == b"payload"


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="Linux no-replace rename")
@pytest.mark.parametrize("collision", [False, True])
def test_destination_without_hardlink_support_still_publishes_safely(tmp_path, monkeypatch, collision):
    staged = tmp_path / "staged"
    staged.write_bytes(b"complete")
    dest = tmp_path / "final"
    if collision:
        dest.write_bytes(b"previous")
    def unsupported(*a, **kw):
        raise OSError(errno.EOPNOTSUPP, "no hardlinks")
    monkeypatch.setattr(library.os, "link", unsupported)
    if collision:
        with pytest.raises(FileExistsError):
            library._publish_staged(staged, dest, False)
        assert dest.read_bytes() == b"previous"
    else:
        library._publish_staged(staged, dest, False)
        assert dest.read_bytes() == b"complete"
