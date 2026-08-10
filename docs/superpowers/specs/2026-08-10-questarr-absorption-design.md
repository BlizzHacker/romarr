# Questarr absorption — design

**Date:** 2026-08-10
**Goal:** ROMarr covers every capability Questarr (github.com/Doezer/Questarr,
v1.4.1) ships, preempts its roadmap, and keeps its own advantages. Questarr is
credited in the README for features it did first — except where the idea
predates it (RomM already did metadata; *arr convention predates both).

## Feature matrix

### Already covered (no work)

| Questarr feature | ROMarr answer |
|---|---|
| IGDB metadata | IGDB + RAWG providers; DAT-verified identity beats filename parsing |
| Torznab / Newznab / Prowlarr | native, Prowlarr is the aggregate default |
| qBittorrent / SAB / NZBGet / Transmission / Deluge | in `downloaders.py` |
| Post-processing unpack (their P0, shipped v1.4.0) | bsdtar reads zip/7z/rar with zip-slip protection |
| Favorite release groups | `profiles.py` preferred terms, points visible in `Judgement.why()` |
| Release blacklist | `profiles.py` Blocklist, carries reason + timestamp |
| Apprise + notifications | 8 notifier types incl. apprise; messages carry scorer reasoning |
| Import history + retention | event store + history API |
| Calendar | `metadata.calendar` (RAWG) |
| Rate limiting, session auth | `ops.RateLimiter`, `auth.py` (+ TOTP, + ForwardAuth SSO — beyond them) |
| UNRAID | cartridge-unraid CA template |
| Filesystem import (their P3) | `upgrade.scan` manual import |
| Playnite/LaunchBox/ES-DE (their P4) | `frontends.py` exports |
| RomM integration (their P4) | ROMarr is RomM-native |
| Webhooks (their P4) | webhook notifier + GG Requestz inbound |

### To absorb (Questarr has it, ROMarr doesn't) — credit Questarr

1. **Scheduled search + RSS sync** — a background scheduler that re-searches
   the Wanted list on an interval with per-item backoff, and polls indexer RSS
   feeds matching new releases against Wanted between full searches.
   `search_missing` exists but only as a manual command.
2. **Collection statuses, ratings, notes** — per-title status
   (wanted/owned/playing/completed/shelved), 0–10 rating, free-text notes.
   Store-level, API, UI. ROMarr flavour: "owned" is derived from the library
   and per-status game lists come from the same store the grid already reads.
3. **rTorrent client** — XML-RPC via stdlib `xmlrpc.client`.
4. **Synology Download Station client** — HTTP API, two-step auth.
5. **Native SSL** — `ROMARR_SSL_CERT` / `ROMARR_SSL_KEY`, `ssl` stdlib wrap.
6. **Stats surface** — per-platform totals, verified %, ratings summary; API
   endpoint feeding a UI panel.
7. **Home Assistant add-on** — packaging directory (config.yaml + Dockerfile
   pointing at the ghcr image).

### Requested mid-design (Wade, 2026-08-10)

**Import Lists** — the Radarr concept, generalised: a stored list of titles
("Top 100 classics", "homebrew essentials", a friend's spreadsheet) that
ROMarr syncs into Wanted on the scheduler. Sources: pasted text (one title
per line, optional `Title<TAB>platform`), a URL fetched on the interval, and
the existing DAT collections for 1G1R full sets. Questarr's only list is
Steam wishlist sync; this subsumes it.

### Preempt their roadmap (they announced it, ROMarr ships it first)

8. **Update checker (their P1)** — poll GitHub releases daily, surface a
   banner + notification when a newer tag exists. Never auto-updates.
9. **Real-Debrid client (their P2)** — debrid as a "torrent" protocol client:
   add magnet, poll, fetch the unrestricted link, download to the client's
   save path so the normal import pipeline takes over.
10. **Indexer page links (their P5)** — interactive search rows link to the
    release's page on the indexer (`comments`/`guid` URL from Torznab).

### Skipped, with reasons

- **xREL.to / G4U.to** — scene-PC-release services; not meaningful for ROMs.
- **Steam wishlist sync** — Steam does not sell SNES ROMs. The retro
  equivalent (import a wishlist file) can ride on the existing request API
  later.
- **PostgreSQL (their P6)** — ROMarr's JSON store is deliberate.
- **Socket.io** — the UI polls; adequate at ROMarr's scale.

## Architecture notes

- **Scheduler** (`romarr/scheduler.py`, new): one daemon thread, a tick loop
  over registered jobs (missing-search, RSS sync, update check). Jobs record
  last-run/next-run so the Tasks page can show them. Per-wanted-item backoff
  stored on the item (`searched_at`, doubling interval, capped) so a title
  that has failed for a year isn't hammered hourly.
- **RSS sync** reuses the indexer drivers' existing search in RSS mode
  (Torznab `t=search` with no query = latest), matches titles against Wanted
  via the same normalisation `selection.py` already uses, and hands matches to
  the normal scoring path — RSS never bypasses the scorer or profiles.
- **Statuses** live on the store's game records; `wanted` remains the queue it
  is today (statuses don't change acquisition semantics; only `wanted`
  triggers searching).
- **New clients** follow `downloaders.py`'s driver-table shape exactly:
  a config dataclass, a client class with `add/list/remove`, a `CLIENT_TYPES`
  entry with fields. Real-Debrid's `list` maps RD torrent states onto the
  queue states the app already understands.
- **SSL** wraps the listener in `serve()`; missing/broken cert files log and
  fall back to HTTP rather than refusing to boot.
- **Update check** is a scheduler job hitting the GitHub releases API
  unauthenticated (60 req/h is plenty for 1/day), comparing semver tags.

## Testing

Every new module gets a test file in the existing style: stdlib `unittest`
/ pytest functions, driver tables asserted for shape, HTTP clients tested
against a stub server or by asserting request construction. The 13 currently
failing tests are stale-drift and get fixed first — features land on green.

## Credits (README)

A "Credits" section names Questarr as the origin of: scheduled search + RSS
sync UX, collection statuses/ratings/notes, Synology DS + rTorrent client
coverage, native SSL, stats page, and the Home Assistant packaging idea.
Metadata is not credited to Questarr (RomM and every *arr had it first).
