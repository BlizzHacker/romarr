"""URLs a human typed: no scheme, and ROMarr still makes the call.

The failure this locks down, reported live: a peer whose public URL was
stored as `romarr.ant1695.fyi` made every Friends-page fetch die with
`MissingSchema: Invalid URL ... No scheme supplied` -- requests never
sent anything, and the operator read "could not reach" for what was a
missing `https://`. The rule now: an explicit scheme is honoured; a
scheme-less address is tried as https first, then http; junk is refused
with a message that says what to type.
"""

import pytest

from romarr.federation import (Federation, ensure_scheme, parse_invite_link,
                               url_candidates)


# -- the helper itself ---------------------------------------------------------

def test_an_explicit_scheme_is_honoured_exactly_once():
    # Somebody who typed https:// told us which one. Guessing a second
    # candidate would silently try http on a deliberate choice.
    assert url_candidates("https://a.example") == ["https://a.example"]
    assert url_candidates("http://a.example") == ["http://a.example"]


def test_a_scheme_less_address_tries_https_before_http():
    assert url_candidates("romarr.ant1695.fyi") == [
        "https://romarr.ant1695.fyi", "http://romarr.ant1695.fyi"]


def test_scheme_probing_survives_the_messy_pastes():
    assert url_candidates("  https://a.example/  ") == ["https://a.example/"]
    assert url_candidates("A.EXAMPLE.com") == ["https://A.EXAMPLE.com",
                                               "http://A.EXAMPLE.com"]
    assert url_candidates("https://a.example:8443/x") == [
        "https://a.example:8443/x"]


def test_junk_yields_no_candidates_rather_than_a_broken_url():
    assert url_candidates("") == []
    assert url_candidates("   ") == []
    assert url_candidates("/just/a/path") == []
    assert url_candidates("ftp://files.example") == []


def test_ensure_scheme_picks_the_secure_first_candidate():
    assert ensure_scheme("romarr.ant1695.fyi") == "https://romarr.ant1695.fyi"
    assert ensure_scheme("https://a.example") == "https://a.example"
    assert ensure_scheme("http://a.example") == "http://a.example"
    assert ensure_scheme("") == ""
    assert ensure_scheme("ftp://x") == ""


# -- normalized where URLs are born -------------------------------------------

def test_a_federation_started_with_a_bare_host_mints_working_links():
    # parse_invite_link REFUSES a scheme-less origin, so before this fix a
    # Federation(url="romarr.local") minted invitations that could not be
    # redeemed by anyone. The URL is normalized at birth instead.
    fed = Federation("Box", "romarr.local")
    assert fed.url == "https://romarr.local"
    invite = fed.invite(name="Box")
    assert invite.link().startswith("https://romarr.local/link#")
    parsed = parse_invite_link(invite.link())
    assert parsed["url"] == "https://romarr.local"
    assert parsed["peer_id"] == invite.peer_id


def test_an_explicit_public_url_is_not_second_guessed():
    fed = Federation("Box", "http://192.168.1.50:7878")
    assert fed.url == "http://192.168.1.50:7878"


def test_add_romm_stores_an_absolute_url():
    fed = Federation("Box", "https://box.example")
    peer = fed.add_romm("romm.lan", token="tok")
    assert peer.url == "https://romm.lan"


def test_accepting_a_peer_normalizes_their_reported_address():
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite(name="Alice")
    # Bob's request body carries a scheme-less URL (the redeem endpoint
    # forwards whatever the redeemer sent).
    peer = alice.accept(invite.peer_id, invite.secret, name="Bob",
                        url="bob.box.lan")
    assert peer.url == "https://bob.box.lan"


# -- the outbound call actually probes -----------------------------------------

class _StubResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _StubPeer:
    """Only what _call_friend touches: url, name, headers()."""

    def __init__(self, url, name="friend"):
        self.url = url
        self.name = name

    def headers(self):
        return {"X-Peer": "test"}


