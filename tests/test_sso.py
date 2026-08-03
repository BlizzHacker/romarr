"""Trusting an identity your reverse proxy already established.

Authentik, Authelia, oauth2-proxy and Cloudflare Access all work the same way:
they authenticate, then add a header naming the user. The app's job is to
believe it -- but *only* from the proxy.

That "only" is the entire security of the arrangement. A header is just text
anybody can send, so an app that believes `X-authentik-username` from any
source has not added SSO, it has added a way to become any user by typing one
header. Every test below exists because of that sentence.
"""

from __future__ import annotations

import pytest

from romarr.sso import FORWARD_PROVIDERS, ForwardAuth, trusted_peer


# --- the peer check, which everything else depends on ---------------------

@pytest.mark.parametrize("peer,allowed", [
    ("10.0.0.5", ["10.0.0.0/8"]),
    ("192.168.0.94", ["192.168.0.0/24"]),
    ("127.0.0.1", ["127.0.0.1/32"]),
    ("172.18.0.3", ["172.16.0.0/12"]),
])
def test_a_peer_inside_a_trusted_range_is_trusted(peer, allowed):
    assert trusted_peer(peer, allowed)


@pytest.mark.parametrize("peer,allowed", [
    ("10.0.0.5", ["192.168.0.0/24"]),
    ("192.168.1.7", ["192.168.0.0/24"]),
    ("8.8.8.8", ["10.0.0.0/8"]),
    ("", ["10.0.0.0/8"]),
    ("not-an-ip", ["10.0.0.0/8"]),
])
def test_a_peer_outside_every_trusted_range_is_not(peer, allowed):
    assert not trusted_peer(peer, allowed)


def test_no_trusted_ranges_trusts_nobody():
    """Fail closed. An empty list is a configuration that has not been
    finished, and reading it as "trust everything" turns an unconfigured
    install into an open one."""
    assert not trusted_peer("10.0.0.5", [])
    assert not trusted_peer("127.0.0.1", None)


def test_a_bare_address_works_as_well_as_a_cidr():
    assert trusted_peer("192.168.0.94", ["192.168.0.94"])
    assert not trusted_peer("192.168.0.95", ["192.168.0.94"])


# --- provider presets ------------------------------------------------------

def test_the_providers_people_actually_run_are_presets():
    for name in ("authentik", "authelia", "oauth2-proxy", "cloudflare-access",
                 "tinyauth", "custom"):
        assert name in FORWARD_PROVIDERS, name


@pytest.mark.parametrize("provider,header", [
    ("authentik", "X-authentik-username"),
    ("authelia", "Remote-User"),
    ("oauth2-proxy", "X-Forwarded-User"),
    ("cloudflare-access", "Cf-Access-Authenticated-User-Email"),
])
def test_each_preset_names_the_header_that_provider_really_sends(provider, header):
    assert FORWARD_PROVIDERS[provider]["user_header"] == header


# --- believing the header --------------------------------------------------

def sso(**kw):
    kw.setdefault("provider", "authentik")
    kw.setdefault("trusted_proxies", ["10.0.0.0/8"])
    return ForwardAuth(**kw)


def test_an_identity_from_the_proxy_is_accepted():
    got = sso().identify({"X-authentik-username": "wade"}, peer="10.0.0.2")
    assert got.ok and got.user == "wade"


def test_the_same_header_from_anywhere_else_is_ignored_entirely():
    """The whole point. Without the peer check this is a login bypass with a
    one-line exploit: `curl -H 'X-authentik-username: admin'`."""
    got = sso().identify({"X-authentik-username": "admin"}, peer="8.8.8.8")
    assert not got.ok
    assert not got.user
    assert "not a trusted proxy" in got.reason


def test_forward_auth_with_no_trusted_proxies_refuses_everyone():
    got = ForwardAuth(provider="authentik", trusted_proxies=[]).identify(
        {"X-authentik-username": "wade"}, peer="10.0.0.2")
    assert not got.ok


def test_a_trusted_proxy_that_sends_no_identity_is_not_a_login():
    """The proxy is reachable but the request was never authenticated -- an
    unprotected path in the proxy config, typically. Not an identity."""
    got = sso().identify({}, peer="10.0.0.2")
    assert not got.ok
    assert "no identity header" in got.reason


def test_the_header_is_matched_case_insensitively():
    got = sso().identify({"x-authentik-username": "wade"}, peer="10.0.0.2")
    assert got.ok and got.user == "wade"


def test_a_custom_header_name_is_honoured():
    got = ForwardAuth(provider="custom", user_header="X-Whoami",
                      trusted_proxies=["10.0.0.0/8"]).identify(
        {"X-Whoami": "wade"}, peer="10.0.0.2")
    assert got.ok and got.user == "wade"


# --- group restriction -----------------------------------------------------

def test_a_required_group_is_enforced():
    auth = sso(required_group="romarr-admins")
    headers = {"X-authentik-username": "wade",
               "X-authentik-groups": "users|romarr-admins|media"}
    assert auth.identify(headers, peer="10.0.0.2").ok


def test_somebody_outside_the_required_group_is_refused():
    auth = sso(required_group="romarr-admins")
    headers = {"X-authentik-username": "guest",
               "X-authentik-groups": "users|media"}
    got = auth.identify(headers, peer="10.0.0.2")
    assert not got.ok
    assert "romarr-admins" in got.reason


def test_a_required_group_with_no_groups_header_is_refused():
    """Authentik only sends groups if the provider is configured to. Treating
    a missing header as "no restriction" would silently disable the
    restriction the operator asked for."""
    auth = sso(required_group="romarr-admins")
    got = auth.identify({"X-authentik-username": "wade"}, peer="10.0.0.2")
    assert not got.ok


@pytest.mark.parametrize("raw", [
    "users|romarr-admins",          # authentik
    "users,romarr-admins",          # oauth2-proxy
    "users romarr-admins",          # whitespace
])
def test_group_lists_are_read_in_every_separator_providers_use(raw):
    auth = sso(required_group="romarr-admins")
    headers = {"X-authentik-username": "w", "X-authentik-groups": raw}
    assert auth.identify(headers, peer="10.0.0.2").ok


def test_no_required_group_means_any_authenticated_user():
    got = sso().identify({"X-authentik-username": "anyone"}, peer="10.0.0.2")
    assert got.ok


# --- how the peer is determined -------------------------------------------

def test_the_peer_is_the_socket_not_a_forwarded_for_header():
    """`X-Forwarded-For` is client-controlled. Taking the leftmost entry as
    the peer lets anybody claim to be the proxy, which is the same bypass the
    peer check exists to close, reintroduced through the back door.
    """
    got = sso().identify(
        {"X-authentik-username": "admin", "X-Forwarded-For": "10.0.0.2"},
        peer="8.8.8.8")
    assert not got.ok
