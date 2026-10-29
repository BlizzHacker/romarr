from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from romarr.dat import BAD_DUMP, UNKNOWN, VERIFIED, Game, Match, Rom
import romarr.library_rename as rename_module
from romarr.hashes import HashIndex
from romarr.library_rename import apply, manifests, plan, rollback
from romarr.app import ROMarr


class Index:
    def __init__(self, body, rom, *, copies=1, extra_roms=()):
        self.sha1 = hashlib.sha1(body).hexdigest()
        game = Game("Canonical Game (USA)", (rom, *extra_roms))
        self.dat = SimpleNamespace(name="No-Intro test", games={game.name: game})
        self.dats = [self.dat] * copies
        self.match = Match(VERIFIED, game.name, rom, self.dat.name)

    def lookup(self, **digest):
        if str(digest.get("sha1") or "") == self.sha1:
            return self.match
        return Match(UNKNOWN)


def fixture(tmp_path, name="archive.nes", canonical="Canonical Game (USA).nes",
            *, copies=1, extra_roms=()):
    library = tmp_path / "library"
    platform = library / "nes"
    platform.mkdir(parents=True)
    body = b"rom"
    source = platform / name
    source.write_bytes(body)
    rom = Rom(canonical, size=len(body), sha1=hashlib.sha1(body).hexdigest())
    return library, platform, source, Index(body, rom, copies=copies,
                                             extra_roms=extra_roms), body


def test_preview_proposes_only_exact_verified_single_file(tmp_path):
    library, _, source, index, _ = fixture(tmp_path)
    result = plan(library, "nes", index)
    row, = result["entries"]
    assert result["eligible"] == 1
    assert row["file"] == "nes/archive.nes"
    assert row["target"] == "nes/Canonical Game (USA).nes"


def test_apply_and_durable_rollback_preserve_bytes(tmp_path):
    library, _, source, index, body = fixture(tmp_path)
    preview = plan(library, "nes", index)
    result = apply(library, "nes", ["nes/archive.nes"], preview["entries"])
    target = library / "nes/Canonical Game (USA).nes"
    assert result["ok"] and target.read_bytes() == body and not source.exists()
    assert manifests(library)[0]["status"] == "complete"
    restored = rollback(library, result["manifest_id"])
    assert restored["ok"] and source.read_bytes() == body and not target.exists()
    assert manifests(library)[0]["status"] == "rolled_back"


def test_preview_refuses_unverified_disc_and_referenced_files(tmp_path):
    library, platform, source, index, body = fixture(tmp_path, name="disc.bin")
    cue = platform / "disc.cue"
    cue.write_text('FILE "disc.bin" BINARY\n')
    result = plan(library, "nes", index)
    row = next(x for x in result["entries"] if x["file"] == "nes/disc.bin")
    assert not row["eligible"]
    assert "refers" in row["reason"]
    iso = platform / "single.iso"
    iso.write_bytes(body)
    iso_row = next(x for x in plan(library, "nes", index)["entries"]
                   if x["file"] == "nes/single.iso")
    assert not iso_row["eligible"] and "Disc images" in iso_row["reason"]


def test_preview_refuses_ambiguous_dat_and_multifile_game(tmp_path):
    library, _, _, index, _ = fixture(tmp_path, copies=2)
    assert not plan(library, "nes", index)["entries"][0]["eligible"]
    library2, _, _, index2, body = fixture(tmp_path / "other",
                                             extra_roms=(Rom("track.bin", size=1),))
    row = plan(library2, "nes", index2)["entries"][0]
    assert not row["eligible"] and "multi-file" in row["reason"]


def test_preview_detects_case_insensitive_destination_collision(tmp_path):
    library, platform, _, index, body = fixture(tmp_path)
    (platform / "canonical game (usa).nes").write_bytes(b"someone else's ROM")
    row = plan(library, "nes", index)["entries"][0]
    assert not row["eligible"] and "case-insensitive" in row["reason"]


