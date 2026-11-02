"""The OpenAPI description, generated from the routes that actually exist.

Written by walking `app.py` for its route literals rather than by hand. A
hand-maintained spec is a second source of truth that drifts the first time
somebody adds an endpoint and forgets, and a spec that documents a route the
server does not serve is worse than no spec -- a client generated from it
fails at runtime with a 404 that looks like a server fault.

`test_openapi.py` fails if any served route is missing a description, so the
drift is a test failure rather than a support question.
"""

from __future__ import annotations

import pathlib
import re

VERSION = "3.1.0"

#: One line per route. Anything served and not described here fails the test,
#: which is the whole mechanism keeping this honest.
DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "/": ("GET", "The web UI."),
    "/favicon.ico": ("GET", "The retro cartridge SVG favicon."),
    "/favicon.svg": ("GET", "The retro cartridge SVG favicon."),
    "/api/health": ("GET", "Liveness. Returns {ok} unauthenticated, the full "
                           "dependency report with a key."),
    "/metrics": ("GET", "Prometheus exposition."),
    "/login": ("GET", "The sign-in screen, or the first-run setup screen on "
                      "an install nobody has claimed. Served without a "
                      "credential; redirects to / once signed in."),
    "/link": ("GET", "Where a peering invitation link lands. A constant page, "
                     "served without a credential: the invitation is in the "
                     "URL fragment, which never reaches the server."),
    "/oidc/login": ("GET", "Start an OpenID Connect sign-in: 303 to the "
                           "configured IdP's authorization endpoint with "
                           "PKCE (S256), state and nonce. Served without a "
                           "credential — obtaining one is the point."),
    "/oidc/callback": ("GET", "The IdP's redirect back. Verifies state "
                              "against the flow cookie, exchanges the code "
                              "with the PKCE verifier, validates the id_token "
                              "(iss/aud/exp/nonce and, where possible, its "
                              "signature) and issues a session. Refuses "
                              "without a live flow cookie."),
    "/api/v1/login": ("POST", "Exchange an API key or password for a session "
                              "cookie. Requires the TOTP code when two-factor "
                              "is enrolled."),
    "/api/v1/setup": ("POST", "First-run claim: set the admin password on an "
                              "install that has none. Open only while "
                              "unclaimed, and answers 409 once it is."),
    "/api/v1/setup/status": ("GET", "Optional Setup Guide checklist: configured "
                                     "names and counts without credentials or "
                                     "connection probes. Established installs "
                                     "are not sent through the guide."),
    "/api/v1/setup/complete": ("POST", "Dismiss or finish the optional Setup Guide. "
                                        "Stores the organize/download choice; "
                                        "the guide remains available in Settings."),
    "/api/v1/setup/dats": ("POST", "Check and load an optional No-Intro or Redump "
                                    "DAT folder visible to ROMarr. Reports missing "
                                    "folders and unreadable DATs with a useful error."),
    "/api/v1/game": ("GET", "The library. `totals` splits it into files on "
                            "disk here, entries catalogued but streamed from "
                            "elsewhere, and the sum; `grand_total` is only how "
                            "many rows are cached and browsable right now. "
                            "Filterable by platform, genre, region, decade, "
                            "origin and source."),
    "/api/v1/wanted/missing": ("GET", "Games wanted but not yet found. DELETE "
                                      "the same path with game and platform "
                                      "-- as query parameters or a JSON body "
                                      "-- to drop a request without importing "
                                      "it."),
    "/api/v1/queue": ("GET", "Active downloads."),
    "/api/queue": ("GET", "Active downloads (legacy path)."),
    "/api/v1/history": ("GET", "What ROMarr has done."),
    "/api/v1/config": ("GET", "Settings, with every credential masked."),
    "/api/v1/system/status": ("GET", "Health of every dependency, plus play-route counts."),
    "/api/v1/system/counts": ("GET", "Library and queue sizes. `games` is "
                                     "every row the library server holds; "
                                     "`games_on_disk` and `games_catalogued` "
                                     "are the two categories it adds up."),
    "/api/platforms": ("GET", "Every platform, its media, extensions, size "
                              "ceiling and how it plays on this install, "
                              "including which browser players can open it."),
    "/api/v1/players": ("GET", "The browser players — EmulatorJS, Ruffle, "
                               "js-dos, Emularity — each with what it runs, "
                               "what it does not, whether this install has it "
                               "enabled, and how many library rows it can "
                               "open. Set with ROMARR_PLAYERS."),
    "/api/v1/play": ("GET", "How one file plays: ?file= a filename or a bare "
                            "extension, ?platform=, and ?missing=1 for a row "
                            "the library server catalogues without holding "
                            "the bytes. Returns the routes on offer, the "
                            "players that would offer one and why they do "
                            "not, and — for a missing file — that there is "
                            "nothing to play or download."),
    "/api/v1/release": ("GET", "Interactive search: scored candidates with the "
                               "reason for each score."),
    "/api/v1/indexer/schema": ("GET", "The nine indexer types and their fields."),
    "/api/v1/metadata/schema": ("GET", "Metadata providers and their fields."),
    "/api/v1/metadata/lookup": ("GET", "Identify a file. Reports matched_by as "
                                       "'dat' or 'filename'."),
    "/api/v1/calendar": ("GET", "Games released recently or due soon, from "
                                "the first configured metadata provider that "
                                "can answer a date range (RAWG or IGDB). "
                                "?back= and ?ahead= are days either side of "
                                "today, and ?back= goes negative: ?back=-1 "
                                "opens the window tomorrow, which is the "
                                "forward half. Every row carries "
                                "owned=false -- these are catalogue entries, "
                                "not library rows."),
    "/api/v1/connection/schema": ("GET", "The eight notification providers."),
    "/api/v1/connection/test": ("POST", "Send a test notification to every "
                                        "configured connection."),
    "/api/v1/blocklist": ("GET", "Releases that will never be taken, with the "
                                 "reason each was blocked."),
    "/api/v1/tag": ("GET", "Tags, by library item."),
    "/api/v1/update": ("GET", "Update status: install shape, channel, the "
                              "latest version on GitHub, ROM Hub plugin "
                              "sources, and the tail of the last self-update."),
    "/api/v1/asset": ("GET", "Relay one image from a configured library "
                             "server, for pages served over https that "
                             "cannot embed http assets directly."),
    "/api/v1/collection": ("GET", "Set-acquisition batches in progress, and "
                                  "the DATs available to plan against."),
    "/api/v1/collection/plan": ("GET", "Compare a DAT against the library: "
                                       "expected, present, missing, and why "
                                       "each dump won its group."),
    "/api/v1/collection/start": ("POST", "Queue every missing title from a "
                                         "plan as a resumable batch."),
    "/api/v1/collection/step": ("POST", "Request the next slice of a batch."),
    "/api/v1/collection/control": ("POST", "pause, resume, retry or cancel a "
                                           "batch."),
    "/api/v1/collection/reconcile": ("POST", "Fulfil Wanted titles whose "
                                             "verified dump is already in the "
                                             "library for a platform."),
    "/api/v1/collection/source": ("GET", "List qBittorrent set torrents and selective-acquisition jobs."),
    "/api/v1/collection/source/plan": ("POST", "Preview missing DAT files from a set torrent within a staging budget."),
    "/api/v1/collection/source/start": ("POST", "Select only the planned qBittorrent files and start a persistent staged acquisition."),
    "/api/v1/collection/source/step": ("POST", "Stage, verify and import completed files from the current torrent batch."),
    "/api/v1/collection/source/control": ("POST", "Cancel or retry a selective torrent acquisition."),
    "/api/v1/audit/quarantine": ("GET", "Preview exact duplicates and list reversible quarantine manifests."),
    "/api/v1/audit/renames": ("GET", "Read the latest safe rename preview and durable rename journals."),
    "/api/v1/audit/renames/preview": ("POST", "Build a read-only DAT-verified single-file rename preview for one platform."),
    "/api/v1/audit/renames/apply": ("POST", "Apply explicitly selected safe renames after rechecking hashes and collisions."),
    "/api/v1/audit/renames/rollback": ("POST", "Restore a verified rename journal without overwriting existing files."),
    "/api/v1/audit/quarantine/move": ("POST", "Re-hash and move selected byte-identical duplicates into same-volume quarantine."),
    "/api/v1/audit/restore": ("POST", "Restore one quarantined duplicate with collision and hash checks."),
    "/api/v1/manualimport": ("GET", "Scan a directory for files ROMarr could adopt."),
    "/api/v1/backup": ("GET", "A restorable snapshot. Credentials are stripped "
                              "unless ?secrets=1."),
    "/api/v1/restore": ("POST", "Restore a backup."),
    "/api/v1/export": ("GET", "Library, wanted or blocklist as JSON or CSV."),
    "/api/v1/frontend/formats": ("GET", "Available frontend export formats."),
    "/api/v1/frontend/export": ("GET", "LaunchBox XML, ES-DE gamelist.xml or "
                                       "Playnite JSON."),
    "/api/v1/hub/catalogue": ("GET", "Search the plugin catalogue."),
    "/api/v1/hub/plugins": ("GET", "The plugin catalogue, unfiltered."),
    "/api/v1/hub/igir": ("GET", "Igir v5.8.0 1G1R front-end status, default profile, and CLI execution (POST runs or previews an Igir 1G1R job with {romm} output tokens)."),
    "/api/v1/hub/fleet": ("GET", "Multi-device and network library sync profiles for ES-DE, RetroPie/ArcadePunks, Batocera, Argosy, Grout, Steam Deck, and RetroArch WebDAV (POST builds a 1G1R + gamelist.xml sync plan)."),
    "/api/v1/mcp": ("GET", "Model Context Protocol (MCP) tool manifest and Open-Eyes desktop bridge configuration for ROMarr."),
    "/api/v1/hub/plugin": ("POST", "Install, enable, disable or uninstall a plugin."),
    "/api/v1/hub/source/check": ("POST", "Whether a repository URL may be "
                                         "installed from."),
    "/api/v1/hub/source/install": ("POST", "Validate a repository against configured "
                                           "plugin source hosts, then install it "
                                           "through ROM Hub. Reports the actual "
                                           "installer outcome; requires authentication."),
    "/api/v1/hub/submit": ("POST", "Validate a catalogue submission and return "
                                   "a link. ROMarr does not post it."),
    "/api/v1/capture": ("POST", "Catalogue rows captured by the browser "
                                "extension from a page the operator visited, "
                                "for sites no HTTP client can read. Validated "
                                "as untrusted input: bounded size, a known "
                                "source, URLs only on that source's own "
                                "hosts, and platforms resolved rather than "
                                "guessed. Appends to idx-<slug>.jsonl and "
                                "reports what was indexed, already known, "
                                "skipped and unmapped."),
    "/api/v1/capture/status": ("GET", "Whether this install can accept a "
                                      "capture: the sources it knows and "
                                      "whether the index directory is "
                                      "writable. What the extension's "
                                      "connection test calls."),
    "/api/v1/webhook": ("POST", "Inbound game request from a front-end."),
    "/api/request": ("POST", "Request a game."),
    "/api/v1/integration/info": ("GET", "Integration service info: name, version, capabilities."),
    "/api/v1/integration/requests": ("POST", "Accept a game request from an external platform "
        "(Cartridge). Starts the same search + grab pipeline; "
        "returns a stable request_id to poll. GET lists all tracked requests."),
    "/api/v1/integration/requests/current": ("GET", "The most recently updated integration request — "
        "id, name, platform, status, assets, error."),
    "/api/v1/system/tasks": ("GET", "The scheduled jobs: interval, last run, "
                                    "last result."),
    "/api/v1/log/tail": ("GET", "The live process log: records after ?since=, "
                                "filtered to ?level= and up."),
    "/api/v1/discover": ("GET", "Browse a storefront shelf: "
                                "?shelf=popular|new|upcoming, served by the "
                                "first configured provider that can browse "
                                "(RAWG or IGDB) and named in provider_label. "
                                "Rows carry owned=false -- they come from a "
                                "catalogue, not from your library. `error` "
                                "says which credential is actually missing."),
    "/api/v1/stats": ("GET", "What this install has done: events, imports by "
                             "platform, grabs by indexer, shelf totals."),
    "/api/v1/game/meta": ("GET", "The shelf: status, rating and notes, for "
                                 "one game (?platform=&game=) or all."),
    "/api/v1/game/roulette": ("GET", "Backlog roulette: one shelf game at "
                                     "random (?status=shelved[,playing]&platform=)."),
    "/api/v1/importlist": ("GET", "Import lists and how many titles each has "
                                  "fed into Wanted."),
    "/api/v1/importlist/schema": ("GET", "The list types and their fields."),
    "/api/v1/importlist/preview": ("POST", "Parse a list without saving it: "
                                           "titles and platform resolution."),
    "/api/v1/connection": ("GET", "Configured notification connections, "
                                  "webhook URLs masked."),
    "/api/v1/queue/action": ("POST", "Act on one queue row by index: retry "
                                     "re-runs the request, remove forgets it."),
    "/api/v1/queue/clear": ("POST", "Empty the queue, or only the rows in "
                                    "{state} (e.g. failed)."),
    "/api/v1/discover/library": ("GET", "Browse the library you already "
                                        "have: ?shelf=top-rated|recent|"
                                        "recently-added|hidden-gems|"
                                        "multiplayer|anniversary|by-genre|"
                                        "by-franchise|by-company, the last "
                                        "three with ?value=. Needs no API "
                                        "key -- the metadata is your own."),
    "/api/v1/calendar/library": ("GET", "A calendar over the dates your "
                                        "library server actually holds: "
                                        "?view=releases (anniversaries)|"
                                        "added|updated|upcoming, ?month="
                                        "YYYY-MM and ?day=. Reports its own "
                                        "coverage; ?decade=1990s still lists "
                                        "that decade."),
    "/api/v1/peer": ("GET", "Peered ROMarr instances: scope, access and "
                            "confirmation state. Tokens never appear here."),
    "/api/v1/peer/invite": ("POST", "Mint a one-time invitation: a link that "
                                    "carries no secret, a short claim code "
                                    "that does, and the older pasted blob."),
    "/api/v1/peer/redeem": ("POST", "Redeem a friend's pasted invitation "
                                    "blob, without calling their server."),
    "/api/v1/peer/claim": ("POST", "Redeem a friend's invitation link with "
                                   "the claim code they sent separately. "
                                   "Calls their server and keeps the token."),
    "/api/v1/peer/accept": ("POST", "Called BY a peer redeeming your "
                                    "invitation, with either the long secret "
                                    "or the short claim code; held "
                                    "unconfirmed, and returns the token."),
    "/api/v1/peer/confirm": ("POST", "Confirm a peer that redeemed your "
                                     "invitation."),
    "/api/v1/peer/policy": ("POST", "Set one peer's scope and access."),
    "/api/v1/peer/shelf": ("GET", "Peer-facing: the projection this peer may "
                                  "see. Peer id + token headers."),
    "/api/v1/peer/netplay": ("POST", "Peer-facing: answer a session offer by "
                                     "matching its DAT hash against this "
                                     "library."),
    "/api/v1/hashes": ("GET", "How many dumps netplay can prove, by "
                              "platform. Populated by audits and imports."),
    "/api/v1/hashes/seed": ("POST", "Fill the hash index from the library "
                                    "server's own hashes, so netplay works "
                                    "without walking the whole library."),
    "/api/v1/peer/romm": ("POST", "Befriend somebody running a plain RomM, "
                                  "using an account on their server. Needs "
                                  "nothing installed on their side."),
    "/api/v1/friends/shelf": ("GET", "Browse what a friend shares with you. "
                                     "Filtered here, fetched from them at "
                                     "most once every two minutes."),
    "/api/v1/friends/want": ("POST", "Add a title seen on a friend's shelf to "
                                     "your own Wanted list; your indexers "
                                     "acquire it, not your friend."),
    "/api/v1/friends/netplay": ("POST", "Offer a friend a game to play, "
                                        "carrying the SHA1 of your dump so "
                                        "both sides agree on the bytes."),
    "/api/v1/ecosystem": ("GET", "The projects ROMarr stands on: library "
                                 "servers, players, indexers, DAT databases "
                                 "-- repo, site and install command for each."),
    "/api/v1/audit": ("GET", "Verify the existing library against your DATs, "
                             "one platform at a time: POST {platform} starts, "
                             "GET polls — verified / bad-dump / unknown "
                             "counts plus byte-identical duplicates."),
    "/api/v1/system/apikey": ("GET", "The API key, for the Settings page. "
                                     "Authenticated, unlike safe_settings "
                                     "which exists to strip credentials."),
    "/api/v1/totp/enroll": ("POST", "Generate a TOTP secret and backup "
                                    "codes; two-factor gates sign-in from "
                                    "then on."),
    "/api/v1/totp/disable": ("POST", "Turn two-factor off."),
    "/api/v1/metadataprovider": ("GET", "Configured metadata providers, "
                                        "credentials masked."),
    "/api/v1/metadataprovider/schema": ("GET", "Provider types and their "
                                               "fields, for the editor."),
    "/api/v1/metadataprovider/test": ("POST", "Look up a known title through "
                                              "a provider config to prove "
                                              "the key works."),
    "/api/v1/launchers": ("GET", "Games the launchers installed on this "
                                 "machine know about — Steam, Epic, GOG "
                                 "Galaxy, Battle.net, EA. No credentials."),
    "/api/v1/connect/sources": ("GET", "Stores that issue a token from a "
                                       "signed-in page, and which page."),
    "/api/v1/connect/steam": ("GET", "Start Steam's OpenID sign-in. Redirects "
                                     "the browser to Steam."),
    "/api/v1/connect/steam/return": ("GET", "Steam's OpenID return. Verifies "
                                            "the assertion with Steam and "
                                            "connects the library."),
    "/api/v1/launchers/connect": ("POST", "Scan this machine's launchers and "
                                          "save the result as an import list."),
    "/api/v1/openapi.json": ("GET", "This document."),
    "/api/v1/webhook/ggrequestz": ("POST", "Inbound request, GG Requestz shape."),
    "/api/search": ("GET", "Search every configured indexer."),
    "/api/import": ("POST", "Import a finished download."),
    "/api/v1/command": ("POST", "Run a task: search, import or refresh."),
    "/api/v1/log": ("GET", "Recent log lines."),
    "/api/v1/release/grab": ("POST", "Grab a specific release from an "
                                     "interactive search."),
    "/api/v1/indexer": ("GET", "Configured indexers, with keys masked."),
    "/api/v1/indexer/test": ("POST", "Test one indexer's connection."),
    "/api/v1/downloadclient": ("GET", "Configured download clients."),
    # Counted, not stated: this said "five" while there were eight, and would
    # have said eight the moment a ninth landed. A description that drifts is
    # the thing this file exists to prevent.
    "/api/v1/downloadclient/schema": ("GET", "Every download client type and "
                                             "its fields: torrent and usenet "
                                             "daemons, debrid and cloud "
                                             "services, watched-folder "
                                             "blackholes, direct HTTP and "
                                             "browser."),
    "/api/v1/downloadclient/test": ("POST", "Test one client's connection."),
    "/api/v1/downloadclient/browser": ("GET", "Whether the headless-browser "
                                              "download lane can run here, "
                                              "and the reason when it cannot. "
                                              "The lane opens a site's real "
                                              "page and clicks its real "
                                              "download control, for sites "
                                              "whose file is behind a form "
                                              "POST or a JS-built link; it "
                                              "refuses CAPTCHAs, bot-detection "
                                              "challenges, header spoofing and "
                                              "logins, and reports such sites "
                                              "as unavailable instead."),
    "/api/v1/library": ("GET", "Configured library servers."),
    "/api/v1/library/schema": ("GET", "Library backend types and their fields."),
    "/api/v1/library/config": ("GET", "One library's stored configuration."),
    "/api/v1/library/test": ("POST", "Test one library server."),
    "/api/v1/hub/status": ("GET", "Whether ROM Hub is installed and reachable."),
    "/api/v1/moonlight": ("GET", "The configured Moonlight host (Wolf, "
                                 "Sunshine or Steam Headless): whether it "
                                 "answers, its app list if readable, which "
                                 "platforms that app list proves, and any "
                                 "clients waiting to pair."),
    "/api/v1/moonlight/pin": ("POST", "Relay a pairing PIN to the Moonlight "
                                      "host. The PIN comes from the user's "
                                      "own Moonlight client and cannot be "
                                      "generated here; a 200 means the host "
                                      "accepted the submission, not that the "
                                      "client paired."),
}

