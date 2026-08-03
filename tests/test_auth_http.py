"""The gate as a request actually meets it.

The unit tests prove `Auth` decides correctly. These prove the decision is
*applied* -- against a real socket, because a gate that is right in isolation
and unwired is exactly as open as no gate at all.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler


@pytest.fixture
def server(tmp_path):
    """A live ROMarr on a loopback port, with auth on and a known key."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_API_KEY": "testkey"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()


def get(url, key=None, cookie=None, method="GET", body=None):
    request = urllib.request.Request(url, method=method)
    if key:
        request.add_header("X-Api-Key", key)
    if cookie:
        request.add_header("Cookie", cookie)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


# --- the gate holds --------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/v1/game",
    "/api/v1/queue",
    "/api/v1/config",
    "/api/v1/system/status",
    "/api/platforms",
    "/api/queue",
])
def test_reading_anything_needs_a_key(server, path):
    base, _ = server
    code, _, _ = get(base + path)
    assert code == 401, f"{path} answered {code} with no key"


@pytest.mark.parametrize("path", [
    "/api/v1/game",
    "/api/v1/system/status",
    "/api/platforms",
])
def test_the_key_opens_it(server, path):
    base, _ = server
    code, _, _ = get(base + path, key="testkey")
    assert code == 200, path


def test_a_wrong_key_is_refused(server):
    base, _ = server
    code, _, _ = get(base + "/api/v1/game", key="wrong")
    assert code == 401


def test_writing_needs_a_key(server):
    """The endpoints that matter most: these queue downloads and rewrite
    settings, and they were the ones answering anybody."""
    base, _ = server
    for method, path, body in [
        ("POST", "/api/v1/webhook", {"data": {"game_title": "x",
                                              "platforms": ["snes"]}}),
        ("PUT", "/api/v1/config", {"min_seeders": 5}),
    ]:
        code, _, _ = get(base + path, method=method, body=body)
        assert code == 401, f"{method} {path} answered {code}"


def test_a_401_says_how_to_authenticate(server):
    base, _ = server
    _, body, _ = get(base + "/api/v1/game")
    assert b"X-Api-Key" in body


# --- what stays open, and how little it says -------------------------------

def test_the_healthcheck_still_works_without_a_key(server):
    """The container HEALTHCHECK has no credential and must not need one."""
    base, _ = server
    code, body, _ = get(base + "/api/health")
    assert code == 200
    assert json.loads(body)["ok"] is True


def test_an_unauthenticated_healthcheck_gives_away_nothing(server):
    """It used to return library paths, client URLs and counts. A liveness
    probe needs one bit; anything more is free reconnaissance."""
    base, _ = server
    _, body, _ = get(base + "/api/health")
    payload = json.loads(body)
    assert set(payload) == {"ok"}


def test_an_authenticated_healthcheck_is_still_the_full_report(server):
    base, _ = server
    _, body, _ = get(base + "/api/health", key="testkey")
    payload = json.loads(body)
    assert "libraries" in payload and "platforms" in payload


def test_the_ui_shell_is_served_so_there_is_somewhere_to_log_in(server):
    base, _ = server
    code, body, _ = get(base + "/")
    assert code == 200 and b"<" in body


# --- logging in ------------------------------------------------------------

def test_the_key_can_be_exchanged_for_a_session(server):
    base, _ = server
    code, _, headers = get(base + "/api/v1/login", method="POST",
                           body={"apikey": "testkey"})
    assert code == 200
    cookie = headers.get("Set-Cookie", "")
    assert "romarr_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite" in cookie

    session = cookie.split(";")[0]
    code, _, _ = get(base + "/api/v1/game", cookie=session)
    assert code == 200, "the session it just issued must work"


def test_a_bad_login_is_refused_and_sets_no_cookie(server):
    base, _ = server
    code, _, headers = get(base + "/api/v1/login", method="POST",
                           body={"apikey": "wrong"})
    assert code == 401
    assert "romarr_session=" not in headers.get("Set-Cookie", "")


# --- the escape hatch, end to end ------------------------------------------

def test_auth_disabled_opens_everything_deliberately(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_AUTH": "disabled"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert get(base + "/api/v1/game")[0] == 200
        assert set(json.loads(get(base + "/api/health")[1])) != {"ok"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_key_is_generated_when_none_is_configured(tmp_path):
    """Secure by default. An install that is open until somebody reads the
    documentation is an install that is open."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    assert service.auth.enabled
    assert len(service.auth.api_key) >= 32


def test_the_generated_key_survives_a_restart(tmp_path):
    data = str(tmp_path / "s.json")
    first = ROMarr({"ROMARR_DATA": data}).auth.api_key
    second = ROMarr({"ROMARR_DATA": data}).auth.api_key
    assert first == second and first


def test_the_key_is_never_returned_by_the_config_endpoint(server):
    """It is a credential, and that endpoint feeds a browser page."""
    base, service = server
    _, body, _ = get(base + "/api/v1/config", key="testkey")
    assert b"testkey" not in body
