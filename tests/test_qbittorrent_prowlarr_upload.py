"""Issue #90: stage Prowlarr torrent bytes locally for remote qBittorrent."""
from pathlib import Path

import pytest

from romarr.app import ROMarr
from romarr.clients import QBittorrent, QbitConfig
from romarr.indexers import (
    Prowlarr, ProwlarrConfig, ProwlarrDownloadError,
)
from romarr.selection import Release


TORRENT = b"d4:infod4:name1:xee"


class Response:
    def __init__(self, status=200, *, chunks=(), headers=None, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self._chunks = chunks
        self._body = body if body is not None else []
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def json(self):
        return self._body


class FetchSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class QbitSession:
    def __init__(self, *, confirm=True):
        self.confirm = confirm
        self.posts = []
        self.gets = []
        self.tag = ""

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        self.tag = kwargs.get("data", {}).get("tags", "")
        return Response(200, body="Ok.")

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        rows = [{"tags": self.tag}] if self.confirm and self.tag else []
        return Response(200, body=rows)


def prowlarr(session):
    return Prowlarr(ProwlarrConfig("http://prowlarr:9696", "SUPERSECRET"), session)


def test_only_the_configured_prowlarr_origin_is_fetchable():
    p = prowlarr(FetchSession([]))
    assert p.is_prowlarr_download_url(
        "http://prowlarr:9696/1/download?apikey=SUPERSECRET")
    assert not p.is_prowlarr_download_url(
        "http://127.0.0.1:9696/1/download?apikey=SUPERSECRET")
    assert not p.is_prowlarr_download_url(
        "http://prowlarr.attacker.test:9696/1/download")
    assert not p.is_prowlarr_download_url(
        "http://user@prowlarr:9696/1/download")


def test_fetch_removes_query_key_and_sends_it_only_to_prowlarr():
    session = FetchSession([Response(chunks=[TORRENT])])
    p = prowlarr(session)
    result = p.fetch_torrent(
        "http://prowlarr:9696/1/download?apikey=SUPERSECRET&link=https%3A%2F%2Ftracker.example%2Fx")

    assert result == TORRENT
    url, kwargs = session.calls[0]
    assert "apikey" not in url.lower() and "SUPERSECRET" not in url
    assert kwargs["headers"] == {"X-Api-Key": "SUPERSECRET"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True


def test_same_origin_redirect_is_followed_without_query_key():
    session = FetchSession([
        Response(302, headers={
            "Location": "/files/result.torrent?apikey=SUPERSECRET"}),
        Response(chunks=[TORRENT]),
    ])
    result = prowlarr(session).fetch_torrent(
        "http://prowlarr:9696/1/download?apikey=SUPERSECRET")
    assert result == TORRENT
    assert len(session.calls) == 2
    assert all("apikey" not in call[0].lower() for call in session.calls)
    assert all(call[1]["headers"]["X-Api-Key"] == "SUPERSECRET"
               for call in session.calls)


def test_magnet_redirect_is_terminal_and_has_no_apikey():
    response = Response(302, headers={"Location":
        "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Metroid&apikey=SUPERSECRET"})
    session = FetchSession([response])
    magnet = prowlarr(session).fetch_torrent(
        "http://prowlarr:9696/1/download?apikey=SUPERSECRET")

    assert magnet == "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Metroid"
    assert response.closed is True
    assert len(session.calls) == 1
    assert "SUPERSECRET" not in magnet


def test_malformed_magnet_redirect_is_refused():
    response = Response(302, headers={"Location": "magnet:?dn=no-infohash"})
    session = FetchSession([response])
    with pytest.raises(ProwlarrDownloadError, match="outside its configured host"):
        prowlarr(session).fetch_torrent(
            "http://prowlarr:9696/1/download?apikey=SUPERSECRET")
    assert response.closed is True
    assert len(session.calls) == 1


def test_cross_origin_redirect_is_refused_and_never_followed():
    session = FetchSession([Response(
        302, headers={"Location": "https://attacker.example/file.torrent"})])
    p = prowlarr(session)
    with pytest.raises(ProwlarrDownloadError, match="outside its configured host") as err:
        p.fetch_torrent(
            "http://prowlarr:9696/1/download?apikey=SUPERSECRET")
    assert "SUPERSECRET" not in str(err.value)
    assert len(session.calls) == 1
    assert "attacker.example" not in repr(session.calls)


def test_oversized_content_length_is_rejected_before_read(monkeypatch):
    import romarr.indexers as indexers
    monkeypatch.setattr(indexers, "MAX_TORRENT_BYTES", 8)
    response = Response(headers={"Content-Length": "9"}, chunks=[TORRENT])
    p = prowlarr(FetchSession([response]))
    with pytest.raises(ProwlarrDownloadError, match="32 MiB safety limit"):
        p.fetch_torrent("http://prowlarr:9696/file.torrent")


def test_chunked_response_is_stopped_at_size_limit(monkeypatch):
    import romarr.indexers as indexers
    monkeypatch.setattr(indexers, "MAX_TORRENT_BYTES", 8)
    p = prowlarr(FetchSession([Response(chunks=[b"x" * 9])]))
    with pytest.raises(ProwlarrDownloadError, match="32 MiB safety limit"):
        p.fetch_torrent("http://prowlarr:9696/file.torrent")


@pytest.mark.parametrize("payload", [
    b"<html>login expired</html>",
    b"d4:infodi1ee",  # The metainfo info field must be a dictionary.
    b"d4:infod4:name1:x",  # Truncated.
])
def test_non_torrent_payload_is_refused(payload):
    p = prowlarr(FetchSession([Response(chunks=[payload])]))
    with pytest.raises(ProwlarrDownloadError, match="valid torrent"):
        p.fetch_torrent("http://prowlarr:9696/file.torrent")


def test_qbittorrent_uploads_multipart_and_requires_tag_confirmation(monkeypatch):
    import romarr.clients as clients
    monkeypatch.setattr(clients, "QBITTORRENT_ADD_CONFIRM_TIMEOUT", 0)
    session = QbitSession(confirm=True)
    qbit = QBittorrent(QbitConfig("https://seedbox.example"), session=session)

    assert qbit.add_torrent_file(TORRENT) is True
    url, kwargs = session.posts[0]
    assert url.endswith("/api/v2/torrents/add")
    assert kwargs["files"]["torrents"] == (
        "romarr.torrent", TORRENT, "application/x-bittorrent")
    assert "urls" not in kwargs.get("data", {})
    assert kwargs["data"]["tags"].startswith("romarr-")
    assert qbit.last_job_id == "qbit-tag:" + kwargs["data"]["tags"]
    assert session.gets[0][1]["params"] == {"tag": kwargs["data"]["tags"]}


def test_qbittorrent_http_200_pending_without_tag_is_not_success(monkeypatch):
    import romarr.clients as clients
    monkeypatch.setattr(clients, "QBITTORRENT_ADD_CONFIRM_TIMEOUT", 0)
    session = QbitSession(confirm=False)
    qbit = QBittorrent(QbitConfig("https://seedbox.example"), session=session)

    assert qbit.add_torrent_file(TORRENT) is False
    assert qbit.last_job_id == ""


def test_qbittorrent_receives_a_safe_magnet_from_prowlarr_redirect(tmp_path):
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Metroid"
    fetch_session = FetchSession([Response(
        302, headers={"Location": magnet + "&apikey=SUPERSECRET"})])
    qbit_session = QbitSession(confirm=True)
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.prowlarr = prowlarr(fetch_session)
    service.clients = [QBittorrent(
        QbitConfig("https://seedbox.example"), session=qbit_session)]
    release = Release(
        "Super Metroid (USA).smc", 3_000_000, 20, (1030,),
        "http://prowlarr:9696/1/download?apikey=SUPERSECRET",
        "torrent", indexer="Prowlarr")

    result = service.grab(release, "Super Metroid", "snes")

    assert result["ok"] is True
    assert qbit_session.posts[0][1]["data"]["urls"] == magnet
    assert "SUPERSECRET" not in repr(qbit_session.posts)
    assert service.queue[-1].state == "grabbed"
    assert service.queue[-1].download_id.startswith("qbit-tag:romarr-")


def test_remote_qbittorrent_never_receives_prowlarr_url_or_key(tmp_path, caplog):
    fetch_session = FetchSession([Response(chunks=[TORRENT])])
    qbit_session = QbitSession(confirm=True)
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.prowlarr = prowlarr(fetch_session)
    service.clients = [QBittorrent(
        QbitConfig("https://seedbox.example"), session=qbit_session)]
    release = Release(
        "Super Metroid (USA).smc", 3_000_000, 20, (1030,),
        "http://prowlarr:9696/1/download?apikey=SUPERSECRET&link=https%3A%2F%2Ftracker.example%2Fdl",
        "torrent", indexer="Prowlarr")

    result = service.grab(release, "Super Metroid", "snes")

    assert result["ok"] is True
    qbit_wire = repr((qbit_session.posts, qbit_session.gets))
    assert "SUPERSECRET" not in qbit_wire
    assert "prowlarr" not in qbit_wire.lower()
    assert "urls" not in repr(qbit_session.posts[0][1].get("data", {}))
    assert service.queue[-1].state == "grabbed"
    assert service.queue[-1].download_id.startswith("qbit-tag:romarr-")
    assert "SUPERSECRET" not in caplog.text


@pytest.fixture
def debrid_service(tmp_path, monkeypatch):
    """Use the real grab and Real-Debrid add path with isolated external calls."""
    from romarr.downloaders import RealDebrid, RealDebridConfig
    from romarr.scheduler import Scheduler
    monkeypatch.setattr(Scheduler, "start", lambda self: None)
    monkeypatch.setattr(ROMarr, "_refresh_counts", lambda self: None)
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    client = RealDebrid(RealDebridConfig(api_token="test", save_path=str(tmp_path / "downloads")))
    calls = []

    def post(path, data):
        """Record the exact magnet submitted and model the service response."""
        calls.append((path, data))
        return {"id": "torrent-id"} if path == "/torrents/addMagnet" else {}

    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr(service, "notify", lambda *args: [])
    service.clients = [client]
    return service, client, calls


def debrid_release(url):
    """Model the BlueRoms release returned through Prowlarr's proxy."""
    return Release("Halo 2 - Xbox", 4_000_000_000, 20, (1000,), url,
                   "torrent", indexer="Prowlarr")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_realdebrid_resolves_trusted_prowlarr_magnet_redirect(debrid_service, status):
    """Only the sanitized magnet reaches Real-Debrid, never the proxy key/URL."""
    service, client, calls = debrid_service
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Halo"
    session = FetchSession([Response(status, headers={"Location": magnet + "&apikey=SUPERSECRET"})])
    service.prowlarr = prowlarr(session)
    result = service.grab(debrid_release("http://prowlarr:9696/1/download?apikey=SUPERSECRET"), "Halo 2", "xbox")
    assert result["ok"]
    assert calls == [("/torrents/addMagnet", {"magnet": magnet}),
                     ("/torrents/selectFiles/torrent-id", {"files": "all"})]
    assert "SUPERSECRET" not in repr(calls) and "prowlarr" not in repr(calls)
    assert service.queue[-1].state == "grabbed"
    assert client._ours() == ["torrent-id"]


@pytest.mark.parametrize("location", ["https://attacker.example/download", "magnet:?dn=no-hash"])
def test_realdebrid_proxy_refuses_unsafe_or_invalid_redirects(debrid_service, location, caplog):
    """Refused redirects never contact the external destination or debrid API."""
    service, client, calls = debrid_service
    session = FetchSession([Response(302, headers={"Location": location})])
    service.prowlarr = prowlarr(session)
    result = service.grab(debrid_release("http://prowlarr:9696/1/download?apikey=SUPERSECRET"), "Halo 2", "xbox")
    assert not result["ok"] and not calls and len(session.calls) == 1
    assert "SUPERSECRET" not in caplog.text


def test_realdebrid_direct_magnet_avoids_proxy_fetch(debrid_service):
    """Keep existing direct-magnet grabs unchanged."""
    service, client, calls = debrid_service
    session = FetchSession([])
    service.prowlarr = prowlarr(session)
    magnet = "magnet:?xt=urn:btih:" + "b" * 40
    assert service.grab(debrid_release(magnet), "Halo 2", "xbox")["ok"]
    assert not session.calls and calls[0][1] == {"magnet": magnet}


def test_realdebrid_torrent_file_is_clear_failure_not_exception(debrid_service):
    """A proxy returning actual bytes must not call an unsupported upload method."""
    service, client, calls = debrid_service
    service.prowlarr = prowlarr(FetchSession([Response(chunks=[TORRENT])]))
    result = service.grab(debrid_release("http://prowlarr:9696/1/download?apikey=SUPERSECRET"), "Halo 2", "xbox")
    assert not result["ok"] and "requires a magnet" in result["error"] and not calls
