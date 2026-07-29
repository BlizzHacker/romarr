"""Prowlarr search, the way the *arrs do it.

Prowlarr aggregates every configured torrent and usenet indexer behind one
Torznab-ish API, which is why Romarr talks to it rather than to indexers
directly: adding a new tracker becomes a Prowlarr concern, not a Romarr one.

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


def _download_link(row: dict, *, private: bool = False) -> str:
    """The safest usable link for a Prowlarr result.

    Prowlarr's field names mislead. `magnetUrl` is frequently NOT a magnet: for
    several indexers it is a link back to Prowlarr carrying `?apikey=<key>` in
    the query string, while the real magnet sits in `guid`. Accepting `guid`
    only when it looked like an http URL therefore discarded a perfectly good
    magnet and reported the release as having no download link -- which is how
    every Pirate Bay and KickassTorrents result failed.

    Order is by safety first and then by what actually works:

      * A literal magnet carries no credential and needs no proxy.
      * `downloadUrl` is Prowlarr fetching the .torrent on our behalf. It
        embeds Prowlarr's API key, so it is fit to hand to a download client
        server-side and never to a browser or a log -- sanitise_for_display
        enforces that at the boundary.
      * A magnet rebuilt from `infoHash` comes last of the usable options,
        because it announces to the public trackers below and nothing else.
      * `guid` as an http URL is the final fallback. It is an identifier, not a
        download -- frequently a details page -- so it must never outrank a
        real `downloadUrl` or a rebuilt magnet.

    `private` suppresses the rebuild entirely. A private tracker's torrent
    disables DHT and PEX and announces only to the tracker itself carrying the
    account's passkey, so a magnet rebuilt with public announce URLs can never
    find a peer. Preferring that rebuild over Prowlarr's own download URL --
    which is what happened whenever a private result carried an infohash --
    turned a working grab into a torrent that sat at zero peers forever, after
    the release had been selected correctly. Returning nothing here instead is
    the honest outcome: it surfaces in History as "no usable download link"
    rather than as a download that never starts.
    """
    def http(*fields: str) -> str:
        for field in fields:
            value = str(row.get(field) or "")
            if value.lower().startswith(("http://", "https://")):
                return value
        return ""

    for field in ("magnetUrl", "guid"):
        value = str(row.get(field) or "")
        if value.startswith("magnet:"):
            return value

    if private:
        # No rebuild is possible, so any link that routes through Prowlarr will
        # do -- it holds the passkey and fetches the .torrent server-side.
        return http("downloadUrl", "magnetUrl", "guid")

    # A real download link beats a rebuilt magnet: it resolves to the actual
    # torrent, with the announce URLs the release was published with, rather
    # than to a hash plus four guesses.
    download_url = http("downloadUrl")
    if download_url:
        return download_url

    info_hash = str(row.get("infoHash") or "").strip()
    if info_hash:
        from urllib.parse import quote
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        title = str(row.get("title") or "")
        if title:
            magnet += f"&dn={quote(title)}"
        return magnet + "".join(f"&tr={quote(t)}" for t in _DEFAULT_TRACKERS)

    return http("magnetUrl", "guid")


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
        # indexerId -> is-private, resolved lazily and cached. See _private_ids.
        self._privacy: dict[int, bool] | None = None

    def _url(self, path: str, **params) -> str:
        base = self._config.base_url.rstrip("/")
        query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        return f"{base}/api/v1/{path.lstrip('/')}" + (f"?{query}" if query else "")

    def _private_ids(self) -> dict[int, bool]:
        """Which of Prowlarr's indexers are private trackers, by id.

        Search results do not say. `privacy` is a property of the indexer, and
        two decisions downstream need it per result -- whether a zero seeder
        count is fatal, and whether a magnet may be rebuilt from an infohash.

        Fetched once and cached for the life of the client. A failure is not
        fatal: an empty map means every result is treated as public, which is
        the behaviour that existed before this and is the safe direction to be
        wrong in -- it can refuse a usable private release, but it cannot
        invent a public magnet for one.
        """
        if self._privacy is not None:
            return self._privacy
        privacy: dict[int, bool] = {}
        try:
            url = f"{self._config.base_url.rstrip('/')}/api/v1/indexer"
            response = self._session.get(
                url, headers={"X-Api-Key": self._config.api_key},
                timeout=self._config.timeout)
            response.raise_for_status()
            for row in response.json():
                if isinstance(row, dict) and row.get("id") is not None:
                    privacy[int(row["id"])] = str(row.get("privacy", "")).lower() == "private"
        except (requests.RequestException, ValueError) as err:
            log.warning("could not read indexer privacy from prowlarr: %s",
                        err.__class__.__name__)
        self._privacy = privacy
        return privacy

    def search(self, term: str, *, limit: int = 100) -> list[Release]:
        """Search every configured indexer for a game."""
        url = self._url("search", query=term, categories=list(SEARCH_CATEGORIES),
                        type="search", limit=limit)
        response = self._session.get(
            url, headers={"X-Api-Key": self._config.api_key},
            timeout=self._config.timeout,
        )
        response.raise_for_status()
        privacy = self._private_ids()
        return [self._to_release(row, privacy) for row in response.json()]

    @staticmethod
    def _to_release(row: dict, privacy: dict[int, bool] | None = None) -> Release:
        # A literal magnet is preferred because it carries no credential at all.
        #
        # But it was previously the ONLY thing accepted, and that quietly broke
        # far more than it protected: usenet has no magnet equivalent, so every
        # NZB result was refused, and so was any torrent offering only a
        # .torrent link. Prowlarr's own URL carries its API key, which is fine
        # to hand to a download client server-side -- that is exactly what
        # Radarr and Sonarr do -- as long as it never reaches a browser or a
        # log. sanitise_for_display is what enforces that at the boundary.
        indexer_id = row.get("indexerId")
        private = bool((privacy or {}).get(
            int(indexer_id) if indexer_id is not None else -1, False))
        download_url = _download_link(row, private=private)

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
            private=private,
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
                # Shown because it explains ranking: a private tracker's
                # low-seeder results are kept where a public one's are not.
                "privacy": row.get("privacy", ""),
                "categories": [c for c in cats if c][:4],
            })
        return out

    def grab(self, guid: str, indexer_id: int) -> bool:
        """Ask Prowlarr to push a release to its configured download client.

        This is the safe path for results with no plain magnet: Prowlarr already
        holds the credentials for both the indexer and the download client, so
        nothing sensitive has to travel through Romarr.
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


