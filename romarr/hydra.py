"""Hydra Launcher download sources as a ROMarr indexer.

A Hydra source is one JSON document -- ``{"name": ..., "downloads": [...]}`` --
that lists releases with their links. Hydra Launcher made the format and its
community keeps hundreds of them; https://library.hydra.wiki catalogues them,
and its API (``api.hydralibrary.com/sources``) is what the Indexers page
offers to pick from.

The idea and the shape of the reader are ported from RetroArr's HydraIndexer
(MIT, Copyright (c) 2026 RetroArr): fetch the document once an hour, filter it
here because it has no query interface, and send a magnet to a torrent client
and an HTTP link to a direct downloader. Three things are ROMarr's own:

* **Platforms.** Console sources (Redump, No-Intro, Rutracker consoles) tag
  every row with a ``platform`` code -- ``ps2``, ``ns``, ``gc``, ``GB/GBC``.
  That code becomes ``Release.platform_hint``, which the scorer treats as
  evidence for that platform and as a refusal for every other one, so a PS2
  request never grabs the PS3 game of the same name.
* **Every link kept.** A row often offers a magnet and four file hosts. The
  best one becomes the download link and the rest ride along as
  ``Release.mirrors``, so a file host that a debrid service cannot unlock
  falls through to the next instead of failing the grab.
* **Link kinds.** ``magnet:`` is ``torrent``. An HTTP link to a file
  (``.zip``, ``.7z``, ``.iso``…) is ``direct`` -- Mister Download Manager or
  the Direct HTTP client fetches it. An HTTP link to a file host's page
  (gofile, 1fichier, pixeldrain…) is ``hoster``; see ``romarr.hosters``.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from urllib.parse import unquote, urlsplit

import requests

from .hosters import is_file_host, looks_like_file
from .platforms import resolve, same_machine
from .selection import Release

log = logging.getLogger(__name__)

#: A source is a static list; a sweep that asks for forty games reads it once.
CACHE_SECONDS = 3600
#: The largest real source measured is ~15 MB (No-Intro, 20,051 rows).
MAX_SOURCE_BYTES = 64 * 1024 * 1024
#: Where library.hydra.wiki reads its catalogue of sources.
LIBRARY_API = "https://api.hydralibrary.com/sources"
#: Newznab "Console" and "PC/Games": what a row is when nothing says more.
CONSOLE_CATEGORY = 1000
PC_CATEGORY = 4050

_CACHE: dict[str, tuple] = {}   # url -> (fetched, document, (name, categories), releases)
_CACHE_LOCK = Lock()

# Hydra platform codes that ROMarr's resolver does not already read. Codes it
# does read (ps2, nes, x360, MAME, "Sega Saturn"…) need no row here.
_PLATFORM_CODES = {
    "ns": "switch", "ns2": "switch-2", "switch": "switch",
    "gc": "ngc", "ngc": "ngc",
    "psv": "psvita", "vita": "psvita",
    "psx_psp": "psp",
    "nec pce": "turbografx16--1", "pce": "turbografx16--1",
    "pcecd": "turbografx-16-slash-pc-engine-cd",
    "ps1": "psx", "psx": "psx",
}


def platform_slug(code) -> str:
    """ROMarr's slug for a Hydra ``platform`` value, or "" when unknown.

    Codes like ``GB/GBC`` and ``NEC PC Engine CD-TurboGrafx-CD`` name two
    labels for one machine; each half is tried until one resolves.
    """
    raw = str(code or "").strip()
    if not raw:
        return ""
    known = _PLATFORM_CODES.get(raw.lower())
    if known:
        return known
    found = resolve(raw)
    if found is not None:
        return found.slug
    slugs = []
    for part in re.split(r"\s*[/|]\s*|\s+-\s*|-(?=[A-Z])", raw):
        part = part.strip()
        if not part:
            continue
        known = _PLATFORM_CODES.get(part.lower())
        found = known or (resolve(part).slug if resolve(part) is not None else "")
        if found:
            slugs.append(found)
    if not slugs:
        return ""
    # "GB/GBC" names two machines: claiming either one would refuse the
    # other's releases. Only a tag whose parts agree gives a hint.
    first = slugs[0]
    return first if all(same_machine(first, s) for s in slugs[1:]) else ""


_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KMGT]?i?B)\b", re.IGNORECASE)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size(value) -> int:
    """Bytes from ``"1.79 GB"``, ``"752 MB"``, ``"From 3,5 GB"`` or a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(int(value), 0)
    match = _SIZE_RE.search(str(value or ""))
    if not match:
        return 0
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper().replace("I", "")
    return int(number * _UNITS.get(unit, 1))


def _uris(row: dict) -> list[str]:
    raw = row.get("uris")
    if raw is None:
        raw = row.get("uri") or row.get("links") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def link_kind(url: str) -> str:
    """``torrent``, ``direct``, ``hoster`` or "" for a link ROMarr cannot use."""
    lowered = url.lower()
    if lowered.startswith("magnet:"):
        return "torrent"
    if not lowered.startswith(("http://", "https://")):
        return ""
    if is_file_host(url):
        return "hoster"
    if looks_like_file(url):
        return "direct"
    # An HTTP link that is neither a known file host nor a file: most often a
    # forum or shop page. Kept, as a last resort, for a debrid service.
    return "hoster"


_KIND_ORDER = {"torrent": 0, "direct": 1, "hoster": 2}