def _call(peer, monkeypatch, responder):
    """Run ROMarr._call_friend with requests.request replaced by responder."""
    from romarr.app import ROMarr
    import requests
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return responder(url)

    monkeypatch.setattr(requests, "request", fake_request)
    return ROMarr._call_friend(None, peer, "GET",
                               "/api/v1/peer/shelf"), calls


def test_the_reported_bug_now_completes_instead_of_erroring(monkeypatch):
    # Exactly the live failure: scheme-less peer URL, shelf fetch. Before the
    # fix this raised MissingSchema from inside requests and reported
    # "could not reach". Now https is tried, fails, and http answers.
    import requests

    def responder(url):
        if url.startswith("https://"):
            raise requests.exceptions.ConnectionError("no TLS here")
        return _StubResponse(payload={"rows": []})

    (body, err), calls = _call(_StubPeer("romarr.ant1695.fyi"), monkeypatch,
                               responder)
    assert err == ""
    assert body == {"rows": []}
    assert len(calls) == 2
    assert "MissingSchema" not in err


def test_https_first_http_fallback_and_the_first_answer_wins(monkeypatch):
    import requests
    seen = []

    def responder(url):
        seen.append(url)
        if url.startswith("https://"):
            raise requests.exceptions.ConnectionError("refused")
        return _StubResponse(payload={"rows": ["ok"]})

    (body, err), calls = _call(_StubPeer("peer.lan"), monkeypatch, responder)
    assert err == ""
    assert body == {"rows": ["ok"]}
    assert calls == ["https://peer.lan/api/v1/peer/shelf",
                     "http://peer.lan/api/v1/peer/shelf"], (
        "https must be tried first, http second -- not the reverse")
    assert seen == calls


def test_an_explicit_http_peer_is_not_upgraded_to_https(monkeypatch):
    # A deliberate http:// (LAN box, no cert) must NOT get an https attempt
    # first: that would add a pointless timeout to every call.
    (body, err), calls = _call(
        _StubPeer("http://192.168.1.9:7878"), monkeypatch,
        lambda url: _StubResponse(payload={"n": 1}))
    assert err == "" and body == {"n": 1}
    assert calls == ["http://192.168.1.9:7878/api/v1/peer/shelf"]


def test_a_non_url_peer_address_explains_itself(monkeypatch):
    (body, err), calls = _call(_StubPeer(""), monkeypatch,
                               lambda url: _StubResponse())
    assert body is None
    assert "not a URL" in err
    assert calls == [], "nothing may be sent for an unusable address"


def test_a_401_on_the_first_candidate_stops_the_probing(monkeypatch):
    # An answered 401 is not "could not reach" -- the server is there and
    # refused the credential. Falling through to http would retry a
    # deliberate refusal on a weaker channel.
    (body, err), calls = _call(
        _StubPeer("peer.example"), monkeypatch,
        lambda url: _StubResponse(status=401))
    assert body is None
    assert "refused the credential" in err
    assert calls == ["https://peer.example/api/v1/peer/shelf"]


def test_an_http_500_answer_is_reported_not_retried_on_http(monkeypatch):
    # Same reasoning: an ANSWER (even a bad one) ends the scheme probe.
    # Retrying a 500 on http would double the load on a sick server.
    (body, err), calls = _call(
        _StubPeer("peer.example"), monkeypatch,
        lambda url: _StubResponse(status=500))
    assert body is None and "HTTP 500" in err
    assert len(calls) == 1


def test_both_schemes_failing_reports_the_last_error(monkeypatch):
    import requests

    def responder(url):
        raise requests.exceptions.ConnectionError(f"dead: {url}")

    (body, err), calls = _call(_StubPeer("peer.example"), monkeypatch,
                               responder)
    assert body is None
    assert "could not reach" in err
    assert len(calls) == 2, "both schemes must have been attempted"
