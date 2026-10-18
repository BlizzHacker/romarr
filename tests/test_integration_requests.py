"""The integration request API, over HTTP.

Every route here was unreachable or crashed: the POST handler read
`route.method`, which a ParseResult does not have, so every request was a 500;
the GET routes lived inside the POST handler, where no GET reaches; and the
two status calls `grab` made for an external request did not match the
store's signatures, so a request that got as far as a download client raised
a TypeError instead of being handed off.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler
from romarr.selection import Release
from romarr.store import SeerrRequest


class FakeProwlarr:
    def __init__(self, releases):
        self._releases = releases
        self._config = type("C", (), {"base_url": "http://prowlarr", "api_key": "k"})()
        self.searches = 0

    def search(self, *a, **kw):
        self.searches += 1
        return list(self._releases)


class FakeClient:
    name = "fake"
    protocol = "torrent"
    configured = True

    def __init__(self, accept=True):
        self.added = []
        self._accept = accept

    def add(self, url, **kw):
        self.added.append(url)
        return self._accept


METROID = Release(title="Super Metroid (USA) [!].smc", size=3_000_000, seeders=20,
                  categories=(1030,), download_url="magnet:?xt=urn:btih:" + "a" * 40,
                  protocol="torrent", indexer="Test")


@pytest.fixture
def server(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    service.prowlarr = FakeProwlarr([METROID])
    service.indexers = []
    service.clients = [FakeClient()]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Api-Key": "k", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    yield service, call
    httpd.shutdown()


def test_info_answers_a_get(server):
    _, call = server
    status, body = call("GET", "/api/v1/integration/info")
    assert status == 200 and body["supports_resume"] is True


def test_a_request_is_handed_off_and_can_be_polled(server):
    service, call = server
    status, body = call("POST", "/api/v1/integration/requests",
                        {"name": "Super Metroid", "platform": "snes",
                         "externalRequestId": "peer-1"})
    assert (status, body["request_id"], body["status"]) == (202, "peer-1", "downloading")
    assert service.clients[0].added == [METROID.download_url]
    assert [q.external_request_id for q in service.queue] == ["peer-1"]

    assert call("GET", "/api/v1/integration/requests")[1]["requests"][0]["status"] == "downloading"
    assert call("GET", "/api/v1/integration/requests/current")[1]["request_id"] == "peer-1"


def test_a_retried_post_is_the_same_request(server):
    service, call = server
    for _ in range(2):
        call("POST", "/api/v1/integration/requests",
             {"name": "Super Metroid", "platform": "snes", "externalRequestId": "peer-1"})
    assert len(service.clients[0].added) == 1


def test_an_id_is_issued_when_the_peer_sends_none(server):
    _, call = server
    status, body = call("POST", "/api/v1/integration/requests",
                        {"name": "Super Metroid", "platform": "snes"})
    assert status == 202 and body["request_id"]


def test_failures_are_reported_not_left_searching(server):
    service, call = server
    _, body = call("POST", "/api/v1/integration/requests",
                   {"name": "Super Metroid", "platform": "no-such-console",
                    "externalRequestId": "peer-2"})
    assert body["status"] == "failed" and "unknown platform" in body["error"]

    # A client that refuses the release: the status call that used to raise.
    service.clients = [FakeClient(accept=False)]
    _, body = call("POST", "/api/v1/integration/requests",
                   {"name": "Super Metroid", "platform": "snes",
                    "externalRequestId": "peer-3"})
    assert body["status"] == "failed" and "rejected" in body["error"]


def test_a_cancelled_request_is_not_handed_off(server):
    service, _ = server
    service.store.put_seerr_request(SeerrRequest("peer-4", "snes", game="Super Metroid",
                                                 status="cancelled"))
    result = service.grab(METROID, "Super Metroid", "snes", external_request_id="peer-4")
    assert result == {"ok": False, "error": "the request was cancelled"}
    assert service.clients[0].added == []
