# Getting started

The first useful result is one imported ROM in the folder you expected. Start there before adding a large collection or scheduling downloads.

After installing, open ROMarr, set a password and choose **Settings → Setup Guide** (`/#setup`). Choose **I already have ROMs** or **Find and download games**. The existing-ROM path skips download and indexer setup; both paths offer optional DAT/ROM Hub steps. Each step can be skipped, and you can return later.

## Choose what you want to do

| Your goal | Required setup | Optional |
|---|---|---|
| Import files you already have | A library destination and local access to the source files | DAT verification, library-server connection |
| Find and download games | A library, a source and a client for that source's protocol | DAT verification, notifications |
| Compare an existing collection | A library and a suitable DAT | Acquisition sources to fetch missing entries |

Prowlarr and qBittorrent are useful for automatic torrent acquisition. You do not need either to import files already on disk.

## 1. Choose the destination

Open **Settings → Libraries**. Choose **Folder** if you want ROMs saved to a directory for ES-DE, Batocera, EmuDeck, LaunchBox or another frontend. For the README Compose example the path is `/roms`.

If you use RomM, Gaseous or Retrom, choose that integration and enter its address and credentials. The library's files must also be mounted into ROMarr; a working API connection alone does not give ROMarr filesystem access. Use the library test and confirm the destination is correct.

**Settings → Media Management** contains folder layout options. Check these against your frontend's own platform folder names before importing a collection. Folder support does not promise that every frontend uses identical names. Existing folders are not automatically renamed or migrated.

## Import a ROM you already have

1. Copy a small ROM you are allowed to use into the folder mounted as `/downloads`.
2. Open **Manual Import** and enter `/downloads` (or the source path as ROMarr sees it).
3. Click **Scan**. Scanning lists candidates; it does not import them.
4. Check the platform. Change it if the guess is wrong, especially for `.bin` and `.iso` files used by several systems.
5. Read the DAT result, then click **Import** on that row.
6. Check **History** and the destination folder. If you connected a library server, check its scan result too.

For a verified single-file import, **Rename on import** uses the DAT's canonical name while retaining the source extension when enabled. Unknown files retain their source filename, and existing destination files are not overwritten. This is different from changing an entire collection in place: a bulk rename and folder-migration preview is on the [roadmap](../ROADMAP.md).

Try a copy of one file first when working with an existing collection. Multi-track discs need their track files as well as a `.cue`; an empty cue sheet alone is not a complete game.

## Find and download a game

### 2. Add a download client

Open **Settings → Download Clients**, add the client you already run and use **Test** before saving.

| Source type | Client |
|---|---|
| Torrent or magnet | qBittorrent, Transmission, Deluge or another supported torrent client |
| Usenet/NZB | SABnzbd, NZBGet or another supported Usenet client |
| Searchable ROM Hub plugin | **ROM Hub** client with a writable staging/save path |
| Direct file link | **Direct HTTP** client |
| Browser-controlled download | **Headless Browser**, which needs additional setup in the [feature reference](FEATURES.md#browser-downloads) |

The client must match the release protocol. Adding qBittorrent does not enable Usenet or ROM Hub downloads.

### 3. Add a search source

Open **Settings → Indexers**. Connect Prowlarr with its URL and API key, or add a supported indexer directly. A Prowlarr installation with no working game sources will not produce results.

For a plugin source, follow [ROM Hub setup](PLUGINS.md) instead. Enabled plugins declaring `search` join searches when ROMarr's ROM Hub acquisition client is configured.

### 4. Search one game

1. Open **Interactive Search** and enter a title and platform.
2. Read the scores and rejection reasons. Check region, revision and format.
3. Grab one suitable result.
4. Follow it in **Queue**. Once the client finishes, ROMarr's scheduled import checks completed downloads.
5. Check **History** for the import result and verify the file is in your library.

Use **Add New** when you are ready for automatic selection. A title with no suitable result can stay on **Wanted** for later searches.

## Optional: DAT verification and missing games

A DAT is a catalogue of expected filenames and checksums, not a download source. No-Intro covers many cartridge systems; Redump covers many disc systems. Obtain a DAT for the system and set its directory in ROMarr. Loose `.dat`/`.xml` files and supported ZIP archives can be loaded.

In Docker the DAT directory needs its own bind mount, for example `/srv/dats:/dats:ro`; then use `/dats` in ROMarr. Set `DAT_PATH=/dats` before starting, or change the DAT directory setting in the UI.

- **Verified:** every member of the imported file/set matches a loaded DAT. One matching track does not verify a whole disc.
- **Bad dump:** at least one member conflicts with a known dump. Manual Import refuses it unless you explicitly choose Import anyway. Completed-download imports record the verdict and can keep the file; verified-only workflows refuse it.
- **Unknown:** at least one member is not identified, with no conflicting member. This prevents the set from being verified and is common with homebrew and translations.

Open **Collections**, choose the DAT and platform, and preview the comparison to see what is missing. A missing entry is a comparison result; downloading it still needs a usable source. Selective set-torrent acquisition also needs an existing qBittorrent torrent and a staging budget.

## Paths that need to agree

Docker gives each container its own filesystem view. The left side of a volume mount is the host folder. The right side is the name inside ROMarr.

| What you have | What ROMarr needs |
|---|---|
| Host `/srv/games`, mounted as `/srv/games:/roms` | Library path `/roms` |
| qBittorrent reports `/downloads/complete/game.zip` | The same file visible at that path inside ROMarr, or a remote path mapping |
| Your files are on another PC | A mounted/shared filesystem reachable by the ROMarr host; a remote API URL alone is not enough |

Example: if qBittorrent and ROMarr share host `/srv/downloads`, mount `/srv/downloads:/downloads` into both. When matching paths is impossible, **Settings → Media Management → remote path mappings** rewrites the client-reported prefix to ROMarr's local one. [More examples](INSTALL.md#the-downloads-path-specifically).

`localhost` is also local to each container. Use service names on a shared Docker network, or a reachable host IP and published port.

## If the first import fails

| Symptom | Check |
|---|---|
| No search results | Source test, platform/query, source access, and Prowlarr's configured indexers |
| Found a release but cannot grab it | A client is configured for that result's protocol |
| Download finished but import cannot find it | Download mount and remote path mapping |
| Library is reachable but import fails | ROMarr can write to its filesystem path; PUID/PGID match your folder permissions |
| Import succeeded but library server has no game | The server scans the same directory and its rescan completed |
| Environment edit had no effect | Connection settings saved after first run are changed in the UI |

The [install troubleshooting guide](INSTALL.md#troubleshooting) covers startup and permission errors. Include your version, install method, error and relevant log when reporting a problem; remove credentials first.
