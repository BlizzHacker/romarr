from threading import Barrier, Event
from types import SimpleNamespace

import pytest
import requests

from romarr.app import ROMarr
from romarr.indexers import Prowlarr, ProwlarrConfig
from romarr.platforms import resolve
from romarr.selection import Release


def release(title, url):
    """Build a minimal Release test fixture."""
    return Release(title=title, download_url=url, size=1, seeders=1,
                   categories=(1000,), protocol="torrent", indexer="test")


def test_three_searches_overlap_and_preserve_duplicate_precedence(tmp_path):
    """Verify both query workers and hub search overlap and keep plain-query precedence."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    barrier = Barrier(3, timeout=5)
    first = release("Game", "same")
    duplicate = release("Game", "same")
    qualified = release("Qualified", "qualified")
    hub = release("Hub", "hub")

    qualified_done = Event()
    hub_done = Event()

    def search(term):
        """Synchronize query workers and return distinct release results."""
        barrier.wait()
        if term == "Game":
            assert qualified_done.wait(5) and hub_done.wait(5)
            return [first]
        qualified_done.set()
        return [duplicate, qualified]

    def hub_search(*args):
        """Synchronize hub search worker with indexer query workers."""
        barrier.wait()
        hub_done.set()
        return [hub]

    service.prowlarr = SimpleNamespace(search=search)
    service.indexers = []
    service._hub_releases = hub_search
    results = service._search_releases("Game", resolve("psx"))
    assert results == [first, qualified, hub]
    assert results[0] is first


def test_failed_query_and_hub_leave_successful_results(tmp_path):
    """Verify one failing query or hub search does not drop surviving results."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    found = release("Game", "game")

    def search(term):
        """Fail on the plain title query while returning results for the qualified one."""
        if term == "Game":
            raise TimeoutError("source timeout")
        return [found]

    def hub_search(*args):
        """Simulate a hub search failure."""
        raise RuntimeError("hub failure")

    service.prowlarr = SimpleNamespace(search=search)
    service.indexers = []
    service._hub_releases = hub_search
    assert service._search_releases("Game", resolve("psx")) == [found]


