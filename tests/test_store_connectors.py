"""Epic, EA and Battle.net: owned libraries, the way Playnite gets them."""

import json

import pytest

from romarr.lists import LIST_TYPES, NO_API_STORES, fetch_entries


class R:
    def __init__(self, body=None, status=200, text=""):
        self._b = body or {}
        self.status_code = status
        self.text = text or json.dumps(self._b)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")

    def json(self):
        return self._b


# -- Epic --------------------------------------------------------------------

class FakeEpic:
    def __init__(self, token_status=200):
        self.token_status = token_status
        self.grants = []

    def post(self, url, data=None, headers=None, timeout=None):
        assert "oauth/token" in url
        assert headers["Authorization"].startswith("basic ")
        self.grants.append(data["grant_type"])
        if self.token_status >= 400:
            return R(status=self.token_status)
        return R({"access_token": "ACCESS", "refresh_token": "REFRESH2"})

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "bearer ACCESS"
        if not params.get("cursor"):
            return R({"records": [{"sandboxName": "Control"}],
                      "responseMetadata": {"nextCursor": "c2"}})
        return R({"records": [{"metadata": {"title": "Alan Wake"}}],
                  "responseMetadata": {}})


def test_epic_exchanges_a_code_and_pages_the_whole_library():
    cfg = {"type": "epic", "epic_code": "CODE"}
    epic = FakeEpic()
    entries = fetch_entries(cfg, session=epic)
    assert [e.game for e in entries] == ["Control", "Alan Wake"]
    assert epic.grants == ["authorization_code"]
    # The refresh token is handed back so later syncs need nothing.
    assert cfg["epic_refresh"] == "REFRESH2"


def test_epic_prefers_the_refresh_token_once_it_has_one():
    epic = FakeEpic()
    fetch_entries({"type": "epic", "epic_code": "OLD",
                   "epic_refresh": "REFRESH"}, session=epic)
    assert epic.grants == ["refresh_token"], \
        "a stored refresh token must beat a stale single-use code"


def test_epic_says_what_to_do_when_the_code_has_expired():
    with pytest.raises(ValueError) as err:
        fetch_entries({"type": "epic", "epic_code": "STALE"},
                      session=FakeEpic(token_status=400))
    assert "single-use" in str(err.value)


def test_epic_without_anything_is_empty():
    assert fetch_entries({"type": "epic"}) == []


# -- EA ----------------------------------------------------------------------

class FakeEa:
    def __init__(self, identity_status=200):
        self.identity_status = identity_status

    def get(self, url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer TOKEN"
        if "identity/pids/me" in url:
            if self.identity_status >= 400:
                return R(status=self.identity_status)
            return R({"pid": {"pidId": "9001"}})
        assert "consolidatedentitlements/9001" in url
        return R({"entitlements": [
            {"originDisplayName": "Dead Space", "offerType": "BASE_GAME"},
            {"originDisplayName": "Dead Space DLC", "offerType": "EXPANSION"},
            {"productName": "Mass Effect"},
        ]})


def test_ea_pulls_owned_base_games_not_dlc():
    entries = fetch_entries({"type": "ea", "ea_token": "TOKEN"},
                            session=FakeEa())
    assert [e.game for e in entries] == ["Dead Space", "Mass Effect"]


def test_ea_says_what_to_do_when_the_token_has_expired():
    with pytest.raises(ValueError) as err:
        fetch_entries({"type": "ea", "ea_token": "TOKEN"},
                      session=FakeEa(identity_status=401))
    assert "fresh one" in str(err.value)


def test_ea_without_a_token_is_empty():
    assert fetch_entries({"type": "ea", "ea_token": ""}) == []


# -- Battle.net --------------------------------------------------------------

BNET = {"gameAccounts": [{"localizedGameName": "Diablo IV"},
                         {"gameName": "World of Warcraft"}]}


def test_battlenet_reads_the_json_the_account_page_shows():
    """That page is data, not a credential -- pasting it stores no secret."""
    entries = fetch_entries({"type": "battlenet",
                             "battlenet_json": json.dumps(BNET)})
    assert [e.game for e in entries] == ["Diablo IV", "World of Warcraft"]


def test_battlenet_can_fetch_with_a_session_cookie_instead():
    class FakeBnet:
        def get(self, url, headers=None, timeout=None):
            assert "account.blizzard.com" in url
            assert headers["Cookie"] == "SESSION=x"
            return R(BNET)

    entries = fetch_entries({"type": "battlenet", "battlenet_cookie": "SESSION=x"},
                            session=FakeBnet())
    assert [e.game for e in entries] == ["Diablo IV", "World of Warcraft"]


def test_battlenet_rejects_something_that_is_not_that_document():
    with pytest.raises(ValueError) as err:
        fetch_entries({"type": "battlenet", "battlenet_json": "my games lol"})
    assert "games-and-subs" in str(err.value)


def test_battlenet_without_anything_is_empty():
    assert fetch_entries({"type": "battlenet"}) == []


# -- the claim that started this ---------------------------------------------

def test_all_three_are_real_connectors_now():
    for store in ("epic", "ea", "battlenet"):
        assert store in LIST_TYPES
    assert set(NO_API_STORES) == {"Nintendo"}
