"""Collection imports use persisted client identity, never collection-root guessing."""
from pathlib import Path
import zipfile
from copy import deepcopy
from types import SimpleNamespace
import pytest
from romarr.app import ROMarr
from romarr.store import QueueItem, Store
from romarr.selective_import import selected_import_path
from romarr.library import import_rom
from romarr.platforms import resolve


@pytest.fixture
def selected(tmp_path):
    root = tmp_path / "downloads"
    path = root / "Collection" / "Sony - PlayStation 2" / "fixture.zip"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fixture.iso", b"x" * (8 * 1024 * 1024))
    identity = {"path": path.relative_to(root).as_posix(), "index": 3,
                "size": path.stat().st_size, "hash": "abc123"}
    queue = QueueItem("Fixture", "ps2", "Collection", 1, "grabbed",
                      file_selection_required=True, selected_file=identity)
    row = {"index": 3, "name": identity["path"], "size": identity["size"],
           "progress": 1, "priority": 1}
    torrent = {"hash": "abc123", "name": "Collection", "save_path": str(root),
               "content_path": str(root / "Collection")}
    client = SimpleNamespace(torrent_files=lambda _: [row])
    return queue, torrent, client, row, path


@pytest.mark.parametrize("prefix", ["", "extra-top-folder/"])
def test_imports_only_exact_nested_zip_with_unrelated_archives(selected, tmp_path, prefix):
    queue, torrent, client, row, path = selected
    if prefix:
        new = Path(torrent["save_path"]) / prefix / queue.selected_file["path"]
        new.parent.mkdir(parents=True)
        path.rename(new)
        path = new
        queue.selected_file["path"] = prefix + queue.selected_file["path"]
        row["name"] = queue.selected_file["path"]
    other = path.parent / "unrelated.zip"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("other.iso", b"x" * (8 * 1024 * 1024))
    source = selected_import_path(queue, torrent, client)
    assert source == path
    outcomes = import_rom(source, resolve("ps2"), tmp_path / "library")
    assert len(outcomes) == 1 and outcomes[0].ok
    assert path.exists() and other.exists()
    assert not list((tmp_path / "library").rglob("other.iso"))


@pytest.mark.parametrize("change", ["missing", "duplicate", "index", "name", "size", "incomplete", "deselected", "hash", "root", "legacy"])
def test_invalid_or_changed_identity_fails_closed(selected, change):
    queue, torrent, client, row, path = selected
    if change == "missing": client.torrent_files = lambda _: []
    elif change == "duplicate": client.torrent_files = lambda _: [row, deepcopy(row)]
    elif change == "index": row["index"] = 4
    elif change == "name": row["name"] = "different.zip"
    elif change == "size": row["size"] += 1
    elif change == "incomplete": row["progress"] = .9
    elif change == "deselected": row["priority"] = 0
    elif change == "hash": torrent["hash"] = "wrong"
    elif change == "root": torrent["save_path"] = ""
    elif change == "legacy": queue.selected_file = {}
    with pytest.raises(ValueError):
        selected_import_path(queue, torrent, client)
    assert path.exists()


@pytest.mark.parametrize("unsafe", ["../outside.zip", "/outside.zip", "C:/outside.zip", "folder/../../outside.zip"])
def test_traversal_refused(selected, unsafe):
    queue, torrent, client, row, path = selected
    queue.selected_file["path"] = row["name"] = unsafe
    with pytest.raises(ValueError, match="unsafe"):
        selected_import_path(queue, torrent, client)


def test_symlink_escape_refused(selected, tmp_path):
    queue, torrent, client, row, path = selected
    outside = tmp_path / "outside.zip"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        selected_import_path(queue, torrent, client)


def test_selection_survives_restart(selected, tmp_path):
    queue, torrent, client, row, path = selected
    state = tmp_path / "state.json"
    store = Store(state)
    store.enqueue(queue)
    loaded = Store(state).queue[0]
    assert loaded.file_selection_required
    assert loaded.selected_file == queue.selected_file
    assert selected_import_path(loaded, torrent, client) == path


def test_legacy_minerva_queue_is_flagged_without_importing_collection(selected, tmp_path):
    queue, torrent, client, row, path = selected
    queue.file_selection_required = False
    queue.selected_file = {}
    queue.indexer = "hub:minerva-archive"
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.queue.append(queue)
    client.configured = True
    client.completed = lambda: [torrent]
    service.clients = [client]
    result = service.import_finished()
    assert queue.state == "import-failed"
    assert "identity is missing" in result[0]["reason"]
    assert path.exists()


def test_import_sweep_opens_exact_archive_above_platform_directory(selected, tmp_path):
    queue, torrent, client, row, path = selected
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "LIBRARY_PATH": str(tmp_path / "library"), "LIBRARY_KIND": "folder"})
    service.store.settings.update(rescan_after_import=False, reconcile_after_import=False)
    service.queue.append(queue)
    client.configured = True
    client.completed = lambda: [torrent]
    service.clients = [client]
    results = service.import_finished()
    assert results[0]["ok"] is True
    assert queue.state == "imported"
    assert queue.imported_paths and Path(queue.imported_paths[0]).exists()
    assert path.exists()


def test_remote_save_root_mapping_is_applied_before_member_resolution(selected):
    queue, torrent, client, row, path = selected
    local = torrent["save_path"]
    torrent["save_path"] = "/remote/downloads"
    mappings = [{"remote": "/remote/downloads", "local": local}]
    assert selected_import_path(queue, torrent, client, mappings) == path


def test_missing_local_member_never_falls_back_to_collection_root(selected):
    queue, torrent, client, row, path = selected
    path.unlink()
    with pytest.raises(FileNotFoundError):
        selected_import_path(queue, torrent, client)
