"""Hydra source setup, from the operator's side of the screen.

A Hydra source could be added and still never deliver anything: the Test
button said "Connected" while every magnet in it had no torrent client to go
to, a host blocking the server was reported as "Rejected the API key" (a
source has no key), and a search without a platform never asked the source at
all. These tests hold the fixes in place, and the last one runs
``scripts/hydra_proof.py`` -- check, save, search, grab, import -- against a
real ROMarr on a real socket, which is what keeps CI proving the guide in
``docs/HYDRA.md`` works.
"""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from romarr import hydra
from romarr.app import ROMarr, make_handler
from romarr.hosters import HosterRoute
from romarr.hydra import (SourceError, check_config, check_source, check_url,
                          explain_request_error, HydraSource, HydraSourceConfig)
from romarr.indexers import INDEXER_TYPES

ROOT = Path(__file__).resolve().parents[1]
MAGNET = "magnet:?xt=urn:btih:" + "b" * 40
SOURCE = {"name": "Test list", "downloads": [
    {"title": "Alpha Quest (USA)", "platform": "snes", "fileSize": "2 MB",
     "uris": [MAGNET]},
    {"title": "Beta Racer (Europe)", "platform": "gba", "fileSize": "8 MB",
     "uris": ["https://files.example.org/Beta%20Racer.zip"]},
    {"title": "Gamma Saga", "fileSize": "1 GB",
     "uris": ["https://gofile.io/d/abc"]},
]}


@pytest.fixture(autouse=True)
def _fresh_cache():
    hydra.clear_cache()
    yield
    hydra.clear_cache()


class _Reply:
    def __init__(self, status: int, body: bytes = b""):
        self.status_code, self._body = status, body
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, size):
        yield self._body

    def close(self):
        pass


class _Session:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def get(self, url, **kw):
        if self.error is not None:
            raise self.error
        return self.reply


def _source(reply=None, error=None, url="https://lists.example.org/s.json"):
    return HydraSource(HydraSourceConfig(url=url, name="My list"),
                       session=_Session(reply, error))


def _ok(document=None):
    return _source(_Reply(200, json.dumps(SOURCE if document is None else document).encode()))


# -- plain-language failures ---------------------------------------------------

@pytest.mark.parametrize("status, words", [
    (404, "404 Not Found: this source has moved"),
    (403, "need no account or API key"),
    (401, "refused to send the source"),
    (429, "slow down"),
    (503, "having trouble"),
    (418, "answered HTTP 418"),
])
def test_http_failures_name_the_host_and_the_next_step(status, words):
    with pytest.raises(SourceError, match=words) as failure:
        _source(_Reply(status)).releases()
    assert str(failure.value).split(maxsplit=1)[0] == "lists.example.org"


@pytest.mark.parametrize("error, words", [
    (requests.ConnectTimeout("slow"), "did not answer within 60 seconds"),
    (requests.exceptions.SSLError("bad cert"), "certificate could not be verified"),
    (requests.ConnectionError("Failed to resolve 'x' (Name or service not known)"),
     "could not look up lists.example.org"),
    (requests.ConnectionError("[Errno 111] Connection refused"), "connection refused"),
    (requests.ConnectionError("reset"), "could not connect to lists.example.org"),
])
def test_network_failures_are_sentences_not_tracebacks(error, words):
    with pytest.raises(SourceError, match=words) as failure:
        _source(error=error).releases()
    assert "HTTPSConnectionPool" not in str(failure.value)


@pytest.mark.parametrize("url", ["", "   ", "ftp://host/s.json", "example.org/s.json", "https://"])
def test_a_url_that_cannot_be_a_source_is_refused_before_any_request(url):
    with pytest.raises(SourceError, match="Paste the source|full web address"):
        check_url(url)


def test_a_web_page_is_told_apart_from_other_non_json():
    with pytest.raises(SourceError, match="returned a web page.*Raw link"):
        _source(_Reply(200, b"<!doctype html><title>hi</title>")).releases()
    with pytest.raises(SourceError, match="not JSON"):
        _source(_Reply(200, b"name,url\n")).releases()


