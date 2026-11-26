"""The connect flows a live run proved broken, pinned so they stay fixed."""

import json

from romarr.connect import TOKEN_SOURCES
from romarr.lists import clean_title, fetch_entries


def test_trademark_symbols_never_reach_an_indexer():
    """A live Battle.net sync produced "World of Warcraft®", which matches
    nothing on any indexer."""
    assert clean_title("World of Warcraft®") == "World of Warcraft"
    assert clean_title("Diablo® II: Resurrected™") == "Diablo II: Resurrected"
    assert clean_title("Ecco the Dolphin") == "Ecco the Dolphin"


def test_battlenet_collapses_one_row_per_game_account():
    """Blizzard lists a row per game ACCOUNT: two WoW characters on separate
    accounts were two identical rows in a live response."""
    doc = {"gameAccounts": [
        {"localizedGameName": "World of Warcraft®", "gameAccountName": "WoW1"},
        {"localizedGameName": "World of Warcraft®", "gameAccountName": "ALT"},
        {"localizedGameName": "Diablo® IV"},
    ]}
    entries = fetch_entries({"type": "battlenet",
                             "battlenet_json": json.dumps(doc)})
    assert [e.game for e in entries] == ["World of Warcraft", "Diablo IV"]


def test_ea_does_not_ask_for_a_session_it_cannot_see():
    """`prompt=none` returned login_required on a live click: it demands a
    session already visible to the request, which is usually not the case."""
    assert "prompt=none" not in TOKEN_SOURCES["ea"]["open"]
    assert TOKEN_SOURCES["ea"]["open"].startswith("https://accounts.ea.com/")


def test_epic_goes_through_login_so_a_eula_can_be_accepted():
    """Hitting /id/api/redirect directly answered EULA_ACCEPTANCE on a live
    click, with no way forward from that page."""
    url = TOKEN_SOURCES["epic"]["open"]
    assert url.startswith("https://www.epicgames.com/id/login?")
    assert "api%2Fredirect" in url


def test_xbox_opens_the_page_with_the_sign_in_on_it():
    """xbl.io/console bounced a signed-out visitor to the docs."""
    assert TOKEN_SOURCES["xbox"]["open"] == "https://xbl.io/"


def test_gog_opens_the_page_that_actually_shows_the_username():
    """gog.com/account is the library page and never shows the username in
    a copyable form."""
    assert TOKEN_SOURCES["gog"]["open"] == "https://embed.gog.com/userData.json"
    assert "public" in TOKEN_SOURCES["gog"]["how"]


def test_epic_diagnoses_a_eula_block_instead_of_blaming_the_code():
    """A live run pasted two FRESH codes and was told both times the code
    was stale; the account actually owed a EULA acceptance."""
    import json as _json
    import pytest
    from romarr.lists import fetch_entries

    class EulaEpic:
        def post(self, url, data=None, headers=None, timeout=None):
            class R:
                status_code = 400
                text = ""

                def json(self):
                    return {"errorCode":
                            "errors.com.epicgames.oauth.corrective_action_required",
                            "metadata": {"correctiveAction": "EULA_ACCEPTANCE"}}

                def raise_for_status(self):
                    raise OSError("400")
            return R()

    with pytest.raises(ValueError) as err:
        fetch_entries({"type": "epic", "epic_code": "FRESH"},
                      session=EulaEpic())
    assert "EULA" in str(err.value)
    assert "single-use" not in str(err.value)


def test_unmatched_titles_are_kept_visible_not_discarded(tmp_path):
    """133 modern titles vanished with only a count. They stay on the list."""
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.put_item("import_lists", {
        "name": "modern", "type": "paste",
        "content": "Diablo IV\nOverwatch\nSuper Metroid\tsnes"})
    out = svc.list_sync()
    assert out["added"] == 1
    assert out["unknown"] == 2
    stored = svc.store.list_items("import_lists")[0]
    assert sorted(stored["unmatched"]) == ["Diablo IV", "Overwatch"]


def test_owned_store_sync_keeps_all_games_past_the_legacy_limit(tmp_path, monkeypatch):
    from romarr import lists
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.store.put_item("import_lists", {"name": "My Steam", "type": "steam",
                                             "api_key": "private-key"})
    entries = [lists.ListEntry(f"Game {i:03}", store_id=str(1000 + i))
               for i in range(620)]
    monkeypatch.setattr(lists, "fetch_entries", lambda cfg: entries)
    assert service.list_sync()["added"] == 0
    stored = service.store.list_items("import_lists")
    assert len(stored[0]["unmatched"]) == 500
    page = lists.owned_games(stored, offset=600)
    assert page["total"] == 620 and len(page["items"]) == 20
    assert page["items"][0]["store_id"] == "1600"
    assert "private-key" not in json.dumps(page)
    assert "owned_snapshot" not in service.safe_settings()["import_lists"][0]


