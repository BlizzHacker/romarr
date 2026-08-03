"""Nobody who is not you gets to queue a download.

ROMarr had no authentication of any kind. `_guard` is an exception handler,
not a gate, so every endpoint -- including the ones that grab a torrent, write
to the library, and rewrite settings -- answered anybody who could reach the
port. On a LAN that is one curl from a guest network.
"""

from __future__ import annotations

import pytest

from romarr.auth import Auth, new_api_key


def test_a_generated_key_is_long_enough_to_be_worth_generating():
    key = new_api_key()
    assert len(key) >= 32
    assert key != new_api_key()


def test_the_right_key_is_accepted_and_a_wrong_one_is_not():
    auth = Auth(api_key="s3cret")
    assert auth.check_key("s3cret")
    assert not auth.check_key("s3cre")
    assert not auth.check_key("s3crett")
    assert not auth.check_key("")
    assert not auth.check_key(None)


def test_an_empty_configured_key_never_authorises():
    """A blank key must not turn into "anything matches".

    `hmac.compare_digest("", "")` is True, so a config that lost its key would
    otherwise authorise every request that sent no key at all -- an outage
    that fails open.
    """
    auth = Auth(api_key="")
    assert not auth.check_key("")
    assert not auth.check_key("anything")
    assert not auth.authorised(headers={}, query={}, cookies={})


# --- how a request presents itself ----------------------------------------

def test_the_header_is_accepted():
    auth = Auth(api_key="k")
    assert auth.authorised(headers={"X-Api-Key": "k"}, query={}, cookies={})


def test_the_header_is_case_insensitive():
    """HTTP header names are case-insensitive and real clients differ."""
    auth = Auth(api_key="k")
    for name in ("X-Api-Key", "x-api-key", "X-API-KEY"):
        assert auth.authorised(headers={name: "k"}, query={}, cookies={}), name


def test_a_bearer_token_is_accepted():
    auth = Auth(api_key="k")
    assert auth.authorised(headers={"Authorization": "Bearer k"}, query={},
                           cookies={})


def test_the_query_string_is_accepted_because_some_senders_cannot_set_headers():
    """The inbound request webhook is the case.

    A front-end that posts a notification may not let you add a header, and
    refusing it would mean either no auth on the one endpoint that queues
    downloads, or no integration at all.
    """
    auth = Auth(api_key="k")
    assert auth.authorised(headers={}, query={"apikey": ["k"]}, cookies={})


def test_a_wrong_key_anywhere_is_still_refused():
    auth = Auth(api_key="k")
    assert not auth.authorised(headers={"X-Api-Key": "nope"}, query={}, cookies={})
    assert not auth.authorised(headers={}, query={"apikey": ["nope"]}, cookies={})
    assert not auth.authorised(headers={}, query={}, cookies={"romarr_session": "nope"})


# --- sessions, so a browser does not hold the key in every request --------

def test_a_session_issued_here_is_accepted_here():
    auth = Auth(api_key="k")
    token = auth.issue_session()
    assert auth.authorised(headers={}, query={}, cookies={"romarr_session": token})


def test_a_session_from_another_install_is_not_accepted():
    """The token is signed with the API key, so it cannot travel."""
    token = Auth(api_key="one").issue_session()
    assert not Auth(api_key="two").authorised(
        headers={}, query={}, cookies={"romarr_session": token})


def test_a_tampered_session_is_refused():
    auth = Auth(api_key="k")
    token = auth.issue_session()
    body, _, signature = token.rpartition(".")
    assert not auth.check_session(f"{body}x.{signature}")
    assert not auth.check_session(f"{body}.{signature}x")
    assert not auth.check_session(body)


def test_a_session_expires():
    auth = Auth(api_key="k", session_seconds=-1)
    assert not auth.check_session(auth.issue_session())


# --- passwords -------------------------------------------------------------

def test_a_password_is_not_stored_in_the_clear():
    auth = Auth(api_key="k")
    stored = auth.hash_password("hunter2")
    assert "hunter2" not in stored
    assert stored.count("$") >= 2, "salt and digest must both be recorded"


def test_the_right_password_is_accepted():
    auth = Auth(api_key="k")
    auth.password_hash = auth.hash_password("hunter2")
    assert auth.check_password("hunter2")
    assert not auth.check_password("hunter3")
    assert not auth.check_password("")


def test_no_password_configured_means_password_login_is_off_not_open():
    auth = Auth(api_key="k")
    assert not auth.check_password("")
    assert not auth.check_password("anything")


def test_the_same_password_hashes_differently_each_time():
    auth = Auth(api_key="k")
    assert auth.hash_password("x") != auth.hash_password("x"), "salt it"


# --- the escape hatch ------------------------------------------------------

def test_auth_can_be_turned_off_deliberately():
    """For an install already behind an authenticating reverse proxy.

    Real, and this operator runs one. Making them keep a second credential
    in front of a proxy that already checked identity is friction with no
    security gain -- but it has to be a deliberate setting, never a default
    and never something a missing key falls back to.
    """
    auth = Auth(api_key="k", enabled=False)
    assert auth.authorised(headers={}, query={}, cookies={})


def test_off_is_only_ever_explicit():
    assert Auth(api_key="k").enabled
    assert Auth(api_key="").enabled, "a missing key must not disable the gate"