def test_json_that_is_not_a_source_says_what_is_missing():
    with pytest.raises(SourceError, match='^My list is JSON but not a Hydra source: it has no "downloads" list'):
        _ok({"games": []}).caps()
    hydra.clear_cache()   # the next document is served from the same URL
    with pytest.raises(SourceError, match="^My list has no downloads ROMarr can use: it lists 1 row, but none"):
        _ok({"downloads": [{"title": "x", "uris": ["ftp://nope"]}]}).caps()


def test_explain_covers_an_unknown_error_without_leaking_it():
    text = explain_request_error(RuntimeError("secret=abc"), "https://h.example/s.json", 5)
    assert text == "h.example could not be read (RuntimeError)."


# -- where each kind of link goes ------------------------------------------------

class _Client:
    def __init__(self, protocol, name, *, hoster=False):
        self.protocol, self.name, self.configured = protocol, name, True
        self.takes_hoster_links = hoster


def test_a_readable_source_with_no_clients_is_not_a_green_test():
    result = check_source(_ok(), [])
    assert result["ok"] is False
    assert result["summary"]["kinds"] == {"torrent": 1, "direct": 1, "hoster": 1}
    text = result["message"]
    assert text.startswith("Read 3 releases from My list.")
    assert "Platforms: Super Nintendo (1), Game Boy Advance (1)." in text
    assert "✗ Magnet links (1) have nowhere to go. Add a torrent client" in text
    assert "✗ Direct file links (1) have nowhere to go. Add Direct HTTP" in text
    assert "✗ File-host links (1) have nowhere to go. Add Real-Debrid or AllDebrid" in text
    assert "Nothing from this source can be downloaded" in text


def test_each_kind_reports_the_client_the_grab_will_use():
    clients = [_Client("torrent", "qBittorrent"), _Client("direct", "Direct HTTP"),
               _Client("torrent", "Real-Debrid", hoster=True)]
    result = check_source(_ok(), clients)
    assert result["ok"] is True and result["warn"] is False
    routes = {r["kind"]: r for r in result["routes"]}
    assert routes["torrent"]["client"] == "qBittorrent"
    assert routes["direct"]["client"] == "Direct HTTP"
    assert routes["hoster"]["client"] == "Real-Debrid or Direct HTTP"
    assert "✓ Magnet links (1) go to qBittorrent." in result["message"]


def test_direct_http_alone_is_only_a_partial_route_for_file_hosts():
    result = check_source(_ok(), [_Client("direct", "Direct HTTP")])
    hoster = next(r for r in result["routes"] if r["kind"] == "hoster")
    assert hoster["ready"] and hoster["partial"]
    assert "Only 0 of 1 have a host with a public download API" in hoster["fix"]
    assert result["ok"] is True and result["warn"] is True


def test_a_pixeldrain_link_counts_as_fetchable_without_debrid():
    doc = {"downloads": [{"title": "Delta", "uris": ["https://pixeldrain.com/u/abc123"]}]}
    assert check_source(_ok(doc), [])["summary"]["public_hoster"] == 1


def test_hoster_route_exposes_what_it_holds():
    direct, debrid = _Client("direct", "Direct HTTP"), _Client("torrent", "AllDebrid", hoster=True)
    route = HosterRoute.for_clients([direct, debrid])
    assert route.direct is direct and route.debrid == (debrid,)


def test_check_config_rejects_a_bad_timeout_and_a_blank_url_in_words():
    assert "Paste the source" in check_config({"type": "hydra", "url": ""}, [])["message"]
    assert "1 to 300" in check_config({"type": "hydra", "url": "https://h/s.json",
                                       "timeout": 0}, [])["message"]


# -- the form ------------------------------------------------------------------

def test_the_hydra_form_shows_three_fields_and_folds_the_rest():
    fields = INDEXER_TYPES["hydra"]["fields"]
    visible = [f["name"] for f in fields if not f.get("advanced")]
    folded = {f["name"] for f in fields if f.get("advanced")}
    assert visible == ["name", "enable", "url"]
    assert folded == {"timeout", "categories", "priority"}


