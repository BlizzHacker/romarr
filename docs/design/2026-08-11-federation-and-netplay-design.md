# Federation: friends, shared libraries, and cross-server netplay

**Status:** design, not yet built. Written before code on purpose — this one
touches other people's servers and other people's games, and the failure
modes are social as much as technical.

## What was asked for

> Make a ROM Hub plugin that lets RomM / Retrom / Gaseous server owners be
> friends. Like Plex friends. Share server access / library access —
> mutual, or one-way — with an optional "let my users access this library
> too, and vice versa". Then build it out to work with EmulatorJS netplay
> so we can have cross-server game battles.

Four separable things, and they should ship in this order because each is
useful alone and the later ones need the earlier ones:

1. **Peering** — two ROMarr instances know each other and trust each other.
2. **Catalogue sharing** — a peer can *see* what you have.
3. **Access** — a peer's users can *play or fetch* what you have.
4. **Netplay** — two people on different servers play the same session.

## Why ROMarr and not the library servers

RomM, Gaseous and Retrom each have their own accounts, permissions and
APIs, and none of them federates. Building this into any one of them
federates that one only. ROMarr already speaks to all four backends and
already holds the credentials — so a ROMarr-to-ROMarr link federates a
RomM to a Retrom without either project changing a line. That is the whole
argument for putting it here.

It ships as a **ROM Hub plugin** rather than core ROMarr for the reason
every source does: the Hub already sandboxes a plugin's network to a
declared allowlist, and a federation plugin's allowlist is exactly "the
peers the operator added". A bug in it cannot reach anything else.

## 1. Peering

**The trust model is invitation + mutual confirmation, never discovery.**
There is no directory, no public index, no "find servers near you". A peer
exists because both operators deliberately made it exist.

```
Alice runs /api/v1/peer/invite
  -> ROMarr mints an invite: peer id, a one-time secret, her public URL
  -> she sends Bob that blob out of band (Discord, mail, paper)
Bob pastes it into his Peers page
  -> his ROMarr calls Alice's /api/v1/peer/accept with the one-time secret
  -> both sides store a LONG-LIVED PER-PEER TOKEN, never a shared password
  -> Alice's UI shows "Bob wants to peer" and she confirms
```

Both halves matter. The one-time secret proves Bob holds Alice's
invitation; Alice's confirmation proves she meant *Bob* and not whoever
the invite leaked to. An invite is single-use and expires in 24 hours.

Per-peer tokens, so revoking Bob does not touch Carol, and a leaked token
is scoped to one relationship. Revocation is immediate and one-sided —
Alice can cut Bob off without Bob's cooperation, which is the property
Plex gets right and most self-hosted sharing gets wrong.

## 2. Catalogue sharing

A peer sees a **projection**, never the library rows themselves:

```json
{"title": "Chrono Trigger (USA)", "platform": "snes",
 "verified": true, "year": 1995, "playable": "emulatorjs"}
```

No filesystem paths, no library-server URLs, no internal ids, no
credentials. The projection is generated from ROMarr's own cache, so
sharing costs the library server nothing.

**What a peer may see is a policy per peer**, with the sensible default
being *nothing* until the owner picks:

| Scope | Meaning |
|---|---|
| `none` | Peered but sharing nothing (the default; useful for netplay-only) |
| `platforms` | Only the platforms you list — "my SNES shelf, not my PS2 rips" |
| `verified` | Only DAT-verified dumps — the "I stand behind these" shelf |
| `all` | Everything ROMarr has cached |

Direction is explicit and separate: `share_out`, `share_in`, or both.
"Mutual" is not a mode, it is two one-way grants that happen to both
exist — because that is what it actually is, and modelling it as one
switch makes revoking half of it impossible.

## 3. Access

Seeing is not fetching. A peer with `share_out: all` still cannot pull a
byte until the owner grants an **access mode**:

- **`catalogue`** — see only. The peer's ROMarr can add the title to its
  own Wanted list and find it through its own indexers. This is the safest
  mode and probably the most useful: it turns a friend's shelf into a
  curated import list rather than a download service.
- **`stream`** — the peer may open a play session against the owner's
  player (RomM's EmulatorJS, or a stream server), time-limited, never a
  file transfer.
- **`fetch`** — the peer may download the file itself.

**`fetch` is off by default and the UI says what it means**, plainly:
enabling it makes your server a download source for that person. The
operator should choose that consciously rather than discover it.

The "let my users access this too, and vice versa" option maps to a
`delegate_users` flag: when set, the peer's *users* — not just its
operator — inherit that peer's access. Off by default, because "I trust
Bob" and "I trust everyone with an account on Bob's server" are different
statements and conflating them is how sharing becomes distribution.

Every peer request is rate-limited and logged with who asked for what.
The Peers page shows that log — a share you cannot audit is a share you
cannot reason about.

## 4. Netplay

EmulatorJS supports netplay through a coordination server: peers agree on
a room, exchange WebRTC offers, and run the same core in lockstep. What is
missing for cross-server play is **agreement on the ROM**, and that is
precisely what ROMarr already knows how to establish.

```
Alice invites Bob to play "Super Mario Kart (USA)"
  -> ROMarr sends the DAT hash, not a filename
  -> Bob's ROMarr looks up that hash in HIS library
     - has it, verified  -> ready
     - has an unverified copy -> warn: netplay desyncs on a different dump
     - does not have it -> offer to acquire it (the normal pipeline)
  -> both sides confirm the same SHA1, then join the room
```

**Matching on the hash rather than the title is the whole trick.** Netplay
between two different dumps of the same game desyncs within seconds, and
the failure looks like a network problem, so people blame their
connection. ROMarr is the only tool in this stack that already verifies
byte-identity — so it is the only one that can promise two players are
running the same bytes before the session starts.

Room coordination rides the existing peer link; no third-party service
and no central server. If a peer relationship exists, netplay works
between exactly those two servers.

## What this deliberately does not do

- **No public server directory.** Federation is between people who already
  know each other. A discoverable index of ROM libraries is a liability
  for every operator on it.
- **No transitive trust.** Bob's peers are not Alice's peers. Trust is not
  a graph you can walk.
- **No anonymous access.** Every request is attributable to a peer.
- **No re-sharing.** A peer may not forward what it fetched from you; the
  projection carries an origin marker so a downstream peer can see the
  content is not that server's to give.

## Build order

1. `romarr/federation.py` — invite/accept/revoke, per-peer tokens, the
   projection, the policy model. Pure logic, fully testable offline.
2. Peers page — invite, paste-invite, per-peer scope and access controls,
   the audit log.
3. `catalogue` access — a peer's shelf as an import-list source, which
   reuses the entire existing Import Lists engine.
4. `stream` and `fetch` access.
5. Netplay handshake, once 1–4 are proven between two real servers.

Each step is shippable and useful without the next.
