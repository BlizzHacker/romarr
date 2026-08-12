# Streaming hosts: Wolf, Sunshine, Steam Headless

**Date:** 2026-08-11
**Status:** implemented
**Code:** [`romarr/playability.py`](../../romarr/playability.py) (the
`MoonlightHost` section), [`romarr/app.py`](../../romarr/app.py),
[`romarr/ui.py`](../../romarr/ui.py)
**Tests:** [`tests/test_moonlight.py`](../../tests/test_moonlight.py) — 60
**Live proof:** [`scripts/moonlight_proof.py`](../../scripts/moonlight_proof.py)
— 39/39 against real Wolf and Sunshine on 2026-08-11, see §6 and §7

The request was "Wolf, Moonlight, Headless Steam compatibility for the
ultimate self-hosted headless homelab setup". This is what those three
actually expose, what ROMarr now does with it, what still needs a human, and
what remains unproven.

It was written against upstream source and shipped that way. On 2026-08-11 a
real Wolf and a real Sunshine were stood up and a real Moonlight client was
pointed at both; §6 says what that proved and what it did not, §7 says what
now exists on the cluster, and four claims in §1 are corrected in place where
the source read one way and the software behaved another.

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

Two traps in there, both read from source, both since **observed on live
hosts** (§7), and both acted on in the code:

- **`PairStatus` is always `0` over plain HTTP.** Sunshine sets it to 1 only
  on the HTTPS server and only when a `uniqueid` query parameter is present;
  Wolf passes `is_https`. So it can never mean "you are not paired", and
  ROMarr reads pairing state from the paired-client list instead. There is a
  test pinning this so nobody "fixes" it later. Confirmed against both live
  hosts, and against Sunshine *with* a `uniqueid` parameter on the plain port,
  which is the case most likely to have been an exception. It was not.
- **`state` is `SUNSHINE_SERVER_FREE` on Wolf too.** Wolf emits that literal
  string (`moonlight-protocol/moonlight.cpp`) because Moonlight clients
  require it. Confirmed live: a Wolf that has never seen an NVIDIA GPU
  answers `SUNSHINE_SERVER_FREE`.

The **host kind is declared by the operator and never sniffed**, and that
decision stands — but the reason given for it was slightly too strong. The
field sets are not in fact identical. Against the live pair:

| Field | Wolf | Sunshine |
|---|---|---|
| `SupportedDisplayMode` | ~170 nested modes, unauthenticated | absent |
| `MaxLumaPixelsHEVC` | `1869449984` | `0` |
| `ServerCodecModeSupport` | `257` | `262145` |
| `mac` | the real NIC address | `00:00:00:00:00:00` |
| `uniqueid` | a lowercase UUID | an upper-case GUID |

So a sniffer could be written. It is still not written, and the difference is
worth being precise about: none of those is a documented contract, every one
of them is a value that moves when either project changes an encoder or a
capture backend, and a wrong guess here silently mislabels every route on the
page. `hostname` is the same kind of hint — Wolf's stock value is literally
`"Wolf"` — and it is the first thing an operator renames. A declared kind
costs one environment variable and cannot rot.

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
| GET | `/api/v1/apps` | **yes** — but see below, it is not the whole list |
| POST | `/api/v1/apps/add`, `/api/v1/apps/delete` | no — see §4 |
| GET | `/api/v1/profiles` | **yes** — where the apps actually are |
| POST | `/api/v1/profiles/add`, `…/remove` | no |
| GET/POST | `/api/v1/sessions`, `…/add`, `…/start`, `…/pause`, `…/stop`, `…/input` | no — see §4 |
| GET/POST | `/api/v1/lobbies`, `…/create`, `…/join`, `…/leave`, `…/stop` | no |
| POST | `/api/v1/runners/start` | no |
| GET | `/api/v1/utils/get-icon` | no |
| POST | `/api/v1/docker/images/inspect`, `…/pull` | no |
| GET | `/api/v1/openapi-schema` | no |

App titles come back on the `title` field of `AppListResponse`.

