from __future__ import annotations

import hashlib
import json
import os

import romarr.quarantine as quarantine_module

import pytest

from romarr.quarantine import manifests, preview, quarantine, restore


def _fixture(tmp_path):
    library = tmp_path / "library"
    platform = library / "snes"
    platform.mkdir(parents=True)
    body = b"identical verified rom"
    keeper = platform / "Game (USA).zip"
    duplicate = platform / "copies" / "Game backup.zip"
    duplicate.parent.mkdir()
    keeper.write_bytes(body)
    duplicate.write_bytes(body)
    sha = hashlib.sha1(body).hexdigest()
    rows = [{"file": "snes/copies/Game backup.zip",
             "same_as": "snes/Game (USA).zip", "sha1": sha,
             "size": len(body)}]
    return library, platform, keeper, duplicate, rows, sha


def test_duplicate_preview_shows_keeper_and_recoverable_bytes():
    rows = [{"file": "snes/copy.zip", "same_as": "snes/game.zip",
             "sha1": "a" * 40, "size": 123}]
    got = preview(rows)
    assert got["groups"] == [{
        "keeper": "snes/game.zip", "files": [
            {"file": "snes/copy.zip", "sha1": "a" * 40, "size": 123}],
        "recoverable_bytes": 123}]


def test_quarantine_rehashes_both_files_moves_atomically_and_persists(
        tmp_path):
    library, platform, keeper, duplicate, rows, sha = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    assert result["ok"]
    assert keeper.exists()
    assert not duplicate.exists()
    entry = result["entries"][0]
    quarantined = library / ".romarr-quarantine" / entry["object"]
    assert quarantined.read_bytes() == keeper.read_bytes()
    recorded = manifests(library)
    assert len(recorded) == 1
    assert recorded[0]["entries"][0]["sha1"] == sha
    assert recorded[0]["entries"][0]["status"] == "quarantined"


