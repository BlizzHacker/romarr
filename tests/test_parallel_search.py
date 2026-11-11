from threading import Barrier
from types import SimpleNamespace

from romarr.app import ROMarr
from romarr.indexers import Prowlarr, ProwlarrConfig
from romarr.platforms import resolve
from romarr.selection import Release


def release(title, url):
    return Release(title=title, download_url=url, size=1, seeders=1,
                   categories=(1000,), protocol="torrent", indexer="test")


def test_three_searches_overlap_and_preserve_duplicate_precedence(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    barrier = Barrier(3, timeout=5)
    first = release("Game", "same")
    duplicate = release("Game", "same")
    qualified = release("Qualified", "qualified")
    hub = release("Hub", "hub")

    def search(term):
        barrier.wait()
        return [first] if term == "Game" else [duplicate, qualified]

    def hub_search(*args):
        barrier.wait()
        return [hub]

    service.prowlarr = SimpleNamespace(search=search)
    service.indexers = []
    service._hub_releases = hub_search
    results = service._search_releases("Game", resolve("psx"))
    assert results == [first, qualified, hub]
    assert results[0] is first


def test_failed_query_and_hub_leave_successful_results(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    found = release("Game", "game")

    def search(term):
        if term == "Game":
            raise TimeoutError("source timeout")
        return [found]

    def hub_search(*args):
        raise RuntimeError("hub failure")

    service.prowlarr = SimpleNamespace(search=search)
    service.indexers = []
    service._hub_releases = hub_search
    assert service._search_releases("Game", resolve("psx")) == [found]


def test_prowlarr_workers_use_own_sessions_and_warm_shared_privacy(tmp_path, monkeypatch):
    from romarr import app

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    barrier = Barrier(2, timeout=5)
    sessions = []

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class Session:
        def __init__(self):
            self.urls = []
            self.closed = False
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def get(self, url, **kwargs):
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
    service.prowlarr = prowlarr
    service.indexers = []
    service._hub_releases = lambda *args: []
    monkeypatch.setattr(app.requests, "Session", Session)

    assert [r.title for r in service._search_releases("Game", resolve("psx"))] == ["Game"]
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert all(session.closed for session in sessions)
    assert prowlarr._session is shared_session
    assert prowlarr._privacy == {7: True}

    sessions.clear()
    service._search_releases("Game", resolve("psx"))
    assert not any(url.endswith("/api/v1/indexer")
                   for session in sessions for url in session.urls)


def test_failed_direct_indexer_does_not_hide_other_sources(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    found = release("Game", "game")
    direct = release("Direct", "direct")

    def broken(term):
        raise RuntimeError("direct source failure")

    service.prowlarr = SimpleNamespace(search=lambda term: [found])
    service.indexers = [SimpleNamespace(name="broken", search=broken),
                        SimpleNamespace(name="working", search=lambda term: [direct])]
    service._hub_releases = lambda *args: []
    assert service._search_releases("Game", resolve("psx")) == [found, direct]
