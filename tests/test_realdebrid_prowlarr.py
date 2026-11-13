"""Resolve trusted Prowlarr download redirects before the Real-Debrid handoff."""

import io
import json
from unittest.mock import Mock
from urllib.parse import quote

import pytest
import requests

from romarr.app import ROMarr
from romarr.downloaders import RealDebrid, RealDebridConfig, match_queue_item
from romarr.indexers import Prowlarr, ProwlarrConfig, ProwlarrDownloadError
from romarr.selection import Release


MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Halo%202%20-%20Xbox"
KEY = "PROWLARR_TEST_SECRET"
URL = f"http://prowlarr:9696/1/download?apikey={KEY}&link=opaque"


class Session:
    """Record requests and return canned responses without network access."""

    def __init__(self, *responses):
        """Queue responses or request exceptions for successive calls."""
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        """Record a local resolver request without following redirects."""
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        """Record a Real-Debrid request and return its next API response."""
        return self.get(url, **kwargs)


def response(status=301, location=MAGNET, *, body=None):
    """Build a real requests response whose body must never be streamed."""
    result = requests.Response()
    result.status_code = status
    if location is not None:
        result.headers["Location"] = location
    result._content = json.dumps(body).encode() if body is not None else b""
    result._content_consumed = True
    result.close = Mock()
    result.iter_content = Mock(side_effect=AssertionError("must not read the body"))
    return result


def prowlarr(session, base="http://prowlarr:9696"):
    """Configure the sole trusted Prowlarr endpoint with a short timeout."""
    return Prowlarr(ProwlarrConfig(base, KEY, timeout=2), session)


def service(tmp_path, fetch_session):
    """Wire the actual grab path to isolated Prowlarr and Real-Debrid fakes."""
    app = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    app.prowlarr = prowlarr(fetch_session)
    rd_session = Session(response(201, body={"id": "T1"}), response(204))
    app.clients = [RealDebrid(RealDebridConfig(
        api_token="RD_TEST_SECRET", save_path=str(tmp_path / "downloads")),
        session=rd_session)]
    return app, rd_session


def grab(app, url=URL):
    """Reproduce BlueRoms' HTTP-only Halo 2 release through the service."""
    release = Release("Halo 2 - Xbox", 3_000_000, 20, (1030,), url,
                      "torrent", indexer="BlueRoms")
    return app.grab(release, "Halo 2", "xbox")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_realdebrid_receives_resolved_magnet(tmp_path, status, caplog):
    """All supported redirect statuses reach RD without the proxy URL or key."""
    fetch = Session(response(status, MAGNET + f"&api%6bey={KEY}"))
    app, rd = service(tmp_path, fetch)

    release = app.prowlarr._to_release({
        "title": "Halo 2 - Xbox", "size": 3_000_000, "seeders": 20,
        "categories": [{"id": 1030}], "magnetUrl": "", "downloadUrl": URL,
        "protocol": "torrent", "indexer": "BlueRoms",
    })
    assert app.grab(release, "Halo 2", "xbox")["ok"] is True
    assert rd.calls[0][1]["data"] == {"magnet": MAGNET}
    assert rd.calls[1][0].endswith("/torrents/selectFiles/T1")
    assert app.clients[0]._ours() == ["T1"]
    assert app.queue[-1].state == "grabbed"
    assert len(fetch.calls) == 1
    url, kwargs = fetch.calls[0]
    assert "apikey" not in url.lower() and KEY not in url
    assert kwargs["headers"] == {"X-Api-Key": KEY}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert 0 < kwargs["timeout"] <= 2
    assert "RD_TEST_SECRET" not in repr(fetch.calls)
    assert KEY not in repr(rd.calls) + caplog.text
    assert "prowlarr" not in repr(rd.calls).lower()


def test_direct_magnet_does_not_contact_prowlarr(tmp_path):
    """Existing direct magnets retain their original handoff."""
    fetch = Session()
    app, rd = service(tmp_path, fetch)
    assert grab(app, MAGNET)["ok"] is True
    assert rd.calls[0][1]["data"] == {"magnet": MAGNET}
    assert fetch.calls == []


