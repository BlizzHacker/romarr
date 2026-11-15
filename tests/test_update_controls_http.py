"""Exercise update owner controls through the actual authenticated HTTP handler."""
from http.server import ThreadingHTTPServer
import threading
import requests
import pytest
from romarr.app import ROMarr, make_handler
from romarr import release_controls

@pytest.fixture
def endpoint(tmp_path):
    app = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": "test-update-owner"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:%s" % server.server_port
    finally:
        server.shutdown(); server.server_close(); worker.join()

@pytest.mark.parametrize("method,path", [("get", "/prs"), ("get", "/releases"),
    ("post", "/pr"), ("post", "/pr/apply"), ("post", "/pr/restore")])
def test_update_preview_routes_require_owner_auth(endpoint, method, path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unauthenticated request reached preview metadata or mutation")
    for name in ("list_prs", "releases", "inspect_pr", "apply_pr", "restore_preview"):
        monkeypatch.setattr(release_controls, name, unexpected)
    response = getattr(requests, method)(endpoint + "/api/v1/update" + path, json={}, timeout=5)
    assert response.status_code == 401


def test_authenticated_catalogue_and_invalid_apply_are_bounded(endpoint, monkeypatch):
    headers = {"X-Api-Key": "test-update-owner"}
    monkeypatch.setattr(release_controls, "list_prs", lambda: [{"number": 201, "title": "Preview"}])
    response = requests.get(endpoint + "/api/v1/update/prs", headers=headers, timeout=5)
    assert response.status_code == 200 and response.json()["items"][0]["number"] == 201
    for path, body in [("/pr", {"number": "201;echo no"}), ("/pr", []),
                       ("/pr/apply", {}), ("/pr/restore", {"confirm": False})]:
        response = requests.post(endpoint + "/api/v1/update" + path, headers=headers, json=body, timeout=5)
        assert response.status_code == 400 and response.json()["error"]
    response = requests.get(endpoint + "/api/v1/update/releases?channel=unknown", headers=headers, timeout=5)
    assert response.status_code == 400


def test_all_channel_choices_persist_through_real_settings_api(endpoint):
    headers = {"X-Api-Key": "test-update-owner"}
    for channel in ("stable", "rc", "beta", "alpha", "nightly"):
        response = requests.put(endpoint + "/api/v1/config", headers=headers,
                                json={"update_channel": channel}, timeout=5)
        assert response.status_code == 200
        current = requests.get(endpoint + "/api/v1/update", headers=headers, timeout=5)
        assert current.status_code == 200
        assert current.json()["settings"]["update_channel"] == channel