def test_quarantine_refuses_a_file_changed_after_the_audit(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    duplicate.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed since audit"):
        quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    assert duplicate.exists() and keeper.exists()


def test_quarantine_refuses_a_keeper_changed_after_the_audit(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    keeper.write_bytes(b"changed keeper")
    with pytest.raises(ValueError, match="changed since audit"):
        quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    assert duplicate.exists() and keeper.exists()


def test_quarantine_refuses_a_path_outside_the_audited_platform(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    rows[0]["file"] = "../outside"
    with pytest.raises(ValueError):
        quarantine(library, platform, [{"file": "../outside"}], rows)
    assert duplicate.exists()


def test_restore_checks_collisions_and_never_overwrites(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    target = platform / entry["original"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"user file")
    with pytest.raises(ValueError, match="already exists"):
        restore(library, result["manifest_id"], entry["id"])
    assert target.read_bytes() == b"user file"


def test_quarantined_file_can_be_restored_and_survives_restart(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    restored = restore(library, result["manifest_id"], entry["id"])
    assert restored["ok"]
    assert (platform / entry["original"]).read_bytes() == keeper.read_bytes()
    assert manifests(library)[0]["entries"][0]["status"] == "restored"


def test_restore_refuses_a_modified_quarantine_object(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    quarantined = library / ".romarr-quarantine" / entry["object"]
    quarantined.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash changed"):
        restore(library, result["manifest_id"], entry["id"])
    assert not (platform / entry["original"]).exists()


def test_atomic_manifest_cleans_up_a_failed_write(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text('{"old": true}')

    def fail_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(quarantine_module.json, "dump", fail_dump)
    with pytest.raises(OSError, match="disk full"):
        quarantine_module._atomic_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_manifest_is_durable_before_a_file_move(tmp_path, monkeypatch):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    real_replace = os.replace
    events = []

    def record_sync(folder):
        events.append(("sync", folder))

    def record_replace(source, destination):
        events.append(("replace", source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(quarantine_module, "_fsync_dir", record_sync)
    monkeypatch.setattr(quarantine_module.os, "replace", record_replace)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    move = next(n for n, event in enumerate(events)
                if event[0] == "replace" and event[1] == duplicate)
    assert ("sync", library / ".romarr-quarantine") in events[:move]
    object_parent = library / ".romarr-quarantine" / result["manifest_id"]
    assert events[move + 1:move + 3] == [
        ("sync", object_parent), ("sync", duplicate.parent)]


@pytest.mark.parametrize("changed", ["candidate", "keeper"])
def test_quarantine_rechecks_files_after_manifest_creation(
        tmp_path, monkeypatch, changed):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    real_atomic = quarantine_module._atomic_json
    changed_file = duplicate if changed == "candidate" else keeper
    written = False

    def mutate_after_validation(path, data):
        nonlocal written
        real_atomic(path, data)
        if not written:
            written = True
            changed_file.write_bytes(b"changed after selection validation")

    monkeypatch.setattr(quarantine_module, "_atomic_json", mutate_after_validation)
    with pytest.raises(ValueError, match="changed since audit"):
        quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    assert duplicate.exists() and keeper.exists()
    assert manifests(library)[0]["status"] == "interrupted"


def _edit_manifest(library, result, edit):
    path = library / ".romarr-quarantine" / ("manifest-" + result["manifest_id"] + ".json")
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("object_path", ["../outside", "/tmp/outside", "other/file"])
def test_restore_rejects_object_paths_outside_its_manifest(
        tmp_path, object_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    _edit_manifest(library, result,
                   lambda data: data["entries"][0].update(object=object_path))
    with pytest.raises(ValueError, match="unsafe quarantine object"):
        restore(library, result["manifest_id"], entry["id"])
    assert not duplicate.exists()


def test_restore_rejects_symlinked_object_directory(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    folder = library / ".romarr-quarantine" / result["manifest_id"]
    moved_folder = tmp_path / "outside"
    folder.rename(moved_folder)
    folder.symlink_to(moved_folder, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        restore(library, result["manifest_id"], entry["id"])
    assert not duplicate.exists()


def test_restore_never_overwrites_a_file_created_during_restore(tmp_path, monkeypatch):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    real_link = os.link

    def create_collision(source, destination):
        destination.write_bytes(b"concurrent user file")
        return real_link(source, destination)

    monkeypatch.setattr(quarantine_module.os, "link", create_collision)
    with pytest.raises(ValueError, match="already exists"):
        restore(library, result["manifest_id"], entry["id"])
    assert duplicate.read_bytes() == b"concurrent user file"
    source = library / ".romarr-quarantine" / entry["object"]
    assert source.read_bytes() == keeper.read_bytes()


def test_restore_recovers_a_crash_after_the_new_name_is_durable(tmp_path, monkeypatch):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    real_sync = quarantine_module._fsync_dir

    def interrupt_after_link(folder):
        real_sync(folder)
        if folder == duplicate.parent:
            raise OSError("simulated crash after link")

    monkeypatch.setattr(quarantine_module, "_fsync_dir", interrupt_after_link)
    with pytest.raises(OSError, match="simulated crash"):
        restore(library, result["manifest_id"], entry["id"])
    source = library / ".romarr-quarantine" / entry["object"]
    assert source.exists() and duplicate.exists()
    monkeypatch.setattr(quarantine_module, "_fsync_dir", real_sync)
    assert restore(library, result["manifest_id"], entry["id"])["already_restored"]
    assert duplicate.read_bytes() == keeper.read_bytes()
    assert not source.exists()
    assert manifests(library)[0]["status"] == "restored"


def test_quarantine_hashes_the_complete_file_including_copier_headers(tmp_path):
    library, platform, keeper, duplicate, rows, _ = _fixture(tmp_path)
    keeper = keeper.with_suffix(".smc")
    duplicate = duplicate.with_suffix(".smc")
    body = b"header" + b"\0" * 506 + b"R" * 32768
    keeper.write_bytes(body)
    duplicate.write_bytes(body)
    rows = [{"file": "snes/" + str(duplicate.relative_to(platform)),
             "same_as": "snes/" + keeper.name,
             "sha1": hashlib.sha1(body).hexdigest(), "size": len(body)}]
    result = quarantine(library, platform, [{"file": rows[0]["file"]}], rows)
    entry = result["entries"][0]
    assert restore(library, result["manifest_id"], entry["id"])["ok"]
    assert duplicate.read_bytes() == body
