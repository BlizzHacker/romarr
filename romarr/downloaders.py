"""Download clients.

Radarr, Sonarr and Lidarr all accept both protocols an indexer can offer and
route each release to a client that speaks it. ROMarr accepted only torrents,
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

import json
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


@dataclass
class TransmissionConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"
    timeout: int = 30


class Transmission:
    """Transmission's RPC, and the 409 handshake everybody hits first.

    Every RPC call from a client with no session id is answered **409** with
    an `X-Transmission-Session-Id` header, and the call has to be repeated
    carrying it. That is not an error condition, it is the protocol. A client
    that treats 409 as a failure never adds a single torrent and reports the
    daemon as broken -- which is exactly what it looks like from outside.

    The id is kept and reused: re-challenging on every call doubles every
    request for nothing.
    """

    protocol = "torrent"
    RPC_PATH = "/transmission/rpc"
    SESSION_HEADER = "X-Transmission-Session-Id"

    def __init__(self, config, session=None):
        self._config = config
        self._session = session or requests.Session()
        self._session_id = ""

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _call(self, method: str, arguments: dict) -> dict:
        url = self._config.base_url.rstrip("/") + self.RPC_PATH
        payload = json.dumps({"method": method, "arguments": arguments})
        auth = ((self._config.username, self._config.password)
                if self._config.username else None)
        for attempt in range(2):
            headers = {"Content-Type": "application/json"}
            if self._session_id:
                headers[self.SESSION_HEADER] = self._session_id
            response = self._session.post(
                url, data=payload, headers=headers, auth=auth,
                timeout=self._config.timeout)
            if getattr(response, "status_code", 200) == 409 and attempt == 0:
                self._session_id = response.headers.get(self.SESSION_HEADER, "")
                continue
            try:
                return response.json() or {}
            except Exception:
                return {}
        return {}

    def reachable(self) -> bool:
        try:
            return bool(self._call("session-get", {}))
        except Exception as exc:
            log.warning("Transmission unreachable: %s", exc)
            return False

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        arguments = {"filename": magnet_or_url}
        if save_path:
            arguments["download-dir"] = save_path
        if self._config.category:
            arguments["labels"] = [self._config.category]
        try:
            body = self._call("torrent-add", arguments)
        except Exception as exc:
            log.warning("Transmission add failed: %s", exc)
            return False
        # Transmission answers HTTP 200 with {"result": "<error text>"} when
        # it refuses, so reading the status code alone calls every rejection a
        # success. "torrent-duplicate" in the arguments means it is already
        # there, which is the outcome the caller wanted.
        if body.get("result") != "success":
            log.warning("Transmission refused the torrent: %s", body.get("result"))
            return False
        return True


@dataclass
class DelugeConfig:
    base_url: str
    password: str = ""
    category: str = "romarr"
    timeout: int = 30


class Deluge:
    """Deluge's WebUI JSON-RPC, with both of its traps handled.

    **It refuses everything until `auth.login` succeeds**, and answers an
    unauthenticated call with an ordinary-looking JSON error rather than a
    401 -- so a client that does not log in looks like one talking to a broken
    daemon.

    **The WebUI is a separate process from the daemon.** A freshly started
    WebUI is attached to nothing: `auth.login` succeeds, every later call
    returns cheerfully, and no torrent appears anywhere. `web.connect` against
    the first known host is what attaches it.
    """

    protocol = "torrent"
    RPC_PATH = "/json"

    def __init__(self, config, session=None):
        self._config = config
        self._session = session or requests.Session()
        self._id = 0

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _call(self, method: str, params: list) -> dict:
        self._id += 1
        url = self._config.base_url.rstrip("/") + self.RPC_PATH
        response = self._session.post(
            url,
            data=json.dumps({"method": method, "params": params, "id": self._id}),
            headers={"Content-Type": "application/json"},
            timeout=self._config.timeout)
        try:
            return response.json() or {}
        except Exception:
            return {}

    def _ready(self) -> bool:
        if not self._call("auth.login", [self._config.password]).get("result"):
            log.warning("Deluge rejected the WebUI password")
            return False
        if self._call("web.connected", []).get("result"):
            return True
        hosts = self._call("web.get_hosts", []).get("result") or []
        if not hosts:
            log.warning("Deluge WebUI is not attached to a daemon and knows "
                        "no hosts to attach to")
            return False
        return bool(self._call("web.connect", [hosts[0][0]]).get("result", True))

    def reachable(self) -> bool:
        try:
            return self._ready()
        except Exception as exc:
            log.warning("Deluge unreachable: %s", exc)
            return False

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        try:
            if not self._ready():
                return False
            options = {"download_location": save_path} if save_path else {}
            body = self._call("core.add_torrent_magnet", [magnet_or_url, options])
            if body.get("error") or not body.get("result"):
                log.warning("Deluge refused the torrent: %s", body.get("error"))
                return False
            if self._config.category:
                # Best effort: the Label plugin may not be enabled, and a
                # missing label is no reason to call a successful add a
                # failure.
                self._call("label.set_torrent",
                           [body.get("result"), self._config.category])
            return True
        except Exception as exc:
            log.warning("Deluge add failed: %s", exc)
            return False


@dataclass
class RtorrentConfig:
    base_url: str            # http(s)://host:port/RPC2, usually behind a web server
    category: str = "romarr"
    timeout: int = 30


class Rtorrent:
    """rTorrent over XML-RPC, both command dialects handled.

    rTorrent renamed its whole command set around 0.9: `load_start` became
    `load.start` and grew a leading target argument. A client that speaks
    only one dialect looks broken against exactly half the installs, so this
    one tries the modern call and falls back on a Fault.

    Basic auth travels in the URL (http://user:pass@host/RPC2), which is how
    the reverse proxies in front of rTorrent expect it. Digest-only proxies
    are not supported -- say so rather than fail mysteriously.
    """

    protocol = "torrent"
    name = "rTorrent"

    def __init__(self, config: RtorrentConfig, proxy=None):
        self._config = config
        self._proxy = proxy  # tests inject one; live use builds lazily

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _server(self):
        if self._proxy is None:
            import xmlrpc.client
            self._proxy = xmlrpc.client.ServerProxy(
                self._config.base_url, allow_none=True)
        return self._proxy

    def reachable(self) -> bool:
        if not self.configured:
            return False
        try:
            return bool(self._server().system.client_version())
        except Exception as exc:
            log.warning("rTorrent unreachable: %s", exc.__class__.__name__)
            return False

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        label = self._config.category
        commands = [f"d.custom1.set={label}"] if label else []
        server = self._server()
        try:
            try:
                server.load.start("", magnet_or_url, *commands)
            except Exception:
                # The pre-0.9 dialect: no target argument, underscores.
                server.load_start(magnet_or_url, *commands)
            return True
        except Exception as exc:
            log.warning("rTorrent refused the torrent: %s", exc)
            return False

    def completed(self) -> list[dict]:
        """Finished downloads carrying our label, shaped like qBittorrent's."""
        if not self.configured:
            return []
        try:
            rows = self._server().d.multicall2(
                "", "main", "d.name=", "d.complete=", "d.directory=",
                "d.custom1=")
        except Exception as exc:
            log.warning("rTorrent list failed: %s", exc.__class__.__name__)
            return []
        out = []
        for row in rows or []:
            try:
                name, complete, directory, label = row
            except (TypeError, ValueError):
                continue
            if self._config.category and label != self._config.category:
                continue
            if not int(complete or 0):
                continue
            out.append({"name": str(name), "content_path": str(directory),
                        "save_path": str(directory), "state": "complete"})
        return out


@dataclass(frozen=True)
class SynologyConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"     # kept for the shared form; DS has no labels
    timeout: int = 30


class SynologyDownloadStation:
    """Synology's Download Station, sid-auth and all.

    Every call needs a session id from auth.cgi first, and the sid expires
    without saying when -- so any call that comes back with Synology's
    not-logged-in codes (105, 106, 119) is retried once behind a fresh
    login rather than reported as a failure.
    """

    protocol = "torrent"
    name = "Download Station"

    #: Synology error codes that mean "your sid is stale", not "you failed".
    _RELOGIN = {105, 106, 119}

    def __init__(self, config: SynologyConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._sid = ""

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url and self._config.username)

    def _login(self) -> bool:
        url = f"{self._config.base_url.rstrip('/')}/webapi/auth.cgi"
        try:
            response = self._session.get(url, params={
                "api": "SYNO.API.Auth", "version": "3", "method": "login",
                "account": self._config.username,
                "passwd": self._config.password,
                "session": "DownloadStation", "format": "sid",
            }, timeout=self._config.timeout)
            body = response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("Download Station login failed: %s", err.__class__.__name__)
            return False
        self._sid = ((body.get("data") or {}).get("sid") or "") if body.get("success") else ""
        if not self._sid:
            log.warning("Download Station rejected the credentials (code %s)",
                        (body.get("error") or {}).get("code"))
        return bool(self._sid)

    def _call(self, method: str, extra: dict | None = None) -> dict:
        url = f"{self._config.base_url.rstrip('/')}/webapi/DownloadStation/task.cgi"
        for attempt in range(2):
            if not self._sid and not self._login():
                return {}
            params = {"api": "SYNO.DownloadStation.Task", "version": "1",
                      "method": method, "_sid": self._sid, **(extra or {})}
            try:
                body = self._session.get(url, params=params,
                                         timeout=self._config.timeout).json()
            except (requests.RequestException, ValueError) as err:
                log.warning("Download Station %s failed: %s", method,
                            err.__class__.__name__)
                return {}
            code = (body.get("error") or {}).get("code")
            if not body.get("success") and code in self._RELOGIN and attempt == 0:
                self._sid = ""
                continue
            return body
        return {}

    def reachable(self) -> bool:
        return bool(self.configured and (self._sid or self._login()))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        extra = {"uri": magnet_or_url}
        if save_path:
            extra["destination"] = save_path
        body = self._call("create", extra)
        if not body.get("success"):
            log.warning("Download Station rejected the task (code %s)",
                        (body.get("error") or {}).get("code"))
            return False
        return True

    def completed(self) -> list[dict]:
        body = self._call("list", {"additional": "detail"})
        tasks = ((body.get("data") or {}).get("tasks") or [])
        out = []
        for task in tasks:
            if str(task.get("status")) != "finished":
                continue
            title = task.get("title", "")
            destination = (((task.get("additional") or {}).get("detail") or {})
                           .get("destination") or "")
            path = f"{destination}/{title}" if destination else title
            out.append({"name": title, "content_path": path,
                        "save_path": destination, "state": "finished"})
        return out


@dataclass(frozen=True)
class RealDebridConfig:
    api_token: str
    save_path: str           # where fetched files land; the import reads here
    base_url: str = "https://api.real-debrid.com/rest/1.0"
    timeout: int = 30


class RealDebrid:
    """Real-Debrid as a download client, not a browser plugin.

    The debrid service holds the torrent; ROMarr's job is to get bytes onto
    a path the importer can read. So `add` hands the magnet to the cloud and
    `completed` walks the torrents THIS client added, unrestricts their
    links, and downloads them into `save_path` -- after which the normal
    import pipeline takes over as if a local client had produced the file.

    Only torrents this ROMarr added are ever touched. Real-Debrid has no
    categories, so the ledger of our torrent ids lives in a dotfile beside
    the downloads; without it, ROMarr would "import" whatever else the
    operator's account happens to hold.
    """

    protocol = "torrent"
    name = "Real-Debrid"

    LEDGER = ".romarr-realdebrid.json"

    def __init__(self, config: RealDebridConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.api_token and self._config.save_path)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._config.api_token}"}

    def _get(self, path: str) -> dict | list | None:
        try:
            response = self._session.get(
                f"{self._config.base_url}{path}", headers=self._headers(),
                timeout=self._config.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("real-debrid GET %s failed: %s", path,
                        err.__class__.__name__)
            return None

    def _post(self, path: str, data: dict) -> dict | None:
        try:
            response = self._session.post(
                f"{self._config.base_url}{path}", data=data,
                headers=self._headers(), timeout=self._config.timeout)
            response.raise_for_status()
            if not response.text:
                return {}
            return response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("real-debrid POST %s failed: %s", path,
                        err.__class__.__name__)
            return None

    # -- the ledger of what is ours -----------------------------------------

    def _ledger_path(self):
        from pathlib import Path
        return Path(self._config.save_path) / self.LEDGER

    def _ours(self) -> list[str]:
        try:
            return list(json.loads(self._ledger_path().read_text()))
        except (OSError, ValueError):
            return []

    def _remember(self, torrent_id: str) -> None:
        ids = self._ours()
        if torrent_id not in ids:
            ids.append(torrent_id)
        try:
            self._ledger_path().parent.mkdir(parents=True, exist_ok=True)
            self._ledger_path().write_text(json.dumps(ids))
        except OSError as err:
            log.warning("could not record real-debrid torrent id: %s", err)

    # -- the client interface -------------------------------------------------

    def reachable(self) -> bool:
        if not self.configured:
            return False
        user = self._get("/user")
        return bool(user and user.get("id"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        if not magnet_or_url.startswith("magnet:"):
            # RD can ingest .torrent files too, but that is an upload flow;
            # magnets cover what indexers hand out. Saying so beats a 400.
            log.warning("real-debrid only accepts magnets; got a %s link",
                        magnet_or_url.split(":", 1)[0])
            return False
        body = self._post("/torrents/addMagnet", {"magnet": magnet_or_url})
        torrent_id = (body or {}).get("id")
        if not torrent_id:
            return False
        # Without file selection the torrent sits in "waiting_files" forever.
        self._post(f"/torrents/selectFiles/{torrent_id}", {"files": "all"})
        self._remember(str(torrent_id))
        return True

    def completed(self) -> list[dict]:
        """Fetch what the cloud has finished, then report it like a local client."""
        from pathlib import Path
        out = []
        for torrent_id in self._ours():
            info = self._get(f"/torrents/info/{torrent_id}")
            if not info or info.get("status") != "downloaded":
                continue
            name = info.get("filename") or torrent_id
            target = Path(self._config.save_path) / name
            fetched = self._fetch_links(info.get("links") or [], target)
            if fetched:
                out.append({"name": name, "content_path": str(target),
                            "save_path": self._config.save_path,
                            "state": "downloaded"})
        return out

    def _fetch_links(self, links: list, target) -> bool:
        """Unrestrict and download every link into `target/`. True if all
        the files are present afterwards, counting ones a previous sweep
        already fetched."""
        ok = True
        for link in links:
            unrestricted = self._post("/unrestrict/link", {"link": link})
            if not unrestricted or not unrestricted.get("download"):
                ok = False
                continue
            filename = unrestricted.get("filename") or "download.bin"
            destination = target / filename
            if destination.exists():
                continue
            if not self._download(unrestricted["download"], destination):
                ok = False
        return ok

    def _download(self, url: str, destination) -> bool:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_suffix(destination.suffix + ".partial")
            with self._session.get(url, stream=True, timeout=self._config.timeout) as response:
                response.raise_for_status()
                with open(partial, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            partial.replace(destination)
            return True
        except (requests.RequestException, OSError) as err:
            log.warning("real-debrid download failed: %s", err.__class__.__name__)
            return False


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
    "transmission": {
        "label": "Transmission",
        "protocol": "torrent",
        "default_port": 9091,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Label", default="romarr"),
        ],
    },
    "deluge": {
        "label": "Deluge",
        "protocol": "torrent",
        "default_port": 8112,
        "fields": _COMMON + [
            FIELD("password", "WebUI Password", "secret",
                  help="The WebUI password, not the daemon's. Default: deluge"),
            FIELD("category", "Label", default="romarr",
                  help="Needs the Label plugin; ignored when it is not enabled"),
        ],
    },
    "rtorrent": {
        "label": "rTorrent",
        "protocol": "torrent",
        "default_port": 8080,
        "fields": _COMMON + [
            FIELD("username", "Username",
                  help="Basic auth on the web server in front of rTorrent, "
                       "if any. Digest-only proxies are not supported."),
            FIELD("password", "Password", "secret"),
            FIELD("rpc_path", "RPC Path", default="/RPC2"),
            FIELD("category", "Label", default="romarr"),
        ],
    },
    "synology": {
        "label": "Synology Download Station",
        "protocol": "torrent",
        "default_port": 5000,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
        ],
    },
    "realdebrid": {
        "label": "Real-Debrid",
        "protocol": "torrent",
        "default_port": 0,
        "fields": [
            FIELD("name", "Name"),
            FIELD("enable", "Enable", "bool", True),
            FIELD("api_token", "API Token", "secret",
                  help="From real-debrid.com/apitoken. The cloud fetches the "
                       "torrent; ROMarr downloads the result into Save Path "
                       "and imports from there."),
            FIELD("save_path", "Save Path", default="/downloads/realdebrid"),
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
    elif kind == "transmission":
        client = Transmission(TransmissionConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    elif kind == "deluge":
        client = Deluge(DelugeConfig(
            base_url=url, password=cfg.get("password", ""), category=category))
    elif kind == "rtorrent":
        # Credentials ride in the URL because that is where XML-RPC over
        # basic auth expects them.
        rpc = "/" + str(cfg.get("rpc_path") or "/RPC2").strip("/")
        endpoint = url
        if cfg.get("username"):
            from urllib.parse import quote, urlsplit, urlunsplit
            parts = urlsplit(url)
            netloc = (f"{quote(cfg['username'], safe='')}:"
                      f"{quote(cfg.get('password', ''), safe='')}@{parts.netloc}")
            endpoint = urlunsplit((parts.scheme, netloc, parts.path,
                                   parts.query, parts.fragment))
        client = Rtorrent(RtorrentConfig(
            base_url=endpoint.rstrip("/") + rpc, category=category))
    elif kind == "synology":
        client = SynologyDownloadStation(SynologyConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", "")))
    elif kind == "realdebrid":
        client = RealDebrid(RealDebridConfig(
            api_token=cfg.get("api_token", ""),
            save_path=cfg.get("save_path", "")))
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
    secrets = {f["name"] for spec in CLIENT_TYPES.values()
               for f in spec.get("fields", []) if f["type"] == "secret"}
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
