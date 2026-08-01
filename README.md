# ROMarr

<table><tr><td>

**Part of [Cartridge](https://github.com/BlizzHacker/rom-hub/blob/master/BRAND.md) by MoveWeight** — a self-hosted retro-gaming ecosystem. Run the whole stack, not just this piece:

```
MoveWeight
└── Cartridge                                  play + acquire your library
    ├── ROMarr  ─────────────  request a game, it finds / grabs / files it
    │   └── ROM Hub + plugins   backend-agnostic sources (RomM/Gaseous/Retrom)
    └── Apps                    Desktop · Xbox · Roku · Stream server
```

**Acquire:** [ROMarr](https://github.com/BlizzHacker/romarr) · [ROM Hub](https://github.com/BlizzHacker/rom-hub) — **Play:** [Desktop](https://github.com/BlizzHacker/RommForDesktop) · [Xbox](https://github.com/BlizzHacker/RommForXbox) · [Roku](https://github.com/BlizzHacker/RommForRoku) · [Stream](https://github.com/BlizzHacker/RommStreamServer)

<sub>Unofficial; not affiliated with or endorsed by RomM, Gaseous or Retrom.</sub>

</td></tr></table>



The *arr for games. Request a title, ROMarr finds it, grabs it, and files it
into your game library.

**It is not tied to one library.** RomM,
[Gaseous](https://github.com/gaseous-project/gaseous-server),
[Retrom](https://github.com/jmberesford/retrom) and *a plain folder* are all
supported, chosen with a single setting:

    LIBRARY_KIND=romm      # or gaseous, or retrom
    LIBRARY_URL=http://your-library:8080
    LIBRARY_API_KEY=...    # or LIBRARY_USERNAME / LIBRARY_PASSWORD

Existing installs need no changes: `LIBRARY_KIND` defaults to `romm` and the
old `ROMM_*` variables are still read.

`folder` is the one that covers everything else. Batocera, RetroPie, Recalbox,
EmulationStation, ES-DE, EmuDeck, Pegasus, Lakka, muOS, ArkOS, LaunchBox,
Playnite and Steam ROM Manager are not servers with APIs -- they read ROMs out
of a directory laid out by platform, which is exactly what ROMarr writes. No
URL, no account, no API key: point it at the path and it works.

    LIBRARY_KIND=folder
    LIBRARY_PATH=/mnt/roms

Adding a backend means implementing four methods -- reachable, count, games,
rescan -- in `romarr/libraries.py`. Nothing else in the service learns which
library is attached.


**The *arr for games.** ROMarr watches for game requests, searches your
indexers through Prowlarr, hands the winner to your download client, and files
the ROM into [RomM](https://github.com/rommapp/romm) where it can actually be
played.

If you run Radarr for films and Sonarr for TV, this is the missing one.

![Interactive search: every release scored, with the reasoning shown](docs/img/interactive-search.png)

*Interactive search: every release your indexers returned, scored, with the
reason for each score written next to it — and a Grab button, so you can
overrule the ranking when you disagree with it.*

```
GG Requestz  ──request──▶  ROMarr  ──search──▶  Prowlarr ──▶ indexers
                              │                                  │
                              │◀──────────── releases ───────────┘
                              │
                              ├──grab──▶  qBittorrent / NZBGet
                              │
                              └──import──▶  RomM library  ──▶  playable
```

![The library, read from RomM](docs/img/library.png)

*The library itself, read from whichever backend is attached — 72,162 games on
the instance this was captured from.*

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
- **Interactive search**, the way Radarr and Sonarr do it: every release your
  indexers returned, scored, *with the reasoning shown next to it*, and a Grab
  button so you can overrule the ranking. Every scoring rule here is an opinion
  somebody may disagree with; this is where you disagree with it
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

- **`PUID`/`PGID` decide who owns the ROMs.** ROMarr writes files that your
  library application has to read. Give it the same ids as that application, or
  `id -u` / `id -g` for your media user. Only `/config` is chowned — never the
  library or downloads volumes, because a recursive chown of a multi-terabyte
  share at every boot is not something an image should do to you.
- **On the downloads volume, the container side is the one that has to match
  your download client.** In `-v /mnt/downloads/complete:/downloads/complete`
  the left side is wherever the files really live on the host, and the right
  side has to be the path the client *reports* — ROMarr asks the client where a
  finished download is and then opens that path itself, so the two have to
  agree. qBittorrent shows it as "Save path" under Options → Downloads, SABnzbd
  as "Completed Download Folder" under Config → Folders. A client running on the
  host rather than in a container reports host paths, which makes both sides
  identical. Get this wrong and the import looks for a finished file where it is
  not: you get "download path does not exist in this container" while the file
  sits in plain sight. When the paths genuinely cannot be made to match, set a
  mapping under Settings → Media Management instead.
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
| `LIBRARY_KIND` | `romm`, `gaseous`, `retrom` or `folder` (default `romm`) |
| `LIBRARY_URL` / `LIBRARY_USERNAME` / `LIBRARY_PASSWORD` | your game library, for the rescan trigger. `ROMM_*` still works |
| `LIBRARY_PATH` | path to the library root — **inside the container** when using Docker, where the compose files use `/roms`. Formerly `ROMM_LIBRARY`, still accepted. Stored on first run; change it on the Settings page afterwards |
| `QBITTORRENT_URL` / `QBITTORRENT_USER` / `QBITTORRENT_PASS` | torrent client |
| `SABNZBD_URL` / `SABNZBD_API_KEY` | usenet client, optional |
| `NZBGET_URL` / `NZBGET_USER` / `NZBGET_PASS` | usenet client, optional |
| `QBITTORRENT_CATEGORY` / `SABNZBD_CATEGORY` / `NZBGET_CATEGORY` | what ROMarr labels its own downloads with, per client. Default `romarr` |
| `GGREQUESTZ_URL` | shows the request front-end's status, optional |
| `ROMARR_DATA` | where history and settings are kept |
| `PUID` / `PGID` / `TZ` | Docker image only — see above |

A protocol with no client configured cannot be grabbed at all — results are
found, ranked, and then refused. The Download Clients page names any protocol
in that state rather than leaving you to work it out.

### Download categories

ROMarr labels its downloads `romarr`, so its jobs are distinguishable from
everything else in a client you already use for other things — the same reason
Radarr and Sonarr each use their own.

**The category does not have to exist in the client first.** SABnzbd 5.0.4
ships `*`, `movies`, `tv`, `audio` and `software`, and `romarr` is none of
them; an undefined category is accepted, kept verbatim on the job, and still
matched by the history filter ROMarr uses to notice a finished download.
Defining it is still worth doing if you want the download in a folder of its
own or a post-processing script — ROMarr does not care either way, because it
takes the finished path from SABnzbd's own `storage` field rather than guessing
where the category put it.

Set `SABNZBD_CATEGORY=software` if you would rather use a built-in, or give two
ROMarrs sharing one client different categories so they stop claiming each
other's downloads.

A protocol with no client configured cannot be grabbed at all — results are
found, ranked, and then refused. The Download Clients page names any protocol
in that state rather than leaving you to work it out.

### Interactive search

Library → **Interactive Search**. Type a game, pick a platform, and every
release comes back scored, with the reasons written beside it:

    +40  20 seeders
    +60  carries a Super Nintendo ROM extension
    +25  region (USA)
    -120 looks like a hack, beta or repack ('hack')

Rejected releases are listed too, greyed out, because *why* something was
refused is usually the answer you came for:

    a compilation, not a single cartridge (classics)
    names another platform (wii)
    too big for a Sega Genesis / Mega Drive cartridge (452MB, ceiling 32MB)

Then take whichever one you actually wanted. A release grabbed by hand goes
through the same queue, history and Wanted handling as one the scorer picked --
Activity does not care how a game was requested.

The same information is on the API: `GET /api/v1/release?game=…&platform=…`
returns the scored list, and `POST /api/v1/release/grab` takes an id from it.
Download links are deliberately absent from both: Prowlarr's `downloadUrl`
carries its API key in the query string, so the release is grabbed by the id
issued with the search and the URL is looked up server-side.

### More than one library

ROMarr drives several library servers at once. Add them under **Settings →
Libraries**: each carries its own address, credentials and filesystem path, and
one is marked default.

Routing is by platform. A library with platform rules receives only those
platforms; anything unmatched goes to the default — so "N64 goes to Retrom" is
one row on that page rather than a second ROMarr. A platform rule beats the
default, because naming a platform is the more specific statement. Two servers
of the *same* kind are fine, and are the common case: a RomM for the house and a
RomM for a child's device.

Each server needs its own path, **as ROMarr sees it**. Two libraries may share a
path if you genuinely point both applications at one tree, but nothing assumes
it. The Libraries page flags a server that answers while its path is missing
here, which is almost always a volume that was never mounted into ROMarr.

The environment seeds the first library on first run, so an existing install
comes up already configured. Everything after that is managed on the page.

![Three libraries: RomM as default, two folders with platform routing](docs/img/libraries.png)

*One RomM as the default, plus a Batocera handheld and an ES-DE cabinet — both
plain folders — taking Game Boy and N64 respectively.*

The Library view aggregates every server and badges each poster with its source;
one unreachable server is reported as a named row rather than an empty shelf.

#### Gaseous appears on its own schedule

Gaseous has no scan trigger — there is no such call in its API. It picks up new
files through two background tasks of its own: `TitleIngestor` every minute,
which processes Gaseous's **Import** directory, and `LibraryScan` every 1440
minutes, which walks the configured library paths. A ROM filed into a Gaseous
library path can therefore take up to a day to appear, and nothing ROMarr sends
changes that. Either point that library's path at Gaseous's Import directory, or
lower `LibraryScan`'s interval in Gaseous.

RomM and Retrom are both told directly and appear at once. For RomM, the account
ROMarr uses needs permission to run tasks — without it the rescan is refused with
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

The longest matching prefix wins, so a specific mapping overrides a broader one
rather than depending on which was added first. A mapping cannot help if the
volume is not mounted here at all — that stays a mount problem. Either way the
log names both the path the client reported and what ROMarr made of it, which is
what tells the two apart: no mapping matched, or a mapping matched and its local
side is wrong.

Use a **dedicated RomM account**, not your admin one. ROMarr needs only
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
