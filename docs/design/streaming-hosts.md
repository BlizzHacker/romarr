# Streaming hosts: Wolf, Sunshine, Steam Headless

**Date:** 2026-08-11
**Status:** implemented
**Code:** [`romarr/playability.py`](../../romarr/playability.py) (the
`MoonlightHost` section), [`romarr/app.py`](../../romarr/app.py),
[`romarr/ui.py`](../../romarr/ui.py)
**Tests:** [`tests/test_moonlight.py`](../../tests/test_moonlight.py) — 56

The request was "Wolf, Moonlight, Headless Steam compatibility for the
ultimate self-hosted headless homelab setup". This is what those three
actually expose, what ROMarr now does with it, what still needs a human, and
what nobody here has proven against real hardware.

The short version, before the detail:

> A Moonlight host is a **desktop**, not a platform router. It can tell you it
> is alive without any credential at all. With a credential it can tell you
> what *applications* it has. Nothing in any of these APIs can tell you what
> those applications will open, and ROMarr does not guess.

---

## 1. What each host genuinely exposes

### The Moonlight protocol — common to all three

Wolf, Sunshine and NVIDIA's original GameStream all serve the same protocol on
the same two ports: **47989** plain HTTP, **47984** HTTPS. Wolf hardcodes both
in `state/data-structures.hpp` (`HTTP_PORT`, `HTTPS_PORT`, overridable with
`WOLF_HTTP_PORT`/`WOLF_HTTPS_PORT`); Sunshine maps them off the same base.

| Endpoint | Port | Auth | Reachable by ROMarr |
|---|---|---|---|
| `GET /serverinfo` | 47989 | **none** | **yes** |
| `GET /pair` | 47989 | none, but starts the 4-phase crypto exchange | no (see §3) |
| `GET /serverinfo` | 47984 | paired **client certificate** | no |
| `GET /applist` | 47984 | paired client certificate | no |
| `GET /launch`, `/resume`, `/cancel` | 47984 | paired client certificate | **no** |

`GET /serverinfo` on the **plain HTTP port** is the entire unauthenticated
surface, and it is a deliberate dead end — everything that does something is
on the HTTPS server behind `get_client_if_paired` (Wolf, `rest/servers.cpp`)
or the equivalent client-certificate check (Sunshine, `nvhttp.cpp`). It
returns flat XML:

```
hostname  appversion  GfeVersion  uniqueid  HttpsPort  ExternalPort
mac  LocalIP  PairStatus  currentgame  state
```

Two traps in there, both verified in source and both acted on in the code:

- **`PairStatus` is always `0` over plain HTTP.** Sunshine sets it to 1 only
  on the HTTPS server and only when a `uniqueid` query parameter is present;
  Wolf passes `is_https`. So it can never mean "you are not paired", and
  ROMarr reads pairing state from the paired-client list instead. There is a
  test pinning this so nobody "fixes" it later.
- **`state` is `SUNSHINE_SERVER_FREE` on Wolf too.** Wolf emits that literal
  string (`moonlight-protocol/moonlight.cpp`) because Moonlight clients
  require it. Combined with an identical field set, this means **`/serverinfo`
  cannot distinguish Wolf from Sunshine.** The host kind is therefore declared
  by the operator and never sniffed. Wolf's stock `hostname` is `"Wolf"`,
  which is a hint and not a fact — it is the first thing anyone renames — so
  it is not acted on.

### Wolf (games-on-whales/wolf)

Wolf has a full REST API, and it is **bound to a UNIX socket and nothing
else** — path from `WOLF_SOCKET_PATH`, `/var/run/wolf/wolf.sock` by
convention. Its own documentation is emphatic about why:

> Exposing the API is highly dangerous, via the API you can pair clients to
> the server, execute arbitrary commands, and more.

The only exposure route Wolf blesses is an nginx `proxy_pass` to the socket,
which its docs give a config for. Endpoints, read from
`src/moonlight-server/api/unix_socket_server.cpp`:

