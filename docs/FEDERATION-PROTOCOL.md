# A peering protocol for self-hosted game libraries

**Status:** draft, with a working reference implementation and live proofs.
**Licence:** MIT, same as ROMarr. Take it, fork it, rename it, implement it
independently — the point is that two servers agree, not that they share
code.

## The problem

RomM, Gaseous, Retrom and Gameyfin all solve "my library, my server" well.
None of them federates. So the thing people actually ask for —

> *let my friend see my SNES shelf, and let us play a game together*

— has no answer today, and each project solving it alone would produce four
incompatible answers.

This is a proposal for one small protocol that any of them could implement,
so a RomM can peer with a Retrom without either project knowing about the
other. It is deliberately tiny: seven endpoints, no discovery service, no
central anything.

## Principles, and why each one is load-bearing

**Invitation, never discovery.** No directory, no "servers near you". A
public index of who holds which ROM library is a liability for every
operator on it. A peer exists because two people deliberately made it exist.

**Mutual confirmation.** The invitation secret proves somebody holds the
invite. It does *not* prove they are who you meant to send it to — so the
peer is held unconfirmed until the inviter confirms. That second step is
what makes a leaked invitation recoverable instead of final.

**Per-peer tokens.** Never a shared secret. Revoking one peer cannot affect
another, and a leaked token is scoped to one relationship.

**One-sided revocation.** You cut a peer off without their cooperation,
immediately. This is the property Plex gets right and most self-hosted
sharing gets wrong.

**Seeing is not fetching.** *What* a peer may see and *what they may do*
with it are separate grants. Bundling them is how "look at my shelf"
quietly becomes "download from my shelf".

**No transitive trust.** Your peers' peers are not your peers. Trust is not
a graph anyone can walk.

**Content is marked as not theirs.** Everything in a projection carries an
`origin`, so a downstream server can see the content is not that server's
to re-share.

## The endpoints

Seven, and three of them are peer-facing (authenticated by peer credential,
not by a user session — the caller is a server, not a browser).

| Endpoint | Who calls it | Purpose |
|---|---|---|
| `POST /peer/invite` | operator | Mint a one-time invitation |
| `POST /peer/redeem` | operator | Redeem an invitation you received |
| `POST /peer/accept` | **a peer** | Complete the handshake with the secret |
| `POST /peer/confirm` | operator | Confirm the peer that redeemed yours |
| `POST /peer/policy` | operator | Set this peer's scope and access |
| `GET  /peer/shelf` | **a peer** | Read the projection you allow |
| `POST /peer/netplay` | **a peer** | Answer a session offer, by hash |
| `DELETE /peer/{id}` | operator | Revoke, immediately |

The consuming side is not part of the wire protocol — it is whatever your UI
calls to *use* a relationship. ROMarr's looks like this, and is listed only
so the shape is clear:

| Endpoint | Purpose |
|---|---|
| `GET  /friends/shelf` | Browse a friend's projection, filtered locally |
| `POST /friends/want` | Add a title you saw there to your OWN wanted list |
| `POST /friends/netplay` | Offer a friend a game, carrying your SHA1 |

Fetch a friend's shelf once and filter it on your side. Proxying every
keystroke to somebody else's server makes your UI responsiveness their
hosting cost.

### The handshake

```
Alice: POST /peer/invite     -> {peer_id, secret, url}
       (sends that blob to Bob out of band — chat, mail, paper)
Bob:   POST /peer/redeem     {invite}        -> stores it, confirmed=true
Bob's SERVER:
       POST alice/peer/accept {peer_id, secret, name, url}
                                             -> stored UNCONFIRMED
Alice: POST /peer/confirm    {peer_id}       -> now usable
```

An invitation is single-use and expires (24h in the reference
implementation). Secrets are compared in constant time.

### Authentication for peer-facing calls

```
X-Peer-Id:    <peer_id>
X-Peer-Token: <token>
```

An unconfirmed peer authenticates as nobody. A confirmed peer whose scope
is still the default sees an empty shelf. So neither route leaks anything
by merely existing.

### Scope — what a peer sees

| Scope | Meaning |
|---|---|
| `none` | **Default.** Peered, sharing nothing. Useful for netplay alone. |
| `platforms` | Only the platforms listed — "my SNES shelf, not my PS2 rips" |
| `verified` | Only dumps verified against a DAT — the "I stand behind these" shelf |
| `all` | Everything |

