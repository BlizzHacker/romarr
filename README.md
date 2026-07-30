# Romarr

A project of the [Move Weight Foundation](https://foundation.moveweight.com), an
Oklahoma non-profit corporation with 501(c)(3) status pending.

The *arr for games. Request a title, Romarr finds it, grabs it, and files it
into your game library.

**It is not tied to one library.** RomM, [Gaseous](https://github.com/gaseous-project/gaseous-server)
and [Retrom](https://github.com/jmberesford/retrom) are all supported, chosen
with a single setting:

    LIBRARY_KIND=romm      # or gaseous, or retrom
    LIBRARY_URL=http://your-library:8080
    LIBRARY_API_KEY=...    # or LIBRARY_USERNAME / LIBRARY_PASSWORD

Existing installs need no changes: `LIBRARY_KIND` defaults to `romm` and the
old `ROMM_*` variables are still read.

Adding a backend means implementing four methods -- reachable, count, games,
rescan -- in `romarr/libraries.py`. Nothing else in the service learns which
library is attached.


**The *arr for games.** Romarr watches for game requests, searches your
indexers through Prowlarr, hands the winner to your download client, and files
the ROM into [RomM](https://github.com/rommapp/romm) where it can actually be
played.

If you run Radarr for films and Sonarr for TV, this is the missing one.

```
GG Requestz  ──request──▶  Romarr  ──search──▶  Prowlarr ──▶ indexers
                              │                                  │
                              │◀──────────── releases ───────────┘
                              │
                              ├──grab──▶  qBittorrent / NZBGet
                              │
                              └──import──▶  RomM library  ──▶  playable
```

## Why it exists

Radarr and Sonarr solved "request a thing, get the thing, filed correctly."
Nobody had done it for ROMs, and ROMs have their own problems that a film
downloader does not:

- **The wrong file is usually in the box.** Game releases ship readmes, box art,
  and often several regional dumps in one archive. Importing `readme.nfo` into
  your library helps nobody.
- **Size is a signal, not a preference.** A 40 GB "SNES" result is a romset, a
  PC port, or a bad match. For a cartridge, *smaller is usually more correct* —
  the opposite of video.
- **Platform names are ambiguous.** "Super Nintendo Entertainment System"
  contains "Nintendo Entertainment System". Naive matching sends every SNES
  request to the NES folder. (There is a test for this, because it happened.)

## What it does

- Searches **Prowlarr**, filtered to console and PC-game categories at the query
  rather than after, so films never enter the ranking in the first place
- **Scores releases** on seeders, region (USA → World → Europe → Japan), and
  size sanity, and rejects prototypes, hacks and demos
- Grabs via **qBittorrent** (torrent), **SABnzbd** or **NZBGet** (usenet),
  routing each release to a client that speaks its protocol
- **Picks the ROM** out of the finished download, preferring the platform's own
  extensions in order and the best regional dump when several are present
- **Imports into RomM** at `<library>/<platform-slug>/`, refusing to overwrite
  unless told
- Triggers a RomM rescan so the game appears without a manual refresh
- **Translates download paths** when the client runs elsewhere, the way Radarr
  and Sonarr's remote path mappings do — without it the import fails with
  "download path does not exist" while the file sits right there
- Keeps **History, Wanted and settings** across a restart
- A web UI shaped like the other *arrs: Library, Wanted, Activity, Settings,
  System

## Safety, deliberately

- **Zip-slip is blocked.** Archive entries that resolve outside the library root
  are dropped, not sanitised — a release that needs sanitising is not one to
  trust. Tested.
- **Prowlarr API keys never reach a browser or a log.** Prowlarr's
  `downloadUrl` is a link back to itself *with the key in the query string*.
  That URL is passed to the download client, which fetches it server-side —
  the same thing Radarr and Sonarr do, and the only way usenet can work at all,
  since an NZB has no magnet equivalent. It is redacted everywhere it could be
  displayed or logged. Tested.
- **Nothing overwrites silently.** An existing ROM is left alone and reported.

## Supported platforms

Cartridge-era systems, deliberately: NES, SNES, Game Boy / Color / Advance,
N64, Genesis / Mega Drive, Master System, Game Gear, Atari 2600 / 7800, Lynx,
TurboGrafx-16, WonderSwan, Neo Geo Pocket, Virtual Boy.

Disc-based platforms are excluded on purpose. Their images are gigabytes, often
multi-file, and cannot be streamed into a browser emulator — which is the point
of feeding RomM in the first place.

## Install

### Docker

```bash
docker run -d --name romarr -p 7878:7878 \
  -e PUID=1000 -e PGID=1000 \
  -e PROWLARR_URL=http://prowlarr:9696 -e PROWLARR_API_KEY=... \
  -e LIBRARY_URL=http://romm:8080 -e LIBRARY_USERNAME=romarr -e LIBRARY_PASSWORD=... \
  -e QBITTORRENT_URL=http://qbittorrent:8080 \
  -v ./config:/config -v /path/to/roms:/roms -v /path/to/downloads:/downloads \
  ghcr.io/blizzhacker/romarr:latest
```

There is a [`docker-compose.yml`](docker-compose.yml) with every setting
commented, which is the easier way in. Images are published for `linux/amd64`,
`linux/arm64` and `linux/arm/v7`, so the same tag runs on a NAS, a Pi or a
server.

Three things worth knowing:

- **`PUID`/`PGID` decide who owns the ROMs.** Romarr writes files that your
  library application has to read. Give it the same ids as that application, or
  `id -u` / `id -g` for your media user. Only `/config` is chowned — never the
  library or downloads volumes, because a recursive chown of a multi-terabyte
  share at every boot is not something an image should do to you.
- **Mount the downloads volume at the path your download client reports.** If
  qBittorrent says `/downloads/complete`, mount it there here too. Otherwise
  the import looks for a finished file where it is not, and you get "download
  path does not exist" while the file sits in plain sight. If matching the path
  is impossible, set a mapping under Settings → Media Management instead.
- **`/config` holds the decisions you make in the UI**, and a setting saved
  there outranks the environment from then on. That is deliberate — an
  environment default must not silently undo a choice — but it means changing
  `LIBRARY_PATH` after first run has no effect. Change it on the Settings page.

### Proxmox LXC

One line:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/BlizzHacker/romarr/main/proxmox/ct/romarr.sh)"
```

### From source

```bash
git clone https://github.com/BlizzHacker/romarr.git && cd romarr
pip install -r requirements.txt
cp .env.example .env    # then edit it
set -a; . ./.env; set +a    # nothing loads .env for you -- no dotenv dependency
python -m romarr
```

## Configuration

| Variable | What it is |
|---|---|
| `PROWLARR_URL` / `PROWLARR_API_KEY` | your Prowlarr, for searching |
| `LIBRARY_KIND` | `romm`, `gaseous` or `retrom` (default `romm`) |
| `LIBRARY_URL` / `LIBRARY_USERNAME` / `LIBRARY_PASSWORD` | your game library, for the rescan trigger. `ROMM_*` still works |
| `LIBRARY_PATH` | path to the library root, e.g. `/mnt/roms`. Formerly `ROMM_LIBRARY`, still accepted |
| `QBITTORRENT_URL` / `QBITTORRENT_USER` / `QBITTORRENT_PASS` | torrent client |
| `SABNZBD_URL` / `SABNZBD_API_KEY` | usenet client, optional |
| `NZBGET_URL` / `NZBGET_USER` / `NZBGET_PASS` | usenet client, optional |
| `QBITTORRENT_CATEGORY` / `SABNZBD_CATEGORY` / `NZBGET_CATEGORY` | what Romarr labels its own downloads with, per client. Default `romarr` |
| `GGREQUESTZ_URL` | shows the request front-end's status, optional |
| `ROMARR_DATA` | where history and settings are kept |
| `PUID` / `PGID` / `TZ` | Docker image only — see above |

A protocol with no client configured cannot be grabbed at all — results are
found, ranked, and then refused. The Download Clients page names any protocol
in that state rather than leaving you to work it out.

### Download categories

Romarr labels its downloads `romarr`, so its jobs are distinguishable from
everything else in a client you already use for other things — the same reason
Radarr and Sonarr each use their own.

**The category does not have to exist in the client first.** SABnzbd 5.0.4
ships `*`, `movies`, `tv`, `audio` and `software`, and `romarr` is none of
them; an undefined category is accepted, kept verbatim on the job, and still
matched by the history filter Romarr uses to notice a finished download.
Defining it is still worth doing if you want the download in a folder of its
own or a post-processing script — Romarr does not care either way, because it
takes the finished path from SABnzbd's own `storage` field rather than guessing
where the category put it.

Set `SABNZBD_CATEGORY=software` if you would rather use a built-in, or give two
Romarrs sharing one client different categories so they stop claiming each
other's downloads.

A protocol with no client configured cannot be grabbed at all — results are
found, ranked, and then refused. The Download Clients page names any protocol
in that state rather than leaving you to work it out.

### More than one library

Romarr drives several library servers at once. Add them under **Settings →
Libraries**: each carries its own address, credentials and filesystem path, and
one is marked default.

Routing is by platform. A library with platform rules receives only those
platforms; anything unmatched goes to the default — so "N64 goes to Retrom" is
one row on that page rather than a second Romarr. A platform rule beats the
default, because naming a platform is the more specific statement. Two servers
of the *same* kind are fine, and are the common case: a RomM for the house and a
RomM for a child's device.

Each server needs its own path, **as Romarr sees it**. Two libraries may share a
path if you genuinely point both applications at one tree, but nothing assumes
it. The Libraries page flags a server that answers while its path is missing
here, which is almost always a volume that was never mounted into Romarr.

The environment seeds the first library on first run, so an existing install
comes up already configured. Everything after that is managed on the page.

The Library view aggregates every server and badges each poster with its source;
one unreachable server is reported as a named row rather than an empty shelf.

#### Gaseous appears on its own schedule

Gaseous has no scan trigger — there is no such call in its API. It picks up new
files through two background tasks of its own: `TitleIngestor` every minute,
which processes Gaseous's **Import** directory, and `LibraryScan` every 1440
minutes, which walks the configured library paths. A ROM filed into a Gaseous
library path can therefore take up to a day to appear, and nothing Romarr sends
changes that. Either point that library's path at Gaseous's Import directory, or
lower `LibraryScan`'s interval in Gaseous.

RomM and Retrom are both told directly and appear at once. For RomM, the account
Romarr uses needs permission to run tasks — without it the rescan is refused with
a 403 and the ROM waits for RomM's next scheduled scan.

### Remote path mapping

If your download client runs in a different container or host, it reports paths
in *its* filesystem. Set the mapping under Settings → Media Management, or in
the settings file:

```json
"remote_path_mappings": [
  { "remote": "/downloads", "local": "/mnt/downloads" }
]
```

The longest matching prefix wins. A mapping cannot help if the volume is not
mounted here at all — that stays a mount problem, and you find out because the
translated path still does not exist.

Use a **dedicated RomM account**, not your admin one. Romarr needs only
enough to trigger a scan.

## Tests

```bash
python -m pytest tests/ -q
```

The decisions that matter — which release to take, which file is the ROM, and
whether an archive entry is safe to extract — are pure functions with no
network, so they are tested directly rather than mocked around.

## Licence

MIT.