def test_apply_rechecks_hash_and_refuses_new_collision(tmp_path):
    library, platform, source, index, _ = fixture(tmp_path)
    preview = plan(library, "nes", index)
    source.write_bytes(b"changed after preview")
    with pytest.raises(ValueError, match="changed since preview"):
        apply(library, "nes", ["nes/archive.nes"], preview["entries"])
    source.write_bytes(b"rom")
    (platform / "Canonical Game (USA).nes").write_bytes(b"preserve me")
    with pytest.raises(ValueError, match="destination already exists"):
        apply(library, "nes", ["nes/archive.nes"], preview["entries"])
    assert source.read_bytes() == b"rom"
    assert (platform / "Canonical Game (USA).nes").read_bytes() == b"preserve me"


def test_rollback_refuses_to_overwrite_original_path(tmp_path):
    library, platform, source, index, _ = fixture(tmp_path)
    result = apply(library, "nes", ["nes/archive.nes"],
                   plan(library, "nes", index)["entries"])
    source.write_bytes(b"unrelated file")
    with pytest.raises(ValueError, match="restore collision"):
        rollback(library, result["manifest_id"])
    assert source.read_bytes() == b"unrelated file"
    assert (platform / "Canonical Game (USA).nes").exists()


def test_unsafe_dat_filenames_are_not_eligible(tmp_path):
    library, _, _, index, _ = fixture(tmp_path, canonical="CON.nes")
    row = plan(library, "nes", index)["entries"][0]
    assert not row["eligible"] and "not safe" in row["reason"]


def test_atomic_source_replacement_during_target_hash_preserves_both_files(
        tmp_path, monkeypatch):
    library, _, source, index, body = fixture(tmp_path)
    preview = plan(library, "nes", index)
    target = library / "nes/Canonical Game (USA).nes"
    real_raw_sha1 = rename_module.raw_sha1
    replaced = False

    def replace_source_during_target_hash(path):
        nonlocal replaced
        digest = real_raw_sha1(path)
        if Path(path) == target and not replaced:
            replacement = source.with_name("replacement.tmp")
            replacement.write_bytes(b"new user data")
            rename_module.os.replace(replacement, source)
            replaced = True
        return digest

    monkeypatch.setattr(rename_module, "raw_sha1", replace_source_during_target_hash)
    with pytest.raises(ValueError, match="source changed during rename"):
        apply(library, "nes", ["nes/archive.nes"], preview["entries"])
    assert replaced
    assert source.read_bytes() == b"new user data"
    assert target.read_bytes() == body


def test_service_preview_apply_rollback_and_hash_index_paths(tmp_path):
    library, _, source, index, body = fixture(tmp_path)
    service = ROMarr.__new__(ROMarr)
    service.library = library
    service.dats = index
    service.hashes = HashIndex(tmp_path / "hashes.json")
    service.hashes.add(index.sha1, "Canonical Game (USA)", "nes", True,
                       "nes/archive.nes")
    started = service.preview_library_renames("nes")
    assert started["status"] == "running"
    service._library_rename_thread.join(timeout=5)
    current = service.library_rename_state()["preview"]
    assert current["status"] == "done" and current["eligible"] == 1
    result = service.apply_library_renames("nes", ["nes/archive.nes"])
    assert result["ok"]
    assert service.hashes.by_sha1(index.sha1).path == "nes/Canonical Game (USA).nes"
    ROMarr._audit_state = {"status": "done", "platform": "nes"}
    restored = service.rollback_library_renames(result["manifest_id"])
    assert restored["ok"] and source.read_bytes() == body
    assert service.hashes.by_sha1(index.sha1).path == "nes/archive.nes"
    assert ROMarr._audit_state["status"] == "stale"
    assert ROMarr._audit_state["platform"] == "nes"


def test_service_apply_refuses_library_changed_after_preview(tmp_path):
    library, _, source, index, body = fixture(tmp_path)
    other = tmp_path / "other-library"
    (other / "nes").mkdir(parents=True)
    (other / "nes/archive.nes").write_bytes(body)
    service = ROMarr.__new__(ROMarr)
    service.library = library
    service.dats = index
    service.hashes = HashIndex(tmp_path / "hashes.json")
    service.preview_library_renames("nes")
    service._library_rename_thread.join(timeout=5)
    service.library = other
    result = service.apply_library_renames("nes", ["nes/archive.nes"])
    assert "changed since preview" in result["error"]
    assert source.exists() and (other / "nes/archive.nes").exists()
    assert not (other / "nes/Canonical Game (USA).nes").exists()