@dataclass(frozen=True)
class TorznabConfig:
    base_url: str
    api_key: str
    name: str = "Torznab"
    categories: tuple[int, ...] = SEARCH_CATEGORIES
    protocol: str = "torrent"       # "torrent" for torznab, "usenet" for newznab
    private: bool = False
    timeout: int = 60


class Torznab:
    """A single Torznab or Newznab indexer, queried directly.

    Prowlarr is still the recommended way to add a tracker -- it maintains the
    definitions, handles logins and Cloudflare, and one credential set serves
    every *arr on the network. This exists because Romarr already offered
    `torznab` and `newznab` indexer types and could test them, but searched
    only Prowlarr: an operator could configure an indexer, see Test pass, and
    then never get a single result from it. Dead configuration that reports
    itself as healthy is worse than no configuration at all.

    Both protocols share this shape. The only differences are the default
    category set and which download client the results are handed to, and both
    are carried on the config rather than branched on here.
    """

    # Torznab and Newznab publish their per-item extras in namespaced <attr>
    # elements with the same name/value shape, so both are read identically.
    _ATTR_SUFFIX = "attr"

    def __init__(self, config: TorznabConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def name(self) -> str:
        return self._config.name

    def _get(self, **params) -> str:
        base = self._config.base_url.rstrip("/")
        query = urlencode({**params, "apikey": self._config.api_key}, doseq=True)
        response = self._session.get(f"{base}?{query}", timeout=self._config.timeout)
        response.raise_for_status()
        return response.text

    def search(self, term: str, *, limit: int = 100) -> list[Release]:
        body = self._get(t="search", q=term, limit=limit,
                         cat=",".join(str(c) for c in self._config.categories))
        return self.parse(body)

    def caps(self) -> bool:
        """Whether the indexer answers a capabilities query.

        `t=caps` needs no search term, so it exercises the URL and the API key
        without asking the site to run a real query.
        """
        self._get(t="caps")
        return True

    def parse(self, body: str) -> list[Release]:
        """Turn a Torznab/Newznab RSS body into releases.

        Separated from the fetch so it is testable without a network, which is
        the only way this parser gets exercised against the shapes that
        actually break it -- a missing size, a bare infohash, several category
        attributes on one item.
        """
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(body)
        except ET.ParseError as err:
            log.warning("%s returned unparseable xml: %s", self._config.name, err)
            return []

        out = []
        for item in root.iter():
            if not item.tag.endswith("item"):
                continue
            out.append(self._item_to_release(item))
        return [r for r in out if r.title]

    def _item_to_release(self, item) -> Release:
        def text(tag: str) -> str:
            for child in item:
                if child.tag.split("}")[-1] == tag and child.text:
                    return child.text.strip()
            return ""

        attrs: dict[str, list[str]] = {}
        for child in item:
            if not child.tag.split("}")[-1].endswith(self._ATTR_SUFFIX):
                continue
            key = (child.get("name") or "").lower()
            if key:
                attrs.setdefault(key, []).append(child.get("value") or "")

        def attr(name: str) -> str:
            values = attrs.get(name) or []
            return values[0] if values else ""

        def as_int(value: str) -> int:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0

        categories = tuple(
            c for c in (as_int(v) for v in attrs.get("category", [])) if c
        )

        # An enclosure url is the conventional place for the .torrent or .nzb;
        # <link> is the fallback and is sometimes a details page.
        enclosure = ""
        for child in item:
            if child.tag.split("}")[-1] == "enclosure" and child.get("url"):
                enclosure = child.get("url")
                break

        row = {
            "title": text("title"),
            "magnetUrl": attr("magneturl"),
            "downloadUrl": enclosure or text("link"),
            "guid": text("guid"),
            "infoHash": attr("infohash"),
        }

        return Release(
            title=row["title"],
            size=as_int(attr("size")) or as_int(text("size")),
            seeders=as_int(attr("seeders")),
            categories=categories,
            download_url=_download_link(row, private=self._config.private),
            protocol=self._config.protocol,
            indexer=self._config.name,
            private=self._config.private,
        )


def build_indexer(cfg: dict) -> Torznab | None:
    """A searchable client for a stored indexer entry, or None.

    Prowlarr entries return None: they are driven by the Prowlarr class, which
    the service holds separately because it aggregates many indexers rather
    than being one.
    """
    kind = str(cfg.get("type") or "").lower()
    if kind not in ("torznab", "newznab"):
        return None
    if not cfg.get("url"):
        return None
    categories = tuple(indexer_categories(cfg)) or SEARCH_CATEGORIES
    return Torznab(TorznabConfig(
        base_url=cfg.get("url", ""),
        api_key=cfg.get("api_key", ""),
        name=cfg.get("name") or kind.title(),
        categories=categories,
        protocol="usenet" if kind == "newznab" else "torrent",
        private=bool(cfg.get("private", False)),
    ))


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
# Torznab for torrents -- as well as pointing at Prowlarr. Romarr read
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
    # Private trackers are ranked differently -- a result with no seeders is
    # kept rather than discarded, and a magnet is never rebuilt from a bare
    # infohash because a private torrent announces only to its own tracker.
    # Prowlarr reports this per indexer; an indexer added directly here has to
    # be told.
    INDEXER_FIELD("private", "Private tracker", "bool", False,
                  help="Keep low-seeder results and never rebuild a public magnet"),
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
