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
other. It is deliberately tiny: nine endpoints and one page, no discovery
service, no central anything.

## Principles, and why each one is load-bearing

**Invitation, never discovery.** No directory, no "servers near you". A
public index of who holds which ROM library is a liability for every
operator on it. A peer exists because two people deliberately made it exist.

**Mutual confirmation.** The invitation secret proves somebody holds the
invite. It does *not* prove they are who you meant to send it to — so the
peer is held unconfirmed until the inviter confirms. That second step is
what makes a leaked invitation recoverable instead of final. It is also what
lets the friendly, short, human-typed form of an invitation exist at all —
see *Sending an invitation*.

**A secret never travels in a URL.** An invitation is two halves: a link that
authorises nothing, and a short code that does. URLs go where credentials must
not — browser history, address bars on shared screens, `Referer` headers, the
fetch a chat client makes to render a preview, every proxy log in between. A
protocol that puts its bearer token in a link has decided all of those are
trusted. This one has not.

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

Nine, and three of them are peer-facing (authenticated by peer credential,
not by a user session — the caller is a server, not a browser).

| Endpoint | Who calls it | Purpose |
|---|---|---|
| `POST /peer/invite` | operator | Mint a one-time invitation |
| `POST /peer/claim` | operator | Redeem an invitation **link** with its claim code |
| `POST /peer/redeem` | operator | Redeem an invitation you received in one piece |
| `POST /peer/accept` | **a peer** | Complete the handshake with the secret or the code |
| `POST /peer/confirm` | operator | Confirm the peer that redeemed yours |
| `POST /peer/policy` | operator | Set this peer's scope and access |
| `GET  /peer/shelf` | **a peer** | Read the projection you allow |
| `POST /peer/netplay` | **a peer** | Answer a session offer, by hash |
| `DELETE /peer/{id}` | operator | Revoke, immediately |

Plus one page, which is not an API: `GET /link` is where an invitation link
lands. It is served without a credential, because the person opening it is
somebody else's operator with no account on your server — that is the whole
situation. It is safe to leave open because it is a **constant**: it takes no
parameters, reads no invitation, touches no state and returns identical bytes
to every caller. There is nothing behind it to ask.

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

### Sending an invitation: a link and a code

Pasting a JSON blob between two people is a thing people get wrong, so the
friendly form is a link. But a link is the worst possible envelope for a
bearer credential, so the link does not contain one.

`POST /peer/invite` returns three things:

```json
{"link": "https://alice.example/link#i=9f2c1a04b7e35d18&n=Alice",
 "code": "K7RM-9TFQ",
 "code_expires_in": 900,
 "invite": {"peer_id": "9f2c1a04b7e35d18", "secret": "<43 chars>", "url": "..."}}
```

**The link carries no secret.** Everything identifying it lives in the URL
**fragment** — the part after the `#`, which a browser never puts on the wire.
That single choice keeps the invitation id out of the inviter's own access
log, out of the `Referer` header of whatever the recipient clicks next, and
out of the request a chat client makes to render a link preview: an unfurler
fetches the bare path `/link` and learns nothing. Losing this link loses
nothing.

**The code is the secret, and it is typed.** Eight characters from Crockford's
base32 — no `I`, `L`, `O` or `U`, so it survives being read down a phone and
so codes people have to say out loud are not words they would rather not say.
Input is normalised, not validated: `k7rm-9tfq`, `K7RM 9TFQ` and `K7RM9TFQ`
are the same code, and a typed `O` is read as `0`. That costs no entropy,
because the ambiguous characters are never minted.

Two halves means the operator *can* send them over two channels — the link in
chat, the code by voice or SMS or another app — and neither half alone is
worth stealing. Sending both in one message is no worse than the one-piece
blob was, so the design is never a regression and is usually an improvement.

**Eight characters is about forty bits, which is small.** Three things carry
the weight instead of length, and all three are load-bearing:

1. **The code expires in 15 minutes** where the invitation lives 24 hours.
   A code that sits in a chat log all day is a credential sitting in a chat
   log all day. Minting another is one request. The two lifetimes differ
   because the two secrets are defended differently: the 256-bit one is
   defended by being unguessable, the short one by ceasing to exist.
2. **Five wrong codes destroy the invitation.** The counter lives on the
   invitation, not on the caller's address — rate-limiting by address is
   defeated by having more addresses, and the thing worth protecting is this
   one invitation. A *correct but expired* code is refused with its own
   message and does **not** spend an attempt: the friend did nothing wrong,
   and "that is wrong" would send them hunting for a typo that is not there.
