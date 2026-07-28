"""Prowlarr search, the way the *arrs do it.

Prowlarr aggregates every configured torrent and usenet indexer behind one
Torznab-ish API, which is why Rommarr talks to it rather than to indexers
directly: adding a new tracker becomes a Prowlarr concern, not a Rommarr one.

Two things here are load-bearing and easy to get wrong:

  * Prowlarr's `downloadUrl`/`magnetUrl` are links back to Prowlarr itself with
    the API key in the query string. Handing one to a user, a log, or a public
    page leaks the key. Only a literal `magnet:` URI is safe to pass around; for
    anything else the grab has to go through Prowlarr server-side.
  * Category filtering must happen in the query, not after. Asking an indexer
    for "Mario" unfiltered returns films, and a scoring pass that has to reject
    them is doing work the indexer would have done for free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from .selection import CONSOLE_CATEGORIES, PC_GAME_CATEGORIES, Release

log = logging.getLogger(__name__)

# Newznab category ids sent with every search. Console + PC games only.
SEARCH_CATEGORIES = (1000, 4050)


# Trackers merged into a rebuilt magnet. An infoHash alone is enough only if
# the client finds peers by DHT; indexers hand back bare hashes routinely, and
# a magnet with no announce target can simply never start.
_DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
)


def _download_link(row: dict) -> str:
    """The safest usable link for a Prowlarr result.

    Prowlarr's field names mislead. `magnetUrl` is frequently NOT a magnet: for
    several indexers it is a link back to Prowlarr carrying `?apikey=<key>` in
    the query string, while the real magnet sits in `guid`. Accepting `guid`
    only when it looked like an http URL therefore discarded a perfectly good
    magnet and reported the release as having no download link -- which is how
    every Pirate Bay and KickassTorrents result failed.

    Preference order is by safety. A literal magnet carries no credential at
    all; one rebuilt from `infoHash` is equally safe; Prowlarr's own URL embeds
    the API key and is a last resort, fit only to hand to a download client
    server-side and never to a browser or a log.
    """
    for field in ("magnetUrl", "guid"):
        value = str(row.get(field) or "")
        if value.startswith("magnet:"):
            return value

    info_hash = str(row.get("infoHash") or "").strip()
    if info_hash:
        from urllib.parse import quote
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        title = str(row.get("title") or "")
        if title:
            magnet += f"&dn={quote(title)}"
        return magnet + "".join(f"&tr={quote(t)}" for t in _DEFAULT_TRACKERS)

    for field in ("downloadUrl", "guid"):
        value = str(row.get(field) or "")
        if value.lower().startswith(("http://", "https://")):
            return value
    return ""


@dataclass(frozen=True)
class ProwlarrConfig:
    base_url: str
    api_key: str
    timeout: int = 60


class Prowlarr:
    """Minimal Prowlarr client: search, and grab server-side."""

    def __init__(self, config: ProwlarrConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    def _url(self, path: str, **params) -> str:
        base = self._config.base_url.rstrip("/")
        query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        return f"{base}/api/v1/{path.lstrip('/')}" + (f"?{query}" if query else "")

    def search(self, term: str, *, limit: int = 100) -> list[Release]:
        """Search every configured indexer for a game."""
        url = self._url("search", query=term, categories=list(SEARCH_CATEGORIES),
                        type="search", limit=limit)
        response = self._session.get(
            url, headers={"X-Api-Key": self._config.api_key},
            timeout=self._config.timeout,
        )
        response.raise_for_status()
        return [self._to_release(row) for row in response.json()]

    @staticmethod
    def _to_release(row: dict) -> Release:
        # A literal magnet is preferred because it carries no credential at all.
        #
        # But it was previously the ONLY thing accepted, and that quietly broke
        # far more than it protected: usenet has no magnet equivalent, so every
        # NZB result was refused, and so was any torrent offering only a
        # .torrent link. Prowlarr's own URL carries its API key, which is fine
        # to hand to a download client server-side -- that is exactly what
        # Radarr and Sonarr do -- as long as it never reaches a browser or a
        # log. sanitise_for_display is what enforces that at the boundary.
        download_url = _download_link(row)

        return Release(
            title=row.get("title") or "",
            size=int(row.get("size") or 0),
            seeders=int(row.get("seeders") or 0),
            categories=tuple(
                int(c["id"]) for c in (row.get("categories") or []) if c.get("id") is not None
            ),
            download_url=download_url,
            protocol=(row.get("protocol") or "torrent").lower(),
            indexer=row.get("indexer") or "",
        )

    def indexers(self) -> list[dict]:
        """What Prowlarr has configured, for the Indexers page.

        Read-only on purpose. Prowlarr owns indexer configuration; duplicating
        add/remove here would give two places to edit one thing and no way to
        tell which had won.
        """
        url = f"{self._config.base_url.rstrip('/')}/api/v1/indexer"
        response = self._session.get(url, headers={"X-Api-Key": self._config.api_key},
                                     timeout=self._config.timeout)
        response.raise_for_status()
        out = []
        for row in response.json():
            if not isinstance(row, dict):
                continue
            cats = [c.get("name", "") for c in (row.get("capabilities") or {})
                    .get("categories", []) if isinstance(c, dict)]
            out.append({
                "name": row.get("name", ""),
                "protocol": row.get("protocol", ""),
                "enable": bool(row.get("enable", False)),
                "categories": [c for c in cats if c][:4],
            })
        return out

    def grab(self, guid: str, indexer_id: int) -> bool:
        """Ask Prowlarr to push a release to its configured download client.

        This is the safe path for results with no plain magnet: Prowlarr already
        holds the credentials for both the indexer and the download client, so
        nothing sensitive has to travel through Rommarr.
        """
        url = self._url("search")
        response = self._session.post(
            url,
            headers={"X-Api-Key": self._config.api_key},
            json={"guid": guid, "indexerId": indexer_id},
            timeout=self._config.timeout,
        )
        if response.status_code >= 400:
            log.warning("prowlarr grab failed: %s %s", response.status_code, response.text[:200])
            return False
        return True


def sanitise_for_display(url: str) -> str:
    """Strip an api key from a URL before it is logged or shown.

    Prowlarr embeds its key in download links; this exists so a stray log line
    or an API response cannot leak it.
    """
    if not url:
        return ""
    import re

    return re.sub(r"(?i)(apikey|api_key)=[^&]*", r"\1=<redacted>", url)



# --- indexer configuration schema ------------------------------------------
#
# Radarr and Sonarr let you add an indexer directly -- Newznab for usenet,
# Torznab for torrents -- as well as pointing at Prowlarr. Rommarr read
# Prowlarr and nothing else, which meant an operator with a single indexer had
# to run Prowlarr to use it at all.
#
# Both are Newznab-shaped: a base URL, an API key and a category list. The only
# difference is which protocol the results are, which is why one schema covers
# both.

INDEXER_FIELD = lambda name, label, kind="text", default="", **kw: {  # noqa: E731
    "name": name, "label": label, "type": kind, "default": default, **kw
}

# 1000-1999 is console, 4050-4069 is PC games. Anything else is not a game and
# would only add noise to the ranking.
DEFAULT_GAME_CATEGORIES = "1000,1010,1020,1030,1040,1050,1060,1070,1080,4050"

_INDEXER_COMMON = [
    INDEXER_FIELD("name", "Name"),
    INDEXER_FIELD("enable", "Enable", "bool", True),
    INDEXER_FIELD("url", "URL", help="Base URL, e.g. https://indexer.example/api"),
    INDEXER_FIELD("api_key", "API Key", "secret"),
    INDEXER_FIELD("categories", "Categories", default=DEFAULT_GAME_CATEGORIES,
                  help="Newznab category ids, comma separated"),
    INDEXER_FIELD("priority", "Priority", "int", 25,
                  help="Lower is preferred when two indexers both have a release"),
]

INDEXER_TYPES = {
    "prowlarr": {
        "label": "Prowlarr",
        "protocol": "any",
        "managed": True,
        "fields": [
            INDEXER_FIELD("name", "Name", default="Prowlarr"),
            INDEXER_FIELD("enable", "Enable", "bool", True),
            INDEXER_FIELD("url", "URL", default="http://localhost:9696"),
            INDEXER_FIELD("api_key", "API Key", "secret"),
        ],
    },
    "torznab": {"label": "Torznab", "protocol": "torrent", "fields": _INDEXER_COMMON},
    "newznab": {"label": "Newznab", "protocol": "usenet", "fields": _INDEXER_COMMON},
}


def indexer_categories(cfg: dict) -> list[int]:
    """Parse the category field, ignoring anything that is not a number."""
    out = []
    for part in str(cfg.get("categories") or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


SECRET_PLACEHOLDER = "********"


def redact_indexer(cfg: dict) -> dict:
    """An indexer configuration safe to send to a browser."""
    spec = INDEXER_TYPES.get(str(cfg.get("type") or "").lower(), {})
    secrets = {f["name"] for f in spec.get("fields", []) if f["type"] == "secret"}
    return {
        k: (SECRET_PLACEHOLDER if k in secrets and v else v)
        for k, v in cfg.items()
    }
