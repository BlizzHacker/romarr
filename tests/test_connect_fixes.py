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
