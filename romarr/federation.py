"""Peering: two ROMarr instances that know and trust each other.

The design is in docs/design/2026-08-11-federation-and-netplay-design.md;
this is step one of it -- the trust model and the catalogue projection,
with no network code in the hot path so all of it can be proven offline.

**Invitation plus mutual confirmation, never discovery.** There is no
directory and no "find servers near you": a peer exists because both
operators deliberately made it exist. Alice mints an invite and sends it
out of band; Bob redeems it; Alice confirms it was Bob she meant. An
invite is single-use and short-lived, so a leaked one is worth little and
not for long.

**Per-peer tokens, never a shared secret.** Revoking Bob cannot affect
Carol, and a token that leaks is scoped to one relationship. Revocation is
one-sided and immediate: Alice cuts Bob off without Bob's cooperation.

**Seeing is not fetching.** A peer's scope decides what appears in the
projection; its access decides what may be done with it. They are separate
because "look at my shelf" and "download from my shelf" are different
grants, and a UI that bundles them makes the smaller one impossible to
give.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- what a peer may see and do ---------------------------------------------

#: How much of the shelf a peer is shown. Default is nothing: a fresh peer
#: is useful for netplay alone, and sharing should be a decision rather
#: than a consequence of peering.
SCOPES = ("none", "platforms", "verified", "all")

#: What a peer may do with what it sees.
#:
#:   catalogue -- see titles only. The peer's ROMarr can add one to its own
#:                Wanted list and find it through its own indexers. This
#:                turns a friend's shelf into a curated import list rather
#:                than a download service, and it is the safest useful mode.
#:   stream    -- open a time-limited play session against the owner's
#:                player. No file changes hands.
#:   fetch     -- download the file. Off by default; enabling it makes this
#:                server a download source for that person, which the
#:                operator should choose rather than discover.
ACCESS = ("catalogue", "stream", "fetch")

INVITE_TTL = 24 * 3600


@dataclass
class Peer:
    """One relationship. Every field is per-peer on purpose."""

    peer_id: str
    name: str
    url: str = ""
    token: str = ""
    #: Outbound: what THEY may see of MINE.
    scope: str = "none"
    platforms: tuple[str, ...] = ()
    access: str = "catalogue"
    #: Whether the peer's own users inherit this, not just its operator.
    #: "I trust Bob" and "I trust everyone with an account on Bob's server"
    #: are different statements; conflating them is how sharing becomes
    #: distribution.
    delegate_users: bool = False
    confirmed: bool = False
    created: str = field(default_factory=_now)

    def may_see(self, game) -> bool:
        """Whether one game appears in this peer's projection."""
        if not self.confirmed or self.scope == "none":
            return False
        if self.scope == "all":
            return True
        if self.scope == "verified":
            return bool(getattr(game, "verified", False))
        if self.scope == "platforms":
            wanted = {p.lower() for p in self.platforms}
            return str(getattr(game, "platform", "")).lower() in wanted
        return False


def new_peer_id() -> str:
    return secrets.token_hex(8)


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Invite:
    """A one-time offer to peer. Single-use, and it expires."""

    peer_id: str
    secret: str
    url: str
    name: str = ""
    created_at: float = field(default_factory=time.monotonic)

    def expired(self, ttl: int = INVITE_TTL) -> bool:
        return (time.monotonic() - self.created_at) > ttl

    def blob(self) -> dict:
        """What the operator sends out of band."""
        return {"peer_id": self.peer_id, "secret": self.secret,
                "url": self.url, "name": self.name}