**`GET /api/v1/apps` is not the host's app list, and assuming it was is the
one thing the fixtures got badly wrong.** On a live Wolf (`stable`, pulled
2026-08-11) that endpoint returned exactly two entries — `Wolf UI` and
`Test ball` — while the nine apps everybody means when they say "Wolf's stock
apps" (Firefox, RetroArch, Steam, Pegasus, Lutris, Prismlauncher, Desktop
(xfce), EmulationStation, Kodi) came back only from `GET /api/v1/profiles`,
under `profiles[].apps[]`. The generated config declares **no top-level
`[[apps]]` at all**; every entry is a `[[profiles.apps]]`. Wolf's own OpenAPI
schema describes `/apps` as "all apps that will be shown in the Moonlight
client", which is a per-profile view rather than a per-host one.

The consequence was not cosmetic: an operator adding PCSX2 the only way Wolf's
config offers would have been invisible, and PS2 would never have gained a
route. `_wolf_apps` now takes the union, with `/profiles` allowed to fail
quietly so an older Wolf still yields what `/apps` gave.

**Not one of the stock apps proves a platform** — see §2. A PCSX2 added to a
profile does, and did.

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
  documented behaviour, not a loophole — and it is now **observed**: every
  call in §7 carried an `Authorization` header and neither of the other two,
  and every one was served.
