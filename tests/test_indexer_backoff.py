"""Shared indexers stop receiving requests after 429; timeout edits reach HTTP."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
import requests

from romarr import indexers
from romarr.app import ROMarr
from romarr.indexers import (
    INDEXER_TYPES, IndexerBackoff, Prowlarr, ProwlarrConfig,
    Torznab, TorznabConfig, build_indexer, indexer_timeout,
)
from test_indexer_secret_merge import drive


class Response:
    """A tiny response whose closure is observable."""

    def __init__(self, status=200, retry=None):
        self.status_code = status
        self.ok = status < 400
        self.headers = {} if retry is None else {"Retry-After": retry}
        self.closed = False
        self.text = "<rss><channel/></rss>"

    def close(self):
        """Record that a rejected response returned its connection."""
        self.closed = True

    def raise_for_status(self):
        """Match requests' ordinary non-429 error behavior."""
        if not self.ok:
            raise requests.HTTPError("ordinary HTTP failure")

    def json(self):
        """Return empty results suitable for Prowlarr and TorrentPotato."""
        return []


class Session:
    """Return queued replies and record real request arguments."""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        """Record a call without networking."""
        self.calls.append((url, kwargs))
        return next(self.responses)


@pytest.fixture
def clock(monkeypatch):
    """Advance cooldown deadlines deterministically without sleeping."""
    now = [1000.0]
    monkeypatch.setattr(indexers.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(indexers.time, "time", lambda: now[0])
    return now


@pytest.mark.parametrize("kind", ["torznab", "newznab", "torrentrss", "torrentpotato", "prowlarr"])
def test_all_clients_suppress_repeated_search_and_caps_during_cooldown(kind, clock):
    """Search and Test share one guard, and sibling clients keep functioning."""
    limited = Response(429, "45")
    session = Session(limited, Response())
    if kind == "prowlarr":
        client = Prowlarr(ProwlarrConfig("https://example/api", "secret"), session)
        test = client.indexers
    else:
        client = build_indexer({"type": kind, "url": "https://example/api?passkey=secret"})
        client._session = session
        test = client.caps
    with pytest.raises(IndexerBackoff, match="45 seconds") as failure:
        client.search("Mario")
    assert "secret" not in str(failure.value)
    assert limited.closed
    with pytest.raises(IndexerBackoff):
        test()
    assert len(session.calls) == 1
    # A different indexer is not paused by this one.
    sibling = Torznab(TorznabConfig("https://other/api", "key"), Session(Response()))
    assert sibling.search("Mario") == []
    clock[0] += 46
    assert test() in (True, [])
    assert len(session.calls) == 2


@pytest.mark.parametrize("header, expected", [
    ("120", 120), ("999999", 86400), ("0", 30), ("garbage", 30),
    ("-1", 30), ("9" * 1000, 30),
    (format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True), 120),
    (format_datetime(datetime.fromtimestamp(900, timezone.utc), usegmt=True), 30),
])
def test_retry_after_seconds_and_http_dates_are_bounded(header, expected, clock):
    """Malformed or expired advice uses a short fallback instead of crashing."""
    client = Torznab(TorznabConfig("https://host/api", "key"), Session(Response(429, header)))
    with pytest.raises(IndexerBackoff) as failure:
        client.search("game")
    assert failure.value.retry_after == expected


def test_fallback_grows_to_fifteen_minutes_then_resets_after_success(clock):
    """Success after the pause resets the consecutive throttling counter."""
    client = Torznab(TorznabConfig("https://host/api", "key"),
                     Session(*[Response(429)] * 8, Response(), Response(429)))
    for expected in [30, 60, 120, 240, 480, 900, 900, 900]:
        with pytest.raises(IndexerBackoff) as failure:
            client.search("game")
        assert failure.value.retry_after == expected
        clock[0] += expected + 1
    assert client.search("game") == []
    with pytest.raises(IndexerBackoff) as failure:
        client.search("game")
    assert failure.value.retry_after == 30


def test_inflight_success_cannot_clear_newer_429_or_shorten_longer_pause(clock):
    """Concurrent replies preserve the longest deadline regardless of arrival."""
    entered = Event()
    release = Event()

    class RacingSession:
        def get(self, url, **kwargs):
            """Delay the first request until another has reported throttling."""
            if "q=slow" in url:
                entered.set()
                assert release.wait(3)
                return Response()
            return Response(429, "300")

    client = Torznab(TorznabConfig("https://host/api", "key"), RacingSession())
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(client.search, "slow")
        assert entered.wait(3)
        with pytest.raises(IndexerBackoff):
            client.search("fast")
        release.set()
        assert slow.result(timeout=3) == []
    with pytest.raises(IndexerBackoff) as failure:
        client.search("again")
    assert failure.value.retry_after == 300


