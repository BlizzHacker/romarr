"""Peering: the trust model, proven offline before any server is involved."""

from dataclasses import dataclass

import pytest

from romarr.federation import ACCESS, SCOPES, Federation, Peer


@dataclass
class G:
    name: str
    platform: str = ""
    year: int = 0
    verified: bool = False


ALICE_SHELF = [
    G("Chrono Trigger", "snes", 1995, True),
    G("Super Metroid", "snes", 1994, False),
    G("Gran Turismo", "psx", 1997, True),
]


def pair():
    """The full handshake, both sides, as two real instances would do it."""
    alice = Federation("Alice", "https://alice.example")
    bob = Federation("Bob", "https://bob.example")
    invite = alice.invite(name="Alice")
    bob.redeem(invite.blob())
    peer = alice.accept(invite.peer_id, invite.secret, name="Bob",
                        url="https://bob.example")
    return alice, bob, peer, invite


# -- the handshake ------------------------------------------------------------

def test_peering_needs_both_sides_to_act():
    alice, bob, peer, _ = pair()
    # Bob redeemed it, so on his side it is plainly intentional.
    assert list(bob.peers.values())[0].confirmed
    # On Alice's side it is held UNCONFIRMED: the secret proves somebody
    # holds the invite, not that it reached who she meant.
    assert not peer.confirmed
    alice.confirm(peer.peer_id)
    assert alice.peers[peer.peer_id].confirmed


def test_an_invite_is_single_use():
    alice, _, _, invite = pair()
    with pytest.raises(ValueError):
        alice.accept(invite.peer_id, invite.secret)


def test_a_wrong_secret_is_refused():
    alice = Federation("Alice")
    invite = alice.invite()
    with pytest.raises(ValueError) as err:
        alice.accept(invite.peer_id, "not-the-secret")
    assert "secret" in str(err.value)
    assert alice.peers == {}


def test_an_expired_invite_is_refused_and_forgotten():
    alice = Federation("Alice")
    invite = alice.invite()
    invite.created_at -= 25 * 3600      # a day and an hour ago
    with pytest.raises(ValueError) as err:
        alice.accept(invite.peer_id, invite.secret)
    assert "expired" in str(err.value)
    with pytest.raises(ValueError):
        alice.accept(invite.peer_id, invite.secret)


def test_a_malformed_invitation_is_refused():
    with pytest.raises(ValueError):
        Federation("Bob").redeem({"url": "https://alice.example"})


# -- authentication and revocation --------------------------------------------

def test_only_a_confirmed_peer_with_its_own_token_authenticates():
    alice, _, peer, _ = pair()
    assert alice.authenticate(peer.peer_id, peer.token) is None, \
        "unconfirmed peers cannot act"
    alice.confirm(peer.peer_id)
    assert alice.authenticate(peer.peer_id, peer.token) is peer
    assert alice.authenticate(peer.peer_id, "wrong") is None
    assert alice.authenticate("nobody", peer.token) is None


def test_revoking_one_peer_leaves_the_others_alone():
    alice = Federation("Alice")
    bobs = []
    for name in ("Bob", "Carol"):
        invite = alice.invite()
        bobs.append(alice.accept(invite.peer_id, invite.secret, name=name))
    for p in bobs:
        alice.confirm(p.peer_id)

    assert alice.revoke(bobs[0].peer_id)
    assert alice.authenticate(bobs[0].peer_id, bobs[0].token) is None
    # Carol is untouched -- per-peer tokens are the whole point.
    assert alice.authenticate(bobs[1].peer_id, bobs[1].token) is bobs[1]
    assert not alice.revoke(bobs[0].peer_id)


# -- what a peer may see ------------------------------------------------------

def confirmed_peer(**kw):
    p = Peer(peer_id="p1", name="Bob", token="t", confirmed=True, **kw)
    return p


