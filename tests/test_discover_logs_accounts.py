"""Discover, the live log ring, and the account-backed list sources."""

import json

import pytest

from romarr.lists import NO_API_STORES, fetch_entries
from romarr.metadata import DISCOVER_SHELVES, discover
from romarr.ops import LogRing


# -- discover ----------------------------------------------------------------

def test_discover_with_no_provider_says_what_to_configure():
    out = discover([])
    assert out["items"] == []
    assert "RAWG" in out["error"]


def test_discover_refuses_an_unknown_shelf():
    out = discover([], shelf="trending")
    assert "unknown shelf" in out["error"]
    for shelf in DISCOVER_SHELVES:
        assert shelf in out["error"]


def test_discover_serves_a_shelf_from_rawg(monkeypatch):
    import romarr.metadata as m

    def fake_get(url):
        assert "api.rawg.io" in url
        assert "metacritic=60%2C100" in url, "popular is metacritic-gated"
        return {"results": [{"name": "Chrono Trigger", "released": "1995-03-11",
                             "rating": 4.6, "background_image": "http://x/y.jpg",
                             "platforms": [{"platform": {"name": "SNES"}}]}]}

    monkeypatch.setattr(m, "_get_json", fake_get)
    out = discover([{"type": "rawg", "api_key": "K"}], shelf="popular")
    assert out["items"][0]["title"] == "Chrono Trigger"
    assert out["provider"] == "rawg"


# -- the log ring ------------------------------------------------------------

def test_the_ring_numbers_and_tails():
    ring = LogRing(size=10)
    ring.add("INFO", "romarr.app", "one")
    ring.add("WARNING", "romarr.app", "two")
    out = ring.tail()
    assert [r["message"] for r in out["items"]] == ["one", "two"]
    assert out["latest"] == 2

    # Polling with the cursor returns only what is new.
    again = ring.tail(since=out["latest"])
    assert again["items"] == []
    ring.add("ERROR", "romarr.hub", "three")
    assert [r["message"] for r in ring.tail(since=out["latest"])["items"]] \
        == ["three"]


def test_the_ring_filters_by_level():
    ring = LogRing(size=10)
    ring.add("DEBUG", "x", "noise")
    ring.add("INFO", "x", "fyi")
    ring.add("ERROR", "x", "bad")
    assert [r["message"] for r in ring.tail(level="warning")["items"]] == ["bad"]


def test_the_ring_is_a_ring():
    ring = LogRing(size=3)
    for n in range(6):
        ring.add("INFO", "x", f"m{n}")
    out = ring.tail()
    assert [r["message"] for r in out["items"]] == ["m3", "m4", "m5"]
    assert out["latest"] == 6, "sequence numbers survive eviction"


def test_the_handler_feeds_the_ring_without_touching_global_config():
    import logging
    ring = LogRing()
    logger = logging.getLogger("romarr.test.ring")
    logger.addHandler(ring.handler())
    logger.warning("through the handler")
    assert ring.tail()["items"][-1]["message"] == "through the handler"
    logger.handlers.clear()


# -- account sources ---------------------------------------------------------

class FakeResponse:
    def __init__(self, body=None, headers=None):
        self._body = body or {}
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_xbox_titles_become_entries():
    class FakeXbl:
        def get(self, url, headers=None, timeout=None):
            assert headers["X-Authorization"] == "XKEY"
            return FakeResponse({"titles": [
                {"name": "Banjo-Kazooie"}, {"name": "Perfect Dark"}, {}]})

    entries = fetch_entries({"type": "xbox", "openxbl_key": "XKEY"},
                            session=FakeXbl())
    assert [e.game for e in entries] == ["Banjo-Kazooie", "Perfect Dark"]


def test_psn_exchanges_npsso_then_walks_the_shelf():
    class FakePsn:
        def get(self, url, params=None, headers=None, timeout=None,
                allow_redirects=True):
            if "authorize" in url:
                assert "npsso=TOK" in headers["Cookie"]
                return FakeResponse(headers={
                    "Location": "com.scee://redirect?code=CODE1&x=1"})
            assert "trophyTitles" in url
            assert headers["Authorization"] == "Bearer JWT"
            return FakeResponse({"trophyTitles": [
                {"trophyTitleName": "Castlevania SOTN"}]})

        def post(self, url, data=None, headers=None, timeout=None):
            assert data["code"] == "CODE1"
            return FakeResponse({"access_token": "JWT"})

    entries = fetch_entries({"type": "psn", "npsso": "TOK"}, session=FakePsn())
    assert [e.game for e in entries] == ["Castlevania SOTN"]


def test_a_stale_npsso_fails_with_instructions():
    class Stale:
        def get(self, url, **kw):
            return FakeResponse(headers={"Location": "com.scee://redirect?error=x"})

    with pytest.raises(ValueError) as err:
        fetch_entries({"type": "psn", "npsso": "OLD"}, session=Stale())
    assert "fresh one" in str(err.value)


def test_itchio_pages_through_purchases():
    class FakeItch:
        def get(self, url, params=None, timeout=None):
            assert "/KEY/my-owned-keys" in url
            if params["page"] == 1:
                return FakeResponse({"owned_keys": [
                    {"game": {"title": "Celeste Classic"}}]})
            return FakeResponse({"owned_keys": []})

    entries = fetch_entries({"type": "itchio", "itchio_key": "KEY"},
                            session=FakeItch())
    assert [e.game for e in entries] == ["Celeste Classic"]


def test_accounts_without_credentials_are_empty_not_errors():
    for kind, field in (("xbox", "openxbl_key"), ("psn", "npsso"),
                        ("itchio", "itchio_key")):
        assert fetch_entries({"type": kind, field: ""}) == []


def test_only_nintendo_is_left_with_nothing_to_connect_to():
    """This list once held EA, Battle.net and Epic, on the claim that they
    had no usable web API. Playnite and LaunchBox pulled owned libraries
    from all three for years, so the claim was simply false -- each is now
    a real connector, and Nintendo is the only honest entry left."""
    assert set(NO_API_STORES) == {"Nintendo"}
    assert NO_API_STORES["Nintendo"]

    from romarr.lists import LIST_TYPES
    for store in ("epic", "ea", "battlenet"):
        assert store in LIST_TYPES, f"{store} must be a real connector now"