def test_late_shorter_429_cannot_shorten_existing_long_pause(clock):
    """Two concurrent rate-limit replies retain the later retry deadline."""
    entered = Event()
    release = Event()

    class RacingSession:
        def get(self, url, **kwargs):
            """Return a shorter delay only after the longer one is installed."""
            if "q=slow" in url:
                entered.set()
                assert release.wait(3)
                return Response(429, "5")
            return Response(429, "300")

    client = Torznab(TorznabConfig("https://host/api", "key"), RacingSession())
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(client.search, "slow")
        assert entered.wait(3)
        with pytest.raises(IndexerBackoff):
            client.search("fast")
        release.set()
        with pytest.raises(IndexerBackoff):
            slow.result(timeout=3)
    with pytest.raises(IndexerBackoff) as failure:
        client.search("again")
    assert failure.value.retry_after == 300


def test_real_http_indexer_receives_one_request_during_pause():
    """Prove requests.Session honors the guard against an actual HTTP server."""
    hits = []

    class Limited(BaseHTTPRequestHandler):
        def do_GET(self):
            """Rate limit every request while counting traffic."""
            hits.append(self.path)
            self.send_response(429)
            self.send_header("Retry-After", "20")
            self.end_headers()

        def log_message(self, *args):
            """Keep the fixture's keyed URL out of test logs."""
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Limited)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = build_indexer({"type": "torznab", "url": f"http://127.0.0.1:{server.server_port}/api"})
    try:
        for _ in range(3):
            with pytest.raises(IndexerBackoff):
                client.search("game")
        assert len(hits) == 1
    finally:
        client._session.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize("value", [0, 301, -1, True, None, 1.5, "bad", "60.0"])
def test_invalid_timeout_is_rejected(value):
    """Prevent zero/unbounded or ambiguous HTTP timeouts."""
    with pytest.raises(ValueError, match="1 to 300"):
        indexer_timeout({"timeout": value})


@pytest.mark.parametrize("kind", list(INDEXER_TYPES))
def test_timeout_reaches_every_driver_and_configuration_schema(kind):
    """Existing generic forms expose the same timeout used by direct requests."""
    assert any(field["name"] == "timeout" for field in INDEXER_TYPES[kind]["fields"])
    if kind != "prowlarr":
        client = build_indexer({"type": kind, "url": "https://host/api", "timeout": "17"})
        reply = Response()
        if kind == "hydra":   # a Hydra source is JSON, not a feed
            from romarr.hydra import clear_cache
            clear_cache()
            reply.text = '{"downloads": [{"title": "Game", "uris": ["magnet:?xt=urn:btih:%s"]}]}' % ("a" * 40)
        session = Session(reply)
        client._session = session
        client.caps()
        assert session.calls[0][1]["timeout"] == 17


def test_save_rejects_invalid_timeout_before_persisting_and_updates_existing(tmp_path):
    """Invalid edits cannot poison the saved config or replace live clients."""
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_AUTH": "disabled"})
    body = {"type": "prowlarr", "url": "https://host", "api_key": "secret", "timeout": 17}
    status, saved = drive(service, "POST", "/api/v1/indexer", body)
    assert status == 200
    assert service.prowlarr._config.timeout == 17
    status, _ = drive(service, "POST", "/api/v1/indexer", {**body, "id": saved["id"], "timeout": 0})
    assert status == 400
    assert service.store.get_item("indexers", saved["id"])["timeout"] == 17
    assert service.prowlarr._config.timeout == 17


def test_unsaved_prowlarr_test_honors_submitted_timeout(tmp_path, monkeypatch):
    """Testing a slow unsaved endpoint uses its edited timeout immediately."""
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_AUTH": "disabled"})
    observed = []

    def configured(self):
        """Observe the actual probe configuration without networking."""
        observed.append(self._config.timeout)
        return []

    monkeypatch.setattr(Prowlarr, "indexers", configured)
    result = service.test_indexer({"type": "prowlarr", "url": "https://host", "timeout": 120})
    assert result["ok"]
    assert observed == [120]


def test_corrupt_saved_timeouts_do_not_block_startup_or_other_indexers(tmp_path):
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_AUTH": "disabled"})
    for kind in ("prowlarr", "torznab"):
        service.store.put_item("indexers", {"type":kind,"url":"https://host.invalid","timeout":None})
    service.store.put_item("indexers", {"type":"torznab","url":"https://valid.invalid","timeout":17})
    service.reload_clients()
    assert len(service.indexers) == 1
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_AUTH": "disabled"})
    assert len(restarted.indexers) == 1