#: Served, but deliberately undescribed: internal or legacy aliases whose
#: presence in a public contract would imply support they do not have.
UNDOCUMENTED: set[str] = set()


def served_routes(source: str | None = None) -> set[str]:
    """Every path `app.py` compares against, read from the source."""
    if source is None:
        source = (pathlib.Path(__file__).with_name("app.py")
                  .read_text(encoding="utf-8"))
    found = set(re.findall(r'route\.path == "(/[^"]*)"', source))
    # `route.path in ("/a", "/b")` is the other form the router uses, and
    # missing it made a served endpoint look undocumented in one direction
    # and undescribed in the other -- the first run of the drift test caught
    # exactly that with /api/v1/webhook.
    for group in re.findall(r'route\.path in \(([^)]*)\)', source):
        found.update(re.findall(r'"(/[^"]*)"', group))
    return found


def _operation(path: str, method: str, summary: str) -> dict:
    return {
        method.lower(): {
            "summary": summary,
            "operationId": (method.lower()
                            + re.sub(r"[^a-zA-Z0-9]+", "_", path).title()
                            .replace("_", "")),
            "responses": {
                "200": {"description": "OK"},
                "401": {"description": "Missing or wrong credential."},
                "429": {"description": "Rate limited. Retry-After says when."},
            },
        }
    }