@pytest.mark.parametrize("location", [
    "https://attacker.example/file", "//attacker.example/1/download",
    "http://127.0.0.1:9696/1/download", "http://169.254.169.254/latest/meta-data/",
    "http://prowlarr.attacker.example:9696/1/download",
    "http://prowlarr:9697/1/download", "https://prowlarr:9696/1/download",
    "http://user:password@prowlarr:9696/1/download",
    "http://prowlarr:9696@attacker.example/1/download",
    "http://[invalid/1/download", "file:///etc/passwd", "ftp://prowlarr:9696/x",
    "/api/v1/system/shutdown", "/1/../api/v1/config", "/%31/download",
    "http://prowlarr:9696/1/\\download", "\thttp://prowlarr:9696/1/download",
    "magnet:?dn=no-infohash", "magnet:?xt=urn:btih:abc",
    "magnet:?xt=urn:btih:" + "g" * 40,
    "magnet://attacker.example?xt=urn:btih:" + "a" * 40,
    "magnet:unexpected?xt=urn:btih:" + "a" * 40,
    MAGNET + "%0d%0aInjected", MAGNET + "\nInjected", MAGNET + "%00",
    MAGNET + f"&dn={KEY}", MAGNET + f"&tr=https%3A%2F%2Ftracker.test%2F{KEY}",
    MAGNET + "&tr=" + "%2550" + KEY[1:],
    MAGNET + "&dn=" + "%2525252541", MAGNET + "&dn=" + "x" * 8192,
    "", None,
])
def test_invalid_redirect_never_reaches_rd(tmp_path, location, caplog):
    """Reject malformed magnets and unsafe HTTP hops before another request."""
    reply = response(location=location)
    fetch = Session(reply)
    app, rd = service(tmp_path, fetch)

    result = grab(app)

    assert result["ok"] is False
    assert len(fetch.calls) == 1
    assert rd.calls == []
    assert app.queue[-1].state == "failed"
    assert app.queue[-1].release_fault is False
    assert KEY not in repr(result) + caplog.text + repr(app.store.events)
    reply.close.assert_called_once()
    reply.iter_content.assert_not_called()


@pytest.mark.parametrize("url", [
    "https://attacker.example/1/download", "http://127.0.0.1:9696/1/download",
    "//prowlarr:9696/1/download", "http://user@prowlarr:9696/1/download",
    "http://prowlarr:9696/api/v1/system/shutdown", "file:///etc/passwd",
    "http://prowlarr:9696/1/%64ownload", "http://prowlarr:9696/1/../1/download",
    "http://prowlarr:9696/1/download/", "http://prowlarr:9696/1/download\t",
])
def test_untrusted_input_is_not_fetched(tmp_path, url):
    """A result can trigger neither arbitrary local requests nor cloud calls."""
    fetch = Session()
    app, rd = service(tmp_path, fetch)
    assert grab(app, url)["ok"] is False
    assert fetch.calls == rd.calls == []


@pytest.mark.parametrize("status", [200, 204, 300, 304, 400, 401, 404, 500])
def test_non_magnet_response_fails_without_reading_body(tmp_path, status):
    """No torrent, HTML page, or response body is consumed or passed to RD."""
    reply = response(status, location=None)
    fetch = Session(reply)
    app, rd = service(tmp_path, fetch)
    assert grab(app)["ok"] is False
    assert rd.calls == []
    reply.close.assert_called_once()
    reply.iter_content.assert_not_called()


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError, ValueError])
def test_request_failure_is_safe_and_retryable(tmp_path, error, caplog):
    """Network failures expose no URL or credentials and do not blame releases."""
    fetch = Session(error(URL))
    app, rd = service(tmp_path, fetch)
    result = grab(app)
    assert result["ok"] is False
    assert rd.calls == []
    assert app.queue[-1].release_fault is False
    assert KEY not in repr(result) + caplog.text


@pytest.mark.parametrize("location", [
    f"/prowlarr/2/download?apiKey={KEY}",
    f"http://prowlarr:9696/prowlarr/2/download?apiKey={KEY}",
    f"../2/download?apiKey={KEY}",
])
def test_redirects_under_configured_url_base(location):
    """Follow only download endpoints under the configured reverse proxy base."""
    replies = [response(302, location), response()]
    fetch = Session(*replies)
    client = prowlarr(fetch, "http://prowlarr:9696/prowlarr/")
    assert client.fetch_torrent(
        URL.replace("/1/download", "/prowlarr/1/download"), magnet_only=True) == MAGNET
    assert len(fetch.calls) == 2
    assert all(KEY not in url and "apikey" not in url.lower() for url, _ in fetch.calls)
    for reply in replies:
        reply.close.assert_called_once()