def test_owned_view_excludes_wishlist_and_accepts_manual_store_titles():
    from romarr.lists import owned_games

    page = owned_games([
        {"type": "steam", "source": "wishlist", "unmatched": ["Wishlist Only"]},
        {"type": "paste", "name": "My Amazon", "provider": "amazon",
         "content": "Game - Definitive Edition\nAnother Game"},
        {"type": "battlenet", "enable": False, "unmatched": ["Disabled"]},
    ], q="definitive")
    assert [row["name"] for row in page["items"]] == ["Game - Definitive Edition"]
    assert page["providers"] == [{"id": "amazon", "name": "Amazon Games", "count": 2}]


def test_device_report_links_to_owned_steam_id_without_exposing_paths():
    from romarr.lists import device_report, owned_games

    report = device_report({"device_id": "Gaming-PC", "name": "Gaming PC", "os": "windows",
                            "games": [{"provider": "steam", "name": "Example™",
                                       "installed": True, "path": "C:/private"}]}, "now")
    assert report["id"] == "gaming-pc"
    assert "path" not in report["games"][0]
    source = {"type": "steam", "owned_snapshot": {"at": "now",
              "games": [{"name": "Example", "store_id": "123"}]}}
    found = owned_games([source], devices=[report])
    assert found["total"] == 1
    assert found["items"][0]["store_id"] == "123"
    assert found["items"][0]["installed_on"] == ["Gaming PC"]


def test_collector_only_displays_active_paid_base_games_and_official_links():
    from datetime import datetime, timezone
    from romarr.free_game_collector import epic_offers

    giveaway = {"id": "1", "title": "Base game", "offerType": "BASE_GAME",
                "productSlug": "example/home",
                "price": {"totalPrice": {"discountPrice": 0, "originalPrice": 999}},
                "promotions": {"promotionalOffers": [{"promotionalOffers": [
                    {"startDate": "2026-11-26T16:00:00Z", "endDate": "2026-12-03T16:00:00Z"}
                ]}]}}
    rows = [giveaway, {**giveaway, "id": "2", "offerType": "ADD_ON"},
            {**giveaway, "id": "3", "promotions": None},
            {**giveaway, "id": "4", "price": {
                "totalPrice": {"discountPrice": 0, "originalPrice": 0}}}]
    payload = {"data": {"Catalog": {"searchStore": {"elements": rows}}}}
    offers = epic_offers(payload, now=datetime(2026, 11, 27, tzinfo=timezone.utc))
    assert [offer["url"] for offer in offers] == ["https://store.epicgames.com/p/example?lang=en-US"]
    assert epic_offers(payload, now=datetime(2026, 12, 4, tzinfo=timezone.utc)) == []


def test_settings_echo_keeps_owned_snapshot_and_account_change_clears_it(tmp_path):
    from romarr.app import ROMarr

    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json")})
    snapshot = {"at": "now", "games": [{"name": "Example", "store_id": "123"}]}
    service.store.put_item("import_lists", {
        "id": "steam", "name": "PC", "type": "steam", "profile": "original",
        "api_key": "private", "owned_snapshot": snapshot,
    })
    patch = {"import_lists": service.safe_settings()["import_lists"]}
    assert "owned_snapshot" not in patch["import_lists"][0]
    restored = service.restore_masked_items(patch)["import_lists"][0]
    assert restored["owned_snapshot"] == snapshot
    assert restored["api_key"] == "private"

    changed = service.restore_masked_items({"import_lists": [{**restored, "profile": "new"}]})
    assert "owned_snapshot" not in changed["import_lists"][0]


def test_xbox_import_reads_openxbl_envelope_and_reports_bad_payload():
    import pytest
    from types import SimpleNamespace

    class Client:
        def get(self, url, **kwargs):
            assert url == "https://api.xbl.io/v2/achievements"
            assert kwargs["allow_redirects"] is False
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: payload)

    cfg = {"type": "xbox", "openxbl_key": "test-key"}
    titles = [{"name": "Halo®"}, {"name": "Fable"}]
    for payload in ({"code": 200, "content": {"titles": titles}}, {"titles": titles}):
        assert [row.game for row in fetch_entries(cfg, session=Client())] == ["Halo", "Fable"]
    payload = {"code": 200, "content": {"titles": []}}
    assert fetch_entries(cfg, session=Client()) == []
    payload = {"code": 401, "content": {"message": "provider text"}}
    with pytest.raises(ValueError, match="rejected the API key"):
        fetch_entries(cfg, session=Client())
    payload = {"code": 200, "content": {}}
    with pytest.raises(ValueError, match="no Xbox title history"):
        fetch_entries(cfg, session=Client())


