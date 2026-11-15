"""Report evidence, scope, publication consent and reversible file actions."""
import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from romarr import file_reports as reports


@pytest.fixture(autouse=True)
def no_real_host_scanner(monkeypatch):
    """Unit tests must never invoke the host's real-time antivirus."""
    monkeypatch.setattr(reports.scanners, "defender_path", lambda: "")


def manager(tmp_path, settings=None, env=None):
    """Create an isolated library and a manager without background boot work."""
    library = tmp_path / "library"
    (library / "snes").mkdir(parents=True)
    (library / "snes/game.sfc").write_bytes(b"ordinary fixture bytes")
    cfg = dict(settings or {})
    return reports.FileReports(lambda: library, tmp_path / "data", lambda: cfg, env or {})


def finished(service, ident):
    """Wait for a bounded fixture review without sleeping across long operations."""
    for _ in range(200):
        with service.lock:
            if ident not in service.active:
                return service._read(ident)
        time.sleep(.01)
    raise AssertionError("review did not finish")


def test_no_scanner_is_not_a_clean_verdict_and_state_survives(tmp_path):
    service = manager(tmp_path)
    r = finished(service, service.submit("snes/game.sfc", "malware", "local private note")["id"])
    assert r["status"] == "needs-review" and r["scanners"] == []
    assert r["sha256"] == hashlib.sha256(b"ordinary fixture bytes").hexdigest()
    fresh = reports.FileReports(service.library, tmp_path / "data", service.settings, {})
    public = fresh.list()["items"][0]
    assert "_library" not in public and public["id"] == r["id"]
    assert (service.library() / "snes/game.sfc").read_bytes() == b"ordinary fixture bytes"
    assert not list(service.directory.glob("scan-*"))


@pytest.mark.parametrize("path", ["../data/secret", "/etc/passwd", "C:\\secret", "", "a/../../secret"])
def test_report_path_is_confined_to_library(tmp_path, path):
    service = manager(tmp_path)
    with pytest.raises((ValueError, OSError)):
        service.submit(path, "malware")


def test_links_outside_library_are_refused(tmp_path):
    service = manager(tmp_path)
    external = tmp_path / "secret"; external.write_bytes(b"private")
    try: (service.library() / "linked").symlink_to(external)
    except OSError: pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError): service.submit("linked", "malware")


def test_positive_evidence_scans_snapshot_not_original(tmp_path, monkeypatch):
    service = manager(tmp_path, {"clamav_enabled": True})
    seen = []
    def scan(cfg, env, target):
        """Return a synthetic engine verdict without creating virus-test bytes."""
        seen.append(target)
        assert target.read_bytes() == b"ordinary fixture bytes"
        assert target != service.library() / "snes/game.sfc"
        return [reports.scanners.Finding(str(target), "Fixture.Signature")], []
    monkeypatch.setattr(reports.scanners, "scan", scan)
    r = finished(service, service.submit("snes/game.sfc", "malware")["id"])
    assert r["status"] == "detection-reported"
    assert r["scanners"][0]["signatures"] == ["Fixture.Signature"]
    assert not seen[0].exists() and (service.library() / "snes/game.sfc").exists()


def test_eicar_signature_is_identified_as_antivirus_test_evidence(tmp_path, monkeypatch):
    service = manager(tmp_path, {"clamav_enabled": True})
    monkeypatch.setattr(reports.scanners, "scan", lambda *a: ([reports.scanners.Finding("fixture", "Eicar-Test-Signature")], []))
    r = finished(service, service.submit("snes/game.sfc", "false-positive")["id"])
    assert r["status"] == "antivirus-test-fixture"


def test_failed_engine_cannot_erase_another_detection(tmp_path, monkeypatch):
    service = manager(tmp_path, {"clamav_enabled": True, "scanner_vt_enabled": True})
    def scan(cfg, *args):
        """Keep successful engine evidence even if another provider cannot finish."""
        if cfg["clamav_enabled"]: return [reports.scanners.Finding("fixture", "Known.Signature")], []
        raise reports.scanners.ScanUnavailable("fixture unavailable")
    monkeypatch.setattr(reports.scanners, "scan", scan)
    r = finished(service, service.submit("snes/game.sfc", "malware")["id"])
    assert r["status"] == "detection-reported"
    assert [x["status"] for x in r["scanners"]] == ["detected", "unavailable"]


def test_clean_scanner_wording_is_no_detections_not_safe(tmp_path, monkeypatch):
    service = manager(tmp_path, {"clamav_enabled": True})
    monkeypatch.setattr(reports.scanners, "scan", lambda *a: ([], []))
    r = finished(service, service.submit("snes/game.sfc", "not-a-game")["id"])
    assert r["status"] == "no-detections" and "not a guarantee" in r["claim"]


def test_snapshot_budget_is_checked_before_background_work(tmp_path, monkeypatch):
    service = manager(tmp_path)
    monkeypatch.setattr(reports, "MAX_BYTES", 2)
    with pytest.raises(ValueError, match="4 GiB"):
        service.submit("snes/game.sfc", "malware")
    assert not service.active


def test_quarantine_restore_preserves_bytes_and_refuses_overwrite(tmp_path):
    service = manager(tmp_path)
    r = finished(service, service.submit("snes/game.sfc", "unwanted-file")["id"])
    original = service.library() / "snes/game.sfc"
    assert service.quarantine(r["id"])["quarantined"] and not original.exists()
    original.write_bytes(b"new file that must survive")
    with pytest.raises(ValueError, match="destination exists"):
        service.restore(r["id"])
    assert original.read_bytes() == b"new file that must survive"
    original.unlink()
    assert not service.restore(r["id"])["quarantined"]
    assert original.read_bytes() == b"ordinary fixture bytes"


