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


@pytest.mark.parametrize("compression", [zipfile.ZIP_DEFLATED, zipfile.ZIP_LZMA])
def test_corrupt_retained_compressed_member_is_not_a_duplicate(tmp_path, compression):
    """Actual decompressor failures in stale retained evidence must allow the new ROM."""
    import lzma
    import struct
    import zlib
    root, old, index = retained(tmp_path, archive=True)
    with zipfile.ZipFile(old, "w", compression=compression) as archive:
        archive.writestr("old-title.gbc", PAYLOAD)
    data = bytearray(old.read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", data, 26)
    start = 30 + name_size + extra_size
    if compression == zipfile.ZIP_DEFLATED:
        data[start] = 7  # A reserved DEFLATE block type.
        error_type = zlib.error
    else:
        data[start + 4] = 255  # Invalid LZMA filter properties.
        error_type = lzma.LZMAError
    old.write_bytes(data)
    with pytest.raises(error_type):
        with zipfile.ZipFile(old) as archive:
            archive.read("old-title.gbc")
    incoming = tmp_path / "new.gb"
    incoming.write_bytes(PAYLOAD)
    assert import_rom(incoming, resolve("gb"), root, hash_index=index)[0].ok


@pytest.mark.parametrize("comment_size", [0, 65535])
def test_zip64_locator_is_refused_before_zipfile_construction(tmp_path, monkeypatch, comment_size):
    """Small ordinary EOCD limits cannot authorize an overriding ZIP64 directory."""
    import struct
    from romarr import import_duplicates
    root, old, index = retained(tmp_path, archive=True)
    with zipfile.ZipFile(old, "a") as archive:
        archive.comment = b"x" * comment_size
    data = old.read_bytes()
    at = data.rfind(b"PK\x05\x06")
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    old.write_bytes(data[:at] + locator + data[at:])
    def forbidden(*args, **kwargs):
        """Any ZipFile allocation after this locator would violate the boundary."""
        pytest.fail("ZIP64 evidence reached ZipFile construction")
    monkeypatch.setattr(import_duplicates.zipfile, "ZipFile", forbidden)
    incoming = tmp_path / "new.gb"
    incoming.write_bytes(PAYLOAD)
    assert import_rom(incoming, resolve("gb"), root, hash_index=index)[0].ok


def test_relative_library_root_deduplicates_same_batch(tmp_path, monkeypatch):
    """Batch records must not apply the relative library prefix a second time."""
    from pathlib import Path
    monkeypatch.chdir(tmp_path)
    incoming = tmp_path / "bundle.zip"
    with zipfile.ZipFile(incoming, "w") as archive:
        archive.writestr("one.gb", PAYLOAD)
        archive.writestr("two.gb", PAYLOAD)
    result = import_rom(incoming, resolve("gb"), Path("library"),
                        hash_index=HashIndex(tmp_path / "index.json"))
    assert sum(item.ok for item in result) == 1
    assert len(list((tmp_path / "library" / "gb").glob("*.gb"))) == 1


def test_audit_keeps_physical_lookup_and_canonical_headered_identity(tmp_path):
    """An actual audit must not make a renamed byte-identical NES import invisible."""
    from romarr.dat import DatIndex
    app, root = service(tmp_path)
    data = b"NES\x1a" + b"\x00" * 12 + PAYLOAD
    existing = root / "nes" / "audited.nes"
    existing.parent.mkdir()
    existing.write_bytes(data)
    app.dats = DatIndex()
    started = app.audit_library("nes")
    assert "error" not in started
    app._audit_thread.join(timeout=5)
    assert not app._audit_thread.is_alive()
    canonical, physical = digest(PAYLOAD), digest(data)
    assert canonical != physical
    assert app.hashes.by_sha1(canonical).sha1 == canonical
    assert app.hashes.by_sha1(physical).sha1 == canonical
    assert app.hashes.for_game("audited", "nes").sha1 == canonical
    assert len(app.hashes) == 1
    reloaded = HashIndex(app.hashes.path)
    reloaded.load()
    assert reloaded.by_sha1(physical).sha1 == canonical
    class RefreshedLibrary:
        """A metadata seed omits physical hashes just as real library APIs do."""
        def hashes(self, limit=500, offset=0):
            return ([{"sha1": canonical, "name": "audited", "platform": "nes",
                      "verified": False, "path": "nes/audited.nes"}], 1) if not offset else ([], 0)
    app.library_server = RefreshedLibrary()
    assert app.index_hashes_from_library()["ok"]
    assert app.hashes.by_sha1(physical).sha1 == canonical
    assert app.hashes.for_game("audited", "nes").sha1 == canonical
    assert len(app.hashes) == 1
    after_seed = HashIndex(app.hashes.path)
    after_seed.load()
    assert after_seed.by_sha1(physical).sha1 == canonical
    incoming = tmp_path / "renamed.nes"
    incoming.write_bytes(data)
    assert not app.adopt(str(incoming), "nes")["ok"]
    assert not (root / "nes" / "renamed.nes").exists()
    # Matching canonical bodies with different physical headers are not byte duplicates.
    altered = tmp_path / "different-header.nes"
    altered.write_bytes(b"NES\x1a" + b"\x01" + b"\x00" * 11 + PAYLOAD)
    assert app.adopt(str(altered), "nes")["ok"]


def test_verified_import_preserves_dat_identity_and_adds_physical_lookup(tmp_path):
    """Publishing a headered dump must keep the canonical hash used for netplay."""
    from romarr.dat import DatIndex, parse_dat
    app, _ = service(tmp_path)
    app.dats = DatIndex()
    app.dats.add(parse_dat(f'<datafile><header><name>Fixture</name></header><game name="Known"><rom name="Known.nes" size="{len(PAYLOAD)}" sha1="{digest(PAYLOAD)}"/></game></datafile>'))
    data = b"NES\x1a" + b"\x00" * 12 + PAYLOAD
    incoming = tmp_path / "renamed.nes"
    incoming.write_bytes(data)
    assert app.adopt(str(incoming), "nes")["ok"]
    assert app.hashes.for_game("Known", "nes").sha1 == digest(PAYLOAD)
    assert app.hashes.by_sha1(digest(data)).sha1 == digest(PAYLOAD)
    assert len(app.hashes) == 1
