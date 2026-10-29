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


def test_race_after_link_never_removes_a_replaced_source(tmp_path, monkeypatch):
    library, _, source, index, _ = fixture(tmp_path)
    preview = plan(library, "nes", index)
    real_link = rename_module.os.link

    def replace_after_link(src, dst):
        real_link(src, dst)
        Path(src).write_bytes(b"new user data")

    monkeypatch.setattr(rename_module.os, "link", replace_after_link)
    with pytest.raises(ValueError, match="changed during rename"):
        apply(library, "nes", ["nes/archive.nes"], preview["entries"])
    assert source.read_bytes() == b"new user data"
    assert not (library / "nes/Canonical Game (USA).nes").exists()


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
    restored = service.rollback_library_renames(result["manifest_id"])
    assert restored["ok"] and source.read_bytes() == body
    assert service.hashes.by_sha1(index.sha1).path == "nes/archive.nes"


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
    assert "library path changed" in result["error"]
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