def test_prowlarr_workers_use_own_sessions_and_warm_shared_privacy(tmp_path, monkeypatch):
    """Verify query workers open isolated sessions while sharing one warmed privacy map."""
    from romarr import app

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    barrier = Barrier(2, timeout=5)
    sessions = []

    class Response:
        def __init__(self, body):
            """Store static JSON response body."""
            self.body = body

        def raise_for_status(self):
            """Pretend HTTP status succeeded."""

        def json(self):
            """Return configured JSON payload."""
            return self.body

    class Session:
        def __init__(self):
            """Record constructed worker session and tracked URLs."""
            self.urls = []
            self.closed = False
            sessions.append(self)

        def __enter__(self):
            """Enter context manager returning self."""
            return self

        def __exit__(self, *args):
            """Mark worker session closed on context exit."""
            self.closed = True

        def get(self, url, **kwargs):
            """Serve privacy or search payloads while forcing worker concurrency."""
            self.urls.append(url)
            if url.endswith("/api/v1/indexer"):
                return Response([{"id": 7, "privacy": "private"}])
            barrier.wait()
            if "query=Game&" in url:
                raise TimeoutError("plain title timed out")
            return Response([{"title": "Game", "indexerId": 7, "seeders": 0,
                              "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40}])

    prowlarr = Prowlarr(ProwlarrConfig(base_url="http://prowlarr:9696", api_key="k"))
    shared_session = prowlarr._session
    privacy_calls = []

    def get_privacy(url, **kwargs):
        """Return a private indexer map from the client's shared session."""
        privacy_calls.append(url)
        return Response([{"id": 7, "privacy": "private"}])

    monkeypatch.setattr(shared_session, "get", get_privacy)
    service.prowlarr = prowlarr
    service.indexers = []
    service._hub_releases = lambda *args: []
    monkeypatch.setattr(app.requests, "Session", Session)

    assert [r.title for r in service._search_releases("Game", resolve("psx"))] == ["Game"]
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert all(session.closed for session in sessions)
    assert prowlarr._session is shared_session
    assert prowlarr._privacy == {7: True}
    assert len(privacy_calls) == 1

    sessions.clear()
    service._search_releases("Game", resolve("psx"))
    assert not any(url.endswith("/api/v1/indexer")
                   for session in sessions for url in session.urls)
    assert len(privacy_calls) == 1


def test_failed_direct_indexer_does_not_hide_other_sources(tmp_path):
    """Verify a failing direct indexer leaves results from healthy sources intact."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    found = release("Game", "game")
    direct = release("Direct", "direct")

    def broken(term):
        """Raise an error simulating a failing direct indexer."""
        raise RuntimeError("direct source failure")

    service.prowlarr = SimpleNamespace(search=lambda term: [found])
    service.indexers = [SimpleNamespace(name="broken", search=broken),
                        SimpleNamespace(name="working", search=lambda term: [direct])]
    service._hub_releases = lambda *args: []
    assert service._search_releases("Game", resolve("psx")) == [found, direct]


@pytest.mark.parametrize("owns_session", [False, True])
@pytest.mark.parametrize("lookup_error", [requests.ConnectionError, RuntimeError])
def test_search_terms_share_privacy_and_retry_on_the_next_search(tmp_path, monkeypatch,
                                                               owns_session, lookup_error):
    """Verify failed privacy lookups fall back cleanly without poisoning later searches."""
    from romarr import app

    barrier = Barrier(2, timeout=5)
    hub_started = Event()
    privacy_calls = []

    class Response:
        def __init__(self, body):
            """Store static JSON response body."""
            self.body = body

        def raise_for_status(self):
            """Pretend HTTP status succeeded."""

        def json(self):
            """Return configured JSON payload."""
            return self.body

    class Session:
        def __enter__(self):
            """Enter context manager returning self."""
            return self

        def __exit__(self, *args):
            """Close mock session context cleanly."""

        def get(self, url, **kwargs):
            """Fail first privacy lookup, then serve private indexer and search results."""
            if url.endswith("/api/v1/indexer"):
                assert hub_started.wait(5)
                privacy_calls.append(url)
                if len(privacy_calls) == 1:
                    raise lookup_error("temporarily unavailable")
                return Response([{"id": 7, "privacy": "private"}])
            barrier.wait()
            return Response([{"title": url, "indexerId": 7, "seeders": 0,
                              "downloadUrl": "http://prowlarr:9696/download"}])

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    monkeypatch.setattr(app.requests, "Session", Session)
    config = ProwlarrConfig(base_url="http://prowlarr:9696", api_key="k")
    service.prowlarr = Prowlarr(config, None if owns_session else Session())
    service.indexers = []

    def hub_search(*args):
        """Signal that the hub task started concurrently before privacy resolves."""
        hub_started.set()
        return []

    service._hub_releases = hub_search

    first = service._search_releases("Game", resolve("psx"))
    assert len(first) == 2
    assert [result.private for result in first] == [False, False]
    assert len(privacy_calls) == 1
    assert service.prowlarr._privacy is None

    for _ in range(2):
        results = service._search_releases("Game", resolve("psx"))
        assert len(results) == 2
        assert all(result.private for result in results)
        assert len(privacy_calls) == 2
        assert service.prowlarr._privacy == {7: True}


def test_mid_search_prowlarr_reload_uses_captured_client_privacy(tmp_path):
    """Verify privacy lookup uses the captured Prowlarr client even after a reload."""
    class Response:
        def __init__(self, body):
            """Store static JSON response body."""
            self.body = body

        def raise_for_status(self):
            """Pretend HTTP status succeeded."""

        def json(self):
            """Return configured JSON payload."""
            return self.body

    class OldSession:
        def get(self, url, **kwargs):
            """Serve private indexer privacy and zero-seeder results for the old client."""
            if url.endswith("/api/v1/indexer"):
                return Response([{"id": 7, "privacy": "private"}])
            return Response([{"title": url, "indexerId": 7, "seeders": 0,
                              "downloadUrl": "http://old:9696/download"}])

    class NewSession:
        def __init__(self):
            """Track unexpected calls made against the reloaded replacement client."""
            self.urls = []

        def get(self, url, **kwargs):
            """Record calls; return an empty map if mistakenly queried."""
            self.urls.append(url)
            return Response([])

    class ReloadingIndexers:
        def __init__(self, svc, replacement):
            """Swap service.prowlarr once iterated after sources captures the old client."""
            self._svc = svc
            self._replacement = replacement

        def __iter__(self):
            """Simulate reload_clients replacing service.prowlarr mid-search."""
            self._svc.prowlarr = self._replacement
            return iter(())

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    old_prowlarr = Prowlarr(ProwlarrConfig(base_url="http://old:9696", api_key="k"), OldSession())
    new_session = NewSession()
    new_prowlarr = Prowlarr(ProwlarrConfig(base_url="http://new:9696", api_key="k"), new_session)

    service.prowlarr = old_prowlarr
    service.indexers = ReloadingIndexers(service, new_prowlarr)
    service._hub_releases = lambda *args: []

    results = service._search_releases("Game", resolve("psx"))
    assert len(results) == 2
    assert all(result.private for result in results)
    assert new_session.urls == []
    assert old_prowlarr._privacy == {7: True}
    assert new_prowlarr._privacy is None
