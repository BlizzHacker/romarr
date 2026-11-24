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
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from urllib.parse import unquote, urlsplit

import requests

from .hosters import host_label, is_file_host, looks_like_file, public_file_url
from .platforms import resolve, same_machine
from .selection import Release

log = logging.getLogger(__name__)

#: Where the step-by-step guide lives; every error message can point at it.
GUIDE_URL = "https://github.com/BlizzHacker/romarr/blob/main/docs/HYDRA.md"


class SourceError(ValueError):
    """A Hydra source could not be read, said the way an operator can act on.

    The message is the whole point: it is shown as-is by the Indexers page's
    Test button and written to the log by a search, so it names the host and
    the next thing to try instead of carrying a urllib3 traceback.
    """

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


def _rows(document) -> list | None:
    """The ``downloads`` list of a source document, or None when it has none."""
    rows = document.get("downloads") if isinstance(document, dict) else document
    return rows if isinstance(rows, list) else None


def parse_source(document, *, indexer: str = "Hydra source",
                 categories: tuple[int, ...] = ()) -> list[Release]:
    """Every usable release in a parsed Hydra source document."""
    rows = _rows(document)
    if rows is None:
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


def check_url(url: str) -> str:
    """The URL, stripped, or a SourceError saying why it cannot be a source."""
    text = str(url or "").strip()
    if not text:
        raise SourceError("Paste the source's address: a web link ending in "
                          ".json, such as one from the library list.")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceError("The source URL must be a full web address that "
                          "starts with https://, such as "
                          "https://example.org/source.json.")
    return text


def explain_request_error(err: Exception, url: str, timeout: int) -> str:
    """One sentence for a failed request, naming the host and what to do.

    Order matters: requests' SSLError and ConnectTimeout are also
    ConnectionErrors, so the specific ones are asked first.
    """
    host = urlsplit(str(url)).hostname or str(url)
    response = getattr(err, "response", None)
    if isinstance(err, requests.HTTPError) and response is not None:
        code = response.status_code
        if code == 404:
            return (f"{host} answered 404 Not Found: this source has moved or "
                    "been removed. Pick it again from the library, or check "
                    "the URL.")
        if code in (401, 403):
            return (f"{host} refused to send the source (HTTP {code}). Hydra "
                    "sources need no account or API key, so the host is "
                    "blocking this server. Try another copy of the source "
                    "from the library.")
        if code == 429:
            return (f"{host} asked ROMarr to slow down (HTTP 429). Wait a few "
                    "minutes and test again.")
        if code >= 500:
            return (f"{host} is having trouble (HTTP {code}). Try again "
                    "later.")
        return f"{host} answered HTTP {code} instead of sending the source."
    if isinstance(err, requests.Timeout):
        return (f"{host} did not answer within {timeout} seconds. Large "
                "sources take a while: raise Request timeout under Advanced "
                "and test again.")
    if isinstance(err, requests.exceptions.SSLError):
        return (f"{host}'s HTTPS certificate could not be verified, so ROMarr "
                "did not read the source.")
    if isinstance(err, (requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema,
                        requests.exceptions.InvalidURL)):
        return ("The source URL must be a full web address that starts with "
                "https://.")
    if isinstance(err, requests.ConnectionError):
        text = str(err).lower()
        if "resolve" in text or "name or service" in text or "nodename" in text:
            return (f"ROMarr could not look up {host}. Check the URL for "
                    "typos, and that this server can reach the internet.")
        if "refused" in text:
            return f"Nothing answered at {host} (connection refused)."
        return (f"ROMarr could not connect to {host}. Check the URL and this "
                "server's internet connection.")
    return f"{host} could not be read ({type(err).__name__})."


