"""Peering: the trust model, proven offline before any server is involved."""

from dataclasses import dataclass

import pytest

from romarr.federation import (ACCESS, CLAIM_ALPHABET, CLAIM_CODE_TTL,
                               CLAIM_LENGTH, MAX_CLAIM_ATTEMPTS, SCOPES,
                               Federation, Peer, new_claim_code,
                               normalise_claim_code, parse_invite_link)


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


# -- the link, which must never be worth stealing -----------------------------
#
# A URL is the worst envelope there is for a bearer credential: history, chat
# previews, referrers and proxy logs all see it. So the link carries an id and
# a name and nothing else, and the secret is a code the friend types.


def test_the_invitation_link_contains_no_secret():
    """The property the whole two-part design exists for."""
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite(name="Alice")
    link = invite.link()
    assert invite.secret not in link
    assert invite.code not in link
    assert invite.code_display not in link
    assert link.startswith("https://alice.example/link#")


def test_everything_identifying_is_in_the_fragment():
    """A fragment is not transmitted. That is what keeps the invitation id
    out of the inviter's access log and out of a link preview's fetch."""
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite(name="Alice")
    before_hash, _, fragment = invite.link().partition("#")
    assert before_hash == "https://alice.example/link", \
        "anything before the # is sent to the server on every click"
    assert invite.peer_id in fragment


def test_a_server_with_no_address_mints_no_link():
    """A link to nowhere reads as a working invitation and fails later, as
    silence -- so there is no link at all until a public URL is set."""
    assert Federation("Alice").invite().link() == ""


def test_a_link_names_the_server_it_came_from():
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite(name="Alice's shelf")
    parsed = parse_invite_link(invite.link())
    assert parsed == {"url": "https://alice.example",
                      "peer_id": invite.peer_id,
                      "name": "Alice's shelf", "rehomed": False}


def test_a_rehomed_link_carries_the_inviters_address_instead():
    """Once the landing page moves the link onto the recipient's own host,
    the origin is no longer the inviter's and `u` has to carry it."""
    parsed = parse_invite_link(
        "http://bob.local:7878/link#i=abc123&u=https%3A%2F%2Falice.example")
    assert parsed["url"] == "https://alice.example"
    assert parsed["rehomed"] is True


def test_junk_is_not_an_invitation_link():
    for bad in ("", "not a url", "https://alice.example/link",
                "ftp://alice.example/link#i=abc", "https://alice.example#i="):
        with pytest.raises(ValueError):
            parse_invite_link(bad)


# -- the claim code -----------------------------------------------------------


def test_a_claim_code_is_short_enough_to_read_aloud():
    code = new_claim_code()
    assert len(code) == CLAIM_LENGTH
    assert set(code) <= set(CLAIM_ALPHABET)
    # The letters that come back as digits over a phone are never minted.
    assert not set("ILOU") & set(code)


def test_a_mistyped_claim_code_still_works():
    """Crockford's rule: never mint the ambiguous characters, always forgive
    them. This costs no entropy and is the difference between a code that
    works over the phone and one that works on the third try."""
    assert normalise_claim_code("k7rm-9tfq") == "K7RM9TFQ"
    assert normalise_claim_code("K7RM 9TFQ") == "K7RM9TFQ"
    assert normalise_claim_code("OI L") == "011"


def test_the_code_and_the_secret_open_the_same_one_invitation():
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite()
    peer = alice.accept(invite.peer_id, invite.code_display, name="Bob")
    # Whichever door they came through, the durable token is the long one.
    assert peer.token == invite.secret
    assert not peer.confirmed, "a claim code is not a grant either"
    # And spending one spends the other: it is one invitation.
    with pytest.raises(ValueError):
        alice.accept(invite.peer_id, invite.secret)


def test_the_claim_code_expires_long_before_the_invitation_does():
    """Forty bits sitting in a chat log all day is forty bits sitting in a
    chat log all day. The link stays good for the full 24 hours."""
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite()
    invite.created_at -= CLAIM_CODE_TTL + 60
    with pytest.raises(ValueError) as err:
        alice.accept(invite.peer_id, invite.code)
    assert "expired" in str(err.value)
    # The invitation itself is untouched, and the long secret still works.
    assert alice.accept(invite.peer_id, invite.secret).peer_id == invite.peer_id


