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
