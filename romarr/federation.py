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

**An invitation travels as a link and a code, and the link holds no
secret.** Pasting a JSON blob is a thing people get wrong, so the friendly
form is a URL -- but a URL is the worst possible envelope for a bearer
credential. URLs land in browser history, in the address bar of a shared
screen, in the `Referer` header of the next click, in the unfurler that
renders a preview in the chat window, and in the access log of every proxy
between the two. So the link carries the invitation's *id* and nothing that
authorises anything, and it carries even that in a URL fragment, which a
browser never puts on the wire at all. The half that authorises is a short
claim code the friend types. The two halves can travel by two channels, and
neither one alone is worth stealing.

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

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

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

#: How long an invitation lives. The long secret is 256 bits, so time is not
#: what protects it -- a day is simply long enough for somebody to read their
#: messages and short enough that forgotten invites stop existing.
INVITE_TTL = 24 * 3600

#: How long the short claim code lives, which is a different question. A code
#: a person can read aloud is worth about forty bits, and forty bits sitting
#: in a chat log all day is a credential sitting in a chat log all day. So the
#: half of an invitation that a human can retype stops working long before the
#: invitation does, and minting another is one click. The link stays valid the
#: full day; only the secret expires early.
CLAIM_CODE_TTL = 15 * 60

#: Wrong claim codes before the whole invitation is destroyed. The code is
#: short enough to be worth guessing, so the cost of guessing wrong has to be
#: that the thing being guessed at stops existing. Five is generous for
#: somebody squinting at a phone and hopeless for anybody enumerating.
#:
#: Burning it is also the alarm: the friend it was meant for finds it gone and
#: says so, which is how the operator learns somebody was guessing at all.
MAX_CLAIM_ATTEMPTS = 5

#: Crockford's base32 -- no I, L, O or U. The first three because they come
#: back as 1, 1 and 0 when a code is read down a phone line, and U because
#: leaving it out is what keeps four-letter words out of codes people have to
#: say out loud to their friends.
CLAIM_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Eight characters, ~40 bits. Deliberately not Plex's four: Plex can afford
#: four because its code is typed into a device that is in the room, seconds
#: after it appears. This one is sent to somebody who may answer in ten
#: minutes, so it buys the extra length rather than the extra trust.
CLAIM_LENGTH = 8

#: What a mistyped character most likely was. Crockford's rule: the alphabet
#: drops the ambiguous letters, and the reader is forgiven for typing them
#: anyway. This costs no entropy -- these characters are never minted -- and
#: it is the difference between a code that works over the phone and one that
#: works over the phone on the third try.
_CLAIM_CONFUSIONS = {"O": "0", "I": "1", "L": "1"}


@dataclass
class Peer:
    """One relationship. Every field is per-peer on purpose."""

    peer_id: str
    name: str
    url: str = ""
    token: str = ""
    #: How ROMarr talks to them.
    #:
    #:   romarr -- they run ROMarr and speak this protocol. Mutual, and they
    #:             decide what I see: I get a projection, never their rows.
    #:   romm   -- they run a plain RomM that has never heard of any of this.
    #:             They hand me an account on it, and ROMarr reads their
    #:             library through RomM's own API. This works today, with
    #:             nothing installed or merged on their side.
    #:
    #: The second is less good and it is worth being plain about why: an
    #: account on somebody's RomM grants whatever RomM grants it, and no
    #: scope set here can narrow that. ROMarr shows what it reads, but it is
    #: not the thing holding the door.
    kind: str = "romarr"
    username: str = ""
    password: str = ""
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

    def headers(self) -> dict[str, str]:
        """What I send when I call THEM.

        The invitation secret ends up as `token` on both sides -- mine for
        verifying them, theirs for verifying me -- so one pair authenticates
        the relationship in both directions without a second exchange.
        """
        return {"X-Peer-Id": self.peer_id, "X-Peer-Token": self.token}

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


def new_claim_code() -> str:
    """A short secret somebody can say out loud without spelling it."""
    return "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(CLAIM_LENGTH))


def normalise_claim_code(text: str) -> str:
    """What the friend typed, as the code it was meant to be.

    Case, spaces and the display hyphen are noise; so are the four letters
    the alphabet does not contain. Folding them here rather than validating
    them means "k7rm-9tfq" and "K7RM 9TFQ" are the same code, and nobody has
    to be told which one the machine wanted.
    """
    out = []
    for char in str(text or "").upper():
        char = _CLAIM_CONFUSIONS.get(char, char)
        if char in CLAIM_ALPHABET:
            out.append(char)
    return "".join(out)