- **`POST /api/pin` cannot be trusted when it says `true`** — confirmed by
  experiment, not by citation. Against Sunshine 2026.516.143833 with a real
  Moonlight client mid-handshake, a **deliberately wrong** PIN returned
  `{"status": true}` and the client never appeared in `/api/clients/list`.
  That is [LizardByte/Sunshine#3944](https://github.com/LizardByte/Sunshine/issues/3944)
  reproduced. So `false` is meaningful and `true` is not, and ROMarr's UI
  never says "Paired" off the back of one.
- **`false` means one thing, not two, and the fixtures said two.** The old
  refusal text offered "either no client is waiting, or the PIN was not four
  digits". Live, a malformed PIN never reaches `nvhttp::pin`: `confighttp`
  rejects it first with **HTTP 400** and
  `{"error": "PIN must be between 0000 and 9999"}`. `{"status": false}` on a
  200 therefore means *no client is waiting*, full stop, and that is what the
  UI now says. Sending an operator to re-check a PIN that was never the
  problem is exactly the kind of confident wrong sentence this module exists
  to avoid.
- **A missing `Content-Type` is rejected before authentication.** Posting
  without it returns 400 `{"error": "Content type mismatch"}`. ROMarr sets it;
  the comment saying why is now backed by having seen the failure.

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
   `{pair_secret, client_ip}` per request — confirmed live, both fields
   populated, e.g.
   `{"pair_secret": "A89E3773BD82B543", "client_ip": "192.168.0.128"}`. ROMarr
   rebuilds the PIN page URL Wolf logs at startup
   (`http://<host>:47989/pin/#<secret>`) from the secret, which beats telling
   an operator to go and read container logs.
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
| `WOLF_SOCKET_PATH` | Path to a mounted `wolf.sock`. A stock Wolf puts it at **`/tmp/sockets/wolf.sock`**, following the `XDG_RUNTIME_DIR` its own compose file sets — not the `/var/run/wolf/` its docs call conventional. |
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

## 6. What is proven, and what is still not

Rewritten on **2026-08-11**, the day this stopped being fixtures-only. The
previous version of this section opened "No live Wolf, Sunshine, Steam
Headless or Moonlight client was in the loop for any of this." That is no
longer true of the first two, or of the client.

### Proven against real software

Every row below is `scripts/moonlight_proof.py` output against the rig in §7
— **39/39** — plus the two HTTP routes driven on a running ROMarr.

| Was unproven | Now |
|---|---|
| `GET /serverinfo` against a real host | Parsed off live Wolf and live Sunshine. Every field the UI shows came back; the always-zero `PairStatus` and the `SUNSHINE_SERVER_FREE` state are observed, not inferred |
| **Wolf's UNIX socket transport** — the one that mattered most | `_UnixHTTPConnection` opened a real `wolf.sock` and drove `/apps`, `/profiles`, `/clients`, `/pair/pending` and `POST /pair/client`. `http.client`'s framing is accepted exactly as written |
| The nginx TCP proxy path (`WOLF_API_URL`) | Stood up per Wolf's documented `proxy_pass`, and every socket assertion above re-run through it with identical results |
| Sunshine's self-signed TLS handshake | TLSv1.3 / `TLS_AES_256_GCM_SHA384` against a `CN=Sunshine Gamestream Host` certificate. A verifying context fails it with `CERTIFICATE_VERIFY_FAILED`, which is why `check_hostname=False` / `CERT_NONE` is there |
| Sunshine's CSRF behaviour | Basic auth with no `Origin` and no `Referer` was served on every call |
| **Every PIN relay, on both implementations** | A real moonlight-qt 6.1.0 started a genuine handshake; ROMarr listed the waiting client (Wolf), relayed the PIN, and **both hosts then listed the client as paired**. Wolf logged `Succesfully paired`; Sunshine's `named_certs` gained an entry named `ROMarr`, the name `submit_pin` sends |
| The Sunshine wrong-PIN claim | No longer a citation of somebody else's bug report. A wrong PIN with a request outstanding returned `{"status": true}` and paired nothing |
| The app-name table against a real install | Wolf's twelve titles and Sunshine's four went through `platforms_from_apps` unchanged. `PCSX2` granted `ps2`; `RetroArch`, `Steam`, `EmulationStation`, `Kodi`, `Firefox`, `Lutris`, `Pegasus`, `Prismlauncher`, `Desktop (xfce)`, `Wolf UI` and `Test ball` granted nothing, which is eleven refusals fired by real data |
| Neither host needs a GPU to be integrated with | Wolf picked `x264`/`x265`/`aom` after failing to find a render node; Sunshine ran on Xvfb. The protocol surface is entirely indifferent to it |

### Still not proven, precisely

- **No video has ever been streamed by this rig, and ROMarr has never asked
  for any.** `/launch` is behind a paired *client* certificate and is
  deliberately not built (§4). Pairing completing is not a session starting:
  nothing here proves a Moonlight client can get a picture out of either
  host, and on these two containers — software encoders, no GPU, no real
  display on Wolf — it very likely cannot. That is irrelevant to ROMarr and
  would be the whole point to anybody actually playing a game.
- **Steam Headless end to end.** Unchanged and untouched: the model (Sunshine
  inside, noVNC beside) is still read from its compose files and README, and
  no container was run. What *is* now proven is the thing it reduces to — its
  streaming surface is Sunshine's, and Sunshine's is proven — so what remains
  is the reduction itself: that `ENABLE_SUNSHINE=true` produces a Sunshine on
  47990 behaving like the one tested here, and that `STEAM_HEADLESS_URL`
  points at a desktop that exists.
- **Wolf on a host with a GPU.** Everything above was read from a Wolf that
  disabled its zero-copy pipeline at startup. No API response here is
  documented to vary with that, and none observably did, but no GPU host was
  in the loop.
- **A second Wolf profile.** `_wolf_apps` unions across `profiles[]`, and the
  live host had one profile. The loop over several is fixture-tested only.
- **Long-lived behaviour.** Every observation is from a single session on
  freshly installed hosts. Nothing here says what a Wolf that has been up for
  a month, or a Sunshine with fifty paired clients, answers.
- **Versions.** Sunshine `2026.516.143833` and Wolf `stable` as of
  2026-08-11. Either project can change any of this, which is why the proof
  is a script and not a paragraph.

---

## 7. The proof rig: what now exists on the cluster

Both hosts are **new containers created for this**; nothing existing was
stopped, reconfigured or deleted. Node **Thiccc** (192.168.0.5) was chosen
over Slimmm because Slimmm already carries the *arr stack, RomM and ROMarr,
and over mw-laptop because its GPU is passed through to a working ComfyUI VM
that must not be disturbed. No GPU is required for any of this (§6).

Rootfs comes from the `OS` LVM volume group on Thiccc. Total footprint **18
GB**.

| | Sunshine | Wolf |
|---|---|---|
| CT | **119** `sunshine-proof` | **128** `wolf-proof` |
| Address | **192.168.0.119**/24 | **192.168.0.128**/24 |
| Template | ubuntu-24.04-standard | debian-12-standard |
| Disk / RAM | 8 GB / 4 GB | 10 GB / 6 GB |
| Software | Sunshine 2026.516.143833, LizardByte's `.deb` | `ghcr.io/games-on-whales/wolf:stable` under Docker |
| Ports | 47989 / 47984 / 47990 | 47989 / 47984, plus nginx on 8080 |
| Admin credential | `romarr` / `proofpass123`, written by `sunshine --creds` into `/root/.config/sunshine/credentials/` | none — Wolf's API is the socket |
| Socket | — | `/tmp/sockets/wolf.sock` |
| `onboot` | 0 | 0 |

The credentials are **throwaway, for a LAN-only proof box, and are recorded
here on purpose** so the rig is reproducible; there is nothing behind them.
Neither container starts on boot. Delete both with `pct destroy 119` and
`pct destroy 128` on Thiccc.

Two details worth having in writing, because both cost time:

- **Wolf's socket is at `/tmp/sockets/wolf.sock`, not
  `/var/run/wolf/wolf.sock`.** It follows `XDG_RUNTIME_DIR`, which Wolf's own
  compose file sets to `/tmp/sockets`. The "by convention" path in
  `WOLF_SOCKET_PATH`'s documentation is not what a stock deployment produces.
- **nginx must be able to write to that socket.** Wolf creates it
  `srwxr-xr-x root:root`, and connecting to a UNIX socket needs write
  permission, so a stock `www-data` nginx gets a 502 and no useful log line.

### Reproducing it

```sh
# On Thiccc. Sunshine:
pct create 119 template-store:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname sunshine-proof --cores 4 --memory 4096 --swap 512 --rootfs OS:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.0.119/24,gw=192.168.0.1 \
  --nameserver "1.1.1.1 8.8.8.8" --unprivileged 0 --features nesting=1 --onboot 0
pct start 119
pct exec 119 -- apt-get install -y xvfb
pct exec 119 -- wget -O /root/sunshine.deb \
  https://github.com/LizardByte/Sunshine/releases/latest/download/sunshine-ubuntu-24.04-amd64.deb
pct exec 119 -- apt-get install -y /root/sunshine.deb
pct exec 119 -- sunshine --creds romarr proofpass123
# then a unit that runs `Xvfb :0 -screen 0 1280x720x24` and
# `sunshine /root/.config/sunshine/sunshine.conf` with DISPLAY=:0, and a
# sunshine.conf carrying `encoder = software` and `capture = x11`.

# Wolf: the same pct create with the debian-12 template, OS:10 and
# --features nesting=1,keyctl=1, plus these four lines appended to
# /etc/pve/lxc/128.conf for its virtual input devices:
#   lxc.cgroup2.devices.allow: c 13:* rwm
#   lxc.cgroup2.devices.allow: c 10:223 rwm
#   lxc.mount.entry: /dev/uinput dev/uinput none bind,optional,create=file
#   lxc.mount.entry: /dev/input dev/input none bind,optional,create=dir
# then Docker, and Wolf's own compose file verbatim from its documentation.
```

The Moonlight client is the official **moonlight-qt 6.1.0 AppImage**, run
headless against `Xvfb :0` — it ships no `offscreen` Qt platform plugin, so a
real X server is needed even though nothing is ever displayed. Its `pair`
subcommand takes `--pin`, which is the only way to drive the human half of
the handshake from a script.

### Running the proof

```sh
DISPLAY=:0 .venv/bin/python scripts/moonlight_proof.py \
  --wolf 192.168.0.128 --wolf-socket /tmp/sockets/wolf.sock \
  --wolf-api-url http://127.0.0.1:8080 \
  --sunshine 192.168.0.119 --sunshine-auth romarr:proofpass123 \
  --moonlight /root/squashfs-root/AppRun
```

It has to run **on the Wolf machine** for the socket half to prove anything.
Without `--moonlight` the pairing proofs are skipped and say so, rather than
passing on a client nobody started.

### What the run changed in the code

Four things, each one a fixture that turned out to be wrong:

1. `_wolf_apps` reads `/api/v1/profiles` as well as `/api/v1/apps`, because
   the second is not the host's app list (§1). This is the only one that was
   costing an operator a real feature.
2. Sunshine's `{"status": false}` is reported as "no client is waiting" and
   nothing else; the malformed-PIN case is a 400 that never reaches it (§1).
3. `_error_sentence` lifts the host's own `error` field out of a failure
   body, so `submit_pin` reports `HTTP 500: Invalid pair secret` rather than
   a line of JSON.
4. `Wolf UI` joined `MULTI_MACHINE_APPS` — it and `Test ball` were the only
   two entries a live `/api/v1/apps` returned, so leaving it unrecognised
   left a stock app that nobody had looked at.
