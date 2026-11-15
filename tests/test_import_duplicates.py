"""Issue 190: retained payload hashes block duplicates across names and platforms."""
import hashlib
import zipfile

import pytest

from romarr.app import ROMarr, QueueItem
from romarr.hashes import HashIndex
from romarr.library import import_rom
from romarr.platforms import resolve

PAYLOAD = b"homebrew-game-payload" * 300


def digest(data=PAYLOAD):
    """Use the exact physical bytes, including any copier header."""
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def retained(tmp_path, *, archive=False, stale=False, platform="gbc"):
    """Create an index row naming a real retained raw or ZIP library copy."""
    root = tmp_path / "library"
    extension = ".gb" if platform == "gb" else ".gbc"
    path = root / platform / ("old-title.zip" if archive else "old-title" + extension)
    path.parent.mkdir(parents=True)
    data = b"changed retained bytes" if stale else PAYLOAD
    if archive:
        with zipfile.ZipFile(path, "w") as zipped:
            zipped.writestr("old-title" + extension, data)
    else:
        path.write_bytes(data)
    index = HashIndex(tmp_path / "hashes.json")
    index.add(digest(), "Old title", platform, path=path.relative_to(root).as_posix())
    return root, path, index


@pytest.mark.parametrize("incoming_zip", [False, True])
@pytest.mark.parametrize("retained_zip", [False, True])
@pytest.mark.parametrize("existing_platform", ["gb", "gbc"])
def test_existing_payload_is_not_published_across_names_or_platforms(tmp_path, incoming_zip, retained_zip, existing_platform):
    """Compare extracted member bytes, never the incoming archive container."""
    root, old, index = retained(tmp_path, archive=retained_zip, platform=existing_platform)
    incoming = tmp_path / ("new-title.zip" if incoming_zip else "new-title.gb")
    if incoming_zip:
        with zipfile.ZipFile(incoming, "w") as zipped:
            zipped.writestr("new-title.gb", PAYLOAD)
    else:
        incoming.write_bytes(PAYLOAD)
    before = incoming.read_bytes()
    result = import_rom(incoming, resolve("gb"), root, hash_index=index)[0]
    assert not result.ok and "already in the library" in result.reason
    assert existing_platform + ":" in result.reason and old.name in result.reason
    assert not (root / "gb" / "new-title.gb").exists()
    assert incoming.read_bytes() == before and old.exists()


@pytest.mark.parametrize("state", ["changed", "missing", "unknown-path", "outside-root"])
def test_stale_or_unverifiable_index_entries_do_not_refuse_import(tmp_path, state):
    """A cached SHA1 alone cannot claim that an actual retained copy exists."""
    root, old, index = retained(tmp_path, stale=state == "changed")
    if state == "missing":
        old.unlink()
    elif state == "unknown-path":
        index.by_sha1(digest()).path = ""
    elif state == "outside-root":
        external = tmp_path / "outside.gb"
        external.write_bytes(PAYLOAD)
        index.by_sha1(digest()).path = str(external)
    incoming = tmp_path / "new-title.gb"
    incoming.write_bytes(PAYLOAD)
    result = import_rom(incoming, resolve("gb"), root, hash_index=index)[0]
    assert result.ok and result.destination.read_bytes() == PAYLOAD


def test_changed_retained_zip_member_does_not_block(tmp_path):
    """An old ZIP member hash must still agree with the bytes currently retained."""
    root, _, index = retained(tmp_path, archive=True, stale=True)
    incoming = tmp_path / "new-title.gb"
    incoming.write_bytes(PAYLOAD)
    assert import_rom(incoming, resolve("gb"), root, hash_index=index)[0].ok


def service(tmp_path):
    """A folder-backed service exercises real manual and scheduled import wiring."""
    root = tmp_path / "library"
    root.mkdir(exist_ok=True)
    app = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                  "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(root)})
    app.store.put_item("libraries", {"type": "folder", "name": "Shelf", "enable": True,
                                    "path": str(root), "is_default": True})
    app.reload_libraries()
    app.store.settings["rescan_after_import"] = False
    app.store.settings["reconcile_after_import"] = False
    return app, root


@pytest.mark.parametrize("enabled", [False, True])
def test_manual_import_honors_hash_index_setting_and_indexes_unknown_dumps(tmp_path, enabled):
    """Manual adoption records raw SHA1 even without a DAT and respects the switch."""
    app, root = service(tmp_path)
    incoming = tmp_path / "original.gb"
    incoming.write_bytes(PAYLOAD)
    first = app.adopt(str(incoming), "gb")
    assert first["ok"]
    entry = app.hashes.by_sha1(digest())
    assert entry is not None and entry.path == "gb/original.gb"
    second = tmp_path / "renamed.gbc"
    second.write_bytes(PAYLOAD)
    app.store.settings["hash_index"] = enabled
    result = app.adopt(str(second), "gbc")
    assert result["ok"] is (not enabled)
    assert second.exists()
    assert (root / "gbc" / "renamed.gbc").exists() is (not enabled)