def _guide_contracts(paths: dict) -> None:
    """Describe the guide's small JSON inputs and outcomes for API clients."""
    inputs = {
        "/api/v1/setup/complete": (
            {"mode": {"type": "string", "enum": ["organize", "download"],
                      "default": "organize"}}, []),
        "/api/v1/setup/dats": (
            {"directory": {"type": "string", "minLength": 1,
                           "description": "DAT folder as seen by the ROMarr process."}},
            ["directory"]),
        "/api/v1/hub/source/install": (
            {"url": {"type": "string", "format": "uri",
                     "description": "HTTPS repository on an allowed plugin source host."}},
            ["url"]),
    }
    for path, (properties, required) in inputs.items():
        operation = paths[path]["post"]
        operation["requestBody"] = {
            "required": bool(required),
            "content": {"application/json": {"schema": {
                "type": "object", "properties": properties, "required": required,
            }}},
        }
        if required:
            operation["responses"]["400"] = {
                "description": "The folder or repository is invalid; error explains the fix."}
    paths["/api/v1/hub/source/install"]["post"]["responses"]["500"] = {
        "description": "ROM Hub could not install the plugin; error contains the reason."}
    checklist_row = {
        "type": "object",
        "properties": {
            "id": {"type": "string"}, "name": {"type": "string"},
            "type": {"type": "string"}, "enabled": {"type": "boolean"},
        },
        "required": ["id", "name", "type", "enabled"],
        "additionalProperties": False,
    }
    paths["/api/v1/setup/status"]["get"]["responses"]["200"] = {
        "description": "Safe setup checklist; connection credentials are never returned.",
        "content": {"application/json": {"schema": {
            "type": "object",
            "properties": {
                "suggested": {"type": "boolean"}, "dismissed": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["organize", "download"]},
                "library_path": {"type": "string"}, "dat_path": {"type": "string"},
                "dat_loaded": {"type": "integer"}, "dat_games": {"type": "integer"},
                "hub_available": {"type": "boolean"},
                **{key: {"type": "array", "items": checklist_row}
                   for key in ("libraries", "download_clients", "indexers")},
            },
        }}},
    }


def spec(version: str = "0.0.0", *, base_url: str = "") -> dict:
    """The document served at /api/v1/openapi.json."""
    paths: dict[str, dict] = {}
    for path in sorted(DESCRIPTIONS):
        method, summary = DESCRIPTIONS[path]
        paths[path] = _operation(path, method, summary)
    _guide_contracts(paths)

    return {
        "openapi": VERSION,
        "info": {
            "title": "ROMarr",
            "version": version,
            "description": (
                "The *arr for games. Request a ROM, ROMarr finds it, grabs "
                "it, verifies it against a No-Intro or Redump DAT, and files "
                "it into your library.\n\n"
                "Every endpoint except `/`, `/api/health` and `/api/v1/login` "
                "requires a credential: `X-Api-Key`, `Authorization: Bearer`, "
                "or `?apikey=`."
            ),
            "license": {"name": "MIT"},
        },
        "servers": [{"url": base_url or "http://localhost:6868"}],
        "components": {
            "securitySchemes": {
                "ApiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
                "Bearer": {"type": "http", "scheme": "bearer"},
            }
        },
        "security": [{"ApiKey": []}, {"Bearer": []}],
        "paths": paths,
    }
