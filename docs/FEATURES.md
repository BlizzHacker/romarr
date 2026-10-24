# Feature reference

Start with the [setup walkthrough](GETTING-STARTED.md) for your first import. This page covers the options you can add afterwards.

## Search and automation

**Interactive Search** shows releases from configured indexers and enabled searchable plugins, with scores and rejection reasons. You choose the grab. **Add New** asks ROMarr to select a suitable result. Requests without a usable result can remain on **Wanted**.

**System → Tasks** shows scheduled jobs, their intervals and their latest results. Completed-download import, wanted searches, RSS, lists and update checks can be managed there. A working connection does not guarantee that a source holds the file you want.

Failed or stalled releases may be blocklisted with a reason, allowing another result to be tried. Inspect **Queue**, **History** and **Blocklist** when a request is not progressing.

## Files, verification and names

The importer reads supported archives, identifies ROM candidates and files them under the destination platform. Multi-track discs need all referenced tracks. Arcade/Neo Geo and DOS archives can be kept intact because their internal layout matters to the emulator.

DAT checksums distinguish **verified**, **bad dump** and **unknown**. A matching title alone does not verify the bytes. No matching DAT means unknown; normal imports do not treat that as a bad dump. A refused bad dump needs an explicit override, which is recorded.

**Settings → Media Management → Rename on import** controls canonical naming for verified single-file imports. With it enabled, a verified file can use the DAT's canonical name while retaining its source extension. Disable it to preserve the source filename. Unknown files preserve their names. Importing preserves the source, and an existing destination is not overwritten. This does not rename a library in place or migrate its platform folders; those need a separate preview and are [planned](../ROADMAP.md).

## Collections and existing set torrents

**Collections** compares a loaded DAT with the selected library/platform. You can review missing entries and apply the available region/1G1R policy. A DAT describes a set; it does not supply downloads.

For an existing qBittorrent set torrent, selective acquisition first previews matched files and sidecars. ROMarr changes file priorities for a batch within your staging budget, verifies downloaded data before importing, and saves progress. Completion or cancellation restores the original torrent priorities. The workflow does not delete the source torrent or its original downloaded data. Account for those source files separately from the staging budget.

## Exact duplicates

Open **Stats → Audit** and select a platform. Review the exact duplicate preview, including the keeper, candidate and recoverable bytes. Select candidates to move them into quarantine under the library root.

Quarantine rechecks the selected bytes and keeper before moving files and writes a manifest for recovery. **Restore** refuses to overwrite another file and checks that the quarantined content still matches. Region variants, revisions and different-platform releases are not duplicates merely because their titles look alike.

Quarantine is reversible storage; it is not permanent deletion. Keep the quarantine directory and manifests together if moving or backing up your library.

## Multiple libraries

**Settings → Libraries** can hold more than one destination. Mark a default and use platform rules to route particular systems elsewhere. Every destination needs its own path as ROMarr sees it, even if its server API is reachable.

For Gaseous, use the appropriate import directory or its background library scan. A reachable server and a successful file copy do not prove that its catalogue has refreshed; check the backend's task/scan result.

The **folder** destination needs only a path. Frontends such as ES-DE, Batocera and EmuDeck still have their own expected directory names and emulator configuration. Check a small import before adopting a whole collection.

Genesis imports use the RomM filesystem slug `genesis`. Existing `genesis-slash-megadrive` folders are not moved automatically; keep an explicit mapping or plan a migration.

## Players and modern consoles

**System → Platforms** reports the routes configured for your install. A browser player, RetroArch server or Moonlight host is a separate component with its own core, format and firmware requirements. A catalogue row without a file cannot be played or downloaded locally.

ROMarr can expose routes through configured EmulatorJS, Ruffle, js-dos, Emularity or streaming hosts where appropriate. This is not a promise that every platform/format runs in a browser. Read the page's reason when a route is unavailable.

Advanced streaming setup and validation limits are in [streaming hosts](design/streaming-hosts.md). PS4/PS5 packages and PS Vita formats have [separate search/import limits](MODERN-CONSOLES.md); ROMarr does not install them onto a console or provide browser emulation for those systems.

## Browser downloads

A normal file URL can use the **Direct HTTP** client. Use **Headless Browser** only for a supported source that needs a browser-controlled download.

The standard Docker image is Alpine and does not include the Playwright client. For Docker browser mode use:

```text
ghcr.io/blizzhacker/romarr:browser
```

That image includes the Playwright client, not Chromium. Run a compatible Playwright server separately and enter its WebSocket address in the client's **Browser Host** field. The currently pinned client/server version is **1.62.0**:

```bash
docker run -d --name playwright --restart unless-stopped -p 3000:3000 \
  --init --ipc=host mcr.microsoft.com/playwright:v1.62.0-noble \
  npx -y playwright@1.62.0 run-server --port 3000 --host 0.0.0.0
```

Use `ws://HOST:3000` as Browser Host, with an address reachable from ROMarr. Keep this endpoint on a trusted network. Client and server Playwright versions must agree. A source install can install Playwright and Chromium into its own environment instead; browser binaries require substantially more disk and RAM than the base app.

CAPTCHAs, access challenges and blocked downloads can stop a fetch. The client reports the reason; it does not promise to bypass source restrictions.

`GET /api/v1/downloadclient/browser` reports availability and why the client cannot run when a requirement is missing.

## Notifications

**Settings → Connections** configures notifications such as Discord and generic webhooks. Use **Test** before saving. A failed test reports the endpoint's HTTP status and available response detail. A 403 can be an upstream permission, webhook or access-policy problem; copying ROMarr settings cannot grant access the endpoint refuses.

Do not post webhook URLs in bug reports: the URL may contain the credential. A successful test confirms that test's delivery, not that all future messages will be accepted.

## GG Requestz

GG Requestz sends approved requests to ROMarr. Open **System → GG Requestz requests** for the setting built for your install. Configure the webhook in **GG Requestz**, not as a ROMarr shell command:

```env
REQUEST_WEBHOOK_URL=http://romarr:6868/api/v1/webhook/ggrequestz?apikey=<ROMARR_API_KEY>
```

The `romarr` hostname works only when both containers share a network where that name resolves. Otherwise use ROMarr's reachable host address. GG Requestz versions that send only approved requests need approval or their auto-approve option enabled.

The URL contains your API key; treat it as a password. A reachable GG Requestz page does not prove it has an outbound webhook configured.

## API

The schema at `/api/v1/openapi.json` describes the current routes on your install. Use an API key from **Settings → General** with an `X-Api-Key` or `Authorization: Bearer` header.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/game` | Library catalogue |
| `GET /api/v1/wanted/missing` | Requested games not imported yet |
| `GET /api/v1/queue` | Current downloads |
| `GET /api/v1/history` | Recorded actions and results |
| `GET /api/v1/release?game=…&platform=…` | Scored release search |
| `POST /api/v1/release/grab` | Grab a release by its search-issued ID |
| `POST /api/request` | Request a game |
| `POST /api/v1/command` | Run a task |
| `GET /api/v1/system/status` | ROMarr and dependency status |

Release grabs use the ID returned by search so clients do not need indexer download URLs containing credentials. [Authentication and environment reference](INSTALL.md#configuration-reference).

## Further reading

- [ROM Hub plugins](PLUGINS.md)
- [Federation protocol](FEDERATION-PROTOCOL.md)
- [Dated test evidence and known gaps](PROOF.md)
- [Roadmap and community requests](../ROADMAP.md)
