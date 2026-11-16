# Soundtracks

ROMarr can find a game's soundtrack and put it into RomM's **Jukebox**, from the **Soundtracks** page. This
is a port of the standalone `romm-soundtracks` tool, built into ROMarr so there is one app and no
second process to run. It is being ported in phases; this page describes phases 1 to 3.

## What phase 1 does

Open **Library → Soundtracks**, select the library and then a game in it. The **Soundtrack** card shows:

* how many soundtrack files RomM holds for the game, and how many of those ROMarr added;
* **Search for a soundtrack**: ranked candidates from Zophar's Domain and the Internet Archive
  (same platform first, then title similarity), each with an **Open** link and a download button
  per format offered, plus a row of manual-search links (KHInsider, VGMRips, Zophar, Internet
  Archive, MusicBrainz, Bandcamp, YouTube);
* progress while a download runs (it continues if you leave the page);
* **Undo**, which removes the files ROMarr added and nothing else.

Files go in **through RomM's own API** (`POST /api/roms/{id}/soundtracks`). RomM creates the
`soundtrack/` folder, registers the tracks and, for a lone ROM file, first moves the ROM into a
folder of its own. ROMarr never writes to the ROM tree.

RomM only accepts MP3, OGG, OPUS, M4A, AAC, WAV, FLAC, SPC, VGM, VGZ and GYM. Other chiptune
formats (NSF, GBS, ...) are read out of archives but skipped, because RomM would refuse them.
MP3 is the safe choice; "original rips" are only useful on consoles whose native format is SPC,
VGM or GYM.

### Sign-in

ROMarr uses the login already saved on the RomM entry on the **Libraries** page. There is no
soundtrack token to configure. For uploads and undo it asks RomM for a token that also carries
`roms.write` (the token ROMarr uses all day stays read-only). If the library entry uses a fixed API
token instead of a username and password, that token must itself be allowed to edit ROMs. The URL
used is the library entry's URL.

### Settings

`soundtrack_sources` (comma separated, default `zophar,archive`) picks which sources the Search
button queries. It can be edited through the settings API; there is no page for it yet.
`music_library_root` (default empty; explicitly configure a read-only folder such as `/music-library`) is the read-only music folder phase 3 searches.

## HTTP API

All routes use the normal ROMarr authentication. `id` is the RomM game id, as on the game page.

| Route | |
|---|---|
| `GET /api/v1/soundtracks/game?id=` | Status: tracks RomM holds, how many ROMarr added, last job |
| `GET /api/v1/soundtracks/search?id=` | Ranked candidates, any source errors, manual links |
| `POST /api/v1/soundtracks/download` `{id, candidate:{provider,id}, format}` | Starts a background job, returns it |
| `GET /api/v1/soundtracks/jobs` | Recent jobs with step, progress and counts |
| `POST /api/v1/soundtracks/undo` `{id}` | Removes what ROMarr added to that game |

`format` is `mp3`, `original`, `both`, `flac` or `files` (Internet Archive items). Only one job
runs per game: a second download or an undo while one is running answers 409. Search runs on the
request (a few seconds, bounded by the one-request-per-second rule); downloading and uploading
never do.

Phase 2 and 3 routes are listed in their own sections below.

Data lives in `romarr-soundtracks.db` beside the ROMarr data file (`ROMARR_DATA`), in source-isolated tables
`sound` and `journal` keyed by RomM game id, the same names the standalone tool used. Phase 2 adds
`merges` and `dup_scan`; `sound` gained a `title` column (older databases are upgraded in place).

## Phase 2: duplicate soundtracks

The **Soundtracks** page (Library group in the navigation, tab **Duplicates**) lists games whose
stored soundtracks are identical.

* **Scan for duplicates** runs in the background. It reads RomM's library, groups games whose
  soundtrack file names and sizes match, then **streams the real bytes from RomM and compares them
  by SHA-256** (names included). Only sets that match byte for byte are listed; a set whose bytes
  differ is never offered. The result is kept until the next scan.
* Pick the game to **keep** and press **Merge**. The merge re-reads and re-hashes the kept copy and
  each other copy first and **refuses any copy that differs**, leaving it alone. Only the
  non-kept games are touched: their soundtrack files are removed through RomM's delete endpoint.
  The kept game is never written to. Each merge is journaled (who kept what, with the file hashes),
  and the removed files drop out of that game's phase 1 Undo list because they no longer exist.