def test_scheduled_duplicate_keeps_queue_failed_and_source_untouched(tmp_path):
    """A duplicate must not fulfil Wanted, clean a download or report a new import."""
    app, root = service(tmp_path)
    old = root / "gbc" / "retained.gbc"
    old.parent.mkdir()
    old.write_bytes(PAYLOAD)
    app.hashes.add(digest(), "Retained", "gbc", path="gbc/retained.gbc")
    incoming = tmp_path / "new.gb"
    incoming.write_bytes(PAYLOAD)
    item = QueueItem("New", "gb", "release", 1, "grabbed", download_id="qbit-tag:190")
    app.queue.append(item)
    removed = []
    class Client:
        """A finished download with cleanup observable to this regression."""
        configured = True
        def completed(self):
            return [{"id": "qbit-tag:190", "name": "release", "content_path": str(incoming)}]
        def remove_download(self, *args, **kwargs):
            removed.append((args, kwargs))
    app.clients = [Client()]
    result = app.import_finished()
    assert not result[0]["ok"] and "gbc: gbc/retained.gbc" in result[0]["reason"]
    assert item.state == "import-failed" and not item.imported_paths and not removed
    assert incoming.read_bytes() == PAYLOAD
    assert not (root / "gb" / "new.gb").exists()


def test_staged_payload_is_checked_again_before_publication(tmp_path, monkeypatch):
    """A download replaced during staging cannot introduce an indexed duplicate."""
    from romarr import library
    root, _, index = retained(tmp_path)
    incoming = tmp_path / "new.gb"
    incoming.write_bytes(b"different incoming bytes")
    original = library._PathSource.copy
    def changed(self, name, destination):
        """Publish a different staged payload than the preflight inspected."""
        original(self, name, destination)
        destination.write_bytes(PAYLOAD)
    monkeypatch.setattr(library._PathSource, "copy", changed)
    result = import_rom(incoming, resolve("gb"), root, hash_index=index)[0]
    assert not result.ok and "already in the library" in result.reason
    assert not (root / "gb" / "new.gb").exists()
    assert not list((root / "gb").glob(".romarr-import-*"))


def test_duplicate_never_deletes_an_existing_empty_destination(tmp_path):
    """Reporting retained bytes must preserve unrelated empty destinations for review."""
    root, _, index = retained(tmp_path)
    incoming = tmp_path / "new.gb"
    incoming.write_bytes(PAYLOAD)
    empty = root / "gb" / "new.gb"
    empty.parent.mkdir()
    empty.touch()
    result = import_rom(incoming, resolve("gb"), root, hash_index=index)[0]
    assert not result.ok and empty.exists() and empty.stat().st_size == 0


def test_identical_members_in_one_batch_publish_only_one_copy(tmp_path):
    """The hash index need not be saved before a second selected member is checked."""
    root = tmp_path / "library"
    index = HashIndex(tmp_path / "hashes.json")
    incoming = tmp_path / "bundle.zip"
    with zipfile.ZipFile(incoming, "w") as zipped:
        zipped.writestr("one.gb", PAYLOAD)
        zipped.writestr("two.gb", PAYLOAD)
    results = import_rom(incoming, resolve("gb"), root, hash_index=index)
    assert sum(result.ok for result in results) == 1
    assert sum("already in the library" in result.reason for result in results) == 1
    assert len(list((root / "gb").glob("*.gb"))) == 1
    assert incoming.exists()


def test_scheduled_unknown_dump_enters_index_before_next_import(tmp_path):
    """The completed-download path indexes physical payloads even without DAT matches."""
    app, root = service(tmp_path)
    incoming = tmp_path / "first.gb"
    incoming.write_bytes(PAYLOAD)
    item = QueueItem("First", "gb", "release", 1, "grabbed", download_id="qbit-tag:191")
    app.queue.append(item)
    class Client:
        """Return the one scheduled completed download."""
        configured = True
        def completed(self):
            return [{"id": "qbit-tag:191", "name": "release", "content_path": str(incoming)}]
    app.clients = [Client()]
    assert app.import_finished()[0]["ok"]
    entry = app.hashes.by_sha1(digest())
    assert entry is not None and entry.path == "gb/first.gb"
    another = tmp_path / "second.gbc"
    another.write_bytes(PAYLOAD)
    assert not app.adopt(str(another), "gbc")["ok"]
