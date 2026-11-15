# SharewareZ feature comparison

Reviewed [SharewareZ](https://github.com/axewater/sharewarez) at commit
`736aff4` (v2.9.9). Its README lists discovery shelves, genre/rating filters,
browser play, Discord notifications, and NFO indexing/viewing. ROMarr already
provides discovery, metadata filters, browser-play routes and notifications.

This change adds an independently implemented, read-only NFO preview in the
Library game's shelf editor. Open **Release notes (NFO)** to read a same-stem
`.nfo` or `.NFO` beside a cached local folder game. UTF-8 and legacy CP437
ASCII art are supported; previews stop at 128 KiB. Content is rendered as text,
including XML/HTML-looking NFOs. Notes are not executed or interpreted.

The authenticated `GET /api/v1/game/nfo?id=...&source=...` endpoint accepts a
cached game's identity, not arbitrary paths. Reads are anchored to configured
folder backends, with symlinks and nonregular files rejected. No recursive
search, archive extraction, write, import, or scan of unrelated NFOs occurs.
Remote library-server IDs are not filesystem paths. Reads reuse the file-report filesystem pins: POSIX descriptor-relative
no-follow opens and Windows native handles that reject reparse points and deny
parent replacement. Other systems lacking secure opens return an explicit
unavailable message. Missing notes return `{"available": false}`.

No upstream source was copied. The inspected upstream commit has no tracked
LICENSE file, so no redistribution permission was assumed. This document credits
the public feature idea; ROMarr retains its existing MIT license.
