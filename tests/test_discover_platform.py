"""Discover by platform (issue #238): one system's catalogue, sorted,
searchable, paged, and marked with what is already in the library."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import romarr.metadata as m
from romarr.app import ROMarr, make_handler
from romarr.metadata import PLATFORM_SORTS, browse_platform

RAWG = {"type": "rawg", "api_key": "K"}
IGDB = {"type": "igdb", "client_id": "a", "token": "b"}


def _query(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def _rawg_page(names, more=False):
    return {"next": "https://api.rawg.io/api/games?page=2" if more else None,
            "results": [{"name": n, "released": "2002-11-17", "rating": 4.5,
                         "background_image": "http://x/c.jpg",
                         "platforms": [{"platform": {"name": "GameCube"}}],
                         "genres": [{"name": "Adventure"}]} for n in names]}


def test_rawg_browse_filters_by_platform_sorts_and_pages(monkeypatch):
    seen = []

    def fake_get(url):
        seen.append(_query(url))
        return _rawg_page(["Metroid Prime"], more=True)

    monkeypatch.setattr(m, "_get_json", fake_get)
    out = browse_platform([RAWG], lambda cfg: [105], sort="released", page=2, limit=500)
    assert out["error"] is None and out["provider"] == "rawg"
    assert out["has_more"] is True
    assert out["items"][0]["title"] == "Metroid Prime"
    assert out["items"][0]["genres"] == ["Adventure"]
    query = seen[0]
    assert query["platforms"] == ["105"]
    assert query["ordering"] == ["-released"]
    assert query["page"] == ["2"]
    assert query["page_size"] == ["40"], "RAWG caps page_size at 40"
    assert "search" not in query


@pytest.mark.parametrize("sort, ordering", [
    ("popular", "-added"), ("rating", "-rating"), ("name", "name")])
def test_rawg_sort_names_map_to_rawg_orderings(monkeypatch, sort, ordering):
    seen = []
    monkeypatch.setattr(m, "_get_json", lambda url: seen.append(url) or _rawg_page(["A"]))
    browse_platform([RAWG], lambda cfg: [7], sort=sort)
    assert _query(seen[0])["ordering"] == [ordering]


def test_rawg_search_within_platform(monkeypatch):
    seen = []
    monkeypatch.setattr(m, "_get_json", lambda url: seen.append(url) or _rawg_page(["Zelda"]))
    out = browse_platform([RAWG], lambda cfg: [105], query="  zelda ")
    query = _query(seen[0])
    assert query["search"] == ["zelda"] and query["platforms"] == ["105"]
    assert out["has_more"] is False


def test_igdb_browse_filters_platform_and_omits_sort_when_searching(monkeypatch):
    bodies = []

    def fake_query(cfg, endpoint, body, strict=False):
        bodies.append(body)
        return [{"id": i, "name": f"Game {i}", "first_release_date": 1000000000,
                 "total_rating": 90, "genres": [{"name": "RPG"}]} for i in range(3)]

    monkeypatch.setattr(m, "igdb_query", fake_query)
    out = browse_platform([IGDB], lambda cfg: [21], sort="rating", limit=2, page=3)
    assert "platforms = (21)" in bodies[0]
    assert "sort total_rating desc" in bodies[0]
    assert "total_rating_count >= 5" in bodies[0]
    assert "limit 3; offset 4;" in bodies[0]
    assert len(out["items"]) == 2 and out["has_more"] is True
    assert out["items"][0]["genres"] == ["RPG"]
    assert out["items"][0]["rating"] == 4.5

    browse_platform([IGDB], lambda cfg: [21], query='say "hi"')
    assert bodies[1].startswith('search "say \\"hi\\""; ')
    assert " sort " not in bodies[1], "IGDB answers 406 to search plus sort"


def test_a_provider_that_does_not_list_the_platform_is_skipped(monkeypatch):
    monkeypatch.setattr(m, "_get_json", lambda url: pytest.fail("RAWG has no ids here"))
    monkeypatch.setattr(m, "igdb_query", lambda *a, **k: [{"id": 1, "name": "Only IGDB"}])
    ids = {"rawg": [], "igdb": [99]}
    out = browse_platform([RAWG, IGDB], lambda cfg: ids[cfg["type"]])
    assert out["provider"] == "igdb"
    assert out["items"][0]["title"] == "Only IGDB"


def test_unknown_sort_and_missing_provider_say_why():
    error = browse_platform([], lambda cfg: [1], sort="hot")["error"]
    assert "unknown sort" in error
    for sort in PLATFORM_SORTS:
        assert sort in error
    assert "RAWG" in browse_platform([], lambda cfg: [1])["error"]


def test_empty_search_says_nothing_matched(monkeypatch):
    monkeypatch.setattr(m, "_get_json", lambda url: {"results": []})
    out = browse_platform([RAWG], lambda cfg: [105], query="qwerty")
    assert out["items"] == [] and "nothing matching 'qwerty'" in out["error"]


def test_rawg_failure_is_reported_not_read_as_empty(monkeypatch):
    monkeypatch.setattr(m, "_get_json", lambda url: None)
    out = browse_platform([RAWG], lambda cfg: [105])
    assert out["items"] == [] and "could not be reached" in out["error"]


def test_rawg_platform_list_is_paged(monkeypatch):
    pages = {"1": {"next": "p2", "results": [{"id": 105, "name": "GameCube"}]},
             "2": {"next": None, "results": [{"id": 79, "name": "SNES"},
                                             {"id": 4, "name": "PC"}]}}
    monkeypatch.setattr(m, "_get_json", lambda url: pages[_query(url)["page"][0]])
    assert [r["id"] for r in m.rawg_platforms(RAWG)] == [105, 79, 4]
    monkeypatch.setattr(m, "_get_json", lambda url: None)
    with pytest.raises(RuntimeError):
        m.rawg_platforms(RAWG)


# -- the service and the route ---------------------------------------------------

@pytest.fixture
def server(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()


def call(base, path):
    req = urllib.request.Request(base + path)
    req.add_header("X-Api-Key", "k")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_route_marks_library_and_wanted_games(server, monkeypatch):
    base, svc = server
    svc.store.put_item("metadata_providers", dict(RAWG))
    svc.store.want("Ico", "ps2")
    monkeypatch.setattr("romarr.app.rawg_platforms",
                        lambda cfg: [{"id": 15, "name": "PlayStation 2"},
                                     {"id": 16, "name": "PlayStation 3"}])
    seen = []
    monkeypatch.setattr(m, "_get_json", lambda url: seen.append(url) or
                        _rawg_page(["Ico", "Okami"]))
    code, out = call(base, "/api/v1/discover/platform?platform=ps2&sort=name&page=x")
    assert code == 200, out
    assert out["platform"]["slug"] == "ps2"
    assert _query(seen[0])["platforms"] == ["15"]
    assert [(g["title"], g["state"], g["owned"]) for g in out["items"]] == [
        ("Ico", "monitored", False), ("Okami", "", False)]
    assert out["page"] == 1 and out["sort"] == "name"


def test_route_refuses_an_unknown_platform(server):
    base, _ = server
    code, out = call(base, "/api/v1/discover/platform?platform=toaster")
    assert code == 200 and "unknown platform" in out["error"]


def test_rawg_platform_ids_are_cached_per_credential(server, monkeypatch):
    _, svc = server
    calls = []
    monkeypatch.setattr("romarr.app.rawg_platforms",
                        lambda cfg: calls.append(cfg["api_key"]) or
                        [{"id": 105, "name": "GameCube"}, {"id": 79, "name": "SNES"}])
    gamecube = svc._rawg_platform_ids(RAWG, "ngc")
    assert gamecube == [105]
    assert svc._rawg_platform_ids(RAWG, "ngc") == [105]
    assert svc._rawg_platform_ids({**RAWG, "api_key": "L"}, "snes") == [79]
    assert calls == ["K", "L"]


def test_discover_page_has_a_platform_tab_wired_to_the_route():
    from romarr.ui import JS
    assert "['platform','By platform']" in JS
    assert "async function discoverPlatform()" in JS
    assert "/api/v1/discover/platform?platform=" in JS
    assert "if(DISC.shelf==='platform'){await discoverPlatform();return;}" in JS
