"""Catalogue rows captured by the browser extension, on their way into an index.

Some ROM sites cannot be catalogued by an HTTP client at all. ROM Hub's
`ctx.http` is GET-only and clears its cookie jar before every request, so a
plugin can never hold a session, submit a form, or follow a link that only
exists once the page's own JavaScript has run. A site gated behind any of
those is not partially reachable from a plugin; it is unreachable.

The operator's own browser answers this honestly. A person really browsing
sends real cookies and a real `Referer`, and passes a bot check because they
are not a bot. Nothing here is circumvented -- the requests were genuinely
made by a person who genuinely visited the page, and this module is only the
part that writes down what they saw.

WHAT ARRIVES HERE IS UNTRUSTED
------------------------------
The payload is assembled by JavaScript running in a page ROMarr does not
control. Every field is therefore treated as a claim to be checked rather than
a value to be stored:

  * the body is size-bounded BEFORE it is parsed (see `MAX_BODY_BYTES` and the
    check in `app._post`), because a declared gigabyte costs a gigabyte to
    read whether or not the reply is a refusal,
  * `source` must name a site this server already knows, and every `url` must
    live on one of that source's own hosts. Without that second rule a page
    could hand ROMarr a library row pointing anywhere it liked, which is the
    whole attack this endpoint would otherwise open,
  * every string is length-capped and every id is charset-bounded, and
  * a row that fails any of it is counted and reported rather than repaired.
    A capture that silently improved itself would be a capture nobody could
    check.

PLATFORMS ARE RESOLVED, NEVER INVENTED
--------------------------------------
`platforms.resolve()` is the only authority on what a platform name means, and
a name it does not know produces an `unmapped` entry in the report -- not a
guessed slug. `platforms.resolve` documents at length what guessing cost the
last time it was tried (4,173 rows filed under the wrong machine), and a slug
invented here would create an index file for a platform the library server
cannot place.

THE OUTPUT IS NOT A NEW FORMAT
------------------------------
Rows land in `idx-<slug>.jsonl` with a `.done` sidecar -- byte for byte the
shape `build_platform_index.py` and `vimm_index.py` already write, because a
capture that needed its own loader would be a capture that never got loaded.
The `.done` key is namespaced `<source>:<id>` for the reason `vimm_index.py`
namespaces its own: an index file is shared with the Archive.org indexer,
whose keys are bare identifiers, and a bare numeric id could collide with one.

Appends are single `write()` calls to a file opened in append mode, which is
what makes this safe to run while `idx-queue` is appending to the same file.
That is the same guarantee the other indexers rely on, arrived at the same
way, and it is why nothing here rewrites a line in place.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .platforms import resolve

log = logging.getLogger(__name__)

#: The largest capture body that will be read, let alone parsed.
#:
#: A listing page of 200 rows is roughly 60 KB of JSON, so this is generous by
#: an order of magnitude and still small enough that refusing costs nothing.
#: The extension splits anything larger into separate posts rather than
#: relying on this to be raised.
MAX_BODY_BYTES = 1_048_576

#: Rows accepted in one post. A page the operator actually opened cannot have
#: more titles on it than this; a payload that claims to is not a page.
MAX_ITEMS = 500

#: Length caps, applied before anything is written.
MAX_TEXT = 300
MAX_URL = 1_000

#: A size beyond this is a typo or a lie -- no single ROM or disc image is a
#: terabyte, and the field feeds a library's "how much would this cost me"
#: arithmetic.
MAX_SIZE_BYTES = 1 << 40

#: Ids that may appear in a `.done` key. Deliberately narrow: the key is
#: matched literally against a file the other indexers also write.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A source name. Never used to build a path -- the platform slug does that --
#: but it is written into every row and into the `.done` sidecar, so it is
#: bounded to the characters those files already contain.
_SOURCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: Characters a filesystem cannot hold. The same narrow set `vimm_index.py`
#: scrubs, and narrow on purpose: No-Intro names legitimately contain commas,
#: apostrophes and brackets, and scrubbing those would change the name the
#: library server matches on and break the match for no gain.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


#: source id -> (label, hosts whose URLs that source may claim).
#:
#: This table is the reason the endpoint is safe to point at a web page. The
#: extension decides which sites it reads; this decides which sites ROMarr
#: will accept rows *about*, and a URL on any other host is refused however
#: convincingly the payload is dressed. A capture claiming to be Vimm and
#: carrying links to somewhere else is exactly the shape of the attack, and it
#: is rejected here rather than noticed later in a library nobody audits.
#:
#: Adding a site is therefore two declarations, not one: an adapter in the
#: extension, and a line here. That asymmetry is deliberate -- the server does
#: not take a stranger's word for which hosts are legitimate.
SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "vimm": ("Vimm's Lair", ("vimm.net",)),
    "retrostic": ("Retrostic", ("retrostic.com",)),
    "theromdepot": ("The ROM Depot", ("theromdepot.com",)),
    "cdromance": ("CDRomance", ("cdromance.org", "cdromance.com")),
}

#: One writer at a time per process. The append itself is atomic, so this
#: guards the read-modify-write of the in-memory `.done` set rather than the
#: file: two posts arriving together must not both decide the same id is new.
_LOCK = threading.Lock()


class Rejected(ValueError):
    """The payload is not a capture. Nothing is written and the caller is told why.

    Separate from a row being skipped: a skipped row is one bad game in an
    otherwise good page, and is reported alongside the ones that worked. This
    is the whole post being malformed, which is a bug in the sender.
    """


def _text(value, limit: int = MAX_TEXT) -> str:
    """One line of plain text, whitespace folded, hard-capped.

    Folding runs of whitespace matters more than it looks: a title lifted from
    a table cell arrives with the newlines and indentation of the page's HTML
    in it, and those would be written into the index and shown to somebody.
    """
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _https_url_on(value, hosts: tuple[str, ...]) -> str:
    """A URL, if it is https and lives on one of `hosts`. Otherwise nothing.

    Subdomains count -- Vimm serves the vault from `vimm.net` and files from
    `dl3.vimm.net` -- but a suffix match alone would accept
    `vimm.net.evil.example`, so the boundary is checked explicitly.
    """
    raw = str(value or "")[:MAX_URL]
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    for allowed in hosts:
        if host == allowed or host.endswith("." + allowed):
            return raw
    return ""


def index_dir(env: dict[str, str] | None = None, data_path: str | Path = "") -> Path:
    """Where `idx-<slug>.jsonl` lives.

    Beside the settings file, because that is where the existing indexes
    already are: `romarr.json` and every `idx-*.jsonl` share `/opt/romarr` on
    a real install. Deriving it rather than hardcoding it is what lets a test
    -- and a Docker install that moved its data -- write somewhere else
    without a special case.
    """
    e = env if env is not None else os.environ
    override = (e.get("ROMARR_INDEX_DIR") or "").strip()
    if override:
        return Path(override)
    if data_path:
        return Path(data_path).parent
    return Path("/opt/romarr")


def _done_keys(path: Path) -> set[str]:
    """Ids already indexed for one platform, from the sidecar."""
    try:
        with path.open(encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    except OSError:
        return set()


def ingest(payload, *, directory: Path) -> dict:
    """Validate a capture and append the rows that survive it.

    Returns a report rather than a count. The operator posting from a browser
    needs to see *what happened to the page they were looking at* -- how many
    rows were new, how many the index already had, and which platforms ROMarr
    could not place -- because the alternative is an extension that says "sent"
    and an index that quietly grew by nothing.
    """
    if not isinstance(payload, dict):
        raise Rejected("a capture is a JSON object")

    source = _text(payload.get("source"), 32).lower()
    if not _SOURCE.match(source):
        raise Rejected("source must be a short lowercase name")
    if source not in SOURCES:
        # Named rather than hinted at. An operator who has just written an
        # adapter needs to know the server is the half that is missing.
        raise Rejected(
            f"unknown source {source!r}; ROMarr accepts captures from "
            f"{', '.join(sorted(SOURCES))}")
    label, hosts = SOURCES[source]

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise Rejected("items must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise Rejected(f"a capture carries at most {MAX_ITEMS} rows, "
                       f"not {len(items)}")

    accepted = duplicates = skipped = 0
    reasons: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    rows: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()

    def skip(reason: str) -> None:
        nonlocal skipped
        skipped += 1
        reasons[reason] = reasons.get(reason, 0) + 1

    for item in items:
        if not isinstance(item, dict):
            skip("not an object")
            continue

        ident = _text(item.get("id"), 64)
        if not _ID.match(ident):
            skip("no usable id")
            continue

        title = _text(item.get("title"))
        if not title:
            skip("no title")
            continue

        # Resolved, never guessed. An unmapped name is a mapping somebody can
        # add in one line of platforms.py; a guessed slug is a directory the
        # library server cannot place and a row nobody goes back to check.
        claimed = _text(item.get("platform"), 120)
        platform = resolve(claimed) if claimed else None
        if platform is None:
            unmapped[claimed or "(none)"] = unmapped.get(claimed or "(none)", 0) + 1
            skip("platform not mapped")
            continue

        url = _https_url_on(item.get("url"), hosts)
        if not url:
            # Worth its own reason: this is the one rejection that means the
            # payload was wrong about something load-bearing rather than
            # merely incomplete.
            skip(f"url is not https on {label}")
            continue

        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            skip("no size")
            continue
        if not 0 < size <= MAX_SIZE_BYTES:
            skip("implausible size")
            continue

        key = f"{source}:{ident}"
        if key in seen:
            # The same title twice on one page -- a listing that repeats a row
            # in a "popular" strip, most often. Counted as a duplicate rather
            # than written twice.
            duplicates += 1
            continue
        seen.add(key)

        region = _text(item.get("region"), 120)
        # The dump's own filename is what the library server should show and
        # match on; the title plus region is only a fallback for a site that
        # never publishes one.
        name = _text(item.get("name"), MAX_TEXT)
        if not name:
            name = f"{title} ({region or 'Unknown'})"

        row = {
            "name": _ILLEGAL.sub("_", name),
            "size": size,
            "url": url,
            "source": source,
            "id": ident,
            "title": title,
            "region": region,
            "version": _text(item.get("version"), 60),
            "platform": platform.slug,
        }
        # Carried when the site has one, because it is the handle a loader
        # needs to implement the site's real download flow. Vimm's capture of
        # 23,980 rows declared this key and filled it on none of them, and the
        # index that resulted had no actionable handle at all.
        media = _text(item.get("media_id"), 64)
        if media:
            row["media_id"] = media

        rows.setdefault(platform.slug, []).append(
            (key, json.dumps(row, ensure_ascii=False)))
        by_platform[platform.slug] = by_platform.get(platform.slug, 0) + 1

    written: dict[str, int] = {}
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        for slug, pending in rows.items():
            done_path = directory / f"idx-{slug}.done"
            done = _done_keys(done_path)
            fresh = [(key, line) for key, line in pending if key not in done]
            duplicates += len(pending) - len(fresh)
            if not fresh:
                continue
            # Append mode and one write per row: that combination is what
            # makes this safe while `idx-queue` is appending to the same file
            # from another process, and it is why nothing here rewrites a line.
            with (directory / f"idx-{slug}.jsonl").open(
                    "a", encoding="utf-8") as index:
                for _, line in fresh:
                    index.write(line + "\n")
            with done_path.open("a", encoding="utf-8") as sidecar:
                for key, _ in fresh:
                    sidecar.write(key + "\n")
            written[slug] = len(fresh)
            accepted += len(fresh)

    log.info("capture from %s: %d indexed, %d already known, %d skipped",
             source, accepted, duplicates, skipped)
    return {
        "ok": True,
        "source": source,
        "site": label,
        "seen": len(items),
        "indexed": accepted,
        "already_indexed": duplicates,
        "skipped": skipped,
        "skipped_reason": reasons,
        # A list of pairs rather than a dict, because these are site labels
        # and a site is entitled to call a platform whatever it likes --
        # including something that is not a usable key.
        "unmapped_platforms": [{"platform": name, "count": count}
                               for name, count in sorted(unmapped.items())],
        "platforms": {slug: written.get(slug, 0) for slug in sorted(by_platform)},
    }


def status(directory: Path) -> dict:
    """What the extension's connection test needs to report a real result.

    Deliberately does something rather than answering `{"ok": true}`: a test
    that only proves a route exists would pass against a server with an
    unwritable data directory, and the operator would find out when a capture
    silently went nowhere.
    """
    writable = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".romarr-capture-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as err:
        log.warning("capture index directory %s is not writable: %s",
                    directory, err)
    return {
        "ok": writable,
        "index_writable": writable,
        "max_items": MAX_ITEMS,
        "max_body_bytes": MAX_BODY_BYTES,
        "sources": [{"source": key, "site": label, "hosts": list(hosts)}
                    for key, (label, hosts) in sorted(SOURCES.items())],
    }
