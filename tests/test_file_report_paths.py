"""Adversarial library path replacements must never read or move outside bytes."""
import contextlib
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from romarr import file_reports as reports


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    """Use tiny source/outside files and deferred reviews for deterministic races."""
    root = tmp_path / "library"
    (root / "snes").mkdir(parents=True)
    (root / "snes/game.rom").write_bytes(b"reviewed fixture")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "game.rom").write_bytes(b"outside private fixture")
    seen = []
    monkeypatch.setattr(reports.scanners, "defender_path", lambda: "")
    def scanner(cfg, env, path):
        """Capture snapshot bytes without contacting a real antivirus service."""
        seen.append(path.read_bytes())
        return [], []
    monkeypatch.setattr(reports.scanners, "scan", scanner)
    service = reports.FileReports(lambda: root, tmp_path / "data", lambda: {"clamav_enabled": True}, {})
    pending = []
    def thread(*, target, args, **kwargs):
        """Keep the real review function but run it only when the test requests it."""
        return SimpleNamespace(start=lambda: pending.append(lambda: target(*args)))
    monkeypatch.setattr(reports.threading, "Thread", thread)
    return service, root, outside, seen, pending


def review(fixture):
    """Complete one authentic queued report against its originally recorded root."""
    service, root, outside, seen, pending = fixture
    row = service.submit("snes/game.rom", "malware")
    pending.pop(0)()
    return service._read(row["id"])


def link(path, target, directory=False):
    """Create test links only where supported, preserving Windows privilege skips."""
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip("Creating test symlinks requires filesystem support/Windows privilege: " + str(exc))


def swap_directory(path, replacement):
    """Replace a directory unless Windows correctly blocks renaming the pinned handle."""
    saved = path.with_name(path.name + "-saved")
    try:
        path.rename(saved)
    except PermissionError:
        return False
    link(path, replacement, True)
    return True


@pytest.mark.parametrize("replacement_link", [False, True])
def test_root_replaced_after_submission_is_not_rebased(fixture, replacement_link):
    service, root, outside, seen, pending = fixture
    row = service.submit("snes/game.rom", "malware")
    assert service._read(row["id"])["_root_id"] == [root.stat().st_dev, root.stat().st_ino]
    root.rename(root.with_name("original-library"))
    if replacement_link:
        link(root, outside, True)
    else:
        (root / "snes").mkdir(parents=True)
        (root / "snes/game.rom").write_bytes(b"outside private fixture")
    pending.pop(0)()
    assert service._read(row["id"])["status"] == "review-failed"
    assert not seen
    assert (outside / "game.rom").read_bytes() == b"outside private fixture"


def test_parent_link_before_snapshot_is_refused(fixture):
    service, root, outside, seen, pending = fixture
    row = service.submit("snes/game.rom", "malware")
    swap_directory(root / "snes", outside)
    pending.pop(0)()
    assert service._read(row["id"])["status"] == "review-failed"
    assert not seen


@pytest.mark.parametrize("replace_root", [False, True])
def test_directory_replacement_during_snapshot_never_scans_outside(fixture, monkeypatch, replace_root):
    service, root, outside, seen, pending = fixture
    row = service.submit("snes/game.rom", "malware")
    original = reports._PinnedLibrary.open
    replaced = []
    @contextlib.contextmanager
    def raced(self, location):
        """Swap a pinned path after the secure source descriptor has opened."""
        with original(self, location) as source:
            if location[2] == "game.rom" and not replaced:
                replaced.append(swap_directory(root if replace_root else root / "snes", outside))
            yield source
    monkeypatch.setattr(reports._PinnedLibrary, "open", raced)
    pending.pop(0)()
    result = service._read(row["id"])
    assert seen == ([] if replaced[0] else [b"reviewed fixture"])
    assert result["status"] == ("review-failed" if replaced[0] else "no-detections")
    assert (outside / "game.rom").read_bytes() == b"outside private fixture"


def test_final_link_swap_before_secure_open_does_not_read_target(fixture, monkeypatch):
    service, root, outside, seen, pending = fixture
    row = service.submit("snes/game.rom", "malware")
    original = reports._PinnedLibrary.open
    @contextlib.contextmanager
    def raced(self, location):
        """Change the final name after parent validation but before its nofollow open."""
        path = root / "snes/game.rom"
        if path.exists() and not path.is_symlink():
            path.rename(path.with_name("saved.rom"))
            link(path, outside / "game.rom")
        with original(self, location) as source:
            yield source
    monkeypatch.setattr(reports._PinnedLibrary, "open", raced)
    pending.pop(0)()
    assert service._read(row["id"])["status"] == "review-failed"
    assert not seen
    assert (outside / "game.rom").read_bytes() == b"outside private fixture"