def parse_invite_link(link: str) -> dict:
    """Pull an invitation out of the URL a friend sent.

    Two facts are needed and both are in the link. The inviter's address is
    the link's own origin, which means the most visible part of the URL --
    the part a person reads before clicking -- is whose server this is about.
    The invitation id rides in the FRAGMENT, which a browser never sends to
    any server: it stays out of the inviter's access log, out of the `Referer`
    header of whatever the friend clicks next, and out of the request a chat
    client makes to render a link preview.

    `u` appears only on a link that has been rehomed onto the recipient's own
    server, where the origin is no longer the inviter's and something has to
    carry it. That is also the flag that tells the landing page which of the
    two servers it is running on.

    There is no secret here. Losing this link loses nothing.
    """
    parts = urlsplit(str(link or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("that is not an invitation link -- it should start "
                         "with http:// or https://")
    fragment = parse_qs(parts.fragment)
    peer_id = (fragment.get("i") or [""])[0].strip()
    if not peer_id:
        raise ValueError("that link carries no invitation id -- copy the "
                         "whole thing, including the part after the #")
    origin = (fragment.get("u") or [""])[0].strip()
    if not origin:
        origin = f"{parts.scheme}://{parts.netloc}"
    return {"url": origin.rstrip("/"), "peer_id": peer_id,
            "name": (fragment.get("n") or [""])[0].strip(),
            "rehomed": bool((fragment.get("u") or [""])[0].strip())}


@dataclass
class Invite:
    """A one-time offer to peer. Single-use, and it expires.

    It has two credentials rather than one, because it travels two ways and
    the two ways are not equally safe:

      secret -- 256 bits. Never appears in a URL. It is what the blob form
                carries, and it is what becomes the long-lived peer token, so
                it has to be big enough that nobody guesses it in the years it
                will be in use.
      code   -- eight characters, so a person can retype it off a screen or
                repeat it down a phone. Short-lived and attempt-limited,
                because eight characters is a number you can count to.

    Presenting either one completes the handshake. Neither one is a grant:
    the peer arrives unconfirmed regardless.
    """

    peer_id: str
    secret: str
    url: str
    name: str = ""
    code: str = field(default_factory=new_claim_code)
    #: Wrong guesses so far. Kept on the invitation rather than per caller,
    #: because rate-limiting by address is defeated by having more addresses
    #: and the thing worth protecting is this one invitation.
    attempts: int = 0
    created_at: float = field(default_factory=time.monotonic)

    def expired(self, ttl: int = INVITE_TTL) -> bool:
        return (time.monotonic() - self.created_at) > ttl

    def code_expired(self, ttl: int = CLAIM_CODE_TTL) -> bool:
        return (time.monotonic() - self.created_at) > ttl

    def code_expires_in(self, ttl: int = CLAIM_CODE_TTL) -> int:
        """Seconds of life left in the code, for a UI that can count down."""
        return max(0, int(ttl - (time.monotonic() - self.created_at)))

    @property
    def code_display(self) -> str:
        """The code as a human should see it: halved, so the eye can hold it."""
        half = CLAIM_LENGTH // 2
        return f"{self.code[:half]}-{self.code[half:]}" if self.code else ""

    def link(self) -> str:
        """The URL the operator sends. It authorises nothing.

        Everything identifying goes after the `#`, which is the whole point:
        a fragment is not transmitted, so this URL reaches the inviter's
        access log, a preview unfurler or a `Referer` header as the bare
        path `/link` and nothing else.

        Empty when this server has no address of its own -- a link to nowhere
        reads as a working invitation and fails much later, as silence.
        """
        if not self.url:
            return ""
        return (f"{self.url.rstrip('/')}/link#i={self.peer_id}"
                f"&n={quote(self.name or '', safe='')}")

    def blob(self) -> dict:
        """What the operator sends out of band, in one piece.

        This one does carry the long secret, so it is the form to send over a
        channel you would send a password over -- and the form to stop using
        once the link and code are in front of you.
        """
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
        """Mint an invitation for somebody to redeem.

        One invitation, two ways to send it: `link()` plus `code`, or the
        one-piece `blob()`. Not two invitations -- spending either half
        spends the whole thing.
        """
        invite = Invite(peer_id=new_peer_id(), secret=new_token(),
                        url=self.url, name=name)
        self._invites[invite.peer_id] = invite
        return invite

    def redeem(self, blob: dict, *, my_name: str = "") -> Peer:
        """Accept somebody else's one-piece invitation, from their side.

        The peer is created already confirmed HERE -- I redeemed it, so I
        plainly meant to. It stays unconfirmed on their side until they
        say the invite reached the person they sent it to.

        Offline, and it can afford to be: the blob already contains the
        durable token, so the callback to their server can happen later.
        The link-and-code path cannot afford that -- a claim code is worth
        nothing once spent -- which is why it lives in the service layer,
        where there is a network.
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

    def add_romm(self, url: str, username: str = "", password: str = "",
                 *, name: str = "", token: str = "") -> Peer:
        """Befriend somebody running a plain RomM.

        There is no handshake, because there is nobody on the far side to
        shake hands with: RomM does not know this protocol exists. What makes
        the relationship is an account my friend created for me on their
        server, so it is confirmed on arrival -- I typed the address and the
        credential myself, which is the same consent the two-step handshake
        exists to establish.
        """
        url = str(url or "").strip().rstrip("/")
        if not url:
            raise ValueError("a RomM friend needs an address")
        if not (username and password) and not token:
            raise ValueError("a RomM friend needs either a username and "
                             "password or an API token on their server")
        peer = Peer(peer_id=new_peer_id(), name=name or url, url=url,
                    kind="romm", username=username, password=password,
                    token=token, confirmed=True)
        self.peers[peer.peer_id] = peer
        return peer

    def accept(self, peer_id: str, secret: str, *, name: str = "",
               url: str = "") -> Peer:
        """Somebody redeemed my invitation. Verify and hold it, unconfirmed.

        `secret` is either credential: the long one from the blob, or the
        short claim code the friend read off a message and typed. Both are
        the same single-use invitation, so redeeming with one spends the
        other.

        Whichever they presented, they hold the invitation. That does NOT
        prove they are who I meant to send it to -- so the peer is created
        unconfirmed and the operator confirms it. That second step is what
        makes a leaked invite recoverable, and it is what lets the claim code
        be short enough to say out loud: the worst a guessed code buys is a
        row on the Friends page marked "awaiting your confirmation".
        """
        invite = self._invites.get(peer_id)
        if invite is None:
            raise ValueError("no invitation with that id")
        if invite.expired():
            del self._invites[peer_id]
            raise ValueError("that invitation has expired; make a new one")

        # Both comparisons run, and both are constant-time: this is a secret
        # comparison on an open path, and which of the two credentials was
        # wrong is not the caller's business either.
        offered = str(secret or "")
        by_secret = hmac.compare_digest(invite.secret, offered)
        by_code = bool(invite.code) and hmac.compare_digest(
            invite.code, normalise_claim_code(offered))

        if by_code and invite.code_expired():
            # Right code, too late. Said plainly and NOT counted as a wrong
            # guess: the friend did nothing wrong, and telling them "that is
            # wrong" would send them hunting for a typo that is not there.
            raise ValueError("that claim code has expired -- ask your friend "
                             "for a fresh one, it only lives 15 minutes")
        if not (by_secret or by_code):
            invite.attempts += 1
            if invite.attempts >= MAX_CLAIM_ATTEMPTS:
                del self._invites[peer_id]
                raise ValueError(
                    "too many wrong codes -- this invitation has been "
                    "destroyed. Ask your friend to mint a new one, and tell "
                    "them somebody else was guessing at the old one")
            raise ValueError("that invitation secret is wrong")

        del self._invites[peer_id]          # single use, always
        peer = Peer(peer_id=peer_id, name=name or "peer", url=url,
                    token=invite.secret, confirmed=False)
        self.peers[peer_id] = peer
        return peer

    # -- surviving a restart -------------------------------------------------
    #
    # Peers lived only in memory until this existed, so every friend, token
    # and sharing policy was lost on restart. Nothing failed loudly: the
    # Friends page simply came back empty, and a peer that called in was an
    # unknown peer. Invitations in flight are deliberately NOT persisted --
    # they expire in 24 hours and a restart is a fine reason to mint a new
    # one, whereas persisting them would widen the window a leaked invite is
    # useful for. That goes double now that an invitation carries a
    # wrong-guess budget: a restart that brought one back would hand it a
    # fresh five attempts, which is the one way a five-attempt limit stops
    # being a limit.

    def dump(self) -> list[dict]:
        """Every relationship, in a form that round-trips through JSON."""
        return [{**asdict(p), "platforms": list(p.platforms)}
                for p in self.peers.values()]

    def restore(self, rows) -> int:
        """Rebuild relationships saved by `dump`. Unknown fields are ignored."""
        known = {f.name for f in fields(Peer)}
        restored = 0
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("peer_id"):
                continue
            data = {k: v for k, v in row.items() if k in known}
            data["platforms"] = tuple(data.get("platforms") or ())
            try:
                peer = Peer(**data)
            except TypeError:
                log.warning("skipping an unreadable saved peer")
                continue
            self.peers[peer.peer_id] = peer
            restored += 1
        return restored

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
        if peer.kind != "romarr":
            # A RomM friend's token is a credential *I* hold for *their*
            # server, not one they present to mine. Letting it authenticate
            # inbound would turn a key my friend gave me into a key to my
            # own library -- and anyone who learned it, including their
            # admins, would hold it.
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
    def netplay_room(peer_id: str, sha1: str) -> str:
        """The room both sides join, derived rather than negotiated.

        Both servers already hold the same two facts -- the peer id of the
        relationship and the SHA1 they just agreed on -- so each can compute
        the room independently and arrive at the same string. That removes
        the one piece of central infrastructure a lobby would otherwise
        need, and it means the room name cannot be guessed by somebody who
        does not already know both halves.
        """
        material = f"{peer_id}:{str(sha1 or '').lower()}".encode()
        return hashlib.sha256(material).hexdigest()[:16]

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
             "kind": p.kind,
             "scope": p.scope, "platforms": list(p.platforms),
             "access": p.access, "delegate_users": p.delegate_users,
             "confirmed": p.confirmed, "created": p.created}
            for p in self.peers.values()
        ]
