# Rommarr

**The *arr for games.** Rommarr watches for game requests, searches your
indexers through Prowlarr, hands the winner to your download client, and files
the ROM into [RomM](https://github.com/rommapp/romm) where it can actually be
played.

If you run Radarr for films and Sonarr for TV, this is the missing one.

```
GG Requestz  ──request──▶  Rommarr  ──search──▶  Prowlarr ──▶ indexers
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
- Grabs via **qBittorrent** (torrent) or **NZBGet** (usenet)
- **Picks the ROM** out of the finished download, preferring the platform's own
  extensions in order and the best regional dump when several are present
- **Imports into RomM** at `<library>/<platform-slug>/`, refusing to overwrite
  unless told
- Triggers a RomM rescan so the game appears without a manual refresh

## Safety, deliberately

- **Zip-slip is blocked.** Archive entries that resolve outside the library root
  are dropped, not sanitised — a release that needs sanitising is not one to
  trust. Tested.
- **Prowlarr API keys never leave the server.** Prowlarr's `downloadUrl` is a
  link back to itself *with the key in the query string*. Rommarr only ever
  passes on a literal `magnet:` URI; anything else is grabbed server-side.
  Tested.
- **Nothing overwrites silently.** An existing ROM is left alone and reported.

## Supported platforms

Cartridge-era systems, deliberately: NES, SNES, Game Boy / Color / Advance,
N64, Genesis / Mega Drive, Master System, Game Gear, Atari 2600 / 7800, Lynx,
TurboGrafx-16, WonderSwan, Neo Geo Pocket, Virtual Boy.

Disc-based platforms are excluded on purpose. Their images are gigabytes, often
multi-file, and cannot be streamed into a browser emulator — which is the point
of feeding RomM in the first place.

## Install

Proxmox LXC, one line:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/BlizzHacker/rommarr/main/ct/rommarr.sh)"
```

Or run it yourself:

```bash
git clone https://github.com/BlizzHacker/rommarr.git && cd rommarr
pip install -r requirements.txt
cp .env.example .env    # then edit it
python -m rommarr
```

## Configuration

| Variable | What it is |
|---|---|
| `PROWLARR_URL` / `PROWLARR_API_KEY` | your Prowlarr, for searching |
| `ROMM_URL` / `ROMM_USERNAME` / `ROMM_PASSWORD` | RomM, for the rescan trigger |
| `ROMM_LIBRARY` | path to RomM's library root, e.g. `/mnt/roms` |
| `QBITTORRENT_URL` / `QBITTORRENT_USER` / `QBITTORRENT_PASS` | torrent client |
| `NZBGET_URL` / `NZBGET_USER` / `NZBGET_PASS` | usenet client, optional |

Use a **dedicated RomM account**, not your admin one. Rommarr needs only
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