def test_preview_pages_and_scopes_large_libraries(tmp_path):
    library, platform, _, index, _ = fixture(tmp_path)
    nested = platform / "collection"
    nested.mkdir()
    (nested / "unknown.nes").write_bytes(b"unknown")
    first = plan(library, "nes", index, offset=0, page_size=1)
    second = plan(library, "nes", index, offset=1, page_size=1)
    assert first["has_more"] and first["entries"][0]["eligible"]
    assert second["offset"] == 1 and not second["entries"][0]["eligible"]
    scoped = plan(library, "nes", index, scope="collection")
    assert scoped["candidate_count"] == 1
    assert scoped["entries"][0]["file"] == "nes/collection/unknown.nes"
    with pytest.raises(ValueError, match="relative folder"):
        plan(library, "nes", index, scope="../outside")


def test_nested_verified_file_flattens_to_platform_root_and_rolls_back(tmp_path):
    library, platform, source, index, body = fixture(tmp_path)
    folder = platform / "old-folder"
    folder.mkdir()
    nested = folder / source.name
    source.rename(nested)
    preview = plan(library, "nes", index, scope="old-folder")
    row, = preview["entries"]
    assert row["target"] == "nes/Canonical Game (USA).nes"
    result = apply(library, "nes", [row["file"]], preview["entries"])
    target = platform / "Canonical Game (USA).nes"
    assert target.read_bytes() == body and not nested.exists()
    rollback(library, result["manifest_id"])
    assert nested.read_bytes() == body and not target.exists()


def test_headered_rom_journals_raw_hash_but_matches_headerless_dat(
        tmp_path, monkeypatch):
    import romarr.dat as dat_module

    library, _, source, index, body = fixture(tmp_path, name="headered.nes")
    source.write_bytes(b"H" * 16 + body)
    expected_dat_sha1 = hashlib.sha1(body).hexdigest()

    def headerless_hash(_path):
        return {"size": len(body), "crc": "00000000", "md5": "",
                "sha1": expected_dat_sha1}

    monkeypatch.setattr(dat_module, "hash_file", headerless_hash)
    preview = plan(library, "nes", index)
    row, = preview["entries"]
    assert row["eligible"]
    assert row["sha1"] == hashlib.sha1(source.read_bytes()).hexdigest()
    result = apply(library, "nes", [row["file"]], preview["entries"])
    target = library / row["target"]
    assert target.read_bytes() == b"H" * 16 + body
    restored = rollback(library, result["manifest_id"])
    assert restored["ok"] and source.read_bytes() == b"H" * 16 + body
    assert not target.exists()


def test_nested_layout_is_refused_before_a_preview_can_move_files(tmp_path):
    library, _, source, index, body = fixture(tmp_path)
    with pytest.raises(ValueError, match="flat library layouts"):
        plan(library, "nes", index, layout="nested")
    assert source.read_bytes() == body


def test_reference_manifest_read_error_fails_closed(tmp_path, monkeypatch):
    library, platform, source, index, body = fixture(tmp_path, name="disc.bin")
    cue = platform / "disc.cue"
    cue.write_text('FILE "disc.bin" BINARY\n')
    original_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == cue:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ValueError, match="Could not read reference manifest"):
        plan(library, "nes", index)
    assert source.read_bytes() == body


def test_preview_builds_one_sha1_index_for_all_files(tmp_path, monkeypatch):
    library, platform, _, index, _ = fixture(tmp_path)
    (platform / "second.nes").write_bytes(b"second")
    calls = 0
    original_index = rename_module._sha1_index

    def counted(dat_index):
        nonlocal calls
        calls += 1
        return original_index(dat_index)

    monkeypatch.setattr(rename_module, "_sha1_index", counted)
    plan(library, "nes", index, page_size=10)
    assert calls == 1


def test_rollback_preserves_identical_bytes_created_at_target_path(tmp_path):
    library, _, source, index, body = fixture(tmp_path)
    result = apply(library, "nes", ["nes/archive.nes"],
                   plan(library, "nes", index)["entries"])
    target = library / "nes/Canonical Game (USA).nes"
    source.write_bytes(body)
    target.unlink()
    target.write_bytes(body)
    with pytest.raises(ValueError, match="restore collision"):
        rollback(library, result["manifest_id"])
    assert source.read_bytes() == body and target.read_bytes() == body