def test_the_indexers_page_guides_the_setup():
    ui = (ROOT / "romarr" / "ui.py").read_text(encoding="utf-8")
    assert "const HYDRA_GUIDE='https://github.com/BlizzHacker/romarr/blob/main/docs/HYDRA.md';" in ui
    for marker in ('id="hy-url"', 'id="hy-check"', "id=\"hy-direct\"", "Save source",
                   "<details class=\"adv\"><summary>Advanced</summary>"):
        assert marker in ui, marker


def test_one_guide_is_linked_from_getting_started_features_and_the_ui():
    guide = (ROOT / "docs" / "HYDRA.md").read_text(encoding="utf-8")
    for doc in ("GETTING-STARTED.md", "FEATURES.md"):
        assert "HYDRA.md" in (ROOT / "docs" / doc).read_text(encoding="utf-8"), doc
    # Every message the Test button can show has a troubleshooting entry.
    for message in ("refused to send the source", "answered 404 Not Found",
                    "returned a web page", "no \"downloads\" list", "could not look up",
                    "did not answer within", "have nowhere to go",
                    "Only", "certificate could not be verified", "slow down"):
        assert message in guide, message
    assert "scripts/hydra_proof.py" in guide


# -- a real ROMarr on a real socket ----------------------------------------------

def _serve(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


class _Blocking(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(403)
        self.end_headers()

    def log_message(self, *a):
        pass


def _service(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_AUTH": "disabled"})
    base, httpd = _serve(make_handler(service))
    return service, base, httpd


def _call(base, method, path, body=None):
    response = requests.request(method, base + path, json=body, timeout=60)
    return response.json()


def test_a_blocked_source_is_never_reported_as_a_rejected_api_key(tmp_path):
    source, src_httpd = _serve(_Blocking)
    service, base, httpd = _service(tmp_path)
    try:
        reply = _call(base, "POST", "/api/v1/indexer/test",
                      {"type": "hydra", "url": source + "/s.json", "name": "Blocked"})
    finally:
        httpd.shutdown()
        src_httpd.shutdown()
    assert reply["ok"] is False
    assert "API key" in reply["message"] and "Rejected" not in reply["message"]
    assert "refused to send the source (HTTP 403)" in reply["message"]


def _proof_module():
    spec = importlib.util.spec_from_file_location("hydra_proof", ROOT / "scripts" / "hydra_proof.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_search_without_a_platform_asks_the_hydra_source_too(tmp_path):
    proof = _proof_module()
    source, src_httpd = proof.serve_fixture()
    service, base, httpd = _service(tmp_path)
    try:
        saved = _call(base, "POST", "/api/v1/indexer",
                      {"type": "hydra", "name": "Proof", "url": source, "enable": True})
        assert saved.get("id")
        found = _call(base, "GET", "/api/v1/release?game=ROMarr+Proof+Cart")
    finally:
        httpd.shutdown()
        src_httpd.shutdown()
    # No Prowlarr is set up: before, this was "search failed: MissingSchema".
    assert "error" not in found
    assert [i["indexer"] for i in found["items"]] == ["Proof"]
    assert found["items"][0]["platform"] == "gbc"


def test_the_proof_script_checks_saves_searches_grabs_and_imports(tmp_path):
    """scripts/hydra_proof.py, end to end, exactly as docs/HYDRA.md runs it."""
    proof = _proof_module()
    service, base, httpd = _service(tmp_path)
    library, downloads = tmp_path / "roms", tmp_path / "downloads"
    try:
        report = proof.run(base, library_path=str(library), download_path=str(downloads),
                           direct_delay=0, timeout=60)
    finally:
        httpd.shutdown()
    steps = {row["step"]: row for row in report}
    assert all(row["ok"] for row in report), report
    assert list(steps) == ["check the source", "add Direct HTTP", "check again",
                           "save the source", "search", "grab",
                           "routed to the promised client", "import"]
    assert "✗ Direct file links (1) have nowhere to go" in steps["check the source"]["detail"]
    assert "✓ Direct file links (1) go to Direct HTTP" in steps["check again"]["detail"]
    assert "direct link went to Direct HTTP" in steps["routed to the promised client"]["detail"]
    imported = list(library.rglob("*.gbc"))
    assert [p.parent.name for p in imported] == ["gbc"]
