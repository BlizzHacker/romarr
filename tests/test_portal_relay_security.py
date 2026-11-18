"""GHSA-rqvw-wx7h-5xq2: anonymous use of the linked RomM API via /assets/..

The request gate let static portal prefixes through without authentication,
the relay forwarded the raw path, and the HTTP client resolved the
dot-segments -- so `/assets/../api/roms` reached RomM's API carrying ROMarr's
stored RomM credential.
"""

from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, has_unsafe_path_segments, make_handler


@pytest.mark.parametrize("path", [
    "/assets/../api/roms?limit=1",
    "/assets/../api/platforms",
    "/emulatorjs/../api/users/me",
    "/_next/%2e%2e/api/tasks/status",
    "/static/%252e%252e/api/roms",
    "/assets/..%2fapi/roms",
    "/assets/.%2e/api/roms",
    "/assets/..\\api/roms",
    "/assets/./x",
])
def test_dot_segments_are_unsafe_however_spelled(path):
    assert has_unsafe_path_segments(path)


@pytest.mark.parametrize("path", [
    "/assets/index-abc123.js",
    "/api/roms?limit=1",
    "/api/roms/12/content/Sonic..The..Hedgehog.md",
    "/emulatorjs/data/loader.js",
])
def test_ordinary_paths_are_not_refused(path):
    assert not has_unsafe_path_segments(path)


class FakeRomm(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802
        FakeRomm.seen.append((self.path, self.headers.get("Authorization"),
                              self.headers.get("X-Api-Key"), self.headers.get("Cookie")))
        authed = self.headers.get("Authorization") == "Bearer secret-token"
        if self.path.startswith("/api/") and not authed:
            self.send_response(401)
            self.end_headers()
            return
        body = b"asset-bytes" if self.path.startswith("/assets/") else b'{"items":[1]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stack(tmp_path):
    FakeRomm.seen = []
    romm = ThreadingHTTPServer(("127.0.0.1", 0), FakeRomm)
    threading.Thread(target=romm.serve_forever, daemon=True).start()
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k",
                      "LIBRARY_KIND": "romm",
                      "LIBRARY_URL": f"http://127.0.0.1:{romm.server_port}",
                      "LIBRARY_API_KEY": "secret-token"})
    service.store.settings["romm_url"] = f"http://127.0.0.1:{romm.server_port}"
    service.store.settings["romm_api_key"] = "secret-token"
    app = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=app.serve_forever, daemon=True).start()

    def raw_get(path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=20)
        conn.putrequest("GET", path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        return r.status, r.read()

    yield service, raw_get
    app.shutdown()
    romm.shutdown()


@pytest.mark.parametrize("path", [
    "/assets/../api/roms?limit=1",
    "/assets/../api/tasks/status",
    "/emulatorjs/../api/users/me",
    "/assets/%2e%2e/api/roms",
])
def test_the_advisory_requests_are_refused_and_never_reach_the_library(stack, path):
    _, get = stack
    status, body = get(path)
    # Refused at the gate, before any route or relay runs.
    assert status == 400 and b"invalid path" in body
    # And nothing was relayed: no upstream request carries the traversed
    # target. (Background library refreshes use their own query strings.)
    target = "/" + path.replace("%2e%2e", "..").split("../", 1)[1]
    assert target not in {seen[0] for seen in FakeRomm.seen}, target


def _asset_hits(name):
    return [seen for seen in FakeRomm.seen if seen[0].startswith(name)]


def test_an_anonymous_relay_ignores_netrc(stack, tmp_path, monkeypatch):
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine 127.0.0.1 login ambient password leaked\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HOME", str(tmp_path))
    _, get = stack
    get("/assets/netrc.js")
    hits = _asset_hits("/assets/netrc.js")
    assert hits and all(h[1] is None for h in hits), hits


def test_an_anonymous_relay_honours_no_proxy(stack, monkeypatch):
    # A proxy that does not exist: if NO_PROXY were ignored the relay would
    # try it and fail, and the asset would never reach the fake library.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    _, get = stack
    get("/assets/noproxy.js")
    assert _asset_hits("/assets/noproxy.js"), "NO_PROXY was ignored"


def test_an_anonymous_asset_is_relayed_without_the_stored_credential(stack):
    _, get = stack
    get("/assets/anon.js")
    hits = _asset_hits("/assets/anon.js")
    assert hits, "the asset request should still be relayed"
    for path, bearer, key, cookie in hits:
        assert bearer is None and key is None, (path, bearer, key)


def test_a_signed_in_asset_still_carries_the_library_credential(stack):
    _, get = stack
    get("/assets/authed.js", {"X-Api-Key": "k"})
    hits = _asset_hits("/assets/authed.js")
    assert hits and hits[-1][1] == "Bearer secret-token"
