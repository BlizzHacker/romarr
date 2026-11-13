"""Proof of package verification, extraction boundaries and import scan gates."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from romarr import emulators, scanners
from romarr.app import ROMarr


class Response:
    """Context-managed release or package response."""
    def __init__(self, data=None, payload=b""):
        self.data = data
        self.payload = payload
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def json(self):
        return self.data

    def iter_content(self, _):
        yield self.payload


class ReleaseServer:
    """Serve a published release and its exact byte payload."""
    def __init__(self, package):
        self.package = package
        self.digest = hashlib.sha256(package).hexdigest()

    def get(self, url, **kwargs):
        if url.startswith("https://api.github.com/"):
            return Response({"tag_name": "v1", "assets": [{
                "name": "duckstation-linux-x64.zip", "size": len(self.package),
                "digest": "sha256:" + self.digest,
                "browser_download_url": "https://github.com/stenzek/duckstation/releases/download/v1/duckstation-linux-x64.zip",
            }]})
        return Response(payload=self.package)


def package(name="emulator", body=b"portable payload"):
    """Create a tiny portable archive including a test executable."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(name, body)
    return data.getvalue()


def test_verified_install_scans_archive_and_extracted_files(tmp_path, monkeypatch):
    monkeypatch.setattr(emulators.platform, "system", lambda: "Linux")
    monkeypatch.setattr(emulators.platform, "machine", lambda: "x86_64")
    remote = ReleaseServer(package())
    hub = emulators.EmulatorHub(tmp_path, remote)
    scanned = []
    def scan(path):
        """Record pre-publication scanner calls."""
        scanned.append(path.is_file())
    result = hub.install("duckstation", "duckstation-linux-x64.zip", remote.digest, scan)
    installed = Path(result["path"])
    assert (installed / "emulator").read_bytes() == b"portable payload"
    assert scanned == [True, False]
    assert hub.catalog()["items"][1]["installed"]
    assert hub.install("duckstation", "duckstation-linux-x64.zip", remote.digest)["already_installed"]


@pytest.mark.parametrize("failure", ["checksum", "traversal", "scanner"])
def test_failed_emulator_install_never_publishes_partial_build(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(emulators.platform, "system", lambda: "Linux")
    monkeypatch.setattr(emulators.platform, "machine", lambda: "x86_64")
    remote = ReleaseServer(package("../escaped" if failure == "traversal" else "emulator"))
    hub = emulators.EmulatorHub(tmp_path, remote)
    if failure == "checksum":
        remote.package = b"changed"
    def scan(path):
        """Reject a package when the antivirus cannot complete."""
        if failure == "scanner":
            raise scanners.ScanUnavailable("Scanner offline")
    with pytest.raises((ValueError, scanners.ScanUnavailable)):
        hub.install("duckstation", "duckstation-linux-x64.zip", remote.digest, scan)
    assert not list((hub.root / "duckstation").glob("*/romarr-install.json"))
    assert not list((hub.root / "duckstation").glob(".install-*"))
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("system,arch,name,wanted", [
    ("Linux", "x86_64", "Lichii-linux-aarch64.AppImage", False),
    ("Linux", "x86_64", "Lichii-linux-x86_64.AppImage", True),
    ("Windows", "AMD64", "duckstation-windows-arm64-release.zip", False),
    ("Windows", "AMD64", "duckstation-windows-x64-release.zip", True),
])
def test_release_selection_matches_host_architecture(monkeypatch, system, arch, name, wanted):
    monkeypatch.setattr(emulators.platform, "system", lambda: system)
    monkeypatch.setattr(emulators.platform, "machine", lambda: arch)
    assert emulators._matches_host(name) is wanted


def test_manual_import_waits_when_enabled_scanner_is_unavailable(tmp_path, monkeypatch):
    source = tmp_path / "Game.sfc"
    source.write_bytes(b"fixture")
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(tmp_path / "roms")})
    service.store.settings["scanner_defender_enabled"] = True
    monkeypatch.setattr(scanners, "defender_path", lambda: "")
    result = service.adopt(str(source), "snes")
    assert not result["ok"]
    assert "waiting" in result["reason"]
    assert source.read_bytes() == b"fixture"
    assert not (tmp_path / "roms" / "snes").exists()


