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
        # Prefer a literal magnet: it is the only link here that is safe to hand
        # onward, because the Prowlarr-hosted alternatives carry the API key.
        magnet = row.get("magnetUrl") or ""
        download_url = magnet if magnet.startswith("magnet:") else ""

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