def test_psn_does_not_exchange_an_error_code_as_an_authorization_code():
    import pytest
    from types import SimpleNamespace

    class Client:
        def get(self, *args, **kwargs):
            return SimpleNamespace(headers={
                "Location": "https://playstation.example/?error=login_required&error_code=4165"
            })

    with pytest.raises(ValueError, match="PlayStation refused"):
        fetch_entries({"type": "psn", "npsso": "stale"}, session=Client())


def test_reconnect_checks_the_replacement_before_saving(tmp_path, monkeypatch):
    from romarr import lists
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    original = service.store.put_item("import_lists", {
        "name": "EA library", "type": "ea", "ea_token": "old-token", "platform": "",
        "enable": True, "added": ["snes/example"], "owned_snapshot": {
            "at": "yesterday", "games": [{"name": "Old listing", "store_id": ""}]},
    })

    def read(config):
        if config["ea_token"] != "valid-token":
            raise ValueError("EA refused the credential")
        return [lists.ListEntry("New listing")]

    monkeypatch.setattr(lists, "fetch_entries", read)
    rejected = service.reconnect_import_list(original["id"], "bad-token")
    assert rejected == {"error": "EA refused the credential"}
    assert service.store.get_item("import_lists", original["id"]) == original

    result = service.reconnect_import_list(original["id"], '{"access_token":"valid-token"}')
    assert result == {"ok": True, "fetched": 1}
    saved = service.store.get_item("import_lists", original["id"])
    assert saved["added"] == ["snes/example"]
    assert saved["owned_snapshot"]["games"] == [{"name": "New listing", "store_id": ""}]
    assert saved["last_sync"]["fetched"] == 1


def test_ea_redirect_fragment_is_accepted():
    from romarr.connect import extract_value

    assert extract_value("ea_token", "https://accounts.ea.com/#access_token=fresh") == "fresh"
    assert extract_value("ea_token", "nucleus:rest#access_token=fresh") == "fresh"


def test_calendar_tracks_confirmed_giveaways_and_ignores_upcoming_sales(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from romarr import free_game_collector as collector
    from romarr.store import Store

    class Clock(datetime):
        instant = datetime(2026, 11, 27, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant

    def promotion(start, end, discount):
        return [{"promotionalOffers": [{"startDate": start, "endDate": end, "discountSetting": {
            "discountType": "PERCENTAGE", "discountPercentage": discount}}]}]

    active = {"id": "active", "title": "Free today", "offerType": "BASE_GAME", "productSlug": "free-now",
              "price": {"totalPrice": {"discountPrice": 0, "originalPrice": 999}},
              "promotions": {"promotionalOffers": promotion("2026-11-26T16:00:00Z", "2026-12-03T16:00:00Z", 0)}}
    future = {**active, "id": "coming", "title": "Free next week",
              "price": {"totalPrice": {"discountPrice": 999, "originalPrice": 999}},
              "promotions": {"upcomingPromotionalOffers": promotion(
                  "2026-12-03T16:00:00Z", "2026-12-10T16:00:00Z", 0)}}
    sale = {**future, "id": "sale", "promotions": {"upcomingPromotionalOffers": promotion(
        "2026-12-03T16:00:00Z", "2026-12-10T16:00:00Z", 40)}}
    rows = [active, future, sale]

    class Feed:
        status_code = 200

        def get(self, *args, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size):
            yield json.dumps({"data": {"Catalog": {"searchStore": {"elements": rows}}}}).encode()

    monkeypatch.setattr(collector, "datetime", Clock)
    store = Store(tmp_path / "state.json")
    current = collector.FreeGameCollector(session=Feed(), store=store).view()
    assert [row["id"] for row in current["items"]] == ["active"]
    assert [row["id"] for row in current["upcoming"]] == ["coming"]
    assert current["ended"] == []
    assert store.get_item("giveaway_history", "US")["offers"][0]["id"] == "active"

    Clock.instant = datetime(2026, 12, 4, tzinfo=timezone.utc)
    rows.clear()
    after = collector.FreeGameCollector(session=Feed(), store=Store(store.path)).view()
    assert after["items"] == after["upcoming"] == []
    assert [row["id"] for row in after["ended"]] == ["active"]

def test_reconnect_api_dispatches_the_authenticated_post(tmp_path):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    import requests
    from romarr.app import ROMarr, make_handler

    app = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": "test-admin"})
    app.reconnect_import_list = lambda item_id, value: {"ok": True, "fetched": 7}
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app)) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/v1/importlist/reconnect"
            body = {"id": "xbox", "credential": "test-only"}
            assert requests.post(url, json=body, timeout=5).status_code == 401
            headers = {"X-Api-Key": "test-admin"}
            assert requests.get(url, headers=headers, timeout=5).status_code == 404
            response = requests.post(url, headers=headers, json=body, timeout=5)
            assert response.status_code == 200 and response.json()["fetched"] == 7
        finally:
            server.shutdown()
            worker.join()
