"""Steam and GOG as list sources, and Gameyfin as a library type."""

import json

from romarr.lists import STEAM_WISHLIST_CAP, fetch_entries
from romarr.libraries import (GameyfinLibrary, FolderConfig, LIBRARY_TYPES,
                              build_library_from_config)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSteam:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if "GetOwnedGames" in url:
            assert params["key"] == "K" and params["steamid"] == "765"
            return FakeResponse({"response": {"games": [
                {"appid": 1, "name": "DOOM (1993)"},
                {"appid": 2, "name": "Chrono Trigger"},
                {"appid": 3},  # a delisted app with no name
            ]}})
        if "GetWishlist" in url:
            return FakeResponse({"response": {"items": [
                {"appid": 10}, {"appid": 11}, {}]}})
        if "appdetails" in url:
            appid = params["appids"]
            return FakeResponse({str(appid): {
                "success": True, "data": {"name": f"Wished {appid}"}}})
        raise AssertionError(f"unexpected URL {url}")


def test_steam_owned_games_become_entries():
    entries = fetch_entries({"type": "steam", "steam_id": "765",
                             "api_key": "K", "source": "owned"},
                            session=FakeSteam())
    assert [e.game for e in entries] == ["DOOM (1993)", "Chrono Trigger"]


def test_steam_wishlist_resolves_names_per_app():
    entries = fetch_entries({"type": "steam", "steam_id": "765",
                             "api_key": "K", "source": "wishlist"},
                            session=FakeSteam())
    assert [e.game for e in entries] == ["Wished 10", "Wished 11"]


def test_steam_without_credentials_is_empty_not_an_error():
    assert fetch_entries({"type": "steam", "steam_id": "", "api_key": ""}) == []


def test_the_wishlist_cap_exists_and_is_sane():
    """One sync must not turn a 1,000-app wishlist into a store-API ban."""
    assert 50 <= STEAM_WISHLIST_CAP <= 300


class FakeGog:
    def get(self, url, timeout=None):
        assert "gog.com/u/wade/games/stats" in url
        page = int(url.rsplit("page=", 1)[1])
        items = {
            1: [{"game": {"title": "Heroes of Might and Magic 3"}},
                {"game": {"title": "Jazz Jackrabbit"}}],
            2: [{"game": {"title": "Outcast"}}],
        }[page]
        return FakeResponse({"pages": 2, "_embedded": {"items": items}})


def test_gog_profile_pages_are_walked():
    entries = fetch_entries({"type": "gog", "gog_username": "wade"},
                            session=FakeGog())
    assert [e.game for e in entries] == [
        "Heroes of Might and Magic 3", "Jazz Jackrabbit", "Outcast"]


def test_gog_without_a_username_is_empty():
    assert fetch_entries({"type": "gog", "gog_username": " "}) == []


# -- Gameyfin ----------------------------------------------------------------

def test_gameyfin_is_a_folder_with_its_own_name(tmp_path):
    lib = build_library_from_config({"type": "gameyfin",
                                     "path": str(tmp_path)})
    assert isinstance(lib, GameyfinLibrary)
    assert lib.name == "Gameyfin"
    assert lib.reachable()
    # The whole integration: files land where Gameyfin's watcher looks, and
    # rescan is honestly a no-op because Gameyfin notices by itself.
    assert lib.rescan() is True


def test_gameyfin_schema_has_no_url_field():
    fields = [f["name"] for f in LIBRARY_TYPES["gameyfin"]["fields"]]
    assert "url" not in fields, \
        "there is no server API to point a URL at; the path IS the integration"
    assert "path" in fields
