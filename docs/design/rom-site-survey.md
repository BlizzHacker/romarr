# ROM site survey — seven candidate ROM Hub plugins

Surveyed 2026-08-12. Every row below was established by **fetching the site**,
not by recalling what it used to be. Each claim names the request that proves
it, because three of the seven turn out to be something other than what the
list assumed.

The survey exists to answer one question — *which of these can become an RPP v1
plugin at all* — and the answer is decided less by each site's HTML than by two
hard properties of the plugin runtime. They are worth stating first, because
they eliminate two candidates outright before any parsing question is reached.

## What the plugin runtime can actually do

From `rom_hub_sdk.context` and `rom_hub/broker/fetcher.py`:

* **`ctx.http` offers `get()` and nothing else.** There is no POST. A site whose
  download or login is a form submission is not partially supported, it is
  unsupported.
* **Cookies are cleared before every request** (`fetcher.get` calls
  `self._client.cookies.clear()`), deliberately, so one plugin's `Set-Cookie`
  cannot ride along on another's request. A plugin therefore **cannot hold a
  session**, so it cannot be logged in, so a site that gates browsing behind a
  login is closed to it.
* **HTTPS only, redirects not followed, 4 MiB response cap**, and the
  User-Agent is the host's (`rom-hub/0.1 (+https://github.com/rommapp/romm)`) —
  a plugin cannot set its own, which also means it cannot forge one.
* Every URL a plugin returns or requests is checked against its manifest
  `[permissions] network` allowlist by `rom_hub/netpolicy.py`. That list is
  enforcement, not documentation.

The 4 MiB cap is not a footnote: it is the reason the Retrostic plugin below
searches rather than mirrors, because one of Retrostic's own sitemap files is
4.9 MB and `ctx.http` would refuse it.

## The table

| # | Site | Unauth. paginated listing? | Stable detail id? | Anti-bot | Login needed? | Still hosts files? | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **Retrostic** | **Yes** — published sitemaps: `consoles.xml` (44) + `roms1/roms2.xml` (**83,797** ROM URLs); plus `GET /search?search_term_string=&currentpage=` | **Yes** — `/roms/<console>/<title-slug>-<id>`, numeric id | Cloudflare fronting, **not challenging**; `robots.txt` is `Disallow:` (allow-all), no crawl-delay | No | Yes — but download is **POST + per-page token** | **BUILD THIS.** search + stream; importer refused |
| 2 | RomuLation | Yes — `sitemap.xml`, 12,243 `/rom/<PLATFORM>/<slug>` + 17 platform pages | Slug only, no numeric id | None observed | No, to browse | Yes — but `/roms/newdownload/…` is **`Disallow`ed in its own robots.txt** and carries an encrypted token | Buildable, browse/metadata only |
| 3 | EmuParadise | Yes — `/<Platform>_ROMs/<id>` + `Games-Starting-With-<A–Z>`; robots is `Allow: /` | Yes — numeric, `/<Platform>_ROMs/<Title>/<id>` | None | No | **NO — downloads are gone** | Metadata-only, or skip |
| 4 | The ROM Depot | `GET /api/getContents` returns the **whole tree in one JSON** — but **401** | path + size + mtime | None | **Yes, to browse at all** | Presumably | **Not buildable as a direct plugin** |
| 5 | CDRomance | Unknown | Unknown | **Cloudflare managed challenge on every path, including `/robots.txt`** | Unknown | Unknown | **Not buildable** — needs defeating a bot check |
| 6 | CoolROM | Not surveyed | Not surveyed | Cloudflare; robots sets `Crawl-delay: 10` | Unknown | Unknown | **Not surveyed on purpose** — see below |
| 7 | Gamulator | — | — | — | — | **Site is down** | Dead |

## The evidence, site by site

### 1. Retrostic — the pick

`https://www.retrostic.com/robots.txt` is the most permissive of the seven:

```
User-agent: *
Disallow:

Sitemap: https://www.retrostic.com/sitemaps/index.xml
Sitemap: https://www.retrostic.com/sitemaps/consoles.xml
Sitemap: https://www.retrostic.com/sitemaps/roms.xml
```

An empty `Disallow:` allows everything, and no crawl-delay is declared for
`*`. There is no rule naming any AI or generic agent. The catalogue is
*published*, so it never has to be discovered by crawling:

* `consoles.xml` — 44 console slugs.
* `roms.xml` — a sitemap **index** pointing at `roms1.xml` (50,000 URLs, 4.9 MB)
  and `roms2.xml` (33,797 URLs, 3.1 MB) — **83,797 ROM URLs total**.

`roms1.xml` at 4.9 MB is over `ctx.http`'s 4 MiB ceiling, which is why the
plugin does not ingest sitemaps at runtime. It does not need to: the site
offers a plain GET search, `GET /search?search_term_string=contra`, 30 results
a page, paginated by `&currentpage=N` — one request per query instead of a
83,797-page mirror. That is both easier and considerably politer.

Detail pages carry a real metadata table (`/roms/nes/contra-388`):