@pytest.mark.parametrize("url", [URL, URL.replace("/1/download", "/prowlarr-other/1/download")])
def test_same_origin_sibling_app_is_not_trusted(url):
    """A shared reverse proxy origin does not grant access to sibling apps."""
    fetch = Session()
    with pytest.raises(ProwlarrDownloadError):
        prowlarr(fetch, "http://prowlarr:9696/prowlarr").fetch_torrent(url, magnet_only=True)
    assert fetch.calls == []


def test_redirect_cannot_escape_url_base():
    """Recheck the URL base on each hop, before sending the API key."""
    fetch = Session(response(302, "/other-app/1/download"))
    with pytest.raises(ProwlarrDownloadError):
        prowlarr(fetch, "http://prowlarr:9696/prowlarr").fetch_torrent(
            URL.replace("/1/download", "/prowlarr/1/download"), magnet_only=True)
    assert len(fetch.calls) == 1


def test_redirect_loop_is_bounded():
    """Stop loops after six requests, closing every response."""
    replies = [response(302, URL) for _ in range(6)]
    fetch = Session(*replies)
    with pytest.raises(ProwlarrDownloadError, match="too many redirects"):
        prowlarr(fetch).fetch_torrent(URL, magnet_only=True)
    assert len(fetch.calls) == 6
    for reply in replies:
        reply.close.assert_called_once()


def test_redirects_share_a_timeout_budget(monkeypatch):
    """A stalled chain cannot multiply the configured timeout by the hop cap."""
    ticks = iter([0, 0.5, 1.0, 1.5, 1.8, 2.1])
    monkeypatch.setattr("romarr.indexers.monotonic", lambda: next(ticks))
    fetch = Session(response(302, URL), response(302, URL))
    with pytest.raises(ProwlarrDownloadError, match="timed out"):
        prowlarr(fetch).fetch_torrent(URL, magnet_only=True)
    assert [kwargs["timeout"] for _, kwargs in fetch.calls] == [1.5, 0.5]


@pytest.mark.parametrize("magnet", [
    MAGNET, "MAGNET:?xt=urn:btih:" + "A" * 40,
    "magnet:?xt=" + quote("urn:btih:" + "A" * 32),
    "magnet:?xt=urn:btmh:1220" + "a" * 64,
])
def test_valid_magnet_identities(magnet):
    """Accept validated hex/base32 BTIH and SHA-256 multihash identities."""
    fetch = Session(response(location=magnet))
    resolved = prowlarr(fetch).fetch_torrent(URL, magnet_only=True)
    assert resolved == "magnet:" + magnet.split(":", 1)[1]


def test_magnet_keeps_tracker_metadata_but_drops_proxy_credentials():
    """Preserve a release's trackers while dropping all top-level API key fields."""
    magnet = MAGNET + "&tr=https%3A%2F%2Ftracker.example%2Fannounce%3Fpasskey%3Dtracker-token"
    fetch = Session(response(location=magnet + f"&APIKEY={KEY}&api%6bey={KEY}#ignored"))
    assert prowlarr(fetch).fetch_torrent(URL, magnet_only=True) == magnet


def test_rd_rejection_is_not_reported_as_success(tmp_path):
    """Resolving a magnet alone does not confirm the cloud job was accepted."""
    app, rd = service(tmp_path, Session(response()))
    rd.responses = [response(400, body={"error": "invalid_magnet"})]
    assert grab(app)["ok"] is False
    assert len(rd.calls) == 1
    assert app.queue[-1].state == "failed"


def test_other_clients_retain_their_url_handoff(tmp_path):
    """The new resolver is scoped to Real-Debrid; other clients keep their URL."""
    fetch = Session()
    app, _ = service(tmp_path, fetch)
    client = Mock(protocol="torrent", name="Other client", TAKES_NAME=False,
                  supports_selective=False, supports_torrent_file=False,
                  last_job_id="job-1", romarr_id="other-client")
    client.add.return_value = True
    app.clients = [client]
    assert grab(app)["ok"] is True
    client.add.assert_called_once_with(URL)
    assert fetch.calls == []


class UnreadBody:
    """Fail if Requests consumes a redirect body before ROMarr validates it."""

    def __init__(self):
        """Track response cleanup without allocating or downloading a body."""
        self.closed = False

    def read(self, *args, **kwargs):
        """Reject eager buffering, including Requests' Response.next preparation."""
        raise AssertionError("redirect body must not be read")

    def close(self):
        """Record that the unread network response was closed."""
        self.closed = True

    def release_conn(self):
        """Provide the urllib3 cleanup interface used by requests.Response."""