def test_a_late_but_correct_code_is_not_counted_as_a_wrong_guess():
    """Telling somebody "that is wrong" sends them hunting for a typo that is
    not there, and spends a guess they did not make."""
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite()
    invite.created_at -= CLAIM_CODE_TTL + 60
    for _ in range(MAX_CLAIM_ATTEMPTS + 2):
        with pytest.raises(ValueError):
            alice.accept(invite.peer_id, invite.code)
    assert invite.attempts == 0
    assert invite.peer_id in alice._invites


def test_guessing_at_a_claim_code_destroys_the_invitation():
    """Eight characters is a number you can count to, so the cost of counting
    has to be that the thing being counted at stops existing."""
    alice = Federation("Alice", "https://alice.example")
    invite = alice.invite()
    for _ in range(MAX_CLAIM_ATTEMPTS - 1):
        with pytest.raises(ValueError):
            alice.accept(invite.peer_id, "AAAA-AAAA")
    with pytest.raises(ValueError) as err:
        alice.accept(invite.peer_id, "AAAA-AAAA")
    assert "destroyed" in str(err.value)
    # Burnt, so the real friend's correct code now fails too. That is the
    # alarm: they say so, and the operator learns somebody was guessing.
    with pytest.raises(ValueError):
        alice.accept(invite.peer_id, invite.code)
    assert alice.peers == {}


def test_a_guessed_code_still_sees_absolutely_nothing():
    """The property that makes a short code safe at all: redeeming is not a
    grant, and mutual confirmation is what stands between the two."""
    alice = Federation("Alice", "https://alice.example")
    alice_shelf = ALICE_SHELF
    invite = alice.invite()
    intruder = alice.accept(invite.peer_id, invite.code, name="not Bob")
    assert alice.authenticate(intruder.peer_id, intruder.token) is None
    assert alice.project(intruder, alice_shelf) == []
    # Even handed the widest possible policy, an unconfirmed peer sees none
    # of it -- the confirmation gate is checked in `may_see`, not only at
    # the door.
    intruder.scope, intruder.access = "all", "fetch"
    assert alice.project(intruder, alice_shelf) == []
    # And the recovery is the same one revocation always was.
    assert alice.revoke(intruder.peer_id)


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


# -- friends who run a plain RomM -------------------------------------------
#
# The far side has never heard of this protocol. There is nobody to shake
# hands with, so the relationship is made by an account my friend created for
# me on their server.


def test_a_romm_friend_needs_an_address_and_a_credential():
    fed = Federation("Alice")
    with pytest.raises(ValueError):
        fed.add_romm("", "user", "pass")
    with pytest.raises(ValueError):
        fed.add_romm("https://romm.example", "", "")


def test_a_romm_friend_is_confirmed_on_arrival():
    """No handshake, because I typed the address and credential myself."""
    fed = Federation("Alice")
    peer = fed.add_romm("https://romm.example/", "me", "secret",
                        name="Dave's RomM")
    assert peer.kind == "romm"
    assert peer.confirmed is True
    assert peer.url == "https://romm.example", "trailing slash is trimmed"
    assert peer.name == "Dave's RomM"


def test_a_romm_friends_credential_cannot_open_my_own_library():
    """The token is one *I* hold for *them*, not one they present to me.

    Letting it authenticate inbound would turn a key my friend handed me --
    which their admins also hold -- into a key to my library.
    """
    fed = Federation("Alice")
    peer = fed.add_romm("https://romm.example", token="their-api-token")
    assert peer.token == "their-api-token"
    assert fed.authenticate(peer.peer_id, "their-api-token") is None


def test_a_romm_friend_survives_a_restart_with_its_credential():
    fed = Federation("Alice")
    peer = fed.add_romm("https://romm.example", "me", "secret")
    restarted = Federation("Alice")
    restarted.restore(fed.dump())
    back = restarted.peers[peer.peer_id]
    assert back.kind == "romm"
    assert (back.username, back.password) == ("me", "secret")