### Access — what they may do

| Access | Meaning |
|---|---|
| `catalogue` | **Default.** See titles. Their server adds them to its own wanted list and acquires through its *own* sources. |
| `stream` | Open a time-limited play session against the owner's player. No file moves. |
| `fetch` | Download the file. **Off by default** — enabling it makes your server a source for that person. |

"Mutual sharing" is not a mode. It is two one-way grants that both happen to
exist, because that is what it is, and a single switch cannot be
half-revoked.

`delegate_users` is its own flag: whether the peer's *users* inherit the
grant, or only its operator. "I trust Bob" and "I trust everyone with an
account on Bob's server" are different statements.

### The projection

A peer never receives library rows. It receives:

```json
{"title": "Chrono Trigger (USA)", "platform": "snes",
 "year": 1995, "verified": true, "origin": "Alice's library"}
```

No filesystem paths, no server URLs, no internal ids, no credentials.

## Netplay: agree on the bytes, not the title

EmulatorJS can already coordinate a session. What it cannot do is establish
that both sides are running **the same ROM** — and two different dumps of
one game desync within seconds. The failure presents as lag, so players
blame their connection and never find it.

The fix is to match on the dump's SHA1 rather than its name:

```
POST /peer/netplay  {"offer": {"title": "...", "platform": "snes",
                               "sha1": "<40 hex>", "verified": true}}
```

Four answers, one of which starts a session:

| Status | Meaning |
|---|---|
| `ready` | Same hash, both sides verified. Play. |
| `unverified` | Hash matches, but one side never checked it against a DAT. |
| `mismatch` | **Same title, different dumps.** Named explicitly, because this is the failure that silently ruins netplay. |
| `missing` | Not here — and the hash says exactly which dump to acquire. |

Any project holding a hash for its files can implement this. A library that
verifies against No-Intro/Redump already has it.

### The part that is easy to get wrong

**An implementation needs a hash it can look up by game.** This is worth
stating because the reference implementation got it wrong first: offers were
built from the library server's game object, which carries a title, a
platform and a cover — and no hash. Every offer went out with `sha1: ""`,
and the far side answered `missing` every time. Nothing errored. The
handshake completed, both servers agreed, and the agreement was empty.

If your library model has no hash field, netplay cannot be built on it, and
the failure is silent rather than loud. ROMarr now keeps a separate index of
`sha1 -> (title, platform, verified)`, populated by DAT verification, and
answers from that.

Two lookups are needed, not one: `by_sha1` to answer an offer, and
`for_game` to *build* one. The second needs title normalisation, because the
name on disk and the name in a metadata server disagree about punctuation and
region tags far more often than they agree.

## Two requirements that are not endpoints

**Relationships must be persisted, tokens included.** Obvious once stated,
and the reference implementation shipped without it: peers lived in a
dictionary, so a restart dropped every friend, token and sharing policy. The
Friends page came back empty and a returning peer authenticated as nobody,
with nothing in the log to say why. Invitations are the exception — they
should *not* survive a restart, since they expire in 24 hours anyway and
persisting them only widens the window a leaked one is useful for.

**A peer token is a credential.** It has to be excluded from whatever
endpoint feeds your settings UI, by name. Do not rely on a naming convention
to do it: ROMarr stores several `_`-prefixed keys it deliberately *does*
show, so a "hide anything underscored" rule would have hidden the wrong half
and shown the tokens.

## Reference implementation

- Protocol + trust model: `romarr/federation.py` (MIT, ~250 lines, no
  network code in the hot path so it is testable offline)
- 16 offline tests: `tests/test_federation.py`
- **15 live proofs between two running servers**:
  `scripts/prove_friends_live.py` — invitation, cross-server handshake with
  no user session, unconfirmed refusal, default-shares-nothing, 5,000-row
  projection with no leaked fields, wrong-token refusal, netplay by hash,
  and immediate one-sided revocation.

Everything above is implemented and proven, not sketched.

## What this deliberately does not specify

Transport security (use TLS), user identity mapping between servers (each
side keeps its own), and the netplay session transport itself (EmulatorJS
already has one). This protocol only settles **who may see what** and
**whether two servers hold the same bytes** — the two questions nobody else
is answering.