def test_a_new_peer_sees_nothing_until_the_owner_says_otherwise():
    """Sharing is a decision, not a consequence of peering."""
    alice = Federation("Alice")
    assert alice.project(confirmed_peer(), ALICE_SHELF) == []


def test_scope_platforms_shares_only_the_named_shelf():
    alice = Federation("Alice")
    peer = confirmed_peer(scope="platforms", platforms=("snes",))
    titles = {g["title"] for g in alice.project(peer, ALICE_SHELF)}
    assert titles == {"Chrono Trigger", "Super Metroid"}


def test_scope_verified_shares_only_what_you_stand_behind():
    alice = Federation("Alice")
    titles = {g["title"] for g in
              alice.project(confirmed_peer(scope="verified"), ALICE_SHELF)}
    assert titles == {"Chrono Trigger", "Gran Turismo"}


def test_scope_all_shares_the_shelf():
    alice = Federation("Alice")
    assert len(alice.project(confirmed_peer(scope="all"), ALICE_SHELF)) == 3


def test_an_unconfirmed_peer_sees_nothing_whatever_its_scope():
    alice = Federation("Alice")
    peer = Peer(peer_id="p", name="x", scope="all", confirmed=False)
    assert alice.project(peer, ALICE_SHELF) == []


def test_the_projection_leaks_no_paths_ids_or_credentials():
    alice = Federation("Alice")
    rows = alice.project(confirmed_peer(scope="all"), ALICE_SHELF)
    for row in rows:
        assert set(row) == {"title", "platform", "year", "verified", "origin"}
        assert row["origin"] == "Alice", "content is marked as not theirs"


# -- the policy surface -------------------------------------------------------

def test_defaults_are_the_careful_ones():
    p = Peer(peer_id="p", name="x")
    assert p.scope == "none", "peering shares nothing by itself"
    assert p.access == "catalogue", "seeing, not fetching"
    assert p.delegate_users is False, "a peer's users are not the peer"
    assert not p.confirmed


def test_status_never_includes_a_token():
    alice, _, peer, _ = pair()
    row = alice.status()[0]
    assert "token" not in row
    assert peer.token not in str(row)


def test_the_vocabularies_are_what_the_design_says():
    assert SCOPES == ("none", "platforms", "verified", "all")
    assert ACCESS == ("catalogue", "stream", "fetch")


# -- surviving a restart ----------------------------------------------------
#
# Peers lived only in memory, so every friend and sharing policy vanished on
# restart. The Friends page came back empty and a peer that called in was an
# unknown peer -- silently, with nothing in the log to explain it.


def test_relationships_survive_a_restart():
    alice, _bob, peer, _invite = pair()
    peer_id = peer.peer_id
    alice.confirm(peer_id)
    alice.peers[peer_id].scope = "verified"
    alice.peers[peer_id].platforms = ("snes", "n64")
    alice.peers[peer_id].access = "stream"

    restarted = Federation("Alice", "https://alice.example")
    assert restarted.restore(alice.dump()) == 1

    peer = restarted.peers[peer_id]
    assert peer.scope == "verified"
    assert peer.platforms == ("snes", "n64"), "tuples survive JSON's lists"
    assert peer.access == "stream"
    assert peer.confirmed is True
    # And the credential still works, which is the point of persisting it.
    assert restarted.authenticate(peer_id, peer.token) is not None


def test_a_restart_does_not_resurrect_an_invitation():
    alice = Federation("Alice", "https://alice.example")
    alice.invite()
    restarted = Federation("Alice", "https://alice.example")
    restarted.restore(alice.dump())
    assert restarted._invites == {}, \
        "an unredeemed invite must not outlive the process that minted it"


def test_restore_skips_junk_rather_than_refusing_to_start():
    fed = Federation("Alice")
    assert fed.restore([{}, {"peer_id": ""}, "not a dict", None]) == 0
    assert fed.restore([{"peer_id": "x", "name": "y", "nonsense": 1}]) == 1, \
        "an unknown field from a newer version must not lose the peer"
