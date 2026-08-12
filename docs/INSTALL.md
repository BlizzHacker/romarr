# Installing ROMarr

Everything needed to go from nothing to a working install, without reading
source. If a step here needs knowledge that is not on this page, that is a bug
in this page — please open an issue.

Written to the same standard as [PROOF.md](PROOF.md): where this says something
works, it was run. Where it has never been run, it says so.

## Contents

- [What ROMarr needs](#what-romarr-needs)
- [Docker, in one command](#docker-in-one-command)
- [Docker Compose](#docker-compose) ← start here for a real install
- [Proxmox LXC](#proxmox-lxc)
- [Home Assistant](#home-assistant)
- [From source](#from-source)
- [First run: claiming the install](#first-run-claiming-the-install)
- [The rule that catches everyone](#the-rule-that-catches-everyone)
- [Configuration reference](#configuration-reference)
- [Where your data lives](#where-your-data-lives)
- [Backing up](#backing-up)
- [Upgrading](#upgrading)
- [Rolling back](#rolling-back)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

---

## What ROMarr needs

ROMarr does not store a library, run an emulator or index anything itself. It
sits between three things you already run:

| | What it does | Required? |
|---|---|---|
| **An indexer** — Prowlarr, or a Torznab/Newznab URL, or a torrent RSS feed | Finds releases | To search for anything. Without it ROMarr runs and finds nothing. |
| **A download client** — qBittorrent, Transmission, Deluge, rTorrent, Synology DS or Real-Debrid for torrents; SABnzbd or NZBGet for usenet | Fetches the release | To grab anything. A release found with no client that speaks its protocol is ranked and then refused. |
| **A game library** — RomM, Gaseous, Retrom, or just a directory | Receives the ROM | `folder` needs nothing but a path, so effectively no. |

Everything else — DAT verification, import lists, streaming hosts, the browser
players, plugins — is optional and off until configured.

**ROMarr starts and serves its UI with none of the three reachable.** The
Settings pages name what is missing. A first run is never a blank failure, so
it is fine to start it before the rest of the stack is ready.

### What it needs from you

- A port. Default **6868**. (It was 7878 before 0.7 — that is Radarr's, and
  running both is the normal case.)
- Two paths that must agree with things outside ROMarr:
  - your **library root**, which your library server also has to scan;
  - your **download client's completed directory**, at the path *the client
    reports*.
  Nearly every install problem is one of these two. Both are explained below.

---

## Docker, in one command

Enough to see the UI and click around. Not enough to import anything — there
is no library or downloads volume here on purpose, so nothing can land in the
wrong place while you are still deciding.

```bash
docker run -d --name romarr \
  --restart unless-stopped \
  -p 6868:6868 \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -v /srv/romarr/config:/config \
  ghcr.io/blizzhacker/romarr:latest
```

Open <http://localhost:6868> and set a password.

Two details that are easy to get wrong and cost an afternoon:

- **`--restart unless-stopped` is not optional.** Without it ROMarr does not
  come back after a host reboot, and the first sign is a week of missed
  scheduled searches.
- **Use an absolute path for `/config`.** `-v ./config:/config` works on Docker
  Engine 23 and newer, and on anything older is rejected as an invalid volume
  name — relative bind sources are a compose feature that `docker run` only
  learned later.

When you are ready to import, move to compose rather than growing this command.

---

## Docker Compose

The recommended install.

```bash
mkdir -p /srv/romarr && cd /srv/romarr
curl -O https://raw.githubusercontent.com/BlizzHacker/romarr/main/docker-compose.yml
```

Create a `.env` beside it with your two host paths:

```ini
ROMARR_ROMS=/mnt/roms
ROMARR_DOWNLOADS=/mnt/downloads
```

Then:

```bash
docker compose up -d
docker compose logs -f      # ctrl-C when you see "ROMarr listening on"
```

Open <http://localhost:6868>.

### Why those two paths refuse to default

Every other setting in the compose file can be wrong and you will find out:
a bad Prowlarr key shows up red on the Settings page, an unreachable library
is reported as unreachable. The two volume paths are the exception, because
Docker **creates** a missing bind-mount source. A placeholder such as
`/path/to/roms` therefore produces a container that starts, reports healthy,
and files ROMs into a directory no library server has ever looked at — with
every indicator green.

So they have no default and compose stops:

```
error while interpolating services.romarr.volumes.[]: required variable
ROMARR_ROMS is missing a value: set ROMARR_ROMS to your library root on the
host, e.g. ROMARR_ROMS=/mnt/roms
```

That is the intended behaviour, not a broken file.

### The downloads path, specifically

Left side of the mount is the host. **The right side must be the path your
download client reports, character for character.** ROMarr asks the client
where a finished download is and then opens that path itself, so the two have
to agree.

- Files at `/mnt/downloads/complete` on the host, SABnzbd reports
  `/downloads/complete` → `- /mnt/downloads/complete:/downloads/complete`
- Client running on the host rather than in a container → it reports host
  paths, so both sides are identical: `- /mnt/downloads:/mnt/downloads`

Where to look it up: qBittorrent → *Options → Downloads → Save path*.
SABnzbd → *Config → Folders → Completed Download Folder*. ROMarr uses whatever
the client's API returns, which is that setting.

If the two genuinely cannot be made to match — a client on another machine —
leave the mount alone and add a **remote path mapping** under *Settings → Media
Management*:

```json
"remote_path_mappings": [
  { "remote": "/downloads", "local": "/mnt/downloads" }
]
```

Longest matching prefix wins, and the log records both the path the client
reported and what ROMarr resolved it to.

### PUID / PGID

ROMarr writes ROM files that another application has to read. Set these to the
same user your library server runs as (`id -u` / `id -g`), or those files land
owned by root and your library either cannot read them or has to run as root
too.

Only `/config` is chowned. The library and downloads volumes are never touched:
they can be multiple terabytes on a NAS, a recursive chown at every boot would
take hours, and the ownership there is one you chose on purpose.

If you run the container with `--user`, rootless Docker, Podman userns, or
Kubernetes `runAsUser`, PUID/PGID are ignored and logged as ignored — there is
nothing to drop to and no authority to chown with.

### Which tag to use

| Tag | What it is |
|---|---|
| `latest` | **Main.** Every push to the default branch that passed the test suite. This is the one that gets fixes. |
| `0.8`, `0` | The latest image built from a `v0.8.x` / `v0.x` git tag. |
| `0.8.0` | Exactly the commit tagged `v0.8.0`. |
| `sha-5cb6a75` | One specific commit. Every build publishes one. |

Two things worth knowing before you pin:

- **`latest` is main, not a release.** It is ahead of `0.8.0`, and the version
  string does not change between releases, so `0.8.0` and `latest` can both
  report `0.8.0` while being different builds. `docker image inspect` on the
  digest is the only reliable comparison.
- Pinning `0.8.0` means you stop receiving fixes until the next tag. For a
  service that reaches out to your indexers, `latest` is the recommended
  default; use a `sha-` tag when you need to sit on a known-good build, and
  see [Rolling back](#rolling-back).

The image is published for `linux/amd64`, `linux/arm64` and `linux/arm/v7`.
**The armv7 leg has never been booted by the maintainer** — it is built in CI
and the published image does contain the right things (32-bit ARM `python3.13`
and `bsdtar`, with `rom_hub` deliberately absent, which is why the Hub tab
reports plugins as unavailable there). A field report from a real 32-bit ARM
board is worth more than another CI build.

---

## Proxmox LXC

On a Proxmox VE host, as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/BlizzHacker/romarr/main/proxmox/ct/romarr.sh)"
```

It creates an unprivileged Debian 13 container, installs ROMarr into
`/opt/romarr` with a virtualenv, writes a systemd unit, starts it, and then
**waits for `/api/health` to answer before claiming success**. If it does not
answer, the script fails and tells you which `journalctl` to read.

Every default is overridable from the environment, so an unattended install is
one line:

```bash
CTID=123 DISK=8 RAM=1024 ROM_PATH=/mnt/roms \
  bash -c "$(curl -fsSL .../proxmox/ct/romarr.sh)"
```

| Variable | Default | |
|---|---|---|
| `CTID` | next free id | |
| `HOSTNAME_` | `romarr` | |
| `DISK` / `CPU` / `RAM` | `4` GB / `1` / `512` MB | |
| `BRIDGE` | `vmbr0` | |
| `STORAGE` | first **active** storage accepting `rootdir` | |
| `OSVERSION` | `13` | Debian |
| `UNPRIVILEGED` | `1` | |
| `ROM_PATH` | `/mnt/roms` | Also added to the unit's `ReadWritePaths` |
| `APP_PORT` | `6868` | |
| `REPO` | `BlizzHacker/romarr` | For forks |

The script is deliberately self-contained. It used to source community-scripts'
`build.func`, which then fetched `install/<app>.sh` from *their* repository —
so the documented command 404'd, and their rename from ProxmoxVE to ProxmoxVED
broke it once already. It now answers to nobody but this repo.

### What it refuses to do

It installs the latest GitHub *release*, falling back to `main`. Before it
declares success it checks the tree it unpacked actually contains
`romarr/auth.py`, and re-fetches from `main` if not.

That check is not theoretical: **v0.7.0 was tagged before authentication
existed**, so a release-tracking installer would have left an install whose API
answered anyone who could reach the port, on a service that queues downloads
and writes to the filesystem. See [SECURITY.md](../SECURITY.md#supported-versions).

If you see `no published release; using main`, that is normal and safe. It can
also mean GitHub rate-limited the lookup (60 requests/hour per IP,
unauthenticated) — the fallback is the same either way, and the auth check
still runs.

### Configuring it

Settings live in `/opt/romarr/.env`, mode 600. Edit it, then:

```bash
systemctl restart romarr
```

State is `/opt/romarr/romarr.json`. Logs are `journalctl -u romarr -f`.

**ROMarr runs as root inside that container.** The unit confines it —
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, and
`ReadWritePaths` limited to `/opt/romarr` and your ROM path — but there is no
unprivileged service user, so imported ROMs are written root-owned (mode 644,
so still world-readable). If your library server reads them over an NFS export
with `root_squash`, that is the thing to know.

---

## Home Assistant

*Settings → Add-ons → Add-on Store → ⋮ → Repositories*, add
`https://github.com/BlizzHacker/romarr`, install **ROMarr**.

It runs the same image Docker users run — ROMarr reads Home Assistant's
`/data/options.json` natively, so there is no add-on-specific build and no
drift between the two. Every option key is upper-cased into an environment
variable (`prowlarr_url` → `PROWLARR_URL`), which means anything in the
[configuration reference](#configuration-reference) can be set on the add-on
page even if it is not in the schema.

Set `romarr_password` before starting. ROMs default to `/media/roms`, which is
Home Assistant's `media` share — where a RomM or Jellyfin add-on can also see
them.

---

## From source

Python 3.11 or newer. Dependencies are `requests` plus, on Linux, `pyseccomp`.

```bash
git clone https://github.com/BlizzHacker/romarr.git && cd romarr
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit it
set -a; . ./.env; set +a      # nothing loads .env for you -- no dotenv dependency
python -m romarr
```

State goes to `/opt/romarr/romarr.json` unless you set `ROMARR_DATA`. On a
laptop you almost certainly want to.

Install `bsdtar` (`libarchive-tools` on Debian/Ubuntu, `libarchive` on Alpine
and Arch, `brew install libarchive` on macOS) or every disc-platform import
fails on a format it cannot open — the PlayStation, PS2 and Wii sets ship
almost entirely as `.7z`, and busybox/GNU `tar` is not a substitute.

---

## First run: claiming the install

ROMarr requires a credential. There is no open mode you can fall into by
forgetting to configure something.

A fresh install is **unclaimed**. The first visit to the web UI asks you to set
a password; that is the whole of first-run setup. Once set, the install is
claimed, that screen becomes a normal sign-in, and the password survives
restarts. Your browser then holds an HMAC-signed, `HttpOnly`, `SameSite=Strict`
session cookie, so the credential is never kept in the page.

`POST /api/v1/setup` is open only while unclaimed and answers `409` afterwards
— a one-shot, not a standing unauthenticated password reset.

**To leave no unclaimed window at all**, claim it from the environment before
it serves a request. This is what a container template should do:

```yaml
ROMARR_PASSWORD: choose-something-long
```

### The API key

For scripts and other \*arrs. One is generated on first run. Three ways to
present it:

```bash
curl -H "X-Api-Key: $KEY"             http://localhost:6868/api/v1/game
curl -H "Authorization: Bearer $KEY"  http://localhost:6868/api/v1/game
curl "http://localhost:6868/api/v1/game?apikey=$KEY"
```

The query form exists for senders that cannot set headers; it will appear in
proxy logs, so prefer a header.

Where to find it:

- *Settings → General* in the UI, or
- `GET /api/v1/system/apikey` with an existing credential, or
- `settings._api_key` in the state file, if you have shell access and no way in.

Set `ROMARR_API_KEY` to pin it to a value you choose. Doing so also counts as
claiming the install.

**Locked out?** An API key also signs a browser in, via *Use an API key
instead* on the sign-in screen. So: set `ROMARR_API_KEY` in the environment,
restart, sign in with it, and set a new password.

TOTP is enrolled from *Settings → General* and applies to interactive sign-in
only. It deliberately does not gate the API key — a script cannot be prompted,
and a key is already a high-entropy secret.

---

## The rule that catches everyone

**Environment variables seed the configuration on the first run. After that,
the Settings page is the authority and the environment is ignored.**

This is deliberate — a decision you saved in the UI must not be silently undone
by a restart — but it means:

```bash
# edit QBITTORRENT_URL in docker-compose.yml
docker compose up -d
# ...and nothing changes. No error. No warning.
```

Measured, not assumed: with a state file already written, starting the
container with `QBITTORRENT_URL`, `PROWLARR_URL` and `LIBRARY_PATH` all changed
to new values, `/api/v1/system/status` still reported the originals.

Once ROMarr has run once, change these on the **Settings** pages:

- download clients, indexers and libraries → *Settings → Download Clients /
  Indexers / Libraries*
- the library path → *Settings → Media Management*

The variables that are read on **every** start, and can be changed in the
environment at any time:

`ROMARR_PORT`, `ROMARR_DATA`, `ROMARR_PASSWORD`, `ROMARR_API_KEY`,
`ROMARR_AUTH` and the SSO set, `ROMARR_SSL_CERT` / `ROMARR_SSL_KEY`,
`ROMARR_PLAYERS`, `ROMARR_JSDOS_URL`, `ROMARR_EMULARITY_URL`, `LOG_LEVEL`,
`DAT_PATH`, the `MOONLIGHT_*` and `WOLF_*` set, `STREAM_SERVER_URL`,
`GGREQUESTZ_URL`, `PUID`, `PGID`, `TZ`.

ROMarr does tell you about this in the one case where it can be certain: if
the stored library path does not exist but the one in the environment does, the
status page says so and names the Settings page as the fix.

---

## Configuration reference

Every variable ROMarr reads. "Seeded" means first-run only — see
[the rule above](#the-rule-that-catches-everyone).

### Core

| Variable | Default | If it is wrong |
|---|---|---|
| `ROMARR_PORT` | `6868` | Nothing listens where you expect. Change the published port to match. |
| `ROMARR_DATA` | `/opt/romarr/romarr.json` (`/config/romarr.json` in Docker) | Points at a new file → a fresh, unclaimed install with a new API key. Points at an unreadable file → **ROMarr refuses to start** and says so, rather than overwriting it. |
| `LOG_LEVEL` | `INFO` | `DEBUG` for the full request trace. |
| `PUID` / `PGID` | `1000` / `1000` | Imported ROMs are owned by the wrong user; your library server may not be able to read them. Docker only. |
| `TZ` | `Etc/UTC` | Timestamps and schedule times are in the wrong zone. |
| `ROMARR_BACKENDS_DIR` | unset | Drop-in library backends. Operator-owned; the Proxmox updater never replaces it. |
| `ROM_HUB_HOME` | `/opt/romarr/.rom-hub` (`/config/rom-hub` in Docker) | Installed plugins live here. Inside the container filesystem rather than a volume means they are lost on every `docker compose pull`. |

### Authentication

| Variable | Default | If it is wrong |
|---|---|---|
| `ROMARR_PASSWORD` | unset | Unset is fine — the first visitor sets it. Set, it claims the install at startup with no setup screen. |
| `ROMARR_API_KEY` | generated | Setting it pins the key and claims the install. |
| `ROMARR_AUTH` | unset (password/key) | `forward` for SSO; `disabled` turns the gate off entirely, meaning **anything that reaches the port is in**, including a request that bypassed your proxy. |
| `ROMARR_TRUSTED_PROXIES` | unset | **Required for `forward`.** Without it anyone could send the identity header themselves. |
| `ROMARR_SSO_PROVIDER` | `authentik` | Also `authelia`, `cloudflare`, `oauth2-proxy`. |
| `ROMARR_SSO_USER_HEADER` / `_GROUPS_HEADER` | per provider | Override the provider's defaults. |
| `ROMARR_SSO_GROUP` | unset | Require membership of this group. |
| `ROMARR_SSL_CERT` / `ROMARR_SSL_KEY` | unset | Serve HTTPS directly. If the pair cannot be loaded ROMarr logs the error and continues on plain HTTP — check the log rather than assuming TLS is on. |

### Indexer

| Variable | Default | If it is wrong |
|---|---|---|
| `PROWLARR_URL` | unset | *Seeded.* No searches. The Settings page shows Prowlarr as unconfigured. |
| `PROWLARR_API_KEY` | unset | *Seeded.* Every search returns nothing; Prowlarr logs a 401. |

### Download clients

At least one is required to grab anything. All *seeded*.

| Variable | Default |
|---|---|
| `QBITTORRENT_URL` / `_USER` / `_PASS` | unset |
| `SABNZBD_URL` / `SABNZBD_API_KEY` | unset |
| `NZBGET_URL` / `NZBGET_USER` / `NZBGET_PASS` | unset |
| `QBITTORRENT_CATEGORY` / `SABNZBD_CATEGORY` / `NZBGET_CATEGORY` | `romarr` |

Transmission, Deluge, rTorrent, Synology Download Station and Real-Debrid are
added from *Settings → Download Clients* rather than the environment.

The category does **not** have to exist in the client first — SABnzbd keeps an
undefined one verbatim and ROMarr still matches it. Define it there anyway if
you want its own folder or a post-processing script.

If a protocol has no client, releases of that protocol are ranked and then
refused. The Download Clients page names any protocol left uncovered.

### Library

| Variable | Default | If it is wrong |
|---|---|---|
| `LIBRARY_KIND` | `romm` | Also `gaseous`, `retrom`, `folder`. |
| `LIBRARY_URL` | unset | *Seeded.* Required except for `folder`. |
| `LIBRARY_USERNAME` / `LIBRARY_PASSWORD` | unset | *Seeded.* Use a **dedicated** account, not your admin one — ROMarr only reads the library and triggers a rescan. |
| `LIBRARY_API_KEY` | unset | *Seeded.* Alternative to username/password. |
| `LIBRARY_PATH` | `/mnt/roms` | *Seeded.* The library root **as ROMarr sees it** — `/roms` in Docker. Wrong, and imports either fail or land where nothing scans. |
| `DAT_PATH` | unset | A directory of No-Intro/Redump DATs. Point it at a directory holding **only** DATs. Pointed at a ROM library it used to hang startup for ten minutes; it now scans three levels deep, stops after 40,000 files, and says on the status page that it stopped early — which is a warning, not a working configuration. |

The older `ROMM_URL`, `ROMM_USERNAME`, `ROMM_PASSWORD`, `ROMM_API_TOKEN` and
`ROMM_LIBRARY` names are still read, so an existing install needs no changes.
Do not set both names for the same thing — the new one wins.

### Optional integrations

| Variable | Default | |
|---|---|---|
| `GGREQUESTZ_URL` | unset | Request front-end, shown on the System page |
| `STREAM_SERVER_URL` | unset | Headless RetroArch. Read-only: ROMarr asks which platforms it can play, and reports PS2/GameCube/Wii/Dreamcast/3DS as playable instead of download-only |
| `MOONLIGHT_HOST` | unset | A Wolf, Sunshine or Steam Headless machine, e.g. `192.168.0.50` |
| `MOONLIGHT_KIND` | `wolf` | Also `sunshine`, `steam-headless`. Not sniffed — `/serverinfo` cannot tell them apart |
| `MOONLIGHT_USER` / `MOONLIGHT_PASS` | unset | Sunshine/Steam Headless admin credentials. Never written to the state file |
| `WOLF_SOCKET_PATH` / `WOLF_API_URL` | unset | Wolf's API is a UNIX socket — mount `wolf.sock`, or use the nginx proxy Wolf's own docs describe |
| `STEAM_HEADLESS_URL` | unset | The container's noVNC/neko desktop, surfaced as a link |
| `ROMARR_PLAYERS` | all four | `emulatorjs,ruffle,jsdos,emularity`, best first. `none` turns every browser route off. Empty means all four, not none |
| `ROMARR_JSDOS_URL` / `ROMARR_EMULARITY_URL` | unset | Where your own js-dos and Emularity live. Without one, ROMarr reports that the player *would* run a file and names the setting that would let it link there |
| `ROMARR_PUBLIC_URL` / `ROMARR_PEER_NAME` | unset / `ROMarr` | How a friend's server reaches this one. Peering is the only feature that needs ROMarr to know its own address |

---

## Where your data lives

| | Docker | Proxmox LXC / source |
|---|---|---|
| Settings, history, API key, password hash | `/config/romarr.json` | `/opt/romarr/romarr.json` |
| Installed plugins | `/config/rom-hub/` | `/opt/romarr/.rom-hub/` |
| Credentials for other services | in the environment | `/opt/romarr/.env` (mode 600) |
| Drop-in backends | `ROMARR_BACKENDS_DIR` | `/opt/romarr/backends/` |
| ROMs | your library volume | your `ROM_PATH` |

**`romarr.json` is the whole install.** Everything else is either replaceable
(the code) or yours already (the ROMs). It is a single JSON file written
atomically — to a sibling temp file and renamed — so a crash mid-write cannot
leave truncated JSON that will not start.

### The ownership trap

If that file ends up owned by a user the container does not run as — an install
that once ran without `PUID`, or a backup restored with `cp` as root — ROMarr
cannot read it.

Older versions responded by starting from defaults and then saving over it: the
API key was regenerated, the history emptied, and **the install came back
unclaimed**, so the next person to reach the port set the password. One
`WARNING` line was the only sign.

That is now two guards. The Docker entrypoint chowns `/config` recursively and
refuses to start if the state file still is not readable; the application
refuses to start on an unreadable state file however it was launched, saying:

```
/config/romarr.json exists but cannot be read ([Errno 13] Permission denied).
This file holds the API key, the password hash and the request history;
starting from defaults would overwrite it and leave the install unclaimed.
Fix the ownership or permissions -- in Docker, PUID/PGID must own /config and
everything inside it -- and start ROMarr again.
```

A file that is *unparseable* rather than unreadable still starts from defaults,
because there is nothing left in it to preserve.

---

## Backing up

Two things, and only one of them is subtle.

**1. The state file.** Copy it while ROMarr is stopped, or accept that a copy
taken mid-write is a copy of the previous version (writes are atomic, so it is
never a torn file).

```bash
docker compose stop
cp /srv/romarr/config/romarr.json /somewhere/safe/romarr-$(date +%F).json
docker compose start
```

**2. Or use the API**, which produces a portable snapshot rather than a file
you have to find:

```bash
# Credentials stripped -- safe to email to somebody helping you debug
curl -H "X-Api-Key: $KEY" http://localhost:6868/api/v1/backup > romarr-backup.json

# With credentials -- treat this as a secret
curl -H "X-Api-Key: $KEY" "http://localhost:6868/api/v1/backup?secrets=1" > romarr-full.json
```

Restore either one:

```bash
curl -X POST -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
     --data-binary @romarr-full.json http://localhost:6868/api/v1/restore
```

Verified round trip: back up, change a setting, restore, setting is back.

Two things the restore does that are worth knowing before you need them:

- Restoring a **credential-free** backup answers with a warning saying download
  clients, indexers and libraries will need their passwords again — and it
  blanks the API key. Your password still works; sign in and set a new key
  under *Settings → General*.
- A backup carries **settings**, not history. History lives in the same file
  but outside the backup payload — copy `romarr.json` if you want the events.

What you do **not** need to back up: the ROMs (that is your library server's
problem), the container, or `/opt/romarr` beyond `.env` and `romarr.json`.

---

## Upgrading

### Docker

```bash
cd /srv/romarr
docker compose pull
docker compose up -d
```

Your `/config` volume carries settings, history and credentials across. Nothing
in the image is stateful.

If you track `latest`, that is main — every push that passed the suite. An
image pulled at a bad moment is rolled back the same way as anything else, see
below.

Running Watchtower or similar? It will update ROMarr whenever `latest` moves.
That is a defensible choice for a service on your own LAN and a bad one if you
need to know what changed; pin a `sha-` tag if you would rather decide.

### Proxmox LXC

```bash
pct exec <ctid> -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/BlizzHacker/romarr/main/proxmox/ct/update.sh)"
```

or the same `bash -c "$(curl ...)"` from inside the container.

It reports the version it is moving you from and to, copies `romarr.json`,
`.env`, `backends/` **and the running build** to `/opt/romarr-backup-<stamp>`,
refuses to install a tree without `romarr/auth.py`, replaces the `romarr/`
package rather than merging over it — a merge leaves modules that upstream
deleted sitting in an importable package, which is how a version reports itself
as new while still running old code — and if the new build does not answer
`/api/health`, **puts the old one back and restarts it** before telling you.

Delete the backup directory once you are happy.

### Home Assistant

The add-on store offers the update when the add-on version changes. The image
itself is the same one Docker users get.

---

## Rolling back

### Docker

Every build publishes a `sha-` tag, so any commit that was ever `latest` can be
pinned:

```bash
docker compose down
# in docker-compose.yml:  image: ghcr.io/blizzhacker/romarr:sha-5cb6a75
docker compose up -d
```

List what exists:

```bash
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:blizzhacker/romarr:pull" \
        | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
curl -s -H "Authorization: Bearer $TOKEN" \
     https://ghcr.io/v2/blizzhacker/romarr/tags/list
```

**A rollback is code-only.** If the version you are leaving wrote a setting the
older one does not understand, the older one drops it — unknown keys are not
stored — so roll back with a copy of `romarr.json` in hand.

### Proxmox LXC

`update.sh` rolls itself back if the new build will not answer. To go back
later, by hand:

```bash
systemctl stop romarr
rm -rf /opt/romarr/romarr
cp -a /opt/romarr-backup-<stamp>/romarr /opt/romarr/
cp -a /opt/romarr-backup-<stamp>/romarr.json /opt/romarr/
systemctl start romarr
```

---

## Troubleshooting

Every entry here is a failure that actually happened, to this project or to
somebody running it.

### It will not start

| What you see | What it is | Fix |
|---|---|---|
| `romarr.json exists but cannot be read` then exit 1 | The state file is owned by a user the container does not run as. Previously this silently reset the install. | `chown -R 1000:1000` (your PUID/PGID) on the directory you mounted at `/config`. Do not delete the file. |
| `cannot change ownership of /config` | A read-only mount, or a filesystem that does not carry uids — some SMB/CIFS shares. | Mount `/config` from a directory the container can own, or run with `--user` so no chown is attempted. |
| `Error response from daemon: create ./config` | Docker older than 23 rejects a relative bind source. | Use an absolute path. |
| Container restarts in a loop, `exit=1` | Almost always the state-file permission error above — read the logs, the message names the file. | |
| `error while interpolating services.romarr.volumes.[]` | `ROMARR_ROMS` or `ROMARR_DOWNLOADS` is unset. Deliberate — see [above](#why-those-two-paths-refuse-to-default). | Put them in `.env`. |
| Proxmox: installed but not answering | The script already told you where to look. | `pct exec <ctid> -- journalctl -u romarr -n 50 --no-pager` |

### It starts, but nothing works

| What you see | What it is | Fix |
|---|---|---|
| Editing a URL in compose changes nothing | The environment seeds on first run only. | Change it on the Settings page. [Explained above](#the-rule-that-catches-everyone). |
| `ROM library: Not available /mnt/roms` | Either the volume is mounted somewhere else, or a path stored on first run outranks the environment. | ROMarr distinguishes these on the status page and names which one it is. If the stored path is the problem, fix it in *Settings → Media Management*. |
| `Download path does not exist` while the file is plainly there | The client reports a path ROMarr cannot see. | Make the container-side download path match what the client reports, or add a remote path mapping. |
| Results found, then refused | No download client speaks that release's protocol. | Add a torrent and/or usenet client — the Download Clients page names the gap. |
| Imported ROM never appears in the library | The rescan was refused. | RomM: grant the account task permission. Or the ROM landed outside the tree your library scans. |
| Every PlayStation / PS2 / Wii import fails on the archive | `bsdtar` is missing. Those sets ship as `.7z`, and busybox/GNU `tar` is not a substitute — ROMarr will not pretend otherwise. | Docker and the Proxmox installer include it. From source: install `libarchive-tools`. |
| Hub tab says plugins are unavailable | `rom-hub` is not installed. Expected on armv7, where its `pydantic` dependency has no musl wheel. | Elsewhere: `pip install "rom-hub @ git+https://github.com/BlizzHacker/rom-hub@master"` |
| ROM imports but will not play in the browser | The platform has no emulator core in your library's web player. | Expected. The ROM is catalogued, not playable in-browser. |
| Scheduled jobs never run on a freshly rebooted host | Fixed in `5cb6a75`. "Never run" was recorded as time `0.0`, and `time.monotonic()` counts from boot on Linux, so on a machine with less uptime than the job's interval every job looked "not due". | Update. |

### Health and monitoring

`/api/health` answers `{"ok": true}` without a credential — that is what the
container `HEALTHCHECK` uses — and returns the full report, with library paths
and client URLs, only to an authenticated caller. The split exists because the
full report was free reconnaissance for anyone who could reach the port.

The healthcheck does catch a genuinely wedged process: `SIGSTOP` on the
container's main process moves it to `unhealthy` after three consecutive
failures, about 100 seconds.

**Docker does not restart an unhealthy container.** `restart: unless-stopped`
acts on the process *exiting*, not on the healthcheck failing. So `unhealthy`
is a signal for whatever you monitor with, not a self-heal. If you want a
wedged ROMarr restarted automatically, that is an autoheal sidecar or your
orchestrator's job.

### Getting a log

```bash
docker logs romarr --tail 200            # Docker
journalctl -u romarr -n 200 --no-pager   # Proxmox LXC / systemd
```

`LOG_LEVEL=DEBUG` adds the full request trace. The UI has a live tail under
*System → Logs*.

---

## Uninstalling

**Docker.** `docker compose down` stops it; add `-v` to remove named volumes.
The `./config` directory and your ROMs are not touched — delete them yourself
if you mean to.

**Proxmox LXC.** `pct stop <ctid> && pct destroy <ctid>`. That takes
`/opt/romarr` with it. If your ROM path was a bind mount from the host, the
ROMs survive; if it was inside the container's disk, they do not. Check with
`pct config <ctid>` before you destroy anything.

**From source.** Remove the checkout and `ROMARR_DATA`.

ROMarr never edits your library server's database and never deletes ROMs it
did not import, so removing it leaves the library exactly as it was.