def test_content_mutation_during_snapshot_is_not_sent_to_scanners(fixture, monkeypatch):
    service, root, outside, seen, pending = fixture
    row = service.submit("snes/game.rom", "malware")
    original = reports._fd_identity
    calls = []
    def mutate(source):
        """Mutate after copying but before the second descriptor observation."""
        calls.append(1)
        if len(calls) == 2:
            try:
                (root / "snes/game.rom").write_bytes(b"changed fixture contents")
            except PermissionError:
                pass  # Windows final read handle correctly denies concurrent writes.
        return original(source)
    monkeypatch.setattr(reports, "_fd_identity", mutate)
    pending.pop(0)()
    changed = (root / "snes/game.rom").read_bytes() != b"reviewed fixture"
    assert seen == ([] if changed else [b"reviewed fixture"])
    assert service._read(row["id"])["status"] == ("review-failed" if changed else "no-detections")


@pytest.mark.parametrize("action", ["quarantine", "restore"])
def test_root_replacement_blocks_both_mutations(fixture, action):
    service, root, outside, seen, pending = fixture
    row = review(fixture)
    if action == "restore":
        service.quarantine(row["id"])
    root.rename(root.with_name("original-library"))
    (root / "snes").mkdir(parents=True)
    (root / "snes/game.rom").write_bytes(b"reviewed fixture")
    with pytest.raises((ValueError, OSError)):
        getattr(service, action)(row["id"])
    assert (root / "snes/game.rom").read_bytes() == b"reviewed fixture"


