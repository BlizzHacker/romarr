"""Download clients.

Radarr, Sonarr and Lidarr all accept both protocols an indexer can offer and
route each release to a client that speaks it. Romarr accepted only torrents,
and only through qBittorrent, which made every usenet indexer in Prowlarr dead
weight -- results came back, scored fine, and were then refused.

Each client here answers three questions the service needs:

  * does it speak this protocol
  * is it reachable
  * take this release

Nothing else. Queue inspection stays with qBittorrent because that is where
the import path reads completed downloads from; the usenet clients report
completion through their own history, which is read the same way.

ON API KEYS: Prowlarr embeds its key in the download links it hands out, and
for usenet there is no keyless alternative -- a magnet has no NZB equivalent.
Radarr and Sonarr both pass that URL to the download client, which fetches it
server-side. That is safe as long as the key never reaches a browser or a log,
which is what sanitise_for_display exists for. Refusing keyed URLs outright, as
this once did, does not protect anything: it just means usenet never works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SabConfig:
    base_url: str
    api_key: str = ""
    category: str = "romarr"
    timeout: int = 30


class SABnzbd:
    """SABnzbd, via its api endpoint."""

    protocol = "usenet"
    name = "SABnzbd"

    def __init__(self, config: SabConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url and self._config.api_key)

    def _call(self, mode: str, _timeout: int | None = None, **params) -> dict | None:
        url = f"{self._config.base_url.rstrip('/')}/api"
        query = {"mode": mode, "output": "json", "apikey": self._config.api_key, **params}
        try:
            response = self._session.get(url, params=query,
                                         timeout=_timeout or self._config.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as err:
            # The key is in the query string, so never log the URL itself.
            log.warning("sabnzbd %s failed: %s", mode, err.__class__.__name__)
            return None

    HEALTH_TIMEOUT = 5

    def reachable(self) -> bool:
        if not self.configured:
            return False
        out = self._call("version", _timeout=self.HEALTH_TIMEOUT)
        return bool(out and out.get("version"))

    def add(self, url: str, *, name: str = "") -> bool:
        """Hand SABnzbd an NZB URL to fetch and queue."""
        if not self.configured:
            return False
        params = {"name": url, "cat": self._config.category}
        if name:
            params["nzbname"] = name
        out = self._call("addurl", **params)
        if not out or not out.get("status"):
            log.warning("sabnzbd rejected the nzb")
            return False
        return True

    def completed(self) -> list[dict]:
        """Finished items in our category, shaped like qBittorrent's."""
        if not self.configured:
            return []
        out = self._call("history", limit=100, category=self._config.category)
        slots = ((out or {}).get("history") or {}).get("slots") or []
        return [
            {
                "name": s.get("name", ""),
                "content_path": s.get("storage") or "",
                "save_path": s.get("storage") or "",
                "state": s.get("status", ""),
            }
            for s in slots
            if str(s.get("status", "")).lower() == "completed"
        ]


@dataclass(frozen=True)
class NzbgetConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"
    timeout: int = 30


class NZBGet:
    """NZBGet, via its JSON-RPC endpoint."""

    protocol = "usenet"
    name = "NZBGet"

    def __init__(self, config: NzbgetConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _rpc(self, method: str, params: list | None = None):
        url = f"{self._config.base_url.rstrip('/')}/jsonrpc"
        auth = None
        if self._config.username:
            auth = (self._config.username, self._config.password)
        try:
            response = self._session.post(
                url, json={"method": method, "params": params or [], "id": 1},
                auth=auth, timeout=self._config.timeout)
            response.raise_for_status()
            return response.json().get("result")
        except (requests.RequestException, ValueError) as err:
            log.warning("nzbget %s failed: %s", method, err.__class__.__name__)
            return None

    def reachable(self) -> bool:
        return bool(self.configured and self._rpc("version"))

    def add(self, url: str, *, name: str = "") -> bool:
        # append(NZBFilename, Content, Category, Priority, AddToTop, AddPaused,
        #        DupeKey, DupeScore, DupeMode, PPParameters)
        result = self._rpc("append", [
            name or "romarr.nzb", url, self._config.category, 0,
            False, False, "", 0, "SCORE", [],
        ])
        # A new-enough NZBGet returns the new id; anything falsy is a refusal.
        if not result:
            log.warning("nzbget rejected the nzb")
            return False
        return True

    def completed(self) -> list[dict]:
        rows = self._rpc("history", [False]) or []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("Status", "")).upper().startswith("SUCCESS"):
                path = row.get("DestDir") or ""
                out.append({
                    "name": row.get("NZBName", ""),
                    "content_path": path,
                    "save_path": path,
                    "state": row.get("Status", ""),
                })
        return out