class Federation:
    """Every peer this instance knows, and the invites in flight."""

    def __init__(self, name: str = "ROMarr", url: str = ""):
        self.name = name
        self.url = url
        self.peers: dict[str, Peer] = {}
        self._invites: dict[str, Invite] = {}

    # -- making a relationship ----------------------------------------------

    def invite(self, name: str = "") -> Invite:
        """Mint an invitation for somebody to redeem."""
        invite = Invite(peer_id=new_peer_id(), secret=new_token(),
                        url=self.url, name=name)
        self._invites[invite.peer_id] = invite
        return invite

    def redeem(self, blob: dict, *, my_name: str = "") -> Peer:
        """Accept somebody else's invitation, from their side.

        The peer is created already confirmed HERE -- I redeemed it, so I
        plainly meant to. It stays unconfirmed on their side until they
        say the invite reached the person they sent it to.
        """
        peer = Peer(peer_id=str(blob.get("peer_id") or ""),
                    name=str(blob.get("name") or "peer"),
                    url=str(blob.get("url") or ""),
                    token=str(blob.get("secret") or ""),
                    confirmed=True)
        if not peer.peer_id or not peer.token:
            raise ValueError("that invitation is missing its id or secret")
        self.peers[peer.peer_id] = peer
        return peer

    def accept(self, peer_id: str, secret: str, *, name: str = "",
               url: str = "") -> Peer:
        """Somebody redeemed my invitation. Verify and hold it, unconfirmed.

        The secret proves they hold the invitation. It does NOT prove they
        are who I meant to send it to -- so the peer is created unconfirmed
        and the operator confirms it. That second step is what makes a
        leaked invite recoverable.
        """
        invite = self._invites.get(peer_id)
        if invite is None:
            raise ValueError("no invitation with that id")
        if invite.expired():
            del self._invites[peer_id]
            raise ValueError("that invitation has expired; make a new one")
        # Constant-time: this is a secret comparison on an open path.
        if not hmac.compare_digest(invite.secret, str(secret or "")):
            raise ValueError("that invitation secret is wrong")
        del self._invites[peer_id]          # single use, always
        peer = Peer(peer_id=peer_id, name=name or "peer", url=url,
                    token=invite.secret, confirmed=False)
        self.peers[peer_id] = peer
        return peer

    def confirm(self, peer_id: str) -> Peer:
        peer = self.peers.get(peer_id)
        if peer is None:
            raise ValueError("no such peer")
        peer.confirmed = True
        return peer

    def revoke(self, peer_id: str) -> bool:
        """One-sided and immediate. No cooperation required."""
        return self.peers.pop(peer_id, None) is not None

    # -- using a relationship -----------------------------------------------

    def authenticate(self, peer_id: str, token: str) -> Peer | None:
        """The peer behind a request, or None. Constant-time compare."""
        peer = self.peers.get(peer_id)
        if peer is None or not peer.confirmed or not peer.token:
            return None
        if not hmac.compare_digest(peer.token, str(token or "")):
            return None
        return peer

    def project(self, peer: Peer, games, limit: int = 5000) -> list[dict]:
        """What this peer is allowed to see, in a shape that leaks nothing.

        No filesystem paths, no library-server URLs, no internal ids, no
        credentials -- a title, a platform, and whether it is verified. The
        origin marker travels so a downstream peer can see the content is
        not that server's to give.
        """
        out = []
        for game in games:
            if not peer.may_see(game):
                continue
            out.append({
                "title": getattr(game, "name", ""),
                "platform": getattr(game, "platform", ""),
                "year": getattr(game, "year", 0) or 0,
                "verified": bool(getattr(game, "verified", False)),
                "origin": self.name,
            })
            if len(out) >= limit:
                break
        return out

    # -- netplay -------------------------------------------------------------
    #
    # EmulatorJS coordinates a session; what it cannot do is agree on the
    # ROM. Two players on different servers running different dumps of the
    # same game desync within seconds, and the failure looks like a network
    # problem -- so people blame their connection and never find it.
    #
    # ROMarr is the only tool in this stack that already knows a file's
    # SHA1 from DAT verification, so it is the only one that can settle the
    # question before the session starts rather than after it fails.

    def netplay_offer(self, game) -> dict:
        """What I send a peer when inviting them to play.

        The hash is the invitation. A title is not: "Super Mario Kart" is
        four different ROMs.
        """
        return {
            "title": getattr(game, "name", ""),
            "platform": getattr(game, "platform", ""),
            "sha1": str(getattr(game, "sha1", "") or "").lower(),
            "verified": bool(getattr(game, "verified", False)),
            "host": self.name,
        }

    @staticmethod
    def netplay_answer(offer: dict, library) -> dict:
        """Whether this side can join, judged on bytes rather than names.

        Four honest outcomes, and only one of them starts a session:

          ready      -- same SHA1, both verified. Play.
          mismatch   -- the title is here and the bytes differ. This is the
                        case that silently ruins netplay, so it is named.
          unverified -- the hash matches but one side never checked it
                        against a DAT; playable, with a caveat said out loud.
          missing    -- not here at all; the normal acquisition pipeline can
                        fetch exactly this dump, because the hash identifies
                        it precisely.
        """
        wanted = str(offer.get("sha1") or "").lower()
        title = str(offer.get("title") or "")
        if not wanted:
            return {"status": "missing",
                    "detail": "the invitation carried no hash, so the dump "
                              "cannot be matched -- ROMarr will not start a "
                              "session it cannot prove"}

        by_title = None
        for game in library:
            if str(getattr(game, "sha1", "") or "").lower() == wanted:
                both_verified = (bool(offer.get("verified"))
                                 and bool(getattr(game, "verified", False)))
                return {
                    "status": "ready" if both_verified else "unverified",
                    "title": getattr(game, "name", title),
                    "detail": "" if both_verified else
                              "the bytes match, but one side has not verified "
                              "this dump against a DAT",
                }
            if not by_title and str(getattr(game, "name", "")).lower() \
                    == title.lower():
                by_title = game

        if by_title is not None:
            return {
                "status": "mismatch",
                "title": getattr(by_title, "name", title),
                "detail": "you both have this game and they are different "
                          "dumps -- netplay would desync within seconds, "
                          "which usually gets blamed on the connection",
            }
        return {"status": "missing", "title": title,
                "detail": "not in this library; the hash identifies exactly "
                          "which dump to acquire"}

    def status(self) -> list[dict]:
        """The Peers page. Tokens never appear here."""
        return [
            {"peer_id": p.peer_id, "name": p.name, "url": p.url,
             "scope": p.scope, "platforms": list(p.platforms),
             "access": p.access, "delegate_users": p.delegate_users,
             "confirmed": p.confirmed, "created": p.created}
            for p in self.peers.values()
        ]