def test_hash_index_normalizes_absolute_import_paths_for_library_moves(tmp_path):
    from romarr.hashes import HashIndex

    root = tmp_path / "library"
    index = HashIndex(tmp_path / "hashes.json")
    entry = index.add("a" * 40, "Game", "nes", True,
                      str(root / "nes/archive.nes"))
    assert index.move_paths({"nes/archive.nes": "nes/Game.nes"},
                            root=root) == 1
    assert entry.path == "nes/Game.nes"
    assert index.move_paths({"nes/Game.nes": "nes/archive.nes"},
                            root=root) == 1
    assert entry.path == "nes/archive.nes"


def test_service_rename_uses_platform_routed_library_and_rolls_back(tmp_path):
    default = tmp_path / "default"
    routed = tmp_path / "routed"
    for root, name in ((default, "default.nes"), (routed, "from-route.nes")):
        (root / "nes").mkdir(parents=True)
        (root / "nes" / name).write_bytes(b"rom")
    rom = Rom("Canonical Game (USA).nes", size=3,
              sha1=hashlib.sha1(b"rom").hexdigest())
    index = Index(b"rom", rom)
    cfg = {"id": "routed", "path": str(routed), "layout": "flat"}
    service = ROMarr.__new__(ROMarr)
    service.library = default
    service.game_libraries = [(cfg, object())]
    service.library_for = lambda _platform: (cfg, object())
    service.library_root = lambda item: Path(item["path"])
    service.library_layout = lambda item: item["layout"]
    service.dats = index
    service.hashes = HashIndex(tmp_path / "hashes.json")
    source = routed / "nes/from-route.nes"
    service.hashes.add(index.sha1, "Canonical Game (USA)", "nes", True,
                       str(source))

    service.preview_library_renames("nes")
    service._library_rename_thread.join(timeout=5)
    preview = service.library_rename_state()["preview"]
    assert preview["status"] == "done"
    assert [row["file"] for row in preview["entries"]] == ["nes/from-route.nes"]
    result = service.apply_library_renames("nes", ["nes/from-route.nes"])
    target = routed / "nes/Canonical Game (USA).nes"
    assert result["ok"] and target.read_bytes() == b"rom"
    assert (default / "nes/default.nes").read_bytes() == b"rom"
    assert service.hashes.by_sha1(index.sha1).path == "nes/Canonical Game (USA).nes"
    restored = service.rollback_library_renames(result["manifest_id"])
    assert restored["ok"] and source.read_bytes() == b"rom"
    assert service.hashes.by_sha1(index.sha1).path == "nes/from-route.nes"


def test_apply_and_rollback_share_a_journal_lock(tmp_path, monkeypatch):
    import threading

    library = tmp_path / "library"
    library.mkdir()
    apply_entered = threading.Event()
    release_apply = threading.Event()
    rollback_entered = threading.Event()
    ident_box = []

    def blocked_apply(_library, _platform, _selected, _planned, ident):
        ident_box.append(ident)
        apply_entered.set()
        assert release_apply.wait(3)
        return {"ok": True, "manifest_id": ident, "entries": []}

    def blocked_rollback(_library, _ident):
        rollback_entered.set()
        return {"ok": True, "entries": []}

    monkeypatch.setattr(rename_module, "_apply_locked", blocked_apply)
    monkeypatch.setattr(rename_module, "_rollback_locked", blocked_rollback)
    apply_thread = threading.Thread(
        target=apply, args=(library, "nes", ["nes/game.nes"], []))
    apply_thread.start()
    assert apply_entered.wait(1)
    rollback_thread = threading.Thread(
        target=rollback, args=(library, ident_box[0]))
    rollback_thread.start()
    assert not rollback_entered.wait(0.1)
    release_apply.set()
    apply_thread.join(2)
    rollback_thread.join(2)
    assert not apply_thread.is_alive() and not rollback_thread.is_alive()
    assert rollback_entered.is_set()

def test_service_preview_requires_at_least_one_loaded_dat(tmp_path):
    service = ROMarr.__new__(ROMarr)
    service.library = tmp_path / "library"
    service.dats = SimpleNamespace(dats=[])
    result = service.preview_library_renames("nes")
    assert "load DATs" in result["error"]
    assert getattr(service, "_library_rename_thread", None) is None