def explain_document(document, name: str) -> str:
    """Why a document that parsed as JSON is still not a usable source, or ""."""
    rows = _rows(document)
    if rows is None:
        return (f"{name} is JSON but not a Hydra source: it has no "
                "\"downloads\" list. Check that the URL points at the "
                "source file itself.")
    if not rows:
        return f"{name} is a Hydra source with no downloads in it yet."
    count = len(rows)
    return (f"{name} has no downloads ROMarr can use: it lists {count:,} "
            f"row{'' if count == 1 else 's'}, but none has both a title and a "
            "magnet or web link.")


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
        url = check_url(self._config.url)
        try:
            return self._download(url)
        except requests.RequestException as err:
            raise SourceError(explain_request_error(
                err, url, self._config.timeout)) from None

    def _download(self, url: str) -> list:
        response = self._session.get(
            url, timeout=self._config.timeout, stream=True,
            headers={"User-Agent": "ROMarr", "Accept": "application/json"})
        try:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_SOURCE_BYTES:
                raise SourceError(f"{self.name} is larger than "
                                  f"{MAX_SOURCE_BYTES // (1024 * 1024)} MB, "
                                  "the most ROMarr reads from one source.")
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
                    raise SourceError(f"{self.name} is larger than "
                                      f"{MAX_SOURCE_BYTES // (1024 * 1024)} MB, "
                                      "the most ROMarr reads from one source.")
                chunks.append(chunk)
        finally:
            response.close()
        body = b"".join(chunks)
        try:
            return json.loads(body.decode("utf-8-sig"))
        except ValueError:
            if body.lstrip()[:1] == b"<":
                raise SourceError(
                    f"{self.name} returned a web page, not a Hydra source (not "
                    "JSON). Use the address of the .json file itself; on "
                    "GitHub that is the Raw link.") from None
            raise SourceError(f"{self.name} did not return a Hydra source (not "
                              "JSON). Check that the URL points at the source "
                              "file.") from None

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

    def _document(self):
        """The cached document behind the last `releases()` call, or None."""
        with _CACHE_LOCK:
            hit = _CACHE.get(self._config.url)
        return hit[1] if hit else None

    def caps(self) -> bool:
        """A source that parses to at least one usable release is reachable."""
        if not self.releases():
            raise SourceError(explain_document(self._document(), self.name))
        return True

    def summary(self) -> dict:
        """What the source holds: counts by link kind, platforms and hosts.

        Raises SourceError, like `caps`, when nothing in it is usable.
        """
        self.caps()
        document = self._document()
        named = document.get("name") if isinstance(document, dict) else ""
        return {**summarize(self.releases(), rows=len(_rows(document) or [])),
                "name": str(named or "").strip()[:120]}

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


def _platform_label(slug: str) -> str:
    found = resolve(slug)
    return found.name if found is not None else slug


def summarize(releases: list, *, rows: int) -> dict:
    """Counts an operator can check a source against before saving it."""
    kinds = Counter(r.protocol for r in releases)
    platforms = Counter(r.platform_hint for r in releases if r.platform_hint)
    hosts = Counter(host_label(r.download_url) for r in releases
                    if r.protocol == "hoster")
    # A file host link Direct HTTP can fetch without an account: the host has a
    # public download API (pixeldrain) for this link or one of its mirrors.
    public = sum(1 for r in releases if r.protocol == "hoster"
                 and any(public_file_url(u) for u in (r.download_url, *r.mirrors)))
    return {
        "rows": rows,
        "releases": len(releases),
        "kinds": {k: kinds[k] for k in _KIND_ORDER if kinds[k]},
        "platforms": [[_platform_label(s), n] for s, n in platforms.most_common(6)],
        "hosts": [[h, n] for h, n in hosts.most_common(5)],
        "public_hoster": public,
        "samples": [r.title for r in releases[:3]],
    }


#: What each kind of link is called on screen, and where its client is added.
_ROUTE_TEXT = {
    "torrent": ("Magnet links", "Add a torrent client (qBittorrent, Transmission, "
                "Deluge…) or a debrid service under Settings → Download Clients."),
    "direct": ("Direct file links", "Add Direct HTTP (built in, no account "
               "needed) or Mister Download Manager under Settings → Download "
               "Clients."),
    "hoster": ("File-host links", "Add Real-Debrid or AllDebrid under Settings → "
               "Download Clients. Direct HTTP alone only fetches hosts with a "
               "public download API, such as pixeldrain."),
}