def test_changed_file_is_not_quarantined_under_old_hash(tmp_path):
    service = manager(tmp_path)
    r = finished(service, service.submit("snes/game.sfc", "malware")["id"])
    (service.library() / "snes/game.sfc").write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="changed"):
        service.quarantine(r["id"])
    assert (service.library() / "snes/game.sfc").read_bytes() == b"different bytes"


def test_github_defaults_to_review_and_excludes_local_notes(tmp_path, monkeypatch):
    service = manager(tmp_path, env={"GH_TOKEN": "must-not-be-reused"})
    r = finished(service, service.submit("snes/game.sfc", "not-a-game", "PRIVATE-NOTE")["id"])
    def forbidden(*a, **k):
        """Default publication must never use maintainer credentials or contact GitHub."""
        raise AssertionError("unexpected request")
    monkeypatch.setattr(reports.requests, "post", forbidden)
    result = service.github("File concern", "Please review", confirm=True, report_id=r["id"])
    assert result["mode"] == "browser"
    assert "PRIVATE-NOTE" not in result["url"] and str(tmp_path) not in result["url"]
    assert "must-not-be-reused" not in result["url"]


def test_direct_github_requires_operator_token_and_explicit_confirmation(tmp_path, monkeypatch):
    service = manager(tmp_path, env={"ROMARR_GITHUB_ISSUES_TOKEN": "fixture-token"})
    calls = []
    def post(url, **kwargs):
        """Capture the sole permitted API destination and return a fixture issue."""
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=201, json=lambda: {"html_url": "https://github.com/BlizzHacker/romarr/issues/999"})
    monkeypatch.setattr(reports.requests, "post", post)
    assert service.github("Example", "User reviewed details")["mode"] == "browser" and not calls
    assert service.github("Example", "User reviewed details", confirm=True)["mode"] == "submitted"
    assert calls[0][0] == "https://api.github.com/repos/BlizzHacker/romarr/issues"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer fixture-token"
    assert "fixture-token" not in str(calls[0][1]["json"])
    with pytest.raises(ValueError, match="30 seconds"):
        service.github("Example", "duplicate", confirm=True)


def test_move_no_replace_preserves_a_raced_destination(tmp_path):
    source = tmp_path / "source"; dest = tmp_path / "dest"
    source.write_bytes(b"source"); dest.write_bytes(b"keep me")
    with pytest.raises(OSError): reports._move_no_replace(source, dest)
    assert source.read_bytes() == b"source" and dest.read_bytes() == b"keep me"


@pytest.fixture
def report_api(tmp_path):
    """Run the real authenticated routes against a tiny isolated library."""
    import json
    from http.server import ThreadingHTTPServer
    from romarr.app import ROMarr, make_handler
    root = tmp_path / "roms"; root.mkdir(); (root / "sample.rom").write_bytes(b"fixture")
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "config.json"), "ROMARR_API_KEY": "fixture-key",
                     "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(root)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(server.server_port), service
    server.shutdown(); server.server_close()


def call_api(base, path, body=None, authenticated=False):
    """Return JSON HTTP responses without copying any production credentials."""
    import json
    import urllib.request
    import urllib.error
    headers = {"Content-Type": "application/json"}
    if authenticated: headers["X-Api-Key"] = "fixture-key"
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response: return response.status, json.load(response)
    except urllib.error.HTTPError as response: return response.code, json.load(response)


@pytest.mark.parametrize("path,body", [("/api/v1/file-reports", None),
    ("/api/v1/file-reports/submit", {"file": "sample.rom", "category": "malware"}),
    ("/api/v1/file-reports/quarantine", {"id": "0" * 32}),
    ("/api/v1/file-reports/restore", {"id": "0" * 32}),
    ("/api/v1/support/github", {"title": "Example", "body": "Details", "confirm": True})])
def test_report_and_publication_routes_require_auth(report_api, path, body):
    base, service = report_api
    assert call_api(base, path, body)[0] == 401
    assert not hasattr(service, "_reports_manager")


def test_authenticated_report_and_browser_issue_draft(report_api):
    base, service = report_api
    code, row = call_api(base, "/api/v1/file-reports/submit", {"file": "sample.rom", "category": "unwanted-file"}, True)
    assert code == 200
    finished(service.report_manager(), row["id"])
    code, data = call_api(base, "/api/v1/file-reports", authenticated=True)
    assert code == 200 and len(data["items"]) == 1
    assert "_library" not in data["items"][0]
    code, draft = call_api(base, "/api/v1/support/github", {"title": "Example concern", "body": "Please review", "report_id": row["id"]}, True)
    assert code == 200 and draft["mode"] == "browser"
    assert draft["url"].startswith("https://github.com/BlizzHacker/romarr/issues/new?")


def test_digest_refuses_oversized_replacement_without_reading(tmp_path, monkeypatch):
    from io import BytesIO
    monkeypatch.setattr(reports, "MAX_BYTES", 8)
    monkeypatch.setattr(reports, "_fd_identity", lambda source: (1, 1, 9, 1, 1))
    class NeverRead(BytesIO):
        def read(self, *args):
            raise AssertionError("oversized replacement was read")
    with pytest.raises(ValueError, match="size limit"):
        reports._digest(NeverRead(b"fixture"))


def test_digest_stops_a_growing_stream_at_budget(monkeypatch):
    from io import BytesIO
    monkeypatch.setattr(reports, "MAX_BYTES", 8)
    monkeypatch.setattr(reports, "_fd_identity", lambda source: (1, 1, 1, 1, 1))
    source = BytesIO(b"x" * 100)
    with pytest.raises(ValueError, match="size limit"):
        reports._digest(source)
    assert source.tell() == 9