def test_manifests_returns_ten_newest_recorded_journals(tmp_path):
    import json

    library = tmp_path / "library"
    folder = library / rename_module.ROOT_NAME
    folder.mkdir(parents=True)
    for number in range(12):
        path = folder / f"manifest-{number:02}.json"
        path.write_text(json.dumps({
            "status": "complete", "created_at": f"2026-01-{number + 1:02}T00:00:00+00:00",
            "entries": []}), encoding="utf-8")
    result = manifests(library)
    assert len(result) == 10
    assert result[0]["manifest_id"] == "11"
    assert result[-1]["manifest_id"] == "02"


def test_preview_cap_explains_omitted_files_remain_unreachable_by_scope(
        tmp_path, monkeypatch):
    library, platform, _, index, _ = fixture(tmp_path)
    (platform / "another.nes").write_bytes(b"unknown")
    monkeypatch.setattr(rename_module, "MAX_FILES", 1)
    result = plan(library, "nes", index)
    assert result["truncated"] is True
    assert "beyond this preview remain untouched" in result["message"]
    assert "cannot reach omitted files elsewhere" in result["message"]


def test_service_partial_apply_reconciles_hashes_and_invalidates_audit(
        tmp_path, monkeypatch):
    import json

    library, _, source, index, _ = fixture(tmp_path)
    service = ROMarr.__new__(ROMarr)
    service.library = library
    service.dats = index
    service.hashes = HashIndex(tmp_path / "hashes.json")
    service.hashes.add(index.sha1, "Canonical Game (USA)", "nes", True,
                       "nes/archive.nes")
    service.preview_library_renames("nes")
    service._library_rename_thread.join(timeout=5)
    row = service.library_rename_state()["preview"]["entries"][0]
    target = library / row["target"]

    def interrupted_apply(root, platform_slug, selected, planned, *, manifest_id):
        source.rename(target)
        journal = root / rename_module.ROOT_NAME / f"manifest-{manifest_id}.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({
            "platform": platform_slug, "status": "interrupted",
            "entries": [{**row, "original": row["file"], "target": row["target"],
                         "status": "moved"}]}), encoding="utf-8")
        raise OSError("simulated interruption")

    monkeypatch.setattr(rename_module, "apply", interrupted_apply)
    ROMarr._audit_state = {"status": "done", "platform": "nes"}
    result = service.apply_library_renames("nes", ["nes/archive.nes"])
    assert "simulated interruption" in result["error"]
    assert service.hashes.by_sha1(index.sha1).path == row["target"]
    assert ROMarr._audit_state["status"] == "stale"
    assert target.exists() and not source.exists()


def test_service_partial_rollback_reconciles_restored_files_and_audit(
        tmp_path, monkeypatch):
    import json

    library, _, source, index, body = fixture(tmp_path)
    service = ROMarr.__new__(ROMarr)
    service.library = library
    service.dats = index
    service.hashes = HashIndex(tmp_path / "hashes.json")
    service.hashes.add(index.sha1, "Canonical Game (USA)", "nes", True,
                       "nes/archive.nes")
    service.preview_library_renames("nes")
    service._library_rename_thread.join(timeout=5)
    applied = service.apply_library_renames("nes", ["nes/archive.nes"])
    target = library / "nes/Canonical Game (USA).nes"
    entry = applied["entries"][0]

    def interrupted_rollback(root, manifest_id):
        target.rename(source)
        journal = root / rename_module.ROOT_NAME / f"manifest-{manifest_id}.json"
        data = json.loads(journal.read_text(encoding="utf-8"))
        data["entries"][0]["status"] = "rolled_back"
        data["status"] = "interrupted"
        journal.write_text(json.dumps(data), encoding="utf-8")
        raise OSError("simulated rollback interruption")

    monkeypatch.setattr(rename_module, "rollback", interrupted_rollback)
    ROMarr._audit_state = {"status": "done", "platform": "nes"}
    result = service.rollback_library_renames(applied["manifest_id"])
    assert "simulated rollback interruption" in result["error"]
    assert service.hashes.by_sha1(index.sha1).path == "nes/archive.nes"
    assert ROMarr._audit_state["status"] == "stale"
    assert source.read_bytes() == body and not target.exists()