@pytest.mark.parametrize("action", ["quarantine", "restore"])
@pytest.mark.parametrize("root_swap", [False, True])
def test_parent_swap_at_rename_never_moves_outside_known_hash_bytes(fixture, monkeypatch, action, root_swap):
    service, root, outside, seen, pending = fixture
    row = review(fixture)
    if action == "restore":
        service.quarantine(row["id"])
    # Even an outside file with the reviewed SHA must remain entirely untouched.
    (outside / "game.rom").write_bytes(b"reviewed fixture")
    if root_swap:
        (outside / "snes").mkdir()
        (outside / "snes/game.rom").write_bytes(b"reviewed fixture")
    outside_before = {p.relative_to(outside).as_posix(): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
    original = reports._move_no_replace
    swapped = []
    def raced(*args, **kwargs):
        """Race after validation but before the atomic dirfd-relative system call."""
        if not swapped:
            swapped.append(swap_directory(root if root_swap else root / "snes", outside))
        return original(*args, **kwargs)
    monkeypatch.setattr(reports, "_move_no_replace", raced)
    if reports.sys.platform != "win32":
        with pytest.raises(ValueError, match="conflict"):
            getattr(service, action)(row["id"])
        assert service._read(row["id"])["action_conflict"]
    else:
        getattr(service, action)(row["id"])
        assert swapped == [False]  # Native directory handles deny parent rename.
    assert (outside / "game.rom").read_bytes() == b"reviewed fixture"
    assert {p.relative_to(outside).as_posix(): p.read_bytes() for p in outside.rglob("*") if p.is_file()} == outside_before


@pytest.mark.parametrize("action", ["quarantine", "restore"])
def test_final_symlink_race_moves_link_itself_and_preserves_unproven_bytes(fixture, monkeypatch, action):
    service, root, outside, seen, pending = fixture
    row = review(fixture)
    if action == "restore":
        service.quarantine(row["id"])
    original = reports._move_no_replace
    source_path = root / (".romarr-file-quarantine/" + row["id"] if action == "restore" else "snes/game.rom")
    saved = source_path.with_name(source_path.name + "-saved")
    def raced(*args, **kwargs):
        """Replace only the final source name immediately before rename."""
        source_path.rename(saved)
        link(source_path, outside / "game.rom")
        return original(*args, **kwargs)
    monkeypatch.setattr(reports, "_move_no_replace", raced)
    with pytest.raises(ValueError, match="conflict"):
        getattr(service, action)(row["id"])
    result = service._read(row["id"])
    assert result["action_conflict"] and not result["quarantined"]
    assert saved.read_bytes() == b"reviewed fixture"
    assert (outside / "game.rom").read_bytes() == b"outside private fixture"
    with pytest.raises(ValueError):
        service.restore(row["id"])


def test_final_regular_file_race_does_not_inherit_reviewed_evidence(fixture, monkeypatch):
    service, root, outside, seen, pending = fixture
    row = review(fixture)
    original = reports._move_no_replace
    saved = root / "snes/saved.rom"
    def raced(*args, **kwargs):
        """Replace the checked source with different ordinary bytes at the last instant."""
        (root / "snes/game.rom").rename(saved)
        (root / "snes/game.rom").write_bytes(b"unreviewed replacement")
        return original(*args, **kwargs)
    monkeypatch.setattr(reports, "_move_no_replace", raced)
    with pytest.raises(ValueError, match="conflict"):
        service.quarantine(row["id"])
    assert saved.read_bytes() == b"reviewed fixture"
    assert (root / ".romarr-file-quarantine" / row["id"]).read_bytes() == b"unreviewed replacement"
    assert service._read(row["id"])["action_conflict"]


def test_destination_race_never_overwrites_either_file(fixture, monkeypatch):
    service, root, outside, seen, pending = fixture
    row = review(fixture)
    destination = root / ".romarr-file-quarantine" / row["id"]
    original = reports._move_no_replace
    def raced(*args, **kwargs):
        """Create a new destination after the caller's absence check."""
        destination.write_bytes(b"keep raced destination")
        return original(*args, **kwargs)
    monkeypatch.setattr(reports, "_move_no_replace", raced)
    with pytest.raises(OSError):
        service.quarantine(row["id"])
    assert destination.read_bytes() == b"keep raced destination"
    assert (root / "snes/game.rom").read_bytes() == b"reviewed fixture"


@pytest.mark.parametrize("replace_root", [False, True])
def test_interrupted_recovery_never_hashes_a_replaced_path(fixture, monkeypatch, replace_root):
    service, root, outside, seen, pending = fixture
    row = review(fixture)
    service.quarantine(row["id"])
    row = service._read(row["id"])
    row["quarantine_state"] = "moving"
    service._save(row)
    if replace_root:
        root.rename(root.with_name("original-library"))
        link(root, outside, True)
    else:
        held = root / ".romarr-file-quarantine" / row["id"]
        held.rename(held.with_name("saved"))
        link(held, outside / "game.rom")
    def forbidden(source):
        """An unsafe recovery path must fail before reading any bytes."""
        raise AssertionError("Recovery attempted to hash a replaced path")
    monkeypatch.setattr(reports, "_digest", forbidden)
    assert service._read(row["id"])["quarantine_state"] == "moving"
    assert (outside / "game.rom").read_bytes() == b"outside private fixture"


def test_limits_exceeded_is_incomplete_not_a_malware_detection(fixture, monkeypatch):
    def limited(*args):
        """A real ClamAV limit heuristic reports incomplete scanning, not malware."""
        return [reports.scanners.Finding("snapshot", "Heuristics.Limits.Exceeded.MaxFileSize")], []
    monkeypatch.setattr(reports.scanners, "scan", limited)
    result = review(fixture)
    assert result["status"] == "needs-review"
    assert result["scanners"][0]["status"] == "incomplete"
    assert "not malware" in result["scanners"][0]["detail"]


def test_windows_parent_aliases_are_rejected_before_open(monkeypatch):
    """DOS normalization must not reinterpret a trailing-space component as traversal."""
    monkeypatch.setattr(reports.sys, "platform", "win32")
    for path in ("snes/.. /secret", "snes/game.rom.", "snes/game.rom "):
        with pytest.raises(ValueError, match="aliases"):
            reports._parts(path)


@pytest.mark.parametrize("signature,status", [("Eicar-Test-Signature", "antivirus-test-fixture"), ("Known.Threat", "detection-reported")])
def test_scan_limits_do_not_erase_real_evidence_or_turn_eicar_into_malware(fixture, monkeypatch, signature, status):
    """Keep genuine signatures distinct from scan-limit heuristics in mixed results."""
    def limited(*args):
        return [reports.scanners.Finding("snapshot", signature), reports.scanners.Finding("snapshot", "Heuristics.Limits.Exceeded.MaxScanSize")], []
    monkeypatch.setattr(reports.scanners, "scan", limited)
    result = review(fixture)
    assert result["status"] == status
    engine = result["scanners"][0]
    assert engine["signatures"] == [signature]
    assert engine["incomplete"] is True
    assert engine["limit_signatures"] == ["Heuristics.Limits.Exceeded.MaxScanSize"]