* **Undo** (per merged game) fetches the kept files, **verifies them against the hashes recorded at
  merge time** (refusing if the kept copy has changed), then re-uploads them to the merged game
  through RomM. The restored files are journaled, so the game page's Undo can remove them again.
  A merge that failed part-way is recorded as *incomplete* and can be undone the same way.
* **Download skips what another game holds.** A download of a source item that another game
  already holds (downloaded by ROMarr, or merged away) is not fetched again: the request answers
  `409` with `held_by` (the game titles), and the game page asks whether to download it anyway
  (`force: true`). This only knows about downloads ROMarr made; use the scan for the rest.
* One job per game still holds: a merge or undo locks the kept game and every merged game, and is
  refused with `409` while any of them has a job running. A scan is one job at a time.

| Route | |
|---|---|
| `GET /api/v1/soundtracks/dups` | Verified groups, merges that can be undone, the latest scan job, scan time |
| `POST /api/v1/soundtracks/dups/scan` | Starts the byte-for-byte scan, returns the job |
| `POST /api/v1/soundtracks/dups/merge` `{keep, drop:[ids]}` | Starts a merge job |
| `POST /api/v1/soundtracks/dups/unmerge` `{id}` | Starts the restore for one merged game |

Progress of all of these appears under `GET /api/v1/soundtracks/jobs` (`kind` is `dup-scan`,
`merge`, `unmerge`, `download` or `album`). A scan downloads every candidate soundtrack from RomM
once, so run it when you want to tidy up, not on a schedule.

## Phase 3: albums from your music library

On the game page, **Use an album from your music library** searches a music folder and uploads an
album's audio into the game's soundtrack, for fan-made albums no source site has.

* The folder is the `music_library_root` setting (default empty; explicitly configure a read-only folder such as `/music-library`). Mount your music
  share there **read-only**. ROMarr only reads it: nothing is written, renamed or linked inside it.
* A folder that holds audio is an album (up to four levels below the root). `CD1`/`CD2`/`Disc 1`
  style folders roll up into the album above them, and their names are folded into the file names
  (`CD2 - 01 Intro.flac`), because a game's soundtrack is a flat list. Search matches every word
  against the album's path; the album's own folder name ranks first. The list is cached for two
  minutes.
* Only the formats RomM's Jukebox plays are taken (MP3, OGG, OPUS, M4A, AAC, WAV, FLAC, SPC, VGM,
  VGZ, GYM); cover art, rip logs, cue sheets and hidden files stay behind.