def test_defender_threat_is_returned_without_remediation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    target = tmp_path / "Game.sfc"
    target.write_bytes(b"fixture")
    monkeypatch.setattr(scanners, "defender_path", lambda: "MpCmdRun.exe")
    commands = []
    def run(command, **kwargs):
        """Return Defender's documented threat result."""
        commands.append(command)
        return SimpleNamespace(returncode=2, stdout=b"Threat Name: Test:Fixture\n", stderr=b"")
    monkeypatch.setattr(scanners.subprocess, "run", run)
    findings, _ = scanners.scan({"scanner_defender_enabled": True}, {}, target)
    assert findings
    assert "-DisableRemediation" in commands[0]
    assert target.exists()


def test_mwm_static_only_response_does_not_clear_antivirus_gate(tmp_path, monkeypatch):
    target = tmp_path / "Game.sfc"
    target.write_bytes(b"fixture")
    monkeypatch.setattr(scanners, "mwm_call", lambda *a: "ClamAV is not installed - static triage only.")
    with pytest.raises(scanners.ScanUnavailable):
        scanners.scan({"scanner_mwm_enabled": True}, {}, target)


def test_mwm_and_arkana_use_documented_inspection_commands(tmp_path, monkeypatch):
    target = tmp_path / "Game.sfc"
    target.write_bytes(b"fixture")
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    calls = []
    def call(settings, env, command, args):
        """Record explicit lab inspection calls."""
        calls.append((command, args))
        return {"score": 0} if command == "lab_analyze" else 42
    monkeypatch.setattr(scanners, "mwm_call", call)
    assert service.security_inspect(str(target), "mwm")["report"]["score"] == 0
    assert service.security_inspect(str(target), "arkana")["report"] == 42
    assert [c[0] for c in calls] == ["lab_analyze", "arkana_analyze"]


def test_release_rate_limit_falls_back_to_published_manifest(tmp_path):
    """A public API rate limit still offers a pinned, digest-verified release."""
    import requests
    class LimitedServer:
        def get(self, *args, **kwargs):
            raise requests.HTTPError("API rate limit")
    release = emulators.EmulatorHub(tmp_path, LimitedServer()).release("melonds")
    assert release["cached_release"]
    assert release["tag"]
    assert any(a["sha256"] and a["installable"] for a in release["assets"])
    assert "verified catalog" in release["note"]


@pytest.mark.parametrize("status,stats,blocked,infected", [
    (404, {}, True, False),
    (429, {}, True, False),
    (200, {}, True, False),
    (200, {"undetected": 12}, False, False),
    (200, {"malicious": 1}, False, True),
])
def test_virustotal_uses_hash_only_and_holds_unknown_results(tmp_path, status, stats, blocked, infected):
    """Never upload bytes or clear an import without a usable hash report."""
    target = tmp_path / "private-name.sfc"
    target.write_bytes(b"synthetic fixture")
    class HashService:
        def get(self, url, **kwargs):
            assert url == "https://www.virustotal.com/api/v3/files/" + hashlib.sha256(target.read_bytes()).hexdigest()
            assert kwargs["headers"] == {"x-apikey": "fixture-token"}
            assert "files" not in kwargs and "data" not in kwargs
            response = Response({"data": {"attributes": {"last_analysis_stats": stats}}})
            response.status_code = status
            return response
    settings = {"scanner_vt_enabled": True}
    env = {"ROMARR_VIRUSTOTAL_API_KEY": "fixture-token"}
    if blocked:
        with pytest.raises(scanners.ScanUnavailable):
            scanners.scan(settings, env, target, HashService())
    else:
        findings, _ = scanners.scan(settings, env, target, HashService())
        assert bool(findings) is infected


def test_clamav_size_limit_holds_import_for_a_complete_scan(tmp_path, monkeypatch):
    """A file refused by ClamAV must not be silently imported unscanned."""
    target = tmp_path / "large.nsp"
    target.write_bytes(b"fixture")
    monkeypatch.setattr(scanners.clamav, "scan", lambda *args: ([], [str(target)]))
    with pytest.raises(scanners.ScanUnavailable, match="size limit"):
        scanners.scan({"clamav_enabled": True, "clamav_address": "tcp://fixture:3310"}, {}, target)