def pick_client(protocol: str, clients: list) -> object | None:
    """The first configured, reachable client that speaks this protocol.

    Configured is checked before reachable so an unconfigured client is simply
    skipped rather than producing a connection error on every request.
    """
    wanted = (protocol or "torrent").lower()
    for client in clients:
        if getattr(client, "protocol", "") != wanted:
            continue
        if not getattr(client, "configured", True):
            continue
        return client
    return None


# --- configuration schema --------------------------------------------------
#
# The *arr applications describe each client type as a field list and render
# the add/edit form from it, so the UI never hard-codes a client's fields and a
# new client type needs no UI change. Same here.
#
# `secret` fields are write-only: they are accepted from the form and stored,
# but replaced with a placeholder on the way out so a password cannot be read
# back out of the API by anyone who can reach it.

FIELD = lambda name, label, kind="text", default="", **kw: {  # noqa: E731
    "name": name, "label": label, "type": kind, "default": default, **kw
}

_COMMON = [
    FIELD("name", "Name"),
    FIELD("enable", "Enable", "bool", True),
    FIELD("host", "Host", default="localhost"),
    FIELD("port", "Port", "int"),
    FIELD("use_ssl", "Use SSL", "bool", False),
    FIELD("url_base", "URL Base", help="Adds a prefix, for use behind a reverse proxy"),
]

CLIENT_TYPES = {
    "qbittorrent": {
        "label": "qBittorrent",
        "protocol": "torrent",
        "default_port": 8080,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Category", default="romarr"),
        ],
    },
    "sabnzbd": {
        "label": "SABnzbd",
        "protocol": "usenet",
        "default_port": 8080,
        "fields": _COMMON + [
            FIELD("api_key", "API Key", "secret"),
            FIELD("category", "Category", default="romarr"),
        ],
    },
    "nzbget": {
        "label": "NZBGet",
        "protocol": "usenet",
        "default_port": 6789,
        "fields": _COMMON + [
            FIELD("username", "Username", default="nzbget"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Category", default="romarr"),
        ],
    },
}

SECRET_PLACEHOLDER = "********"


def base_url_for(cfg: dict) -> str:
    """Assemble host/port/ssl/url_base into the URL a client actually uses.

    Stored as parts rather than one string because that is what the form
    edits, and joining them in one place keeps a stray trailing slash or a
    missing scheme from becoming three different bugs.
    """
    scheme = "https" if cfg.get("use_ssl") else "http"
    host = str(cfg.get("host") or "localhost").strip().rstrip("/")
    # Tolerate someone pasting a whole URL into the host field.
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
            scheme = prefix[:-3]
    port = cfg.get("port")
    base = f"{scheme}://{host}" + (f":{int(port)}" if port else "")
    url_base = str(cfg.get("url_base") or "").strip().strip("/")
    return f"{base}/{url_base}" if url_base else base


def build_client(cfg: dict):
    """Turn a stored configuration into a live client, or None if unknown."""
    kind = str(cfg.get("type") or "").lower()
    spec = CLIENT_TYPES.get(kind)
    if spec is None:
        return None
    url = base_url_for(cfg)
    category = cfg.get("category") or "romarr"

    if kind == "qbittorrent":
        from .clients import QBittorrent, QbitConfig
        client = QBittorrent(QbitConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    elif kind == "sabnzbd":
        client = SABnzbd(SabConfig(
            base_url=url, api_key=cfg.get("api_key", ""), category=category))
    elif kind == "nzbget":
        client = NZBGet(NzbgetConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    else:
        return None

    # Carry the stored identity so callers can report which row failed.
    client.config_id = cfg.get("id")
    client.display_name = cfg.get("name") or spec["label"]
    client.enabled = bool(cfg.get("enable", True))
    return client


def redact(cfg: dict) -> dict:
    """A client configuration safe to send to a browser."""
    spec = CLIENT_TYPES.get(str(cfg.get("type") or "").lower(), {})
    secrets = {f["name"] for f in spec.get("fields", []) if f["type"] == "secret"}
    return {
        k: (SECRET_PLACEHOLDER if k in secrets and v else v)
        for k, v in cfg.items()
    }


def merge_secrets(new: dict, old: dict | None) -> dict:
    """Keep the stored secret when the form sends back the placeholder.

    An edit form shows `********` for a password. Saving that verbatim would
    silently replace the real credential with eight asterisks -- the client
    would then fail to authenticate and nothing would say why.
    """
    if not old:
        return new
    spec = CLIENT_TYPES.get(str(new.get("type") or "").lower(), {})
    for field in spec.get("fields", []):
        if field["type"] != "secret":
            continue
        name = field["name"]
        if new.get(name) in (SECRET_PLACEHOLDER, "", None):
            new[name] = old.get(name, "")
    return new