* **Upload only.** Each file is sent through RomM's upload API straight from the library file,
  opened read-only. Hardlinking (the standalone tool's shortcut) is not implemented: ROMarr's
  container mounts the music library and the ROM tree as separate mounts, which blocks hardlinks,
  and ROMarr never writes to the ROM tree anyway.
* A track RomM already has under that name is skipped, a file RomM refuses is skipped, and every
  upload is journaled, so **Undo** (the same button as for downloads) removes only what was added.
* Path safety: the album id must resolve, after following symlinks, to a folder strictly inside
  the library root, and a symlinked file that leads out of the album is not collected. The search
  does not follow symlinked folders.

| Route | |
|---|---|
| `GET /api/v1/soundtracks/library/search?q=` | Albums `{id, name, tracks, bytes}`; `id` is the path below the library root |
| `POST /api/v1/soundtracks/library/add` `{id, album}` | Starts an upload job for the game with that RomM id |

## Safety rules

* **Never overwrites.** A track RomM already has under that name is skipped.
* **Undo only removes what ROMarr added.** Each upload is journaled with the file id RomM gave it,
  and undo deletes exactly those ids through RomM. Files you added yourself are never touched, and
  a journal entry for a different game is ignored. A file RomM no longer has counts as already gone.
  If a delete fails part-way, the rest stay journaled so Undo can be retried.
* **No empty `soundtrack/` folders.** ROMarr creates no folders; RomM does, and only when a real
  file is being sent. Nothing is sent unless it is an accepted audio file.
* **Archives are streamed, not extracted.** Only audio extensions are read out of a zip, names are
  flattened (`../../x.mp3` becomes `x.mp3`, nothing is written to a path from the archive), and an
  archive over 6 GB is refused before anything is read.
* **Polite to the sources.** At most one request per second per host, with a User-Agent that says
  ROMarr is asking. Download links must point at zophar.net or archive.org. Sites behind a bot
  challenge (KHInsider) are never scraped, only linked.
* **A ROM whose name would change** when RomM moves it into a folder (its name ends in a dotted
  number, like `Game v1.2`) is left alone, because that would break name-based save matching.
* **No credentials in output.** Nothing logs or returns the library password or a token; errors
  from RomM have the token scrubbed.

## Not ported yet

* **Phase 4:** 3DS encryption status and the decrypt tool, and held originals (the safety copy kept
  before any ROM is replaced).
* Direct delivery (writing files into the game folder instead of uploading through RomM) and
  hardlinks from the music library are not planned; they were the standalone tool's fallbacks.
* "Download confident matches" for a whole selection, and a soundtrack column or filter on the
  library page, come with the later phases.

## Assumptions about RomM's API

The calls are the ones the standalone tool made against a live RomM: `GET /api/roms/{id}`
(`fs_name`, `has_multiple_files`), `GET /api/roms/{id}/soundtracks/metadata` (list of
`file_name`/`file_id`), `POST /api/roms/{id}/soundtracks` (multipart, one part named after the file,
`x-upload-filename` header, `201` on success; `400`/`409` mean that file is refused),
`DELETE /api/roms/{id}/soundtracks/{file_id}` (`404` = already gone). The standalone tool
authenticated these with a client token; ROMarr authenticates with the password-grant bearer token
it already uses elsewhere, with the `roms.write` scope added. That the same grant is accepted for
the upload endpoints has not been tried against a live RomM.

The phase 2 and 3 calls add: `GET /api/roms?limit=500&offset=&with_files=true` (each item has
`id`, `name`, `platform_display_name` and `files[]` with `file_name`, `file_size_bytes`, `category`)
and `GET /api/roms/{id}/files/{file_id}/content/{file_name}` (the stored bytes of one file; the
`file_id` is the one `soundtracks/metadata` returns, which is assumed to be the ROM file's id).
None of this has been tried against a live RomM; the tests use a loopback fake.

## Source identity and safety in ROMarr

The library and game selectors are explicit on the Soundtracks page. The targets endpoint (`GET /api/v1/soundtracks/targets`) returns stable `library-token:game-id` selectors. Use that selector as `id` on game/search/download/undo/library-add/torrent endpoints. A bare numeric id remains compatible with one unambiguous library; ambiguous ids are refused. Duplicate listing uses `GET /api/v1/soundtracks/dups?library=token`; a duplicate scan posts `{library:token}`. Merge keep/drop and unmerge ids use qualified selectors. Cross-library merges are refused.

Each configured library identity has a separate `romarr-soundtracks-<token>.db` beside ROMARR_DATA. Job locks, watches and destructive journals cannot cross these databases. Old unscoped `romarr-soundtracks.db` files are left untouched because the owning server cannot be proven. Changing the configured server or stable library id creates another scope; review old state rather than reassigning it to a different server.

Album files are opened through pinned directory and file handles, validated against the selected object identity and copied into private temporary snapshots before upload. Source files are not renamed or deleted. ZIP metadata is bounded before ZipFile construction using the shared ZIP64/central-directory checks. Redirects are limited and every target must remain an approved HTTPS source; connections resolve and validate public addresses and connect directly to those addresses while checking the original TLS name. Environment proxies are not used for these source requests. Configured RomM servers can be private addresses and use their own operator-selected URL; authenticated write requests do not follow redirects.

A maximum of four soundtrack jobs runs per library at once. Source metadata is limited to8MiB, each source stream/temporary audio file to6GiB, source streams with a30minute budget checked between chunks (socket read timeouts also apply) and ZIPs to10000members. Folder/track listings have limits. Large operations still need free private temporary disk space. These limits are safety boundaries, not claims that every remote operation is instantaneous.

Upload receipts are matched to the bytes later read from RomM before a file becomes undoable. Undo verifies the saved source identity, file name, byte hash and size before deletion and refuses changed content. RomM does not provide a transaction spanning a remote upload and a local journal write, or an atomic compare-and-delete promise: interruption can leave uploaded files needing manual review, and concurrent server-side changes should be paused during destructive cleanup. Unknown ownership is never guessed to make an undo succeed.

Soundtrack torrent watches are bound to the download-client identity as well as the library. Existing unbound watches are not automatically imported. Stopping a watch leaves the torrent seeding. The music library is disabled until an explicit root is configured in the Soundtrack settings; disabling both catalogue sources is supported. Only use audio you own or are authorized to obtain; no audio is bundled with ROMarr. See [CREDITS.md](../CREDITS.md) for source attribution.

Game selection is bounded to500 rows per response. The targets endpoint accepts `q` and `library` so every matching title in a large library remains reachable without a huge dropdown. Existing enabled antivirus scanning applies to all staged audio uploads, including album, catalogue, torrent and restore uploads; a scan failure or finding blocks the upload.
