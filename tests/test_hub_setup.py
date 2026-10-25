"""Own-source installs perform the install after checking the allowed forge."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr import app
from romarr.app import ROMarr, make_handler


@pytest.fixture
def server(tmp_path, monkeypatch):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "ROMARR_API_KEY": "testkey"})
    installs = []
    monkeypatch.setattr(app.hub, "install",
                        lambda source: installs.append(source) or {"ok": True, "out": "Installed"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service, installs
    httpd.shutdown()
    httpd.server_close()


def post(base, path, body, authenticated=True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["X-Api-Key"] = "testkey"
    req = urllib.request.Request(base + path, method="POST", headers=headers,
                                 data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_allowed_repository_is_actually_installed(server):
    base, _, installs = server
    code, result = post(base, "/api/v1/hub/source/install",
                        {"url": "https://github.com/example/plugin"})
    assert code == 200 and result["ok"] is True
    assert installs == ["https://github.com/example/plugin"]


@pytest.mark.parametrize("source", [
    "http://github.com/example/plugin", "https://unapproved.example/plugin",
    "https://[invalid-host/example/plugin",
    "/local/plugin", "git@github.com:example/plugin",
    "https://user:secret@github.com/example/plugin",
])
def test_rejected_source_never_reaches_installer(server, source):
    base, _, installs = server
    code, result = post(base, "/api/v1/hub/source/install", {"url": source})
    assert code == 400 and result["ok"] is False and result["error"]
    assert installs == []


def test_configured_source_hosts_are_applied(server):
    base, service, installs = server
    service.store.settings["plugin_hosts"] = ["forge.example"]
    assert post(base, "/api/v1/hub/source/install",
                {"url": "https://github.com/example/plugin"})[0] == 400
    assert post(base, "/api/v1/hub/source/install",
                {"url": "https://forge.example/example/plugin"})[0] == 200
    assert installs == ["https://forge.example/example/plugin"]


def test_installer_failure_is_reported_instead_of_claiming_success(server, monkeypatch):
    base, _, _ = server
    monkeypatch.setattr(app.hub, "install",
                        lambda source: {"ok": False, "err": "ROM Hub is not installed"})
    code, result = post(base, "/api/v1/hub/source/install",
                        {"url": "https://github.com/example/plugin"})
    assert code == 500 and result["ok"] is False
    assert result["error"] == "ROM Hub is not installed"


def test_source_check_remains_a_preview_only(server):
    base, _, installs = server
    code, result = post(base, "/api/v1/hub/source/check",
                        {"url": "https://github.com/example/plugin"})
    assert code == 200 and result["ok"] is True
    assert installs == []


def test_source_install_requires_authentication(server):
    base, _, installs = server
    assert post(base, "/api/v1/hub/source/install",
                {"url": "https://github.com/example/plugin"}, False)[0] == 401
    assert installs == []


def test_generic_url_install_cannot_bypass_source_gate(server):
    base, _, installs = server
    code, result = post(base, "/api/v1/hub/plugin", {
        "slug": "https://unapproved.example/plugin", "action": "install"})
    assert code == 400 and result["ok"] is False
    assert installs == []


def test_generic_catalogue_slug_install_still_works(server):
    base, _, installs = server
    code, result = post(base, "/api/v1/hub/plugin",
                        {"slug": "my-plugin", "action": "install"})
    assert code == 200 and result["ok"] is True
    assert installs == ["my-plugin"]