class Adapter(requests.adapters.BaseAdapter):
    """Exercise real Requests session logic with an in-memory transport."""

    def __init__(self, replies):
        """Queue raw responses for the real session's prepared HTTP requests."""
        self.replies = list(replies)
        self.calls = []

    def send(self, request, **kwargs):
        """Return headers without letting the session access a network."""
        self.calls.append(request)
        reply = self.replies.pop(0)
        reply.request = request
        reply.url = request.url
        return reply

    def close(self):
        """Implement the adapter resource interface without opening sockets."""


def raw_response(status, location):
    """Build an unconsumed redirect response, unlike the canned JSON helpers."""
    reply = requests.Response()
    reply.status_code = status
    reply.headers["Location"] = location
    reply.raw = UnreadBody()
    return reply


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("magnet_only", [False, True])
def test_real_session_does_not_consume_redirect_bodies(status, magnet_only):
    """Requests must not read an unlimited redirect body to prepare Response.next."""
    reply = raw_response(status, MAGNET)
    adapter = Adapter([reply])
    client = Prowlarr(ProwlarrConfig("http://prowlarr:9696", KEY))
    with client._session:
        client._session.mount("http://", adapter)
        assert client.fetch_torrent(URL, magnet_only=magnet_only) == MAGNET
    assert len(adapter.calls) == 1
    assert adapter.calls[0].headers["X-Api-Key"] == KEY
    assert KEY not in adapter.calls[0].url
    assert reply.raw.closed
    assert reply._content is False


def test_normal_session_redirects_are_preserved():
    """Ordinary Prowlarr API requests still use Requests' automatic redirects."""
    redirect = raw_response(302, "/api/v1/search-next")
    redirect.raw = io.BytesIO(b"redirect")
    final = response(200, location=None, body=[])
    adapter = Adapter([redirect, final])
    client = Prowlarr(ProwlarrConfig("http://prowlarr:9696", KEY))
    with client._session:
        client._session.mount("http://", adapter)
        result = client._session.get("http://prowlarr:9696/api/v1/search", timeout=2)
    assert result.json() == []
    assert len(adapter.calls) == 2
    assert adapter.calls[-1].url == "http://prowlarr:9696/api/v1/search-next"


def test_real_session_records_malformed_redirect_failure(tmp_path):
    """A Location parsing error must become a failed queue row, not an exception."""
    reply = raw_response(301, "http://[invalid/1/download")
    adapter = Adapter([reply])
    app, rd = service(tmp_path, Session())
    app.prowlarr = Prowlarr(ProwlarrConfig("http://prowlarr:9696", KEY))
    with app.prowlarr._session:
        app.prowlarr._session.mount("http://", adapter)
        assert grab(app)["ok"] is False
    assert app.queue[-1].state == "failed"
    assert rd.calls == []
    assert reply.raw.closed


def test_terminal_response_after_deadline_is_rejected(monkeypatch):
    """A slow headers response cannot turn an expired request into success."""
    ticks = iter([0, 0.5, 2.1])
    monkeypatch.setattr("romarr.indexers.monotonic", lambda: next(ticks))
    reply = response()
    with pytest.raises(ProwlarrDownloadError, match="timed out"):
        prowlarr(Session(reply)).fetch_torrent(URL, magnet_only=True)
    reply.close.assert_called_once()


@pytest.mark.parametrize(("magnet", "digest"), [
    (MAGNET, "a" * 40),
    ("magnet:?xt=urn:btih:" + "A" * 32, "0" * 40),
    ("magnet:?xt=" + quote("urn:btih:" + "a" * 40), "a" * 40),
    (MAGNET.replace("?xt=", "?XT="), "a" * 40),
    (MAGNET.replace("?xt=", "?Xt="), "a" * 40),
    (MAGNET.replace("?xt=", "?xT="), "a" * 40),
    (MAGNET.replace("?xt=", "?%58%54="), "a" * 40),
])
def test_resolved_hash_matches_a_renamed_rd_download(tmp_path, magnet, digest):
    """Persist a usable torrent identity so RD's different filename still imports."""
    app, _ = service(tmp_path, Session(response(location=magnet)))
    assert grab(app)["ok"] is True
    row = app.queue[-1]
    assert row.release_id == "btih:" + digest
    assert match_queue_item(app.queue, {"hash": digest, "name": "Halo.2.Xbox"}) is row
    assert match_queue_item(app.queue, {"hash": "b" * 40, "name": row.release}) is None
    assert json.loads((tmp_path / "state.json").read_text())["queue"][-1]["release_id"] == row.release_id