| Method | Path | ROMarr uses it |
|---|---|---|
| GET | `/api/v1/events` (SSE) | no |
| GET | `/api/v1/pair/pending` | **yes** |
| POST | `/api/v1/pair/client` | **yes** |
| POST | `/api/v1/unpair/client` | no |
| GET | `/api/v1/clients` | **yes** |
| POST | `/api/v1/clients/settings` | no |
| GET | `/api/v1/apps` | **yes** |
| POST | `/api/v1/apps/add`, `/api/v1/apps/delete` | no — see §4 |
| GET/POST | `/api/v1/profiles`, `…/add`, `…/remove` | no |
| GET/POST | `/api/v1/sessions`, `…/add`, `…/start`, `…/pause`, `…/stop`, `…/input` | no — see §4 |
| GET/POST | `/api/v1/lobbies`, `…/create`, `…/join`, `…/leave`, `…/stop` | no |
| POST | `/api/v1/runners/start` | no |
| GET | `/api/v1/utils/get-icon` | no |
| POST | `/api/v1/docker/images/inspect`, `…/pull` | no |
| GET | `/api/v1/openapi-schema` | no |

App titles come back on the `title` field of `AppListResponse`. Wolf's stock
`config.v5.toml` ships nine apps: Firefox, RetroArch, Steam, Pegasus, Lutris,
Prismlauncher, Desktop (xfce), EmulationStation, Kodi, plus a "Test ball"
pattern. **Not one of them proves a platform** — see §2.

### Sunshine (LizardByte/Sunshine)

A REST API on **47990** (Moonlight base port + 1), HTTPS with a self-signed
certificate, HTTP basic auth with the admin credentials. From `docs/api.md`
and `src/confighttp.cpp`:

| Method | Path | ROMarr uses it |
|---|---|---|
| GET | `/api/apps` | **yes** — returns `apps.json` verbatim; titles on `name` |
| POST/DELETE | `/api/apps`, `/api/apps/{index}`, `/api/apps/close` | no |
| GET | `/api/clients/list` | **yes** |
| POST | `/api/clients/unpair`, `…/unpair-all`, `…/update` | no |
| POST | `/api/pin` — `{"pin": "1234", "name": "..."}` | **yes** |
| GET/POST | `/api/config` | no |
| GET | `/api/csrf-token`, `/api/logs`, `/api/browse`, `/api/covers/{i}` | no |
| POST | `/api/password`, `/api/restart` | no |

Two findings worth having in writing:

- **CSRF is not in the way, by Sunshine's design.** `validate_csrf_token`
  returns true outright when a request carries neither `Origin` nor
  `Referer`, on the reasoning that a page in a browser cannot make a
  non-browser client issue requests. A server-side call from ROMarr sends
  neither, so basic auth alone is enough for `POST /api/pin`. This is
  documented behaviour, not a loophole.