def routes(summary: dict, clients) -> list[dict]:
    """For each kind of link the source offers, the client a grab would use.

    Decided by the same `pick_client` the grab calls, so a check that says
    "Direct HTTP" means the grab hands the link to Direct HTTP.
    """
    from .downloaders import pick_client

    out = []
    for kind, count in summary.get("kinds", {}).items():
        label, fix = _ROUTE_TEXT[kind]
        client = pick_client(kind, list(clients or ()))
        row = {"kind": kind, "label": label, "count": count, "client": "",
               "ready": client is not None, "fix": "" if client else fix}
        if kind == "hoster" and client is not None:
            # HosterRoute: debrid clients take any host; a direct client
            # alone takes only hosts with a public API.
            debrid = list(client.debrid)
            names = [str(getattr(c, "name", "") or type(c).__name__) for c in debrid]
            if client.direct is not None:
                names.append(str(getattr(client.direct, "name", "") or "Direct HTTP"))
            row["client"] = " or ".join(names)
            if not debrid:
                public = summary.get("public_hoster", 0)
                row["partial"] = True
                row["fix"] = (f"Only {public:,} of {count:,} have a host with a "
                              "public download API. " + fix)
        elif client is not None:
            row["client"] = str(getattr(client, "name", "") or type(client).__name__)
        out.append(row)
    return out


def check_config(cfg: dict, clients) -> dict:
    """`check_source` for an unsaved indexer form: the Test button's payload."""
    from .indexers import build_indexer, indexer_timeout

    try:
        url = check_url(cfg.get("url"))
        indexer_timeout(cfg)
    except ValueError as err:   # a SourceError, or an out-of-range timeout
        return {"ok": False, "message": str(err), "guide": GUIDE_URL}
    named = str(cfg.get("name") or "").strip()
    source = build_indexer({**cfg, "type": "hydra", "url": url,
                            "name": named or "This source"})
    try:
        return check_source(source, clients, named=bool(named))
    except Exception as err:   # noqa: BLE001 - a Test must answer, not 500
        log.warning("Hydra source test failed", exc_info=True)
        return {"ok": False, "guide": GUIDE_URL,
                "message": f"The source could not be tested ({type(err).__name__})."}


def check_source(source: HydraSource, clients, *, named: bool = True) -> dict:
    """The Test button for a Hydra source: can it be read, and can it be grabbed?

    ``ok`` is true only when the source reads *and* at least one kind of link
    in it has a client to go to -- a green Test that is followed by a grab
    failing with "no download client configured" is the trap this replaces.
    With ``named`` false (a URL pasted with no name), the source is called by
    the name its own document gives.
    """
    try:
        summary = source.summary()
    except SourceError as err:
        return {"ok": False, "message": str(err), "guide": GUIDE_URL}
    paths = routes(summary, clients)
    title = source.name if named else (summary.get("name") or "the source")
    lines = [f"Read {summary['releases']:,} releases from {title}."]
    if summary["platforms"]:
        lines.append("Platforms: " + ", ".join(
            f"{label} ({n:,})" for label, n in summary["platforms"]) + ".")
    for row in paths:
        if row["ready"]:
            text = f"✓ {row['label']} ({row['count']:,}) go to {row['client']}."
            if row.get("partial"):
                text += " " + row["fix"]
        else:
            text = f"✗ {row['label']} ({row['count']:,}) have nowhere to go. {row['fix']}"
        lines.append(text)
    ready = any(row["ready"] for row in paths)
    if not ready:
        lines.append("Nothing from this source can be downloaded until one of "
                     "those clients is added.")
    return {"ok": ready, "message": "\n".join(lines), "summary": summary,
            "routes": paths, "warn": ready and not all(
                r["ready"] and not r.get("partial") for r in paths),
            "guide": GUIDE_URL}


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