| Parameter | Info |
|---|---|
| File Name | `Contra (USA).zip` |
| Region | `US` (`itemprop="gameLocation"`) |
| Console | `NES` (`itemprop="gamePlatform"`) |
| File Size | `unknown` (`itemprop="fileSize"` — frequently unset) |
| Downloads | `127251` |

**Why there is no importer.** The download control is a form:

```html
<form method="post" action="/roms/nes/contra-388/download" id="dl">
  <input type="hidden" name="rom_url"     value="contra-388">
  <input type="hidden" name="console_url" value="nes">
  <input type="hidden" name="session"     value="2511784539">
</form>
```

POST, plus a per-page-render `session` value. `ctx.http` cannot POST and the
host's own downloader is `stream("GET", …)` (`rom_hub/importer.py:203`), so
neither half of the system can submit it. The play page additionally embeds
`https://downloads.retrostic.com/roms/Contra%20(USA).zip` inside its emulator
iframe, and reaching into that to synthesise a direct GET **would** produce a
working importer — by routing around the download flow the site actually
publishes. That is the same call the Vimm work made when it refused to forge a
`Referer`, so the importer is declined and says so out loud rather than
guessing a URL.

**Stream is genuine.** `/roms/<console>/<slug>/play` returns 200 and hosts the
site's own player. The plugin returns that page — the site's URL, with the
site's branding intact — rather than the inner iframe target.

Platform mapping: 43 of the 44 consoles resolve against ROMarr's
`platforms.resolve()`. `bbc-micro` (4,753 ROMs) has no ROMarr platform, and
`neo-geo` (357) is deliberately unmapped — see the plugin README.

### 2. RomuLation — browse yes, download no

Its robots.txt is specific, and it is the reason the importer is off the table:

```
Disallow: /roms/images
Disallow: /roms/download
Disallow: /roms/newdownload
Disallow: /roms/archive
```

The live download control on `/rom/NDS/0008-Pac-Pix-(U)` is
`/roms/newdownload/guest/7015/<base64 Laravel iv+value+mac payload>` — i.e. it
sits under a path the site's own robots.txt forbids, *and* it is tokenised per
render. Honouring robots.txt means an importer cannot exist here. Browsing is
fine: `sitemap.xml` publishes 12,243 `/rom/<PLATFORM>/<slug>` detail URLs across
17 platforms, no challenge, no login. Note the id is the slug — there is no
numeric id — so entries are keyed by path.

### 3. EmuParadise — verified: the downloads really are gone

This was called out for specific verification, and the suspicion is correct.
Browsing is entirely healthy: `robots.txt` is `Allow: /`, the platform index,
the `Games-Starting-With-<letter>` pages and the numeric-id detail pages all
return 200, and the section counts are live (NES 2,774; MAME 34,305; PSX 5,134).

But every `…/<id>-download` page ends the same way. Three well-known NES
titles, fetched individually:

| Title | Page | Result |
|---|---|---|
| Castlevania (USA) | `/…/55051-download` | `This game is unavailable` |
| Contra (USA) | `/…/55137-download` | `This game is unavailable` |
| Final Fantasy (USA) | `/…/55514-download` | `This game is unavailable` |

Each page still renders "We are preparing your download… Please scroll down to
get your download link!" and then states the game is unavailable — the download
*chrome* survived the 2018 removal, the files did not.

**Recommendation: skip it, or build it explicitly as metadata-only.** An
importer here would be a capability that always refuses. If the browse tree is
wanted for its titles and ids, that is a legitimate metadata plugin — but it
must be declared as one, not shipped as a download source that never delivers.

### 4. The ROM Depot — blocked by the runtime, not by the site

The most interesting near-miss. It is a Vite/React SPA (`/robots.txt` itself
returns the SPA shell, so there is no robots.txt), and its JS bundle
(`/assets/index-DVaFPJRP.js`, 989 KB) names a clean, small API:

```
/api/getContents   /api/download/   /api/login   /api/checkLoginStatus
/api/profile       /api/download-history   …
```

`GET /api/getContents` is a single unpaginated call returning the entire
directory tree as JSON (`name`, `size`, `mtime`, `path`, child counts) — which
would have made it the easiest of all seven by a distance.

It returns **`401 {"message":"Unauthorized"}`** unauthenticated. Per the survey
constraints I did not attempt to log in, and the owner's account was not used.

That is academic anyway, because the bundle shows the session is established by
`fetch("/api/login", {method:"POST", …})` and carried by a **cookie**
(credentials are `same-origin`; no `Authorization` header appears anywhere in
the bundle). A plugin has no POST and has its cookie jar cleared before every
request. **A ROM Hub plugin cannot authenticate here — not as a limitation to
work around, but by the runtime's design.**

The only honest route would be the Vimm pattern: an operator-run browser
extension that captures a catalogue while the operator is logged in, with the
provider reading that catalogue and revalidating every URL. That is a real
option and a considerably larger build than a fetch-based plugin. It is not the
easiest one, and it should be a deliberate decision rather than a default.

