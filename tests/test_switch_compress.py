"""Issue #278: Switch imports compressed to NSZ/XCZ through Compressatorium.

The service is faked at the `requests.Session` boundary, so these pin what
ROMarr sends, how it reads each answer, and that every failure keeps the
original. The app-level tests replace `compress_import` and run the worker
inline, so they pin the wiring: the setting, the platform, the hash index
move and the History event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romarr import switch_compress
from romarr.app import ROMarr
from romarr.compressatorium import (
    TOOL_BY_MODE, CompressatoriumClient, CompressatoriumConfig, compress_mode_for,
    mode_for,
)
from romarr.switch_compress import (
    COMPRESSED, FAILED_STATUS, KEPT, SKIPPED, CompressOutcome, compress_import,
    compression_for, parse_compression,
)


# ---------------------------------------------------------------- fakes --

class _Response:
    """A canned `requests.Response`."""

    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = ""
        self.content = b"x" if payload is not None else b""

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Service:
    """A Compressatorium that answers health, tools, jobs and cancels.

    `finish` runs on the first status poll, so a test decides what the job
    did to the filesystem (write the .nsz, remove the .nsp) and what the
    service reports.
    """

    def __init__(self, *, tools=("nsz",), submit=None, status=None,
                 finish=None):
        self.tools = list(tools)
        self.submit = submit or _Response(200, {"id": "job-1", "status": "queued"})
        self.status = status or {"id": "job-1", "status": "completed"}
        self.finish = finish
        self.calls = []

    def _path(self, url):
        return "/" + url.split("://", 1)[1].split("/", 1)[1]

    def get(self, url, **kwargs):
        path = self._path(url)
        self.calls.append(("GET", path, kwargs))
        if path == "/health":
            return _Response(200, {"status": "ok"})
        if path == "/api/tools":
            return _Response(200, {"available": self.tools, "unavailable": []})
        if path == "/api/jobs/job-1":
            if self.finish is not None:
                self.finish()
                self.finish = None
            return _Response(200, self.status)
        return _Response(404, {"detail": "Not Found"})

    def post(self, url, **kwargs):
        self.calls.append(("POST", self._path(url), kwargs))
        return self.submit

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", self._path(url), kwargs))
        return _Response(200, {})

    def posted(self):
        """The JSON bodies POSTed to /api/jobs."""
        return [c[2]["json"] for c in self.calls if c[:2] == ("POST", "/api/jobs")]


def _client(service, api_key=""):
    return CompressatoriumClient(
        CompressatoriumConfig(base_url="http://comp:8080", api_key=api_key),
        session=service)


def _nsp(tmp_path, name="Game [0100000000010000][v0].nsp"):
    path = tmp_path / "switch" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"nsp")
    return path


def _compressed(source: Path, *, remove=True):
    """A `finish` callback: write the output and drop the original."""
    def run():
        switch_compress.expected_output(source).write_bytes(b"nsz")
        if remove:
            source.unlink()
    return run


# ----------------------------------------------------------- mode map --

@pytest.mark.parametrize("name, mode", [
    ("Game.nsp", "nsz_compress"),
    ("Game.NSP", "nsz_compress"),
    ("Game.xci", "nsz_compress"),
    ("Game.nsz", ""),
    ("Game.xcz", ""),
    ("Game.nsp.part", ""),
    ("nsp-pack.zip", ""),
    ("Game.iso", ""),
])
def test_compress_mode_is_chosen_by_the_parsed_suffix(name, mode):
    assert compress_mode_for(name) == mode


def test_compress_mode_runs_on_the_nsz_tool_and_leaves_decompress_alone():
    assert TOOL_BY_MODE["nsz_compress"] == "nsz"
    assert mode_for("Game.nsp") == ""
    assert mode_for("Game.nsz") == "nsz_decompress"


# -------------------------------------------------------- compression --

@pytest.mark.parametrize("raw, canonical", [
    ("block:18", "block:18"),
    (" Solid:3 ", "solid:3"),
    ("block:01", "block:1"),
    ("block:22", "block:22"),
])
def test_valid_compression_is_canonicalised(raw, canonical):
    assert parse_compression(raw) == canonical


@pytest.mark.parametrize("raw", [
    "", None, "block", "block:0", "block:23", "solid:100", "zstd:18",
    "block:18,lzma", "block:-1", "18",
])
def test_invalid_compression_is_refused(raw):
    with pytest.raises(ValueError):
        parse_compression(raw)


def test_an_xci_is_always_sent_as_block():
    assert compression_for("Game.xci", "solid:5") == "block:5"
    assert compression_for("Game.nsp", "solid:5") == "solid:5"


def test_the_setting_is_validated_when_saved(tmp_path):
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    assert s.store.settings["compress_switch_imports"] is False
    assert s.store.settings["nsz_compression"] == "block:18"
    with pytest.raises(ValueError):
        s.store.update_settings({"nsz_compression": "solid:30",
                                 "compress_switch_imports": True})
    # A refused value commits nothing else from the same patch.
    assert s.store.settings["compress_switch_imports"] is False
    saved = s.store.update_settings({"nsz_compression": "SOLID:9"})
    assert saved["nsz_compression"] == "solid:9"


# --------------------------------------------------------- the client --

def test_submit_job_sends_compression_only_when_given():
    service = _Service()
    client = _client(service)
    client.submit_job(file_path="/lib/a.nsp", mode="nsz_compress",
                      delete_on_verify=True, compression="block:18")
    client.submit_job(file_path="/lib/a.nsz", mode="nsz_decompress")
    first, second = service.posted()
    assert first == {"file_path": "/lib/a.nsp", "mode": "nsz_compress",
                     "duplicate_action": "skip", "delete_on_verify": True,
                     "compression": "block:18"}
    assert "compression" not in second


def test_a_job_reads_its_warning_and_deleted_paths():
    service = _Service(status={"id": "job-1", "status": "completed",
                               "warning": "source kept", "deleted_paths": ["/a.nsp"]})
    result = _client(service).job_status("job-1")
    assert result.warning == "source kept"
    assert result.deleted_paths == ("/a.nsp",)


# ---------------------------------------------------- compress_import --

def test_a_compressed_file_is_skipped_without_asking_the_service(tmp_path):
    service = _Service()
    nsz = tmp_path / "Game.nsz"
    nsz.write_bytes(b"nsz")
    outcome = compress_import(nsz, url="http://comp:8080", client=_client(service))
    assert outcome.status == SKIPPED
    assert service.calls == []


def test_an_nsp_is_compressed_and_the_original_goes(tmp_path):
    source = _nsp(tmp_path)
    output = source.with_suffix(".nsz")
    service = _Service(finish=_compressed(source),
                       status={"id": "job-1", "status": "completed",
                               "output_path": str(output)})
    outcome = compress_import(source, url="http://comp:8080", compression="block:18",
                              client=_client(service, api_key="tok"),
                              poll_interval=0)
    assert outcome == CompressOutcome(str(source), COMPRESSED, str(output), "job-1")
    body, = service.posted()
    assert body == {"file_path": str(source), "mode": "nsz_compress",
                    "duplicate_action": "skip", "delete_on_verify": True,
                    "compression": "block:18"}
    post = next(c for c in service.calls if c[0] == "POST")
    assert post[2]["headers"] == {"Authorization": "Bearer tok"}


def test_an_xci_with_a_solid_setting_is_submitted_as_block(tmp_path):
    source = _nsp(tmp_path, "Game.xci")
    service = _Service(finish=_compressed(source))
    compress_import(source, url="http://comp:8080", compression="solid:7",
                    client=_client(service), poll_interval=0)
    assert service.posted()[0]["compression"] == "block:7"


def test_a_failed_job_keeps_the_original(tmp_path):
    source = _nsp(tmp_path, "Game.xci")
    service = _Service(status={
        "id": "job-1", "status": "failed",
        "error_message": "Round-trip failed: nsz did not rebuild this XCI "
                         "byte-for-byte, so the Source is kept."})
    outcome = compress_import(source, url="http://comp:8080",
                              client=_client(service), poll_interval=0)
    assert outcome.status == FAILED_STATUS
    assert "byte-for-byte" in outcome.error
    assert source.is_file()
    assert "original kept" in outcome.describe()


def test_a_completed_job_that_kept_its_source_says_so(tmp_path):
    source = _nsp(tmp_path)
    service = _Service(finish=_compressed(source, remove=False),
                       status={"id": "job-1", "status": "completed",
                               "warning": "another job was reading it"})
    outcome = compress_import(source, url="http://comp:8080",
                              client=_client(service), poll_interval=0)
    assert outcome.status == KEPT
    assert outcome.error == "another job was reading it"
    assert source.is_file()


def test_a_missing_nsz_tool_submits_nothing(tmp_path):
    source = _nsp(tmp_path)
    service = _Service(tools=("chdman",))
    outcome = compress_import(source, url="http://comp:8080",
                              client=_client(service), poll_interval=0)
    assert outcome.status == FAILED_STATUS
    assert "prod.keys" in outcome.error
    assert service.posted() == []


def test_a_refused_submission_keeps_the_original(tmp_path):
    source = _nsp(tmp_path)
    service = _Service(submit=_Response(403, {"detail": "outside volumes"}))
    outcome = compress_import(source, url="http://comp:8080",
                              client=_client(service), poll_interval=0)
    assert outcome.status == FAILED_STATUS
    assert outcome.error == "status 403"
    assert source.is_file()


def test_a_job_past_its_deadline_is_cancelled(tmp_path):
    source = _nsp(tmp_path)
    service = _Service(status={"id": "job-1", "status": "processing"})
    outcome = compress_import(source, url="http://comp:8080",
                              client=_client(service), job_timeout=0,
                              poll_interval=0)
    assert outcome.status == FAILED_STATUS
    assert "cancelled" in outcome.error
    assert ("DELETE", "/api/jobs/job-1") in [c[:2] for c in service.calls]
    assert source.is_file()


def test_no_url_means_no_job(tmp_path):
    outcome = compress_import(_nsp(tmp_path), url="")
    assert outcome.status == FAILED_STATUS
    assert "URL" in outcome.error


# ------------------------------------------------------- app wiring --

class _NowPool:
    """Runs submitted work immediately, so the test sees the outcome."""

    def submit(self, fn):
        fn()


def _svc(tmp_path, monkeypatch, *, enabled=True, outcome=None):
    library = tmp_path / "lib"
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                    "LIBRARY_PATH": str(library),
                    "COMPRESSATORIUM_URL": "http://comp:8080"})
    s.library = library
    s._compress_pool = _NowPool()
    s.store.update_settings({"compress_switch_imports": enabled,
                             "rescan_after_import": False})
    calls = []

    def fake(path, **kwargs):
        calls.append((Path(path), kwargs))
        return outcome(Path(path)) if outcome else CompressOutcome(str(path), SKIPPED)

    monkeypatch.setattr(switch_compress, "compress_import", fake)
    return s, calls


def test_the_setting_off_means_no_job(tmp_path, monkeypatch):
    s, calls = _svc(tmp_path, monkeypatch, enabled=False)
    s._compress_after_import(_nsp(s.library), "switch", "Game")
    assert calls == []
    assert s.store.history(kind="compress") == []


def test_only_switch_nsp_and_xci_imports_are_compressed(tmp_path, monkeypatch):
    s, calls = _svc(tmp_path, monkeypatch)
    s._compress_after_import(_nsp(s.library), "3ds", "Game")
    s._compress_after_import(_nsp(s.library, "Game.nsz"), "switch", "Game")
    s._compress_after_import(None, "switch", "Game")
    assert calls == []


def test_a_compressed_import_moves_the_library_entry(tmp_path, monkeypatch):
    def done(path):
        return CompressOutcome(str(path), COMPRESSED,
                               str(path.with_suffix(".nsz")), "job-1")

    s, calls = _svc(tmp_path, monkeypatch, outcome=done)
    s.store.update_settings({"nsz_compression": "solid:12"})
    source = _nsp(s.library)
    s.hashes.add("a" * 40, "Game", "switch", True,
                 path=source.relative_to(s.library).as_posix())
    queue_item = type("Q", (), {"imported_paths": [str(source)]})()

    s._compress_after_import(source, "switch", "Game", queue_item)

    (path, kwargs), = calls
    assert path == source
    assert kwargs == {"url": "http://comp:8080", "api_key": "",
                      "compression": "solid:12"}
    assert s.hashes.by_sha1("a" * 40).path == "switch/Game [0100000000010000][v0].nsz"
    assert queue_item.imported_paths == [str(source.with_suffix(".nsz"))]
    event, = s.store.history(kind="compress")
    assert event["platform"] == "switch" and event["game"] == "Game"
    assert event["detail"].startswith("compressed ")


def test_a_failed_compression_is_recorded_and_changes_nothing(tmp_path, monkeypatch):
    def failed(path):
        return CompressOutcome(str(path), FAILED_STATUS, job_id="job-1",
                               error="Round-trip failed")

    s, _ = _svc(tmp_path, monkeypatch, outcome=failed)
    source = _nsp(s.library)
    s.hashes.add("b" * 40, "Game", "switch", True,
                 path=source.relative_to(s.library).as_posix())

    s._compress_after_import(source, "switch", "Game")

    assert s.hashes.by_sha1("b" * 40).path.endswith(".nsp")
    event, = s.store.history(kind="compress")
    assert "failed, original kept: Round-trip failed" in event["detail"]


def test_an_unexpected_error_is_recorded_not_raised(tmp_path, monkeypatch):
    def boom(path):
        raise RuntimeError("bug")

    s, _ = _svc(tmp_path, monkeypatch, outcome=boom)
    s._compress_after_import(_nsp(s.library), "switch", "Game")
    event, = s.store.history(kind="compress")
    assert "unexpected error: RuntimeError" in event["detail"]


def test_a_manual_switch_import_reaches_the_compress_hook(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    s = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(root),
                "COMPRESSATORIUM_URL": "http://comp:8080"})
    s.store.put_item("libraries", {"type": "folder", "name": "Shelf", "enable": True,
                                   "path": str(root), "is_default": True})
    s.reload_libraries()
    s._compress_pool = _NowPool()
    s.store.update_settings({"compress_switch_imports": True,
                             "rescan_after_import": False})
    seen = []
    monkeypatch.setattr(switch_compress, "compress_import",
                        lambda path, **kw: seen.append(Path(path))
                        or CompressOutcome(str(path), FAILED_STATUS, error="test"))
    source = tmp_path / "in" / "Game [0100000000010000][v0].nsp"
    source.parent.mkdir()
    source.write_bytes(b"\0" * 64)

    result = s.adopt(str(source), "switch")

    assert result["ok"] is True, result
    imported = Path(result["imported"][0]["file"])
    assert seen == [imported]
    assert imported.is_file()
    assert s.store.history(kind="compress")[0]["detail"].endswith("original kept: test")
