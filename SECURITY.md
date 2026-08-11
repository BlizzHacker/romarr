# Security

ROMarr runs on home servers next to download clients, library servers and
private services, and it holds credentials for all of them. This document says
what it protects, what it does not, and how to report a problem.

It is written to be checkable. Where it claims a property, there is a test
named for it; where something is not protected, it says so plainly rather than
implying otherwise.

## Reporting a vulnerability

Report privately through GitHub's
[security advisories](https://github.com/BlizzHacker/romarr/security/advisories/new).
Please do not open a public issue for anything exploitable.

Include what you were running, what you did, and what happened. A proof of
concept helps but is not required — a clear description of the mechanism is
enough to act on.

Expect an acknowledgement within a week. Fixes go to `main` with a regression
test, and the advisory is published once a release carrying the fix exists.
Credit is given unless you ask otherwise.

## Supported versions

Fixes land on `main` and in the next release. Only the latest release is
supported; there are no backports to older tags.

**Anything at or below `v0.7.0` has no authentication at all** — its API
answers whoever reaches the port, on a service that queues downloads and writes
to the filesystem. If you installed from a release tarball on or before
2026-08-02, upgrade. The Proxmox installer now refuses to deploy a build
without `romarr/auth.py`.

## What ROMarr is

An acquisition and automation service. It searches indexers, hands releases to
a download client, verifies finished files against No-Intro and Redump DATs,
and files them into a library server.

That means it is **administrator-facing software**. It deliberately reaches
arbitrary operator-supplied addresses — your Prowlarr, your qBittorrent, your
RomM — and most of them are on RFC1918 addresses. ROMarr does not and will not
block private-range destinations, because doing so would break its entire
purpose. The boundary that matters is not "which addresses may be reached" but
"who may ask ROMarr to reach them", which is authentication.

## Authentication

Required by default. There is no configuration mistake that produces an open
install; the only way off is explicit.

- A fresh install is **unclaimed**: the first visit to the web UI sets the
  password. `POST /api/v1/setup` is open only while unclaimed and answers `409`
  afterwards, so it is a one-shot rather than a standing unauthenticated
  password reset.
- Setting `ROMARR_PASSWORD` or your own `ROMARR_API_KEY` claims the install
  before it serves a request, leaving no unclaimed window. This is what a
  container template should do.
- Passwords are hashed with scrypt (N=2^14, r=8, p=1), never stored plaintext.
- Browsers hold an HMAC-signed session cookie: `HttpOnly`, `SameSite=Strict`,
  `Path=/`. `SameSite=Strict` is what stands in for CSRF tokens — another site
  cannot make an authenticated request even if it knows the URL.
- The cookie is not `Secure`, because ROMarr is normally plain HTTP on a LAN
  and a `Secure` cookie would never be stored. Put it behind TLS if it leaves
  your network.
- API clients present `X-Api-Key`, `Authorization: Bearer` or `?apikey=`. The
  query form exists for senders that cannot set headers; it will appear in
  proxy logs, so prefer a header.
- TOTP (RFC 6238) gates interactive sign-in. It deliberately does not gate the
  API key: a script cannot be prompted, and a key is already high-entropy.
- Login is rate limited. Wrong credentials return `401` without revealing
  which of password or key was wrong.

Exactly five routes answer without a credential, and no others:

| Route | Why |
|---|---|
| `/` | Serves the sign-in or setup screen when you are not signed in, the app when you are. |
| `/login` | The sign-in screen. It has to answer somebody holding nothing. |
| `/api/v1/login` | Exchanges a password or key for a session. |
| `/api/v1/setup` | First-run claim. Open only while unclaimed; `409` after. |
| `/api/health` | Container health checks have no credential to offer. |
| `/api/v1/connect/steam/return` | Steam's OpenID redirect. It *cannot* carry the session cookie — see below. |

`/api/health` returns one bit unauthenticated — it used to return library paths
and client URLs, which is what a health check does not need.

**The Steam return is open because it has to be, and carries its own
credential instead.** The session cookie is `SameSite=Strict`, so when Steam
redirects the browser back to ROMarr the browser deliberately withholds it and
the request arrives with nothing. Loosening the cookie to `Lax` would fix that
one flow by weakening every other route against cross-site requests, so it is
not done. Instead the start of the flow — `/api/v1/connect/steam`, which *is*
authenticated — mints a `state`: 24 bytes of `secrets.token_urlsafe`, single
use, expiring in ten minutes. The return leg is refused without a live one, so
possession of it stands in for the cookie, and it can only have come from a
signed-in session.

Two further checks apply before anything is connected: the identity Steam
asserts must match `https://steamcommunity.com/openid/id/<17 digits>` exactly,
and the whole assertion is posted back to Steam with `check_authentication` and
must come back `is_valid:true`. The parameters arrive through the user's
browser and are treated as attacker-controlled until Steam itself confirms
them.

### `ROMARR_AUTH=disabled`

Turns the gate off. **Anything reaching the port is then in, including a
request that bypassed your proxy.** If something in front already
authenticates, prefer `ROMARR_AUTH=forward`, which keeps that proxy as the
authority but verifies the request actually came through it.

Forward mode requires `ROMARR_TRUSTED_PROXIES`. Identity headers are only
believed from those CIDRs, checked against the socket peer address — never
against `X-Forwarded-For`, which anybody can set. Without the CIDR list,
forward auth would be a header anyone could send.

## Filesystem

ROMarr writes files whose names come from torrents, which are attacker
controlled.

- Every archive member is checked to resolve inside the destination before
  extraction — zip, 7z and rar alike. The check lives above the format
  readers, not inside the zip one, because the format an attacker picks is the
  one with the gap.
- Members failing the check are **dropped, not sanitised**: a release that
  needs sanitising is not one to trust.
- Multi-file sets are flattened to base names, so a set cannot traverse out of
  the directory it was given — safe by construction rather than by check.
- Existing files are never overwritten unless explicitly asked.
- Remote path mappings translate what a download client reports into what
  ROMarr can see. They are operator configuration, not attacker input.

## Secrets

- The config API masks every credential; a value is masked by field type, so a
  secret left behind after changing a provider's type is still masked.
- Backups exclude secrets unless `?secrets=1` is passed explicitly.
- The API key is stored under a leading underscore so it is structurally
  outside what `safe_settings` will serialise, rather than relying on a
  maintained list.
- Credentials are not logged, including on failure paths.
- Neither unauthenticated page ever contains the API key.

## Plugins — read this before installing one

**ROM Hub plugins are code. Confinement is real but partial — read what it
does and does not cover before installing one.**

Each plugin runs as its own subprocess with no library token, no filesystem
mount of yours, and no network sockets of its own. Its only route outward is an
RPC back to ROMarr, which checks the destination against the hosts that plugin
declared in its manifest *before* opening a connection. A plugin that asks for
a host it did not declare is refused and the request never reaches the network.
A seccomp filter enforces this; it needs Linux and `pyseccomp`.

What that does **not** cover:

- A plugin runs arbitrary code in its subprocess and can read any file ROMarr
  can. Filesystem confinement needs a mount namespace and is not in place.
- Memory is not capped. The wall-clock timeout and output-size cap are.
- It can reach every host it declared. Read that list before installing; a
  search plugin asking for hosts unrelated to its source is the warning sign.

**If the filter cannot be installed, ROMarr falls back to running plugins with
no confinement at all and logs a warning saying so.** It previously set that
opt-out unconditionally, which switched off a boundary that in fact worked —
the only thing missing was `pyseccomp`. The fallback sets
`ROM_HUB_ALLOW_UNSANDBOXED=1`, which ROM Hub documents as no confinement
at all; ROMarr never sets it while the filter is available. Check the
startup log to see which state your install is in.

What *is* enforced:

- A plugin subprocess gets an **allowlisted** environment, not ROMarr's. It
  does not inherit `ROMARR_API_KEY`, `ROMARR_PASSWORD`, `PROWLARR_API_KEY`,
  `QBITTORRENT_PASS` or any other credential. It gets what a process needs to
  run, proxy settings, and the library credentials it needs to import — nothing
  else. An allowlist, because a denylist stops covering whatever is added next.
- Installing from a URL is checked against a host allowlist.
- Disabling a plugin removes it from every fan-out.

Treat installing a plugin as equivalent to running its author's code on your
server, because that is what it is.

## Dependencies

ROMarr's own code is Python standard library plus `requests`. The small
surface is deliberate: fewer dependencies is fewer supply-chain paths.

## What is not protected

Stated so nobody assumes otherwise:

- **No filesystem confinement for plugins.** Network and exec are
  confined; a plugin can still read any file ROMarr can. See above.
- **No protection against a malicious library or indexer you configured.**
  ROMarr trusts responses from services you pointed it at.
- **No encryption at rest.** `romarr.json` and `.env` hold credentials in
  plaintext; they are `chmod 600` and rely on filesystem permissions.
- **No multi-user model.** There is one operator. Forward auth can require a
  group, but ROMarr does not distinguish users or keep per-user permissions.
- **No audit log of who did what**, because there is no "who".
- **No rate limiting on the API generally** — only on login.
- **DAT verification is an integrity check, not a security control.** It tells
  you a file matches a known-good dump. It is not malware scanning, and an
  UNKNOWN verdict means "not in your DAT", not "dangerous".
