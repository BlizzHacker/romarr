"""WebSocket upgrades across the authenticated library relay."""

from __future__ import annotations

import base64
import http.client
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from romarr import portal_socket
from romarr.app import ROMarr, make_handler

FRAMES = b"\x81\x05hello"
CLIENT_FRAME = b"\x81\x81abcd\x19"
APP_KEY = "romarr-fixture-key"
LIBRARY_KEY = "library-fixture-key"


@pytest.fixture
def portal_stack(tmp_path):
    """Serve a local library with real upgrade and HTTP endpoints."""
    seen = []

    class FakeLibrary(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.headers.get("Upgrade"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"polling")
                return
            seen.append((self.path, self.headers.get("Origin"),
                         self.headers.get("Authorization"), self.headers.get("Sec-WebSocket-Extensions")))
            key = portal_socket.accept_key(self.headers["Sec-WebSocket-Key"])
            header = ("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {key}\r\n\r\n")
            # An Engine.IO greeting can arrive in the same packet as the handshake.
            self.connection.sendall(header.encode() + FRAMES)
            request = self.rfile.read(len(CLIENT_FRAME))
            if request == CLIENT_FRAME:
                self.connection.sendall(b"\x81\x02ok")

        def log_message(self, *args):
            pass

    library = ThreadingHTTPServer(("127.0.0.1", 0), FakeLibrary)
    threading.Thread(target=library.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{library.server_port}"
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": APP_KEY,
                      "LIBRARY_URL": origin, "LIBRARY_API_KEY": LIBRARY_KEY})
    app = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=app.serve_forever, daemon=True).start()
    try:
        yield service, origin, app.server_port, seen
    finally:
        app.shutdown()
        app.server_close()
        library.shutdown()
        library.server_close()


def upgrade(port, path, *, key=APP_KEY, origin=None):
    """Open a foreground WebSocket socket and return its response stream."""
    auth = f"X-Api-Key: {key}\r\n" if key else ""
    nonce = base64.b64encode(os.urandom(16)).decode()
    request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n"
               f"Origin: {origin or f'http://127.0.0.1:{port}'}\r\n"
               f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Extensions: permessage-deflate\r\n{auth}\r\n")
    connection = socket.create_connection(("127.0.0.1", port), timeout=4)
    connection.sendall(request.encode())
    return connection


def test_the_upgrade_pins_the_library_and_relays_in_both_directions(portal_stack):
    service, library, port, seen = portal_stack
    # Explicit selection must work independently of the last portal opened.
    service.active_portal_url = lambda: ""
    path = "/ws/socket.io/?EIO=4&transport=websocket"
    route = "/api/v1/portal/proxy?" + urlencode({"url": library, "path": path})
    with upgrade(port, route) as connection, connection.makefile("rb") as response:
        assert response.readline() == b"HTTP/1.1 101 Switching Protocols\r\n"
        http.client.parse_headers(response)
        assert response.read(len(FRAMES)) == FRAMES
        connection.sendall(CLIENT_FRAME)
        assert response.read(4) == b"\x81\x02ok"
    assert seen == [(path, library, f"Bearer {LIBRARY_KEY}", None)]


@pytest.mark.parametrize(("key", "origin", "path", "status"), [
    (None, None, "/ws/socket.io/", 401),
    (APP_KEY, "https://outside.invalid", "/ws/socket.io/", 403),
    (APP_KEY, None, "/ws/unrelated", 400),
])
def test_refused_upgrades_never_reach_the_library(portal_stack, key, origin, path, status):
    _, _, port, seen = portal_stack
    with upgrade(port, path, key=key, origin=origin) as connection, connection.makefile("rb") as response:
        assert response.readline().split()[1] == str(status).encode()
    assert seen == []


def test_engine_io_polling_still_uses_the_http_relay(portal_stack):
    _, _, port, _ = portal_stack
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        client.request("GET", "/ws/socket.io/?EIO=4&transport=polling", headers={"X-Api-Key": APP_KEY})
        response = client.getresponse()
        assert response.status == 200 and response.read() == b"polling"
    finally:
        client.close()


def test_public_origin_pins_its_scheme_even_behind_a_proxy():
    host = "romarr.example:8080"
    public = f"https://{host}"
    assert portal_socket.origin_allowed(public, "internal:7878", "", public)
    assert not portal_socket.origin_allowed(f"http://{host}", host, "http", public)
    assert not portal_socket.origin_allowed(f"https://{host}", host, "", "")
    assert portal_socket.origin_allowed(f"https://{host}", host, "", "", "https")


def test_tls_context_uses_requests_ca_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "ca"
    bundle.mkdir()
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "unused"))
    context = portal_socket._tls_context()
    assert context.check_hostname
    assert context.verify_mode == portal_socket.ssl.CERT_REQUIRED
