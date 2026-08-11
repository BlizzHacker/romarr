"""Invitation links and claim codes, proven between two live servers.

`test_federation.py` proves the trust model offline. This proves the wire: a
link that a chat client could safely preview, a code somebody typed, and two
ROMarrs on loopback ports that end up peered without anybody pasting JSON.

The reason it is two real servers rather than one with the far side stubbed:
the claim exchange is the one part of peering where a *credential* crosses the
wire in a direction it never crossed before -- a short code out, a long token
back -- and a stub would prove only that the stub agreed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler
from romarr.federation import CLAIM_CODE_TTL, MAX_CLAIM_ATTEMPTS


def _serve(service):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def pair(tmp_path):
    """Alice and Bob, each a whole ROMarr on its own port."""
    servers = []
    made = {}
    for who in ("alice", "bob"):
        service = ROMarr({"ROMARR_DATA": str(tmp_path / f"{who}.json"),
                          "ROMARR_API_KEY": f"{who}key",
                          "ROMARR_PEER_NAME": who.title()})
        httpd, base = _serve(service)
        servers.append(httpd)
        # Known only after the socket is bound, which is also true of any
        # real install behind a reverse proxy on a port it did not choose.
        service.federation.url = base
        service.store.settings["public_url"] = base
        made[who] = (service, base)
    yield made
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def call(url, *, key=None, method="GET", body=None, headers=None):
    request = urllib.request.Request(url, method=method)
    if key:
        request.add_header("X-Api-Key", key)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def mint(base, key="alicekey"):
    code, raw = call(base + "/api/v1/peer/invite", key=key, method="POST",
                     body={})
    assert code == 200, raw
    return json.loads(raw)


# --- the landing page --------------------------------------------------------

def test_the_landing_page_answers_a_stranger(pair):
    """It has to: the person opening an invitation link is somebody else's
    operator, and has no account on the server that minted it."""
    _, base = pair["alice"]
    code, body = call(base + "/link")
    assert code == 200
    assert b"ROM" in body


def test_the_landing_page_is_the_same_bytes_for_everybody(pair):
    """SECURITY.md's whole argument for `/link` being open is that there is
    nothing behind it to ask."""
    alice, base = pair["alice"]
    invite = mint(base)
    _, anonymous = call(base + "/link")
    _, with_key = call(base + "/link", key="alicekey")
    _, with_the_invitation_in_the_query = call(
        base + "/link?i=" + invite["invite"]["peer_id"])
    assert anonymous == with_key == with_the_invitation_in_the_query
    assert invite["invite"]["peer_id"].encode() not in anonymous
    assert invite["invite"]["secret"].encode() not in anonymous
    assert alice.federation._invites, "and it did not spend the invitation"


# --- what the operator is handed ---------------------------------------------

def test_minting_produces_a_link_that_is_not_worth_stealing(pair):
    _, base = pair["alice"]
    invite = mint(base)
    assert invite["link"].startswith(base + "/link#")
    assert invite["invite"]["secret"] not in invite["link"]
    assert invite["code"] not in invite["link"]
    assert invite["code"].replace("-", "") not in invite["link"]
    # And the code is short enough to read down a phone line.
    assert len(invite["code"].replace("-", "")) == 8
    assert 0 < invite["code_expires_in"] <= CLAIM_CODE_TTL


def test_an_install_with_no_address_says_so_instead_of_minting_a_dead_link(
        tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_API_KEY": "k"})
    service.federation.url = ""
    service.store.settings["public_url"] = ""
    httpd, base = _serve(service)
    try:
        invite = mint(base, key="k")
        assert invite["link"] == "", "a link to nowhere fails later, as silence"
        assert "warning" in invite
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- the whole flow ----------------------------------------------------------

def test_a_link_and_a_code_peer_two_servers(pair):
    alice, alice_url = pair["alice"]
    bob, bob_url = pair["bob"]
    invite = mint(alice_url)

    status, raw = call(bob_url + "/api/v1/peer/claim", key="bobkey",
                       method="POST",
                       body={"link": invite["link"], "code": invite["code"]})
    assert status == 200, raw
    result = json.loads(raw)
    assert result["ok"] and result["name"] == "Alice"

    # Bob holds a real, durable credential -- not the eight characters he
    # typed, which are already worthless.
    friend = bob.federation.peers[result["peer_id"]]
    assert friend.token == invite["invite"]["secret"]
    assert friend.url == alice_url
    assert friend.confirmed, "Bob typed the code his friend gave him"

    # Alice holds him UNCONFIRMED. This is the step that makes a leaked or
    # guessed invitation recoverable, and it is why a short code is safe.
    held = alice.federation.peers[result["peer_id"]]
    assert not held.confirmed
    assert held.url == bob_url, "she can call him back"


def test_a_redeemed_but_unconfirmed_peer_sees_nothing(pair):
    """The claim succeeded. That is not access, and this is where it shows."""
    alice, alice_url = pair["alice"]
    bob, bob_url = pair["bob"]
    invite = mint(alice_url)
    call(bob_url + "/api/v1/peer/claim", key="bobkey", method="POST",
         body={"link": invite["link"], "code": invite["code"]})
    peer_id = invite["invite"]["peer_id"]
    token = invite["invite"]["secret"]
    headers = {"X-Peer-Id": peer_id, "X-Peer-Token": token}

    status, _ = call(alice_url + "/api/v1/peer/shelf", headers=headers)
    assert status == 401, "an unconfirmed peer authenticates as nobody"
    status, _ = call(alice_url + "/api/v1/peer/netplay", method="POST",
                     headers=headers, body={"offer": {"sha1": "a" * 40}})
    assert status == 401

    # Alice confirms, and only now is there a relationship to use. It is
    # still empty, because peering shares nothing by itself.
    call(alice_url + "/api/v1/peer/confirm", key="alicekey", method="POST",
         body={"peer_id": peer_id})
    status, raw = call(alice_url + "/api/v1/peer/shelf", headers=headers)
    assert status == 200
    assert json.loads(raw)["items"] == []


def test_a_claim_works_once(pair):
    alice, alice_url = pair["alice"]
    bob, bob_url = pair["bob"]
    invite = mint(alice_url)
    body = {"link": invite["link"], "code": invite["code"]}
    assert call(bob_url + "/api/v1/peer/claim", key="bobkey",
                method="POST", body=body)[0] == 200
    status, raw = call(bob_url + "/api/v1/peer/claim", key="bobkey",
                       method="POST", body=body)
    assert status == 400
    assert "no invitation" in json.loads(raw)["error"]


def test_an_expired_code_is_refused_but_leaves_the_invitation_alive(pair):
    alice, alice_url = pair["alice"]
    _, bob_url = pair["bob"]
    invite = mint(alice_url)
    held = alice.federation._invites[invite["invite"]["peer_id"]]
    held.created_at -= CLAIM_CODE_TTL + 60

    status, raw = call(bob_url + "/api/v1/peer/claim", key="bobkey",
                       method="POST",
                       body={"link": invite["link"], "code": invite["code"]})
    assert status == 400
    assert "expired" in json.loads(raw)["error"]
    # The link is good for 24 hours; only the half a human can retype is not.
    assert invite["invite"]["peer_id"] in alice.federation._invites


def test_guessing_a_code_over_the_wire_destroys_the_invitation(pair):
    alice, alice_url = pair["alice"]
    _, bob_url = pair["bob"]
    invite = mint(alice_url)
    for _ in range(MAX_CLAIM_ATTEMPTS):
        call(bob_url + "/api/v1/peer/claim", key="bobkey", method="POST",
             body={"link": invite["link"], "code": "AAAA-AAAA"})
    assert invite["invite"]["peer_id"] not in alice.federation._invites
    # Which is also the alarm: the friend it was meant for now fails.
    status, raw = call(bob_url + "/api/v1/peer/claim", key="bobkey",
                       method="POST",
                       body={"link": invite["link"], "code": invite["code"]})
    assert status == 400
    assert alice.federation.peers == {}


def test_a_claim_needs_both_halves(pair):
    _, alice_url = pair["alice"]
    _, bob_url = pair["bob"]
    invite = mint(alice_url)
    status, raw = call(bob_url + "/api/v1/peer/claim", key="bobkey",
                       method="POST", body={"link": invite["link"], "code": ""})
    assert status == 400
    assert "separately" in json.loads(raw)["error"], \
        "the message has to say WHY there is a second thing"
    status, raw = call(bob_url + "/api/v1/peer/claim", key="bobkey",
                       method="POST",
                       body={"link": "https://nope.example/link",
                             "code": invite["code"]})
    assert status == 400


def test_claiming_is_the_operators_own_action(pair):
    """It spends a credential and stores a relationship. The peer-facing
    routes are open; this one is not."""
    _, alice_url = pair["alice"]
    _, bob_url = pair["bob"]
    invite = mint(alice_url)
    status, _ = call(bob_url + "/api/v1/peer/claim", method="POST",
                     body={"link": invite["link"], "code": invite["code"]})
    assert status == 401


def test_the_pasted_blob_still_works(pair):
    """Nobody's in-flight invitation breaks because a nicer form arrived."""
    alice, alice_url = pair["alice"]
    bob, bob_url = pair["bob"]
    invite = mint(alice_url)
    status, raw = call(bob_url + "/api/v1/peer/redeem", key="bobkey",
                       method="POST", body={"invite": invite["invite"]})
    assert status == 200, raw
    peer_id = json.loads(raw)["peer_id"]
    assert bob.federation.peers[peer_id].token == invite["invite"]["secret"]
    # And the long secret still completes the handshake on Alice's side.
    status, raw = call(alice_url + "/api/v1/peer/accept", method="POST",
                       body={"peer_id": peer_id,
                             "secret": invite["invite"]["secret"],
                             "name": "Bob", "url": bob_url})
    assert status == 200
    assert not json.loads(raw)["confirmed"]
