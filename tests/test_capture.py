"""The ingest endpoint the browser extension posts to.

The payload comes from JavaScript running in a page ROMarr does not control,
so most of what is asserted here is about refusal: what the endpoint declines
to store, and that it says so rather than repairing it quietly.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr import capture
from romarr.app import ROMarr, make_handler


def row(**over):
    """A capture row that would be accepted, so a test can spoil one field."""
    return {"id": "3393", "title": "4-in-1 Fun Pak", "platform": "Game Boy",
            "name": "4-in-1 Fun Pak (USA, Europe).gb", "size": 58368,
            "url": "https://vimm.net/vault/3393", "media_id": "3279",
            "region": "USA,Europe", "version": "1.0", **over}


def post(directory, **over):
    return capture.ingest({"source": "vimm", "items": [row()], **over},
                          directory=directory)


# --- what it writes ---------------------------------------------------------

def test_a_capture_lands_in_the_index_the_other_indexers_write(tmp_path):
    """The whole point of the shape: no new loader, no special case."""
    report = post(tmp_path)
    assert report["indexed"] == 1
    assert report["platforms"] == {"gb": 1}

    written = [json.loads(line) for line in
               (tmp_path / "idx-gb.jsonl").read_text(encoding="utf-8").splitlines()]
    assert written == [{
        "name": "4-in-1 Fun Pak (USA, Europe).gb", "size": 58368,
        "url": "https://vimm.net/vault/3393", "source": "vimm", "id": "3393",
        "title": "4-in-1 Fun Pak", "region": "USA,Europe", "version": "1.0",
        "platform": "gb", "media_id": "3279"}]


def test_the_done_sidecar_is_namespaced_like_the_other_indexers(tmp_path):
    """A bare numeric id could collide with an Archive.org identifier in the
    same file, which is why vimm_index.py namespaces its keys too."""
    post(tmp_path)
    assert (tmp_path / "idx-gb.done").read_text(encoding="utf-8") == "vimm:3393\n"


def test_media_id_is_actually_filled(tmp_path):
    """The 23,980-row Vimm capture declared this key and filled it on none of
    them, leaving an index with no actionable handle at all."""
    written = json.loads((post(tmp_path), (tmp_path / "idx-gb.jsonl")
                          .read_text(encoding="utf-8"))[1])
    assert written["media_id"] == "3279"


def test_a_row_with_no_media_id_simply_omits_the_key(tmp_path):
    """Declaring it empty is what produced a field nobody could rely on."""
    capture.ingest({"source": "vimm", "items": [row(media_id="")]},
                   directory=tmp_path)
    written = json.loads((tmp_path / "idx-gb.jsonl").read_text(encoding="utf-8"))
    assert "media_id" not in written


def test_platform_is_never_blank_on_a_stored_row(tmp_path):
    """`platform` was empty on nearly every row of the previous capture,
    because the extension recorded whatever the detail page happened to show.
    A row that cannot name its platform is now skipped, not stored blank."""
    report = capture.ingest({"source": "vimm", "items": [row(platform="")]},
                            directory=tmp_path)
    assert report["indexed"] == 0
    assert report["skipped"] == 1
    assert not (tmp_path / "idx-gb.jsonl").exists()


def test_appending_twice_does_not_duplicate_the_row(tmp_path):
    post(tmp_path)
    again = post(tmp_path)
    assert again["indexed"] == 0
    assert again["already_indexed"] == 1
    assert len((tmp_path / "idx-gb.jsonl").read_text(
        encoding="utf-8").splitlines()) == 1


def test_the_same_id_twice_in_one_payload_is_written_once(tmp_path):
    """A listing that repeats a row in a "popular" strip is one game."""
    report = capture.ingest({"source": "vimm", "items": [row(), row()]},
                            directory=tmp_path)
    assert (report["indexed"], report["already_indexed"]) == (1, 1)


def test_rows_split_across_the_platforms_they_resolve_to(tmp_path):
    report = capture.ingest({"source": "vimm", "items": [
        row(), row(id="4402", platform="Game Boy Advance",
                   url="https://vimm.net/vault/4402")]}, directory=tmp_path)
    assert report["platforms"] == {"gb": 1, "gba": 1}
    assert (tmp_path / "idx-gba.jsonl").exists()


# --- platforms are resolved, never invented ---------------------------------

def test_an_unmappable_platform_is_reported_rather_than_guessed(tmp_path):
    """The hard rule in this codebase. platforms.resolve documents what
    guessing cost last time: 4,173 rows filed under the wrong machine.

    "Sega Nomad" is a real machine ROMarr has no platform for, which is the
    case that matters: an unmapped name is usually a real console somebody can
    add in one line, not a typo."""
    report = capture.ingest(
        {"source": "vimm", "items": [row(platform="Sega Nomad")]},
        directory=tmp_path)
    assert report["indexed"] == 0
    assert report["unmapped_platforms"] == [{"platform": "Sega Nomad", "count": 1}]
    # No file was invented for it.
    assert not list(tmp_path.glob("idx-*.jsonl"))


def test_a_good_row_still_lands_when_another_row_is_unmapped(tmp_path):
    """One unplaceable game must not cost the operator the page."""
    report = capture.ingest({"source": "vimm", "items": [
        row(), row(id="999", platform="Sega Nomad",
                   url="https://vimm.net/vault/999")]}, directory=tmp_path)
    assert report["indexed"] == 1
    assert report["unmapped_platforms"] == [{"platform": "Sega Nomad", "count": 1}]


# --- the payload is untrusted ----------------------------------------------

def test_a_url_off_the_sources_own_hosts_is_refused(tmp_path):
    """The attack this endpoint would otherwise open: a page handing ROMarr a
    library row that points anywhere it likes."""
    report = capture.ingest(
        {"source": "vimm", "items": [row(url="https://evil.example/vault/1")]},
        directory=tmp_path)
    assert report["indexed"] == 0
    assert "url is not https" in "".join(report["skipped_reason"])


def test_a_lookalike_host_does_not_pass_the_suffix_check(tmp_path):
    report = capture.ingest(
        {"source": "vimm",
         "items": [row(url="https://vimm.net.evil.example/vault/3393")]},
        directory=tmp_path)
    assert report["indexed"] == 0


def test_a_real_subdomain_does_pass(tmp_path):
    """Vimm serves the vault from one host and files from another."""
    assert capture._https_url_on("https://dl3.vimm.net/x", ("vimm.net",))


def test_plain_http_is_refused(tmp_path):
    report = capture.ingest(
        {"source": "vimm", "items": [row(url="http://vimm.net/vault/3393")]},
        directory=tmp_path)
    assert report["indexed"] == 0


@pytest.mark.parametrize("payload, complaint", [
    ("not an object", "JSON object"),
    ({"items": [row()]}, "source"),
    ({"source": "VIMM!", "items": [row()]}, "source"),
    ({"source": "nosuchsite", "items": [row()]}, "unknown source"),
    ({"source": "vimm"}, "items"),
    ({"source": "vimm", "items": []}, "items"),
    ({"source": "vimm", "items": "everything"}, "items"),
])
def test_a_malformed_payload_is_rejected_whole(payload, complaint, tmp_path):
    """Storing the good half of a payload that lied about the rest would put
    rows in the index nobody could account for."""
    with pytest.raises(capture.Rejected) as raised:
        capture.ingest(payload, directory=tmp_path)
    assert complaint in str(raised.value)
    assert not list(tmp_path.glob("idx-*"))


def test_too_many_rows_is_rejected_rather_than_truncated(tmp_path):
    """A page the operator opened cannot have more titles on it than this, so
    a payload that claims to is not a page."""
    many = [row(id=str(n)) for n in range(capture.MAX_ITEMS + 1)]
    with pytest.raises(capture.Rejected):
        capture.ingest({"source": "vimm", "items": many}, directory=tmp_path)


@pytest.mark.parametrize("bad", [
    {"id": ""}, {"id": "../../etc/passwd"}, {"id": "a b"}, {"title": ""},
    {"size": 0}, {"size": -1}, {"size": "58368"}, {"size": True},
    {"size": capture.MAX_SIZE_BYTES + 1},
])
def test_one_bad_field_skips_that_row_and_reports_why(bad, tmp_path):
    report = capture.ingest({"source": "vimm", "items": [row(**bad)]},
                            directory=tmp_path)
    assert report["indexed"] == 0
    assert report["skipped"] == 1
    assert sum(report["skipped_reason"].values()) == 1


def test_a_row_that_is_not_an_object_is_skipped(tmp_path):
    report = capture.ingest({"source": "vimm", "items": ["nope", row()]},
                            directory=tmp_path)
    assert (report["indexed"], report["skipped"]) == (1, 1)


def test_a_title_carrying_page_markup_whitespace_is_folded(tmp_path):
    capture.ingest({"source": "vimm",
                    "items": [row(title="  Fun\n\t   Pak  ", name="")]},
                   directory=tmp_path)
    written = json.loads((tmp_path / "idx-gb.jsonl").read_text(encoding="utf-8"))
    assert written["title"] == "Fun Pak"


def test_characters_a_filesystem_cannot_hold_are_scrubbed_from_the_name(tmp_path):
    capture.ingest({"source": "vimm", "items": [row(name='a/b:c?d.gb')]},
                   directory=tmp_path)
    written = json.loads((tmp_path / "idx-gb.jsonl").read_text(encoding="utf-8"))
    assert written["name"] == "a_b_c_d.gb"


def test_a_long_field_is_capped_rather_than_stored(tmp_path):
    capture.ingest({"source": "vimm", "items": [row(title="x" * 5000, name="")]},
                   directory=tmp_path)
    written = json.loads((tmp_path / "idx-gb.jsonl").read_text(encoding="utf-8"))
    assert len(written["title"]) == capture.MAX_TEXT


# --- how it is authenticated ------------------------------------------------

def make(tmp_path, key="secret-key"):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json"),
                      "ROMARR_API_KEY": key,
                      "ROMARR_INDEX_DIR": str(tmp_path / "idx")})
    return make_handler(service)


def test_capture_is_not_an_open_path(tmp_path):
    """It takes the operator's API key. A key is a credential, not a reason to
    add a route to the list of ones that answer without one."""
    handler = make(tmp_path)
    assert "/api/v1/capture" not in handler.OPEN_PATHS
    assert "/api/v1/capture/status" not in handler.OPEN_PATHS


def test_both_capture_routes_are_documented():
    """test_openapi enforces this across the whole surface; named here so a
    failure points at the endpoint that caused it."""
    from romarr.openapi import DESCRIPTIONS, served_routes
    served = served_routes()
    for path in ("/api/v1/capture", "/api/v1/capture/status"):
        assert path in served
        assert path in DESCRIPTIONS


def test_the_index_directory_sits_beside_the_settings_file(tmp_path):
    """Where every idx-*.jsonl on a real install already is."""
    assert capture.index_dir({}, tmp_path / "sub" / "romarr.json") == tmp_path / "sub"
    assert capture.index_dir({"ROMARR_INDEX_DIR": str(tmp_path)},
                             "/opt/romarr/romarr.json") == tmp_path


def test_the_status_report_says_whether_a_capture_could_actually_land(tmp_path):
    """A connection test that only proved the route existed would pass against
    a server whose data directory is unwritable."""
    report = capture.status(tmp_path / "idx")
    assert report["ok"] is True and report["index_writable"] is True
    assert {s["source"] for s in report["sources"]} == set(capture.SOURCES)
    # It leaves nothing behind.
    assert not list((tmp_path / "idx").iterdir())


def test_every_declared_source_has_at_least_one_host():
    """A source with no hosts would accept a URL pointing anywhere."""
    for source, (label, hosts) in capture.SOURCES.items():
        assert hosts and all(h and "/" not in h for h in hosts), source
        assert label


# --- the gate, as a request actually meets it -------------------------------
#
# The tests above prove the validation is right. These prove it is *wired* --
# against a real socket, because an extension posts over one and a rule that
# is correct in isolation and unreachable is no rule at all.

@pytest.fixture
def server(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_API_KEY": "testkey",
                      "ROMARR_INDEX_DIR": str(tmp_path / "idx")})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path / "idx"
    httpd.shutdown()
    httpd.server_close()


def call(url, *, key=None, body=None, method="GET", raw=None):
    request = urllib.request.Request(url, method=method)
    if key:
        request.add_header("X-Api-Key", key)
    if body is not None or raw is not None:
        request.add_header("Content-Type", "application/json")
        request.data = raw if raw is not None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.mark.parametrize("path, method", [
    ("/api/v1/capture", "POST"),
    ("/api/v1/capture/status", "GET"),
])
def test_posting_a_capture_without_a_key_is_refused(server, path, method):
    base, _ = server
    status, _ = call(base + path, method=method,
                     body={"source": "vimm", "items": [row()]}
                     if method == "POST" else None)
    assert status == 401


def test_a_wrong_key_is_refused_so_the_connection_test_means_something(server):
    base, _ = server
    assert call(base + "/api/v1/capture/status", key="nope")[0] == 401
    assert call(base + "/api/v1/capture/status", key="testkey")[0] == 200


def test_a_capture_posted_with_the_key_reaches_the_index(server):
    base, idx = server
    status, report = call(base + "/api/v1/capture", key="testkey", method="POST",
                          body={"source": "vimm", "items": [row()]})
    assert status == 200 and report["indexed"] == 1
    assert json.loads((idx / "idx-gb.jsonl").read_text(encoding="utf-8"))["id"] == "3393"


def test_a_malformed_capture_answers_400_and_writes_nothing(server):
    base, idx = server
    status, report = call(base + "/api/v1/capture", key="testkey", method="POST",
                          body={"source": "vimm", "items": "everything"})
    assert status == 400 and report["ok"] is False
    assert not idx.exists() or not list(idx.glob("idx-*"))


def test_an_oversized_capture_is_refused_before_it_is_read(server):
    """Bounded by Content-Length rather than after parsing: read(length) on a
    declared gigabyte allocates a gigabyte first and refuses second."""
    base, idx = server
    status, report = call(base + "/api/v1/capture", key="testkey", method="POST",
                          raw=b'{"source":"vimm","items":[]}'
                              + b" " * capture.MAX_BODY_BYTES)
    assert status == 413
    assert report["limit_bytes"] == capture.MAX_BODY_BYTES
    assert not idx.exists() or not list(idx.glob("idx-*"))


def test_invalid_json_under_the_limit_is_a_400_not_a_crash(server):
    base, _ = server
    assert call(base + "/api/v1/capture", key="testkey", method="POST",
                raw=b"{not json")[0] == 400