3. **Redeeming still grants nothing.** A correctly guessed code buys a row on
   the inviter's Friends page marked *awaiting your confirmation* and nothing
   else. This is the same mutual-confirmation step that makes a leaked long
   secret recoverable, and it is the reason a short code is safe to offer at
   all. Remove that step and the code has to get much longer, at which point
   it is not a thing anyone reads aloud.

Burning an invitation is also the alarm. The friend it was meant for finds it
gone and says so, which is how the operator learns somebody was guessing.

### The handshake

```
Alice: POST /peer/invite     -> {link, code, invite:{peer_id, secret, url}}
       (sends the link in chat; sends the code separately)
Bob:   POST /peer/claim      {link, code}
Bob's SERVER:
       POST alice/peer/accept {peer_id, secret: <the code>, name, url}
                                             -> stored UNCONFIRMED
                                             <- {token, name, url}
       (keeps `token`, stores Alice confirmed=true on his side)
Alice: POST /peer/confirm    {peer_id}       -> now usable
```

`/peer/accept` returns the long token to whoever completes the handshake.
That caller has just proved it holds this exact single-use invitation, and
the token it receives opens nothing until Alice confirms — so this is not a
widening. It is how a short code becomes a durable credential: **short code
in, long token out, once, server to server.** Without it, eight characters
would have to *be* the long-lived peer token, and forty bits is not a
credential you leave in place for years.

The older one-piece form still works, and is the path to use against a server
that predates links:

```
Alice: POST /peer/invite     -> {invite: {peer_id, secret, url}}
       (sends that blob out of band — the way you would send a password)
Bob:   POST /peer/redeem     {invite}        -> stores it, confirmed=true
Bob's SERVER:
       POST alice/peer/accept {peer_id, secret, name, url}
```

The difference between `/peer/claim` and `/peer/redeem` is that claim goes
over the network and redeem does not. It has to: a blob already contains the
durable token, so storing it and calling back later is survivable. A claim
code is worth nothing after it is spent, so the exchange happens now or the
friend is left holding eight dead characters.

An invitation is single-use and expires (24h in the reference
implementation; 15 minutes for the claim code). Both credentials are compared
in constant time, and both comparisons always run — which of the two was
wrong is not the caller's business either.

### Where the link lands, with no central anything

Alice sends Bob a link. Bob clicks it and his browser goes to **Alice's**
server, because Alice's address is the only address Alice knows. That is the
whole difficulty with a Plex-style link in a system that refuses to have a
directory: `plex.tv/link` works because `plex.tv` exists.

So `/link` is a page whose job is to hand the visitor back to their own
server. It reads the fragment in the browser, and:

- **No `u` in the fragment** → this is the inviter's server. Ask for the
  visitor's own ROMarr address (remembered locally) and rewrite the same path
  and fragment onto that host, adding `u=<this origin>` so their server knows
  who invited them.
- **`u` present** → the link has been rehomed and the visitor is on their own
  server. Hand the invitation to the app and let it ask for the claim code.

That one flag is the whole state machine, and it means the page never has to
ask the server which of the two it is. Pasting the link straight into
*Friends → I have an invitation* skips the hop entirely, and is the same code
path.

A link is a thing somebody else wrote, so the redeeming UI shows **the address
it is about to send the code to** before it sends it. Nothing is lost if the
address is a stranger's — the code is one that stranger already knew — but the
operator should be told whose server they are peering with, and that fact is
the most visible part of the URL by construction.

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
persisting them only widens the window a leaked one is useful for. That goes
double for the wrong-guess counter, which lives on the invitation: a restart
that resurrected an invitation would hand it a fresh budget of guesses.

**A peer token is a credential.** It has to be excluded from whatever
endpoint feeds your settings UI, by name. Do not rely on a naming convention
to do it: ROMarr stores several `_`-prefixed keys it deliberately *does*
show, so a "hide anything underscored" rule would have hidden the wrong half
and shown the tokens.

## Reference implementation

- Protocol + trust model: `romarr/federation.py` (MIT, ~400 lines, no
  network code in the hot path so it is testable offline)
- The invitation landing page: `romarr/ui.py`, `link_page()` — a constant,
  which is a property `tests/test_security_claims.py` asserts rather than
  claims
- 36 offline tests: `tests/test_federation.py`
- 12 tests over two ROMarrs on real loopback sockets:
  `tests/test_invitation_links.py` — the link carries no secret, the landing
  page is the same bytes for everybody, a link and a code peer two servers,
  the claim works once, an expired code leaves the invitation alive, guessing
  destroys it, and a redeemed-but-unconfirmed peer is refused the shelf and
  netplay both
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
