"""Manual import scans private snapshots and complete disc sets."""
from types import SimpleNamespace
import pytest
from romarr.app import ROMarr
from romarr import scanners


def service(tmp_path):
    """Create an isolated folder-backed service with scanning enabled."""
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                  "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(tmp_path / "roms")})
    svc.store.settings["scanner_defender_enabled"] = True
    return svc


def test_original_changes_after_scan_cannot_replace_imported_bytes(tmp_path, monkeypatch):
    """Scan and import use the same snapshot, including its original filename."""
    src = tmp_path / "Game.sfc"
    src.write_bytes(b"scanned")
    svc = service(tmp_path)
    def scan(settings, env, target):
        assert target.name == "Game.sfc"
        assert target != src
        assert target.read_bytes() == b"scanned"
        src.write_bytes(b"changed-after-scan")
        return [], []
    monkeypatch.setattr(scanners, "scan", scan)
    result = svc.adopt(str(src), "snes")
    assert result["ok"]
    files = list((tmp_path / "roms").rglob("*.sfc"))
    assert len(files) == 1 and files[0].read_bytes() == b"scanned"
    assert not list(tmp_path.glob(".manual-scan-*"))


def test_cue_tracks_are_scanned_before_any_import(tmp_path, monkeypatch):
    """A malicious referenced track refuses the whole selected disc set."""
    src = tmp_path / "Disc.cue"
    src.write_text('FILE "Track.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    (tmp_path / "Track.bin").write_bytes(b"unsafe")
    svc = service(tmp_path)
    def scan(settings, env, target):
        assert target.is_dir()
        assert (target / "Disc.cue").is_file()
        assert (target / "Track.bin").read_bytes() == b"unsafe"
        return [scanners.Finding(str(target / "Track.bin"), "fixture threat")], []
    monkeypatch.setattr(scanners, "scan", scan)
    result = svc.adopt(str(src), "psx")
    assert not result["ok"]
    assert "fixture threat" in result["refused"][0]["reason"]
    assert not list((tmp_path / "roms").rglob("*.bin"))


def test_defender_exit_two_without_detection_holds_import(tmp_path, monkeypatch):
    """Operational failure cannot be labeled as a malware detection."""
    src = tmp_path / "Game.sfc"
    src.write_bytes(b"fixture")
    monkeypatch.setattr(scanners, "defender_path", lambda: "MpCmdRun.exe")
    monkeypatch.setattr(scanners.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=2, stdout=b"Scan failed: 0x80070005", stderr=b""))
    with pytest.raises(scanners.ScanUnavailable, match="without explicit detection"):
        scanners.scan({"scanner_defender_enabled": True}, {}, src)