### 5. CDRomance — stop

Every path, `/robots.txt` included, returns **HTTP 403 with a Cloudflare
managed challenge** (`cf_chl_opt`, `cType: 'managed'`, "Enable JavaScript and
cookies to continue"). Not a rate limit and not a soft check — the site cannot
be read at all without executing the challenge.

Getting past that is exactly the circumvention this project does not do, so
this is a full stop rather than a hard problem. **Not buildable. Recommend
dropping it from the list.**

### 6. CoolROM — not surveyed, deliberately

`https://coolrom.com/robots.txt` (the `.com.au` host 301s to it) resolves and
is readable, and it contains this:

```
User-agent: GPTBot
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: Claude-Web
Disallow: /
```

The site operator has explicitly asked Anthropic agents not to crawl it. **I
stopped there and fetched no further pages**, so I cannot report its listing
shape, its id scheme or whether it still serves files — and I would rather
report that gap than fill it by ignoring the request.

Two things follow, and they should not be conflated:

* **I should not survey it.** That is settled by the rule above.
* **Whether the operator's own plugin may fetch it is a separate question** for
  the operator, not for me. `rom-hub/0.1` is not `anthropic-ai`; it falls under
  `User-agent: *`, which allows the site with `Crawl-delay: 10` and disallows
  `/api/`, `/members/`, `/admin/`, `/cr-admin/`, `/ads/`, `/queue.php`.

So: buildable in principle by someone who is not me, at a 10-second crawl
delay, against a structure nobody has yet verified. It should not be ranked or
scheduled until somebody surveys it firsthand.

### 7. Gamulator — dead

* `gamulator.com` — **NXDOMAIN**. The apex has no address record at all.
* `www.gamulator.com` — resolves to Cloudflare (104.26.8.39, 104.26.9.39,
  172.67.68.38) and every path returns **HTTP 530, `error code: 1016`** —
  Cloudflare's "Origin DNS error", i.e. the DNS record is still pointed at
  Cloudflare but Cloudflare can no longer find an origin behind it.

Nothing to plug into. **Drop it.**

## Difficulty ranking

| Rank | Site | Why |
|---|---|---|
| **1** | **Retrostic** | Allow-all robots, published sitemaps, a plain **GET** search, stable numeric ids, structured detail metadata, 43/44 platforms resolving, and a real stream target. No login, no challenge, no token needed for anything the plugin does. |
| 2 | RomuLation | Also clean and unauthenticated, but the id is a slug, the catalogue is one 2.5 MB sitemap, and robots.txt forbids the download paths — so it is a browse-only plugin with less to work from. |
| 3 | EmuParadise | Structurally the simplest HTML of all, and its browse tree is healthy — but the files are gone, so the useful half of a plugin cannot exist. Ranked here only as a metadata source. |
| 4 | CoolROM | Possibly fine, genuinely unknown. Cannot be ranked on evidence because it must be surveyed by someone else first. |
| 5 | The ROM Depot | Would be #1 if `ctx.http` had POST and a cookie jar. It does not, by design. Needs the Vimm extension pattern or nothing. |
| — | CDRomance | Excluded: unreadable without defeating a Cloudflare challenge. |
| — | Gamulator | Excluded: the site is down. |

**Pick: Retrostic**, built first as the reference the others are cloned from.

## What shipped

All seven repositories exist under `BlizzHacker` on git.moveweight.com, built
in the difficulty order above so each one cloned a proven pattern.

| Repo | Capabilities | Tests |
|---|---|---|
| `romarr-plugin-retrostic` | `search`, `stream` | 100 |
| `romarr-plugin-romulation` | `search` | 70 |
| `romarr-plugin-emuparadise` | `search` (metadata only) | 112 |
| `romarr-plugin-theromdepot` | none — documentation + catalogue entry | — |
| `romarr-plugin-cdromance` | none — documentation + catalogue entry | — |
| `romarr-plugin-coolrom` | none — documentation + catalogue entry | — |
| `romarr-plugin-gamulator` | none — documentation + catalogue entry | — |

The four with no capabilities ship a `catalog/plugins.json` marked
`status: "broken"` so they appear in ROMarr's plugin catalogue as *known and
blocked* rather than as missing, each with the specific reason. ROM Hub
rejects a manifest declaring zero capabilities (`rom_hub/manifest.py`), so
none of them ships a `manifest.toml` — they are deliberately not installable.

## What the reference plugin establishes for the clones

* Sitemaps are for *deciding the platform table offline*; the runtime path is
  the site's own search endpoint. Bulk mirroring is neither needed nor kind.
* A capability that cannot be performed honestly is **omitted from
  `[capabilities]` and explained in the README** — not shipped as one that
  raises at runtime.
* The platform table is checked against `romarr.platforms.resolve()` by a test,
  so a slug cannot be invented. Systems with no ROMarr platform are counted,
  skipped, and named in the README.
* `[permissions] network` lists exactly the hosts used and no more.
