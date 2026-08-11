"""One-click connect: Steam OpenID, and the token-page shortcuts."""

import pytest

from romarr.connect import (TOKEN_SOURCES, new_state, steam_login_url,
                            steam_verify)

RETURN = "https://romarr.example.com/api/v1/connect/steam/return"


def test_the_login_url_asks_steam_to_identify_the_signed_in_user():
    url = steam_login_url(RETURN)
    assert url.startswith("https://steamcommunity.com/openid/login?")
    # identifier_select is what makes it one click rather than a login form.
    assert "identifier_select" in url
    assert "checkid_setup" in url
    assert "romarr.example.com" in url


class FakeSteam:
    def __init__(self, body="is_valid:true\n"):
        self.body = body
        self.posted = None

    def post(self, url, data=None, timeout=None):
        self.posted = data

        class R:
            text = self.body

            def raise_for_status(self):
                pass
        return R()


def _assertion(steam_id="76561198000000000"):
    return {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "id_res",
        "openid.claimed_id": f"https://steamcommunity.com/openid/id/{steam_id}",
        "openid.identity": f"https://steamcommunity.com/openid/id/{steam_id}",
        "openid.sig": "abc",
    }


def test_a_valid_assertion_yields_the_steam_id():
    steam = FakeSteam()
    assert steam_verify(_assertion(), session=steam) == "76561198000000000"
    # The whole point of the second round trip: mode is flipped to
    # check_authentication and Steam is asked to confirm its own signature.
    assert steam.posted["openid.mode"] == "check_authentication"
    assert steam.posted["openid.sig"] == "abc"


def test_an_assertion_steam_will_not_validate_is_refused():
    assert steam_verify(_assertion(), session=FakeSteam("is_valid:false\n")) == ""


def test_an_identity_that_is_not_steams_is_refused_before_any_request():
    """These parameters arrive through the user's browser, so a forged
    claimed_id must never reach the verification step, let alone past it."""
    forged = _assertion()
    forged["openid.claimed_id"] = "https://evil.example.com/openid/id/123"
    steam = FakeSteam()
    assert steam_verify(forged, session=steam) == ""
    assert steam.posted is None, "nothing should have been sent"


def test_a_non_numeric_steam_id_is_refused():
    forged = _assertion()
    forged["openid.claimed_id"] = \
        "https://steamcommunity.com/openid/id/../../admin"
    assert steam_verify(forged, session=FakeSteam()) == ""


def test_query_values_may_arrive_as_lists():
    """parse_qs gives {key: [value]}; the verifier must cope."""
    listy = {k: [v] for k, v in _assertion().items()}
    assert steam_verify(listy, session=FakeSteam()) == "76561198000000000"


def test_steam_being_unreachable_is_a_refusal_not_a_crash():
    class Down:
        def post(self, *a, **kw):
            raise OSError("no route")

    assert steam_verify(_assertion(), session=Down()) == ""


def test_state_values_are_unguessable_and_unique():
    assert len({new_state() for _ in range(50)}) == 50
    assert len(new_state()) >= 24


def test_every_token_source_names_a_page_and_a_field():
    for store in ("psn", "xbox", "itchio", "gog"):
        spec = TOKEN_SOURCES[store]
        assert spec["open"].startswith("https://")
        assert spec["field"] and spec["label"] and spec["how"]