- **`POST /api/pin` cannot be trusted when it says `true`.**
  `nvhttp::pin()` returns false only when *no client is waiting* or the PIN
  is not four digits. A **wrong** PIN with a request outstanding still
  returns `{"status": true}` — reported upstream as
  [LizardByte/Sunshine#3944](https://github.com/LizardByte/Sunshine/issues/3944).
  So `false` is meaningful and `true` is not, and ROMarr's UI never says
  "Paired" off the back of one.

### Steam Headless (Steam-Headless/docker-steam-headless)

**It is not a fourth protocol, and modelling it as one would have been
wrong.** It is a container that runs Steam on a desktop and streams it out
with **Sunshine** — `ENABLE_SUNSHINE=true`, `SUNSHINE_USER`, `SUNSHINE_PASS`
in its own compose files, Sunshine on 47990 as usual — alongside a
noVNC/neko browser desktop on `PORT_NOVNC_WEB`. Its compose files run
`network_mode: host`.

So ROMarr models it as **a Sunshine host with a web-desktop URL attached**.
It reuses Sunshine's client wholesale; the only differences are what the UI
calls it and the extra link. There is no Steam-Headless-specific API to
integrate with, and inventing one would have been a fiction.

---

## 2. The design decision that mattered

`playability.py` already had a stream tier and a `StreamServer` that answers
"can you play this platform" from `/api/play/route`. That server was *built*
for the question: it reads its own core directory and one GET settles it.

A Moonlight host has no such endpoint and never will. It has a list of
applications. **What an application can open is a question about somebody's
filesystem that no API exposes.**

The over-claim this had to avoid is concrete: an operator with Wolf running
sees "PS2 streams from Wolf", clicks a PS2 game, and gets Wolf's app list with
Firefox and Steam on it. That is worse than never having offered the route,
and `playability.py`'s stated rule is that every table fails toward
under-claiming.

So the rule implemented is:

**An app grants a platform a stream route only when its name is an emulator
for exactly one machine.**

- `EMULATOR_APPS` — PCSX2 → ps2, Dolphin → ngc + wii, flycast/redream/reicast
  → dc, DuckStation → psx, PPSSPP → psp, azahar/Citra/Lime3DS → 3ds,
  melonDS/DeSmuME → nds, Yabause → saturn, DOSBox → dos. There is no
  configuration of PCSX2 that plays a Dreamcast disc, which is what makes
  the inference safe. Mesen, Mednafen and ScummVM are in the table with an
  **empty** tuple — Mesen 2 runs SNES and Game Boy as well as NES, so the
  name does not narrow to one machine — kept listed so the reasoning is on
  record rather than rediscovered.
- `MULTI_MACHINE_APPS` — RetroArch, Steam, EmulationStation, Pegasus, Lutris,
  Kodi, Firefox, Prism Launcher, Desktop, Test ball. Listed **by name with a
  reason each**, so "we checked" cannot decay into "nobody looked" — the same
  device `NO_EJS_CORE` already uses. A RetroArch with no cores and a RetroArch
  with forty look identical from outside the container.
- Anything unrecognised grants nothing.

`MoonlightHost` duck-types `StreamServer` (`tier`, `why`, `reachable`,
`label`), so `routes_for` consumes it unchanged — no fifth route kind was
invented, because "rendered elsewhere and delivered as video" is one idea and
an operator does not care whether the pixels came from RetroArch or Wolf.

Two supporting changes fell out of that:

- The STREAM route detail was the hardcoded string `"headless RetroArch"`.
  It now comes from the answering source's `engine`, because telling somebody
  streaming PS2 off Wolf's PCSX2 that RetroArch is running it is a lie.
- `StreamSources` fronts both tiers behind the single `stream=` slot
  `routes_for` takes. First answer wins, RetroArch first — it *knows* which
  cores it has, while a Moonlight host is inferring from names, and an
  inferred answer must not displace a known one. Attribution is resolved
  **per platform** (`attribution(slug)`), because with two sources configured,
  naming the first for a route the second granted is exactly the confident
  wrong sentence this module exists to avoid.

### Three refusals, three different sentences

They lead to three different fixes, so they read differently:

| State | What `why()` says |
|---|---|
| Host is off | *(nothing — the `stream_unreachable` field carries it)* |
| Host is up, app list unreadable | "reachable … but ROMarr cannot read its app list (…), so it cannot say which platforms it plays" |
| Host is up, app list read, no emulator | "reachable and its app list has no emulator for this platform; install one and it appears here" |

**Reachability is not capability**, and the default case is the middle row:
reading the app list needs an admin credential ROMarr may not have, so the
common outcome for a working host is that it grants nothing and says why.

---

## 3. What needs a human, and cannot not

**Moonlight pairing cannot be automated.** This is stated in the module
docstring, in the API payload (`pairing.manual` on every response), in the UI
before the input rather than after a failure, and here.

The protocol is a four-phase exchange in which the **client** generates a PIN,
both sides derive `SHA256(SALT + PIN)[0:16]` as an AES key, and the client
proves possession of it. Wolf's own field documentation says it outright —
`PairRequest.pin` in `src/moonlight-server/api/api.hpp` is annotated *"The PIN
created by the remote Moonlight client"*.

The PIN exists on a screen in somebody's hand. No host-side API can produce
it, guess it, or skip it. Any ROMarr feature claiming to pair a client on its
own would be a lie with a progress bar.

What ROMarr does instead is everything on either side of the human:

1. **Notice a client is waiting.** Wolf's `GET /api/v1/pair/pending` returns
   `{pair_secret, client_ip}` per request. ROMarr rebuilds the PIN page URL
   Wolf logs at startup (`http://<host>:47989/pin/#<secret>`) from the secret,
   which beats telling an operator to go and read container logs.
   **Sunshine has no equivalent endpoint** — its API can accept a PIN and
   cannot tell you one is wanted. The UI says that rather than rendering an
   empty list, because empty would read as "nobody is waiting".
2. **Be the box the PIN is typed into**, and post it to the host.
3. **Report the paired-client list afterwards**, which is the only honest
   pairing verdict either host offers.

The UI never says "Paired" after a submission. It says the host accepted the
submission and that the host does not report whether the PIN was correct —
because, per §1, neither of them does.

---

## 4. What was deliberately not built

- **Launching a game.** `/launch` is on the Moonlight HTTPS server behind a
  paired client certificate in both implementations. To use it, ROMarr would
  have to pair *itself* as a Moonlight client: implement the four-phase
  crypto, hold a client key, and ask a human for a PIN on its own behalf —
  and it would then still have nowhere to put the video. Wolf's
  `POST /api/v1/sessions/start` exists but drives Wolf's own streaming
  pipeline to a client that has already paired; it is not a way for a browser
  to get a picture.
- **A `moonlight://` deep link.** There is no registered URI scheme to hand a
  browser. moonlight-qt uses that string internally in
  `app/backend/computermanager.cpp` only to parse a typed-in host address, and
  registers no scheme handler; the request to add one
  ([moonlight-qt#29](https://github.com/moonlight-stream/moonlight-qt/issues/29))
  is still a request. The UI offers `moonlight stream <host>`, which does
  exist as a CLI (`app/cli/commandlineparser.cpp` — `stream`, `pair`, `list`,
  `quit`).
- **Adding apps to a host.** Both APIs can create app entries. ROMarr filing a
  ROM is not the same act as reconfiguring somebody's streaming host, and a
  tool that starts writing to Wolf's config because it imported a game has
  exceeded its remit.
- **Sniffing the host kind.** See §1 — `/serverinfo` cannot distinguish them.

---

## 5. Configuration

| Variable | Meaning |
|---|---|
| `MOONLIGHT_HOST` | Host, `host:port`, or a URL (scheme discarded). Default port 47989. |
| `MOONLIGHT_KIND` | `wolf` (default), `sunshine`, or `steam-headless`. |
| `MOONLIGHT_USER` / `MOONLIGHT_PASS` | Sunshine/Steam Headless admin credentials, for the app list and PIN relay. Read from the environment and **never written to the store**, same rule as `ROMARR_API_KEY`. |
| `WOLF_SOCKET_PATH` | Path to a mounted `wolf.sock`. |
| `WOLF_API_URL` | An nginx proxy in front of that socket, per Wolf's own documented config. |
| `STEAM_HEADLESS_URL` | The noVNC/neko desktop, surfaced as a link. |

Everything is optional. Without `MOONLIGHT_HOST`, ROMarr behaves exactly as
before. With it and nothing else, ROMarr probes `/serverinfo` and reports a
live host that grants no platform anything — which is the truth.

New routes, both in [`romarr/openapi.py`](../../romarr/openapi.py):

- `GET /api/v1/moonlight`
- `POST /api/v1/moonlight/pin`

Wolf's socket needs `AF_UNIX`. Where the platform has none (Windows), the
client says so and points at `WOLF_API_URL` rather than failing obscurely.

---

## 6. What is NOT proven

In the style of [`docs/PROOF.md`](../PROOF.md), and this section is the
important one.

**No live Wolf, Sunshine, Steam Headless or Moonlight client was in the loop
for any of this.** 56 tests assert ROMarr's half of each conversation —
request shapes, response parsing, every refusal, the breaker, the
attribution — against fixtures built from the upstream source read at the
commits linked above. That proves ROMarr does not misbehave. It does not
prove the conversation happens.

Specifically unverified:

- **`GET /serverinfo` against a real host.** The XML fixture is hand-built
  from `moonlight.cpp` and `nvhttp.cpp`. Field names and the always-zero
  `PairStatus` are read from source, not observed.
- **Wolf's UNIX socket transport.** `_UnixHTTPConnection` has never opened a
  real `wolf.sock`. The HTTP framing is `http.client`'s and is not in doubt;
  whether Wolf's server accepts it exactly as written is untested.
- **The nginx TCP proxy path** (`WOLF_API_URL`) has never been stood up.
- **Sunshine's self-signed TLS handshake** with `check_hostname=False` /
  `CERT_NONE` has never met a real Sunshine certificate.
- **Every PIN relay.** No PIN has been submitted to a real host by this code,
  on either implementation. The claim that Sunshine returns `true` for a wrong
  PIN is upstream's bug report, not our observation.
- **The app-name table against real installs.** `EMULATOR_APPS` is matched
  against titles as *typically* written. A host whose PCSX2 entry is called
  "PS2" gains nothing, and that is the intended failure direction — but how
  often it fires in practice is unknown.
- **Steam Headless end to end.** The model (Sunshine inside, noVNC beside) is
  read from its compose files and README. No container was run.

Closing any of these is one person with the hardware running it and posting
what happened. The highest-value one is the Wolf socket path, because it is
the only transport here that is not plain HTTP.