def _published(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def parse_source(document, *, indexer: str = "Hydra source",
                 categories: tuple[int, ...] = ()) -> list[Release]:
    """Every usable release in a parsed Hydra source document."""
    rows = document.get("downloads") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = html.unescape(str(row.get("title") or "")).strip()
        links = [(link_kind(u), u) for u in _uris(row)]
        links = [(k, u) for k, u in links if k]
        if not title or not links:
            continue
        links.sort(key=lambda pair: _KIND_ORDER[pair[0]])
        kind, url = links[0]
        slug = platform_slug(row.get("platform"))
        cats = tuple(categories) or ((CONSOLE_CATEGORY,) if slug and slug != "win"
                                      else (PC_CATEGORY,))
        extra = {}
        published = _published(row.get("uploadDate"))
        if published and "published" in Release.__dataclass_fields__:
            extra["published"] = published
        release = Release(
            title=title,
            size=parse_size(row.get("fileSize") or row.get("size")),
            # A source publishes no seeder count. 1, and private, so a magnet
            # is ranked on its title and size rather than refused as dead --
            # the same reading a bare RSS feed gets.
            seeders=1 if kind == "torrent" else 0,
            categories=cats,
            download_url=url,
            protocol=kind,
            indexer=indexer,
            private=kind == "torrent",
            info_url=str(row.get("repackLinkSource") or row.get("page") or ""),
            platform_hint=slug,
            mirrors=tuple(u for k, u in links[1:] if k != "torrent"),
            **extra,
        )
        out.append(release)
    return out


def _words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", unquote(str(text or "")))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).split()


@dataclass
class HydraSourceConfig:
    url: str
    name: str = "Hydra source"
    categories: tuple[int, ...] = ()
    timeout: int = 60


class HydraSource:
    """One Hydra source URL, searched like an indexer."""

    def __init__(self, config: HydraSourceConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def name(self) -> str:
        return self._config.name

    def _fetch(self) -> list:
        response = self._session.get(
            self._config.url, timeout=self._config.timeout, stream=True,
            headers={"User-Agent": "ROMarr", "Accept": "application/json"})
        try:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_SOURCE_BYTES:
                raise ValueError(f"{self.name} is larger than "
                                 f"{MAX_SOURCE_BYTES // (1024 * 1024)} MB")
            chunks, total = [], 0
            stream = getattr(response, "iter_content", None)
            if callable(stream):
                pieces = stream(1024 * 1024)
            else:   # a response that cannot stream: read it whole
                body = getattr(response, "content", None)
                pieces = [body if isinstance(body, bytes)
                          else str(getattr(response, "text", "") or "").encode("utf-8")]
            for chunk in pieces:
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError(f"{self.name} is larger than "
                                     f"{MAX_SOURCE_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
        finally:
            response.close()
        try:
            return json.loads(b"".join(chunks).decode("utf-8-sig"))
        except ValueError as err:
            raise ValueError(f"{self.name} did not return a Hydra source "
                             f"(not JSON: {err.__class__.__name__})") from None

    def releases(self) -> list:
        """The whole source: the document is fetched at most once an hour per
        URL, and parsed again whenever this indexer's name or categories
        differ from the last parse, so an edit takes effect at once."""
        url = self._config.url
        shape = (self._config.name, tuple(self._config.categories))
        with _CACHE_LOCK:
            hit = _CACHE.get(url)
            fresh = hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS
            if fresh and hit[2] == shape:
                return hit[3]
            document = hit[1] if fresh else None
        if document is None:
            document = self._fetch()
            stamp = time.monotonic()
        else:
            stamp = hit[0]
        parsed = parse_source(document, indexer=self._config.name,
                              categories=self._config.categories)
        with _CACHE_LOCK:
            _CACHE[url] = (stamp, document, shape, parsed)
        return parsed

    def caps(self) -> bool:
        """A source that parses to at least one usable release is reachable."""
        if not self.releases():
            raise ValueError(f"{self.name} has no downloads ROMarr can use")
        return True

    def search(self, term: str, *, limit: int = 100) -> list[Release]:
        wanted = [w for w in _words(term) if len(w) > 2] or _words(term)
        if not wanted:   # RSS sync asks with an empty query: the newest rows
            return list(self.releases())[:limit]
        out = []
        for release in self.releases():
            haystack = set(_words(release.title))
            if all(w in haystack for w in wanted):
                out.append(release)
                if len(out) >= limit:
                    break
        return out


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


_LIBRARY_CACHE: dict[str, tuple[float, list]] = {}


def library_sources(*, query: str = "", session=None, timeout: int = 30) -> list[dict]:
    """Hydra sources listed by library.hydra.wiki: name, URL and its status labels.

    Read once an hour. Only https sources ending in .json are returned,
    because that is all ROMarr can add.
    """
    with _CACHE_LOCK:
        hit = _LIBRARY_CACHE.get("all")
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            rows = hit[1]
        else:
            rows = None
    if rows is None:
        http = session or requests
        rows, page = [], 1
        while page <= 20:
            response = http.get(LIBRARY_API, params={"page": page, "limit": 100},
                                timeout=timeout, headers={"User-Agent": "ROMarr"})
            response.raise_for_status()
            body = response.json()
            for item in body.get("sources") or []:
                url = str(item.get("url") or "").strip()
                if not (url.startswith("https://") and urlsplit(url).path.endswith(".json")):
                    continue
                rows.append({"name": str(item.get("title") or item.get("name") or url),
                             "url": url,
                             "status": [str(s) for s in item.get("status") or []]})
            if page >= int(body.get("totalPages") or 1):
                break
            page += 1
        with _CACHE_LOCK:
            _LIBRARY_CACHE["all"] = (time.monotonic(), rows)
    needle = query.strip().lower()
    return [r for r in rows if not needle or needle in r["name"].lower()]
