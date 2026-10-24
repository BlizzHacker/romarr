from __future__ import annotations

import hashlib
import json

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
