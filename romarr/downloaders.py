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

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

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

    #: `add` accepts the release title -- see hand_off. Declared rather than
    #: inferred from the signature because the caller holds a client it knows
    #: nothing else about, and handing the keyword to one that cannot take it
    #: is a TypeError in the middle of a grab.
    TAKES_NAME = True

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

    #: See hand_off, and SABnzbd.TAKES_NAME for why it is a declaration.
    TAKES_NAME = True

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


@dataclass(frozen=True)
class NzbVortexConfig:
    base_url: str
    api_key: str = ""
    category: str = ""       # NZBVortex calls these "groups"
    #: NZBVortex serves its API over HTTPS with a certificate it generated for
    #: itself, so demanding a valid chain means it can never connect at all.
    #: Off by default for that reason, and a field rather than a constant so
    #: an operator who put a real certificate in front of it can say so.
    verify_tls: bool = False
    timeout: int = 30


class NZBVortex:
    """NZBVortex, and the challenge-response it will not do without.

    There is no "send the key" mode. The daemon hands out a nonce, the client
    invents one of its own, and the two are hashed with the API key --
    BASE64(SHA256("nonce:cnonce:apikey")) -- so the key itself never crosses
    the wire. Appending `?apikey=` the way every other usenet client would
    gets a refusal and no explanation of it.

    The session id expires without saying when, so a call answered with the
    not-logged-in result is retried once behind a fresh login. Same shape as
    Download Station's sid, for the same reason.
    """

    protocol = "usenet"
    name = "NZBVortex"

    #: See hand_off. NZBVortex takes the NZB as an upload, and the name of the
    #: uploaded file is what it shows in its queue and its history -- so the
    #: release title is the only thing that makes a finished download
    #: findable by the importer.
    TAKES_NAME = True

    #: The one queue state that means finished. The rest of the ladder --
    #: repairing, joining, uncompressing, moving -- are all still in flight,
    #: and 21 and up are the failures.
    DONE_STATE = 20

    #: What the daemon answers when the session id has gone stale.
    NOT_LOGGED_IN = 1

    def __init__(self, config: NzbVortexConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._sid = ""

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url and self._config.api_key)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/api/{path.lstrip('/')}"

    def _fetch(self, path: str, **kwargs) -> dict | None:
        try:
            response = self._session.get(
                self._url(path), timeout=self._config.timeout,
                verify=self._config.verify_tls, **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("nzbvortex %s failed: %s", path, err.__class__.__name__)
            return None

    def _login(self) -> bool:
        body = self._fetch("auth/nonce")
        nonce = (body or {}).get("authNonce") or (body or {}).get("authnonce")
        if not nonce:
            log.warning("nzbvortex would not issue a nonce")
            return False
        # The client's half of the pair. Random per attempt: reusing one turns
        # the exchange back into a replayable secret, which is the thing the
        # nonce was there to prevent.
        cnonce = secrets.token_hex(16)
        digest = hashlib.sha256(
            f"{nonce}:{cnonce}:{self._config.api_key}".encode()).digest()
        body = self._fetch("auth/login", params={
            "nonce": nonce, "cnonce": cnonce,
            "hash": base64.b64encode(digest).decode(),
        })
        # The key spelling differs between API levels; both mean the same
        # thing and guessing one of them is a login that silently never works.
        self._sid = str((body or {}).get("sessionID")
                        or (body or {}).get("sessionId") or "")
        if not self._sid:
            log.warning("nzbvortex rejected the API key")
        return bool(self._sid)

    def _call(self, path: str, *, params: dict | None = None,
              files=None) -> dict:
        for attempt in range(2):
            if not self._sid and not self._login():
                return {}
            query = {"sessionid": self._sid, **(params or {})}
            if files is None:
                body = self._fetch(path, params=query)
            else:
                try:
                    response = self._session.post(
                        self._url(path), params=query, files=files,
                        timeout=self._config.timeout,
                        verify=self._config.verify_tls)
                    response.raise_for_status()
                    body = response.json()
                except (requests.RequestException, ValueError) as err:
                    log.warning("nzbvortex %s failed: %s", path,
                                err.__class__.__name__)
                    return {}
            if body is None:
                return {}
            if body.get("result") == self.NOT_LOGGED_IN and attempt == 0:
                self._sid = ""
                continue
            return body
        return {}

    def reachable(self) -> bool:
        if not self.configured:
            return False
        version = self._fetch("app/appversion")
        # appversion needs no session, so a version with no login means the
        # daemon is up but the key is wrong -- which is worth separating.
        return bool(version) and self._login()

    def add(self, url: str, *, name: str = "") -> bool:
        """Fetch the NZB and upload it. NZBVortex takes no URLs.

        Every other usenet client here is handed the link and fetches it
        itself; this one only accepts an upload, so ROMarr is the thing that
        does the GET. That is the same server-side fetch the module header
        describes, one hop further along.
        """
        if not self.configured:
            return False
        try:
            response = self._session.get(url, timeout=self._config.timeout)
            response.raise_for_status()
            payload = response.content
        except requests.RequestException as err:
            # Never the URL: Prowlarr's key is in it.
            log.warning("could not fetch the nzb for nzbvortex: %s",
                        err.__class__.__name__)
            return False
        filename = f"{name or 'romarr'}.nzb"
        params = {"priority": 0}
        if self._config.category:
            params["groupname"] = self._config.category
        body = self._call("nzb/add", params=params, files={
            "name": (filename, payload, "application/x-nzb")})
        if not body or not body.get("add_uuid"):
            log.warning("nzbvortex rejected the nzb")
            return False
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        params = {"limitDone": 100}
        if self._config.category:
            params["groupName"] = self._config.category
        body = self._call("nzb", params=params)
        out = []
        for item in (body or {}).get("nzbs") or []:
            if not isinstance(item, dict):
                continue
            if int(item.get("state") or 0) != self.DONE_STATE:
                continue
            path = item.get("destinationPath") or ""
            out.append({
                "name": item.get("uiTitle", ""),
                "content_path": path,
                "save_path": path,
                "state": "done",
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
    name = "Transmission"
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


class Vuze(Transmission):
    """Vuze, which is Transmission's RPC wearing a different badge.

    Vuze has no remote protocol of its own worth writing a client for: what it
    exposes is the Transmission RPC, served by the plugin of that name, right
    down to the 409 session handshake. So this IS the Transmission client with
    a different label -- the alternative is a second copy of that handshake,
    maintained separately and wrong six months later.

    Two things an operator has to know, which is why the type exists at all
    rather than being a note telling them to pick Transmission: the plugin is
    not installed by default, and Vuze itself stopped being developed. BiglyBT
    is the fork that did not.
    """

    name = "Vuze"


class BiglyBT(Vuze):
    """BiglyBT: the maintained fork of Vuze, and the same RPC.

    Separate from Vuze only so the client list says what an operator actually
    runs. A status row reading "Vuze" against a BiglyBT install is the kind of
    small lie that costs somebody an hour when something else breaks.
    """

    name = "BiglyBT"


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
    name = "Deluge"
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
class Aria2Config:
    base_url: str
    secret: str = ""
    save_path: str = ""
    category: str = ""       # aria2 has no labels; carried for the status row
    timeout: int = 30


class Aria2:
    """aria2 over JSON-RPC, with the two things that trip every client.

    **The RPC secret is a positional argument, not a header.** It is the first
    parameter of every call and it is spelled `token:<secret>`. Sent as a
    header, a query parameter or a bare string it is simply not seen, and the
    daemon answers "Unauthorized" while working perfectly.

    **A magnet finishes twice.** aria2 downloads the metadata as a download of
    its own, so the gid `add` returns completes in a second and the transfer
    anybody cares about is a different one, linked by `followedBy`. Following
    the returned gid therefore reports a finished download containing one
    .torrent file. `completed` reads the stopped list instead and drops the
    metadata rows.
    """

    protocol = "torrent"
    name = "aria2"
    RPC_PATH = "/jsonrpc"

    def __init__(self, config: Aria2Config,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _rpc(self, method: str, params: list | None = None):
        url = self._config.base_url.rstrip("/") + self.RPC_PATH
        args = list(params or [])
        if self._config.secret:
            args.insert(0, f"token:{self._config.secret}")
        try:
            response = self._session.post(
                url, json={"jsonrpc": "2.0", "id": "romarr",
                           "method": method, "params": args},
                timeout=self._config.timeout)
            response.raise_for_status()
            body = response.json() or {}
        except (requests.RequestException, ValueError) as err:
            log.warning("aria2 %s failed: %s", method, err.__class__.__name__)
            return None
        if body.get("error"):
            log.warning("aria2 refused %s: %s", method,
                        (body.get("error") or {}).get("message"))
            return None
        return body.get("result")

    def reachable(self) -> bool:
        if not self.configured:
            return False
        return bool((self._rpc("aria2.getVersion") or {}).get("version"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        options = {}
        target = save_path or self._config.save_path
        if target:
            options["dir"] = target
        # The URI list is a list even for one URI, and the options object has
        # to come after it -- aria2 reads its parameters by position.
        return bool(self._rpc("aria2.addUri", [[magnet_or_url], options]))

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        rows = self._rpc("aria2.tellStopped", [0, 1000]) or []
        out = []
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != "complete":
                continue
            if row.get("followedBy"):
                # A metadata-only download. Its "file" is the .torrent aria2
                # just fetched, and importing that would import nothing.
                continue
            directory = row.get("dir") or ""
            name = (((row.get("bittorrent") or {}).get("info") or {})
                    .get("name") or "")
            files = row.get("files") or []
            if not name and files:
                # A plain HTTP download has no bittorrent block; its name is
                # the file it wrote.
                name = os.path.basename(str(files[0].get("path") or ""))
            if not name:
                continue
            out.append({"name": name,
                        "content_path": f"{directory}/{name}" if directory else name,
                        "save_path": directory, "state": "complete"})
        return out


@dataclass(frozen=True)
class FloodConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"     # a Flood tag
    save_path: str = ""
    timeout: int = 30


class Flood:
    """Flood's REST API, which authenticates with a cookie and nothing else.

    `POST /api/auth/authenticate` sets a JWT cookie and every later call
    depends on it; there is no key or header to send instead. So the cookie is
    kept on the session and re-minted when a call is refused, rather than
    re-authenticating on every request -- which would double the traffic to a
    UI that is already proxying a real torrent client behind it.

    Flood has tags rather than categories. Same job, so the category field
    drives them, and `completed` reports only what carries ours.
    """

    protocol = "torrent"
    name = "Flood"

    def __init__(self, config: FloodConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._authed = False

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url and self._config.username)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/api{path}"

    def _login(self) -> bool:
        try:
            response = self._session.post(
                self._url("/auth/authenticate"),
                json={"username": self._config.username,
                      "password": self._config.password},
                timeout=self._config.timeout)
            response.raise_for_status()
        except requests.RequestException as err:
            log.warning("flood rejected the credentials: %s",
                        err.__class__.__name__)
            self._authed = False
            return False
        self._authed = True
        return True

    def _call(self, method: str, path: str, payload: dict | None = None):
        for attempt in range(2):
            if not self._authed and not self._login():
                return None
            try:
                caller = getattr(self._session, method)
                kwargs = {"timeout": self._config.timeout}
                if payload is not None:
                    kwargs["json"] = payload
                response = caller(self._url(path), **kwargs)
                status = getattr(response, "status_code", 200)
                if status in (401, 403) and attempt == 0:
                    # The token expired. One silent re-login, then believe it.
                    self._authed = False
                    continue
                response.raise_for_status()
                return response.json() if response.text else {}
            except (requests.RequestException, ValueError) as err:
                log.warning("flood %s failed: %s", path, err.__class__.__name__)
                return None
        return None

    def reachable(self) -> bool:
        if not self.configured:
            return False
        return self._call("get", "/auth/verify") is not None

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        body = {"urls": [magnet_or_url],
                "tags": [self._config.category] if self._config.category else [],
                "start": True}
        destination = save_path or self._config.save_path
        if destination:
            body["destination"] = destination
        if self._call("post", "/torrents/add-urls", body) is None:
            log.warning("flood refused the torrent")
            return False
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        body = self._call("get", "/torrents") or {}
        out = []
        for torrent in (body.get("torrents") or {}).values():
            if not isinstance(torrent, dict):
                continue
            tags = torrent.get("tags") or []
            if self._config.category and self._config.category not in tags:
                continue
            # `status` is a list, not a word: a finished torrent that is still
            # seeding carries both "complete" and "seeding".
            if "complete" not in (torrent.get("status") or []):
                continue
            directory = torrent.get("directory") or ""
            name = torrent.get("name") or ""
            out.append({"name": name,
                        "content_path": f"{directory}/{name}" if directory else name,
                        "save_path": directory, "state": "complete"})
        return out


@dataclass(frozen=True)
class FreeboxConfig:
    base_url: str            # http(s)://mafreebox.freebox.fr/api/v1
    app_id: str = ""
    app_token: str = ""
    save_path: str = ""
    category: str = ""       # becomes a subfolder; the box has no labels
    timeout: int = 30


class FreeboxDownload:
    """The Freebox's download manager, challenge and base64 and all.

    **The app_token is never sent.** `GET /login` returns a challenge, the app
    answers with HMAC-SHA1(app_token, challenge), and what comes back is a
    session token for the `X-Fbx-App-Auth` header. That token expires quietly,
    so a 403 opens a new session instead of being reported as a bad token.

    **Paths are base64 in both directions.** A directory typed into the form
    has to be encoded before it is sent, and the one the box reports back has
    to be decoded before the importer can look in it. Getting either wrong
    puts the files somewhere nobody searches, with no error anywhere.

    Obtaining an app_token in the first place requires somebody to physically
    authorise the app on the front panel of the box. ROMarr cannot do that and
    does not pretend to: the token is a field, and the operator fills it in.
    """

    protocol = "torrent"
    name = "Freebox"

    def __init__(self, config: FreeboxConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._token = ""

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url and self._config.app_id
                    and self._config.app_token)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _open_session(self) -> bool:
        try:
            challenge = self._session.get(
                self._url("login"), timeout=self._config.timeout).json()
            phrase = ((challenge.get("result") or {}).get("challenge") or "")
            if not phrase:
                log.warning("freebox issued no challenge")
                return False
            password = hmac.new(self._config.app_token.encode("ascii"),
                                phrase.encode("ascii"), hashlib.sha1).hexdigest()
            body = self._session.post(
                self._url("login/session"),
                json={"app_id": self._config.app_id, "password": password},
                timeout=self._config.timeout).json()
        except (requests.RequestException, ValueError, AttributeError) as err:
            log.warning("freebox login failed: %s", err.__class__.__name__)
            return False
        self._token = str((body.get("result") or {}).get("session_token") or "")
        if not self._token:
            log.warning("freebox refused the app token (%s)",
                        body.get("error_code"))
        return bool(self._token)

    def _call(self, method: str, path: str, *, data=None, payload=None) -> dict:
        for attempt in range(2):
            if not self._token and not self._open_session():
                return {}
            try:
                kwargs = {"timeout": self._config.timeout,
                          "headers": {"X-Fbx-App-Auth": self._token}}
                if data is not None:
                    kwargs["data"] = data
                if payload is not None:
                    kwargs["json"] = payload
                response = getattr(self._session, method)(self._url(path), **kwargs)
                if getattr(response, "status_code", 200) in (401, 403) \
                        and attempt == 0:
                    self._token = ""
                    continue
                return response.json() or {}
            except (requests.RequestException, ValueError) as err:
                log.warning("freebox %s failed: %s", path,
                            err.__class__.__name__)
                return {}
        return {}

    def reachable(self) -> bool:
        return bool(self.configured and (self._token or self._open_session()))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        form = {"download_url": magnet_or_url}
        directory = save_path or self._config.save_path
        if directory and self._config.category:
            directory = f"{directory.rstrip('/')}/{self._config.category}"
        if directory:
            form["download_dir"] = base64.b64encode(
                directory.encode("utf-8")).decode("ascii")
        body = self._call("post", "downloads/add", data=form)
        if not body.get("success"):
            log.warning("freebox refused the download (%s)",
                        body.get("error_code"))
            return False
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        body = self._call("get", "downloads/")
        out = []
        for task in body.get("result") or []:
            if not isinstance(task, dict) or task.get("status") != "done":
                continue
            directory = _from_base64(task.get("download_dir") or "")
            name = task.get("name") or ""
            out.append({"name": name,
                        "content_path": f"{directory}/{name}" if directory else name,
                        "save_path": directory, "state": "done"})
        return out


def _from_base64(value: str) -> str:
    """Decode a Freebox path, or hand back what we were given.

    A path that is not valid base64 is far more likely to be a box answering
    in plain text than a corrupt field, and turning that into an exception
    would lose every other download in the same sweep.
    """
    try:
        return base64.b64decode(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value


#: The uTorrent WebUI's torrent list is positional: an array per torrent with
#: nothing naming the slots. Hadouken copied the format wholesale, so both
#: read it through these. Only the four that matter here are named.
UTORRENT_NAME = 2
UTORRENT_PROGRESS = 4        # tenths of a percent, so 1000 is finished
UTORRENT_LABEL = 11
UTORRENT_SAVE_PATH = 26


def _utorrent_completed(rows, category: str) -> list[dict]:
    """Finished torrents from a uTorrent-shaped list, ours only."""
    out = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) <= UTORRENT_SAVE_PATH:
            continue
        try:
            progress = int(row[UTORRENT_PROGRESS])
        except (TypeError, ValueError):
            continue
        if progress < 1000:
            continue
        if category and str(row[UTORRENT_LABEL] or "") != category:
            continue
        name = str(row[UTORRENT_NAME] or "")
        directory = str(row[UTORRENT_SAVE_PATH] or "")
        out.append({"name": name,
                    "content_path": f"{directory}/{name}" if directory else name,
                    "save_path": directory, "state": "complete"})
    return out


@dataclass(frozen=True)
class HadoukenConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"
    timeout: int = 30


class Hadouken:
    """Hadouken's JSON-RPC, and the uTorrent-shaped arrays it answers with.

    `webui.list` does not return objects. It returns the uTorrent WebUI's
    positional arrays, which is why the index constants above this class
    exist: without them the completion check is `row[4] >= 1000` and nobody
    reading it a year later knows what row 4 was.

    Offered because installs of it exist and its API still answers, not
    because it is a good idea: Hadouken has been unmaintained since 2015. An
    operator with a free choice should make a different one, which is what the
    field help says.
    """

    protocol = "torrent"
    name = "Hadouken"
    RPC_PATH = "/api"

    def __init__(self, config: HadoukenConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _rpc(self, method: str, params: list | None = None):
        url = self._config.base_url.rstrip("/") + self.RPC_PATH
        auth = ((self._config.username, self._config.password)
                if self._config.username else None)
        try:
            response = self._session.post(
                url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or []},
                auth=auth, timeout=self._config.timeout)
            response.raise_for_status()
            body = response.json() or {}
        except (requests.RequestException, ValueError) as err:
            log.warning("hadouken %s failed: %s", method,
                        err.__class__.__name__)
            return None
        if body.get("error"):
            log.warning("hadouken refused %s: %s", method, body.get("error"))
            return None
        return body.get("result")

    def reachable(self) -> bool:
        return bool(self.configured and self._rpc("core.getSystemInfo"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        result = self._rpc("webui.addTorrent",
                           ["url", magnet_or_url,
                            {"label": self._config.category}])
        # It answers with an empty string on success, so anything that is not
        # None is an acceptance -- `if not result` would call every success a
        # refusal.
        if result is None:
            log.warning("hadouken rejected the torrent")
            return False
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        body = self._rpc("webui.list") or {}
        return _utorrent_completed(body.get("torrents"), self._config.category)


@dataclass(frozen=True)
class UTorrentConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"
    timeout: int = 30


class UTorrent:
    """uTorrent and BitTorrent's Web UI, with the token dance it requires.

    Every call needs `token=` from `/gui/token.html`, which is an HTML
    fragment holding the token in a div rather than anything resembling an
    API. The token is bound to the cookie that same response sets, so the two
    are taken together and re-taken together; keeping one and losing the other
    produces a refusal that looks like a wrong password.

    **A label cannot be set while adding a URL.** The WebUI's `add-url` takes
    no label, and `setprops` needs the infohash, which `add-url` does not
    return. A magnet carries its own hash, so that case is labelled straight
    after the add; a .torrent URL does not, and that release lands unlabelled.
    Said out loud in the field help, because a category that silently applies
    to half the grabs is worse than one that never does.
    """

    protocol = "torrent"
    name = "uTorrent"
    GUI_PATH = "/gui/"

    #: The token lives in `<div id='token' style='display:none;'>TOKEN</div>`.
    _TOKEN = re.compile(r"<div[^>]*id=['\"]token['\"][^>]*>([^<]+)</div>", re.I)

    #: A magnet's infohash: 40 hex characters. Base32 magnets exist and are
    #: not converted here -- an unlabelled torrent beats a wrong hash.
    _BTIH = re.compile(r"\bxt=urn:btih:([0-9a-fA-F]{40})\b")

    def __init__(self, config: UTorrentConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._token = ""

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _auth(self):
        return ((self._config.username, self._config.password)
                if self._config.username else None)

    def _fetch_token(self) -> bool:
        url = self._config.base_url.rstrip("/") + self.GUI_PATH + "token.html"
        try:
            response = self._session.get(url, auth=self._auth(),
                                         timeout=self._config.timeout)
            response.raise_for_status()
        except requests.RequestException as err:
            log.warning("utorrent would not issue a token: %s",
                        err.__class__.__name__)
            return False
        found = self._TOKEN.search(response.text or "")
        self._token = found.group(1).strip() if found else ""
        if not self._token:
            log.warning("utorrent's token page held no token")
        return bool(self._token)

    def _call(self, params: dict) -> dict:
        url = self._config.base_url.rstrip("/") + self.GUI_PATH
        for attempt in range(2):
            if not self._token and not self._fetch_token():
                return {}
            try:
                response = self._session.get(
                    url, params={"token": self._token, **params},
                    auth=self._auth(), timeout=self._config.timeout)
                if getattr(response, "status_code", 200) in (400, 401) \
                        and attempt == 0:
                    # The token went stale with the session. Take a new pair.
                    self._token = ""
                    continue
                response.raise_for_status()
                return response.json() or {}
            except (requests.RequestException, ValueError) as err:
                log.warning("utorrent call failed: %s", err.__class__.__name__)
                return {}
        return {}

    def reachable(self) -> bool:
        if not self.configured:
            return False
        return bool(self._call({"action": "getsettings"}))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        if not self._call({"action": "add-url", "s": magnet_or_url}):
            log.warning("utorrent rejected the torrent")
            return False
        found = self._BTIH.search(magnet_or_url or "")
        if found and self._config.category:
            # Best effort, like Deluge's label: the torrent is added either
            # way, and a missing label is not a reason to call the add a
            # failure.
            self._call({"action": "setprops", "hash": found.group(1).upper(),
                        "s": "label", "v": self._config.category})
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        body = self._call({"list": 1})
        return _utorrent_completed(body.get("torrents"), self._config.category)


@dataclass(frozen=True)
class PorlaConfig:
    base_url: str
    api_token: str = ""
    save_path: str = ""
    category: str = "romarr"
    timeout: int = 30


class Porla:
    """porla's JSON-RPC, whose params are an object and not an array.

    Every other JSON-RPC client in this module sends a positional list.
    porla takes a named object, and handing it a list is a parse error rather
    than a wrong answer -- so the two dialects are worth keeping apart in the
    reader's head.

    Completion is `progress`, a fraction, not a state word: a torrent that has
    all its bytes reads 1.0 whether it is seeding, paused or idle.
    """

    protocol = "torrent"
    name = "porla"
    RPC_PATH = "/api/v1/jsonrpc"

    def __init__(self, config: PorlaConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url and self._config.api_token)

    def _rpc(self, method: str, params: dict | None = None):
        url = self._config.base_url.rstrip("/") + self.RPC_PATH
        try:
            response = self._session.post(
                url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}},
                headers={"Authorization": f"Bearer {self._config.api_token}"},
                timeout=self._config.timeout)
            response.raise_for_status()
            body = response.json() or {}
        except (requests.RequestException, ValueError) as err:
            log.warning("porla %s failed: %s", method, err.__class__.__name__)
            return None
        if body.get("error"):
            log.warning("porla refused %s: %s", method,
                        (body.get("error") or {}).get("message"))
            return None
        return body.get("result")

    def reachable(self) -> bool:
        return bool(self.configured and self._rpc("sys.versions"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        params = {"magnet_uri": magnet_or_url}
        target = save_path or self._config.save_path
        if target:
            params["save_path"] = target
        result = self._rpc("torrents.add", params)
        if not result or not result.get("info_hash"):
            log.warning("porla rejected the torrent")
            return False
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        params = {"page_size": 500}
        if self._config.category:
            params["filters"] = {"category": self._config.category}
        result = self._rpc("torrents.list", params) or {}
        out = []
        for torrent in result.get("torrents") or []:
            if not isinstance(torrent, dict):
                continue
            try:
                if float(torrent.get("progress") or 0) < 1:
                    continue
            except (TypeError, ValueError):
                continue
            directory = torrent.get("save_path") or ""
            name = torrent.get("name") or ""
            out.append({"name": name,
                        "content_path": f"{directory}/{name}" if directory else name,
                        "save_path": directory, "state": "complete"})
        return out


@dataclass(frozen=True)
class DebridConfig:
    """What every debrid service needs, because they all need the same three.

    One dataclass rather than seven: the services differ in their API, not in
    what an operator has to type. A token, somewhere to put the bytes, and the
    endpoint. `username` is carried for the one of them that authenticates
    with an account rather than a key.
    """

    api_token: str
    save_path: str           # where fetched files land; the import reads here
    base_url: str = ""
    username: str = ""
    password: str = ""
    timeout: int = 30


class DebridClient:
    """The half of a debrid service that is the same for all of them.

    A debrid service is not a download client the way qBittorrent is. It takes
    the release, fetches it on its own hardware, and then offers HTTP links.
    ROMarr's job is the last leg: get the bytes onto a path the importer can
    read. So `add` hands the release to the cloud and `completed` walks what
    THIS ROMarr sent, resolves each finished item to direct links, and
    downloads them into `save_path` -- after which the normal import pipeline
    takes over as if a local client had produced the files.

    **Only what this ROMarr added is ever touched.** Not one of these services
    has a category or a label to filter on, so the ledger of our own ids --
    kept in a dotfile beside the downloads, so that moving the directory moves
    the queue with it -- is the only thing standing between "import my grabs"
    and "import whatever else this account happens to hold".

    A subclass supplies four things: how to check the account (`reachable`),
    how to hand over a release (`add`), which of our items are finished
    (`_finished`), and how to turn one item's link into a direct URL
    (`_resolve` -- and most do not need to, because most hand out a usable URL
    with the listing). Everything under that -- the ledger, the streaming
    download, the .partial rename, the skip-what-is-already-here rule -- is
    identical for all of them and lives here once.
    """

    protocol = "torrent"
    name = "Debrid"

    #: Per service, so two rows pointed at the same save path do not read each
    #: other's ids and "import" the other's downloads.
    LEDGER = ".romarr-debrid.json"

    #: What the status row calls a finished item. Cosmetic, but each service
    #: has its own word for it and echoing theirs makes a log readable.
    DONE_STATE = "downloaded"

    def __init__(self, config, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.api_token and self._config.save_path)

    # -- talking to the service ---------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._config.api_token}"}

    def _get(self, path: str, params: dict | None = None):
        # params is only passed when there is one: a service whose GETs never
        # take a query string should not have every call carry an empty dict.
        kwargs = {"headers": self._headers(), "timeout": self._config.timeout}
        if params:
            kwargs["params"] = params
        try:
            response = self._session.get(f"{self._config.base_url}{path}", **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("%s GET %s failed: %s", self.name, path,
                        err.__class__.__name__)
            return None

    def _post(self, path: str, data: dict | None = None, files=None):
        kwargs = {"data": data, "headers": self._headers(),
                  "timeout": self._config.timeout}
        if files is not None:
            kwargs["files"] = files
        try:
            response = self._session.post(f"{self._config.base_url}{path}", **kwargs)
            response.raise_for_status()
            if not response.text:
                return {}
            return response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("%s POST %s failed: %s", self.name, path,
                        err.__class__.__name__)
            return None

    # -- the ledger of what is ours -----------------------------------------

    def _ledger_path(self) -> Path:
        return Path(self._config.save_path) / self.LEDGER

    def _ours(self) -> list:
        try:
            return list(json.loads(self._ledger_path().read_text()))
        except (OSError, ValueError):
            return []

    @staticmethod
    def _entry_id(entry) -> str:
        """An entry's id, whichever of the two shapes it is.

        A bare id is enough for the services whose listing endpoint returns
        everything else; the ones whose listing loses the name or the link
        record those alongside it. Both shapes live in the same file, and one
        reader for both keeps a ledger written by an older ROMarr working.
        """
        if isinstance(entry, dict):
            return str(entry.get("id", ""))
        return str(entry)

    def _remember(self, item_id, **extra) -> None:
        entries = self._ours()
        if any(self._entry_id(e) == str(item_id) for e in entries):
            return
        entries.append({"id": str(item_id), **extra} if extra else str(item_id))
        try:
            self._ledger_path().parent.mkdir(parents=True, exist_ok=True)
            self._ledger_path().write_text(json.dumps(entries))
        except OSError as err:
            log.warning("could not record the %s item id: %s", self.name, err)

    # -- the client interface -----------------------------------------------

    def _finished(self) -> list[tuple[str, list]]:
        """`(name, links)` for every one of our items the cloud has finished.

        A "link" is whatever this service needs to produce a URL later -- a
        string, or a tuple its own `_resolve` understands.
        """
        raise NotImplementedError

    def _resolve(self, link) -> tuple[str, str]:
        """`(filename, direct URL)` for one link.

        The default is for the services that hand out a usable URL with the
        listing, which is most of them. The ones that mint a URL per file, or
        that have to unrestrict first, override it.
        """
        if isinstance(link, tuple):
            return link
        return _basename_of(str(link)), str(link)

    def completed(self) -> list[dict]:
        """Fetch what the cloud has finished, then report it like a local client."""
        if not self.configured:
            return []
        out = []
        for name, links in self._finished():
            target = Path(self._config.save_path) / name
            if self._fetch_links(links, target):
                out.append({"name": name, "content_path": str(target),
                            "save_path": self._config.save_path,
                            "state": self.DONE_STATE})
        return out

    def _fetch_links(self, links: list, target) -> bool:
        """Download every link into `target/`. True if all the files are
        present afterwards, counting ones a previous sweep already fetched."""
        ok = True
        for link in links:
            filename, url = self._resolve(link)
            if not url:
                ok = False
                continue
            destination = target / (filename or "download.bin")
            if destination.exists():
                continue
            if not self._download(url, destination):
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
            log.warning("%s download failed: %s", self.name,
                        err.__class__.__name__)
            return False


def _basename_of(url: str) -> str:
    """The filename a direct link implies.

    Any directory component is dropped for the same reason `_filename_for`
    drops one: a name arriving from somebody else's server does not get to
    choose a path on ours.
    """
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return os.path.basename(name.replace("\\", "/")).strip() or "download.bin"


@dataclass(frozen=True)
class RealDebridConfig:
    api_token: str
    save_path: str           # where fetched files land; the import reads here
    base_url: str = "https://api.real-debrid.com/rest/1.0"
    username: str = ""       # unused; kept so every debrid config is one shape
    timeout: int = 30


class RealDebrid(DebridClient):
    """Real-Debrid as a download client, not a browser plugin.

    The two things it needs that the shared base cannot know: a magnet has to
    have its files SELECTED after it is added or it sits in "waiting_files"
    for ever, and a link has to be unrestricted before it can be fetched.
    """

    name = "Real-Debrid"

    LEDGER = ".romarr-realdebrid.json"
    DEFAULT_URL = "https://api.real-debrid.com/rest/1.0"

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

    def _finished(self) -> list[tuple[str, list]]:
        out = []
        for entry in self._ours():
            torrent_id = self._entry_id(entry)
            info = self._get(f"/torrents/info/{torrent_id}")
            if not info or info.get("status") != "downloaded":
                continue
            out.append((info.get("filename") or torrent_id,
                        info.get("links") or []))
        return out

    def _resolve(self, link) -> tuple[str, str]:
        unrestricted = self._post("/unrestrict/link", {"link": link})
        if not unrestricted or not unrestricted.get("download"):
            return "", ""
        return (unrestricted.get("filename") or "download.bin",
                unrestricted["download"])


class AllDebrid(DebridClient):
    """AllDebrid, whose magnets and whose links are two different calls.

    v4.1 took the finished links out of `magnet/status` and put them behind
    `magnet/files`, which answers with a folder TREE rather than a list: `n`
    is a name, `l` a link, `e` the entries of a directory. Reading only the
    top level of that finds every file of a single-file torrent and none at
    all of a multi-file one -- so the tree is walked. Older installs still
    return `links` inline, which is why both are read.

    Everything is wrapped in `{"status": ..., "data": ...}`, and an HTTP 200
    with `"status": "error"` is a refusal, so the envelope is checked rather
    than the status code.
    """

    name = "AllDebrid"
    LEDGER = ".romarr-alldebrid.json"
    DEFAULT_URL = "https://api.alldebrid.com/v4"
    DONE_STATE = "ready"

    @staticmethod
    def _data(body) -> dict:
        if not body or body.get("status") != "success":
            return {}
        return body.get("data") or {}

    def reachable(self) -> bool:
        if not self.configured:
            return False
        return bool(self._data(self._get("/user")).get("user"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        if not magnet_or_url.startswith("magnet:"):
            # `magnets[]` takes a magnet or a bare infohash. A .torrent URL is
            # a different endpoint and a file upload; saying so beats a 400.
            log.warning("alldebrid only accepts magnets; got a %s link",
                        magnet_or_url.split(":", 1)[0])
            return False
        body = self._post("/magnet/upload", {"magnets[]": magnet_or_url})
        magnets = self._data(body).get("magnets") or []
        first = magnets[0] if magnets else {}
        if not isinstance(first, dict) or first.get("error") or not first.get("id"):
            log.warning("alldebrid rejected the magnet")
            return False
        self._remember(first["id"])
        return True

    def _finished(self) -> list[tuple[str, list]]:
        out = []
        for entry in self._ours():
            ident = self._entry_id(entry)
            magnet = self._data(self._post("/magnet/status", {"id": ident})) \
                .get("magnets") or {}
            if isinstance(magnet, list):
                magnet = magnet[0] if magnet else {}
            if str(magnet.get("status", "")).lower() != "ready":
                continue
            links = magnet.get("links")
            if links is None:
                files = self._data(self._post("/magnet/files", {"id[]": ident}))
                rows = files.get("magnets") or []
                tree = rows[0].get("files") if rows and isinstance(rows[0], dict) else []
                links = _alldebrid_links(tree)
            else:
                links = [row.get("link") if isinstance(row, dict) else row
                         for row in links]
            out.append((magnet.get("filename") or ident,
                        [link for link in links if link]))
        return out

    def _resolve(self, link) -> tuple[str, str]:
        unlocked = self._data(self._post("/link/unlock", {"link": link}))
        if not unlocked.get("link"):
            return "", ""
        return (unlocked.get("filename") or _basename_of(unlocked["link"]),
                unlocked["link"])


def _alldebrid_links(tree) -> list[str]:
    """Every download link in an AllDebrid file tree, at any depth."""
    out = []
    for node in tree or []:
        if not isinstance(node, dict):
            continue
        if node.get("l"):
            out.append(node["l"])
        out.extend(_alldebrid_links(node.get("e")))
    return out


class Premiumize(DebridClient):
    """Premiumize.me, which finishes a transfer into a FOLDER, not a file.

    `transfer/list` gives a finished transfer a `folder_id`, and the files
    are one `folder/list` away -- each already carrying a direct `link`, so
    nothing has to be unrestricted. A single-file transfer sets `file_id`
    instead and has no folder at all, which is the case that quietly imports
    nothing if only `folder_id` is read.

    "finished" is not the only done state: a transfer that is being seeded
    back has every byte and reads `seeding`.
    """

    name = "Premiumize"
    LEDGER = ".romarr-premiumize.json"
    DEFAULT_URL = "https://www.premiumize.me/api"
    DONE_STATE = "finished"

    DONE = {"finished", "seeding"}

    def reachable(self) -> bool:
        if not self.configured:
            return False
        body = self._get("/account/info")
        return bool(body and body.get("status") == "success")

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        body = self._post("/transfer/create", {"src": magnet_or_url})
        if not body or body.get("status") != "success" or not body.get("id"):
            log.warning("premiumize rejected the transfer: %s",
                        (body or {}).get("message"))
            return False
        self._remember(body["id"])
        return True

    def _finished(self) -> list[tuple[str, list]]:
        body = self._get("/transfer/list") or {}
        mine = {self._entry_id(e) for e in self._ours()}
        out = []
        for transfer in body.get("transfers") or []:
            if not isinstance(transfer, dict):
                continue
            if str(transfer.get("id")) not in mine:
                continue
            if str(transfer.get("status", "")).lower() not in self.DONE:
                continue
            name = transfer.get("name") or str(transfer.get("id"))
            out.append((name, self._links_for(transfer)))
        return out

    def _links_for(self, transfer: dict) -> list:
        if transfer.get("folder_id"):
            listing = self._get("/folder/list",
                                {"id": transfer["folder_id"]}) or {}
            return [(item.get("name") or _basename_of(item.get("link", "")),
                     item.get("link") or "")
                    for item in listing.get("content") or []
                    if isinstance(item, dict) and item.get("type") == "file"]
        if transfer.get("file_id"):
            item = self._get("/item/details", {"id": transfer["file_id"]}) or {}
            if item.get("link"):
                return [(item.get("name") or _basename_of(item["link"]),
                         item["link"])]
        return []


class TorBox(DebridClient):
    """TorBox, whose download URLs are minted one file at a time.

    `torrents/mylist` names the files but does not link them; a CDN URL comes
    from `torrents/requestdl` per file and lasts three hours. So the links in
    `_finished` are `(torrent id, file id, name)` triples and `_resolve` is
    what actually asks for a URL -- which also means a sweep that finds
    everything already downloaded asks for no URLs at all.

    That one endpoint takes the key as a query parameter rather than in the
    header, alone among all of them. It is not a mistake in the docs.
    """

    name = "TorBox"
    LEDGER = ".romarr-torbox.json"
    DEFAULT_URL = "https://api.torbox.app/v1/api"

    def reachable(self) -> bool:
        if not self.configured:
            return False
        body = self._get("/user/me")
        return bool(body and body.get("success"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        # createtorrent is a multipart form even when all it carries is a
        # magnet; a urlencoded body is answered with a validation error.
        body = self._post("/torrents/createtorrent", None,
                          files={"magnet": (None, magnet_or_url)})
        data = (body or {}).get("data") or {}
        if not body or not body.get("success") or not data.get("torrent_id"):
            log.warning("torbox rejected the magnet: %s",
                        (body or {}).get("detail"))
            return False
        self._remember(data["torrent_id"])
        return True

    def _finished(self) -> list[tuple[str, list]]:
        body = self._get("/torrents/mylist") or {}
        mine = {self._entry_id(e) for e in self._ours()}
        out = []
        for torrent in body.get("data") or []:
            if not isinstance(torrent, dict):
                continue
            ident = str(torrent.get("id"))
            if ident not in mine:
                continue
            # Finished is not the same as still here: `download_present` goes
            # false when TorBox expires an old torrent off its storage, and
            # asking for its links then returns nothing at all.
            if not (torrent.get("download_finished")
                    and torrent.get("download_present")):
                continue
            links = []
            for entry in torrent.get("files") or []:
                if not isinstance(entry, dict):
                    continue
                links.append((ident, entry.get("id"),
                              entry.get("short_name") or entry.get("name") or ""))
            out.append((torrent.get("name") or ident, links))
        return out

    def _resolve(self, link) -> tuple[str, str]:
        torrent_id, file_id, filename = link
        body = self._get("/torrents/requestdl", {
            "token": self._config.api_token,
            "torrent_id": torrent_id, "file_id": file_id,
        })
        url = (body or {}).get("data")
        if not body or not body.get("success") or not isinstance(url, str):
            return "", ""
        # TorBox names files by their full path inside the torrent; only the
        # last component is a filename, and the rest is not ours to create.
        return os.path.basename(str(filename).replace("\\", "/")) or _basename_of(url), url


class DebridLink(DebridClient):
    """Debrid-Link's seedbox, which links every file in the listing.

    One call does the whole of `_finished`: `seedbox/list` returns each
    torrent with its files and each file with a `downloadUrl`, so nothing has
    to be resolved afterwards and the base class's default `_resolve` is the
    right one.

    The envelope is `{"success": ..., "value": ...}` -- not `data`, which is
    what every other service in this file calls it.
    """

    name = "Debrid-Link"
    LEDGER = ".romarr-debridlink.json"
    DEFAULT_URL = "https://debrid-link.com/api/v2"
    DONE_STATE = "complete"

    @staticmethod
    def _value(body):
        if not body or not body.get("success"):
            return None
        return body.get("value")

    def reachable(self) -> bool:
        if not self.configured:
            return False
        return self._value(self._get("/account/infos")) is not None

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        value = self._value(self._post("/seedbox/add", {"url": magnet_or_url}))
        if not isinstance(value, dict) or not value.get("id"):
            log.warning("debrid-link rejected the torrent")
            return False
        self._remember(value["id"])
        return True

    def _finished(self) -> list[tuple[str, list]]:
        value = self._value(self._get("/seedbox/list")) or []
        mine = {self._entry_id(e) for e in self._ours()}
        out = []
        for torrent in value:
            if not isinstance(torrent, dict) or str(torrent.get("id")) not in mine:
                continue
            try:
                if float(torrent.get("downloadPercent") or 0) < 100:
                    continue
            except (TypeError, ValueError):
                continue
            links = [(entry.get("name") or _basename_of(entry.get("downloadUrl", "")),
                      entry.get("downloadUrl") or "")
                     for entry in torrent.get("files") or []
                     if isinstance(entry, dict)]
            out.append((torrent.get("name") or str(torrent.get("id")), links))
        return out


class Offcloud(DebridClient):
    """Offcloud's cloud downloads, and the archive that may not be one.

    A multi-file download is explored with `cloud/explore/<id>`, which returns
    a flat list of direct links. A single file has nothing to explore, and
    that call fails -- but the URL for it was in the answer to `cloud`, at
    add time, so it is written into the ledger there rather than guessed at
    from a URL pattern later.

    Authentication is a `key` parameter rather than a header, so `_headers`
    is deliberately empty and every call carries the key itself.
    """

    name = "Offcloud"
    LEDGER = ".romarr-offcloud.json"
    DEFAULT_URL = "https://offcloud.com/api"

    def _headers(self) -> dict:
        return {}

    def _key(self) -> dict:
        return {"key": self._config.api_token}

    def reachable(self) -> bool:
        if not self.configured:
            return False
        # There is no account endpoint that is not also an action, so the
        # proxy list -- which is read-only and always present -- stands in.
        body = self._post("/proxy", self._key())
        return body is not None

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        body = self._post("/cloud", {**self._key(), "url": magnet_or_url})
        if not body or not body.get("requestId"):
            # `not_available` is Offcloud saying the account lacks the add-on
            # for this kind of download, which is a billing answer, not a bug.
            log.warning("offcloud rejected the download: %s",
                        (body or {}).get("not_available")
                        or (body or {}).get("error"))
            return False
        self._remember(body["requestId"], name=body.get("fileName") or "",
                       url=body.get("url") or "")
        return True

    def _finished(self) -> list[tuple[str, list]]:
        out = []
        for entry in self._ours():
            ident = self._entry_id(entry)
            body = self._post("/cloud/status",
                              {**self._key(), "requestId": ident}) or {}
            status = body.get("status")
            if isinstance(status, dict):
                name = status.get("fileName") or ""
                state = str(status.get("status", ""))
            else:
                name, state = "", str(status or "")
            if state.lower() != "downloaded":
                continue
            stored = entry if isinstance(entry, dict) else {}
            name = name or stored.get("name") or ident
            links = self._get(f"/cloud/explore/{ident}", self._key())
            if not isinstance(links, list) or not links:
                links = [stored.get("url")] if stored.get("url") else []
            out.append((name, [link for link in links if link]))
        return out


class PutIo(DebridClient):
    """put.io, whose finished transfers leave the transfer list.

    `transfers/list` holds what is still in flight; a transfer that completed
    a while ago is simply not in it any more. So `_finished` asks for each of
    our transfers by id instead of filtering a list, which is also the only
    way to learn its `file_id` afterwards.

    A file's URL is minted on request and is single-use, so `_resolve` asks
    for one per file at the moment it is needed.
    """

    name = "put.io"
    LEDGER = ".romarr-putio.json"
    DEFAULT_URL = "https://api.put.io/v2"
    DONE_STATE = "COMPLETED"

    def reachable(self) -> bool:
        if not self.configured:
            return False
        body = self._get("/account/info")
        return bool(body and body.get("info"))

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        body = self._post("/transfers/add", {"url": magnet_or_url})
        transfer = (body or {}).get("transfer") or {}
        if not transfer.get("id"):
            log.warning("put.io rejected the transfer")
            return False
        self._remember(transfer["id"])
        return True

    def _finished(self) -> list[tuple[str, list]]:
        out = []
        for entry in self._ours():
            ident = self._entry_id(entry)
            transfer = (self._get(f"/transfers/{ident}") or {}).get("transfer") or {}
            if str(transfer.get("status", "")).upper() != "COMPLETED":
                continue
            file_id = transfer.get("file_id")
            if not file_id:
                continue
            name = transfer.get("name") or str(ident)
            out.append((name, self._files_under(file_id)))
        return out

    def _files_under(self, file_id) -> list:
        """Every file below this id, folders walked.

        A single-file transfer's `file_id` IS the file, and a multi-file one's
        is the folder it was put in -- `files/list` tells the two apart by the
        `file_type` of what it lists.
        """
        listing = self._get("/files/list", {"parent_id": file_id}) or {}
        rows = listing.get("files")
        if not rows:
            # Not a folder: ask for it directly.
            item = (self._get(f"/files/{file_id}") or {}).get("file") or {}
            return [(item.get("name") or "", item["id"])] if item.get("id") else []
        out = []
        for item in rows:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if item.get("file_type") == "FOLDER":
                out.extend(self._files_under(item["id"]))
            else:
                out.append((item.get("name") or "", item["id"]))
        return out

    def _resolve(self, link) -> tuple[str, str]:
        filename, file_id = link
        body = self._get(f"/files/{file_id}/url")
        url = (body or {}).get("url")
        if not url:
            return "", ""
        return filename or _basename_of(url), url


class Linksnappy(DebridClient):
    """Linksnappy, which authenticates with an account and starts by hand.

    Two things separate it from every other service here.

    **There is no API key.** `AUTHENTICATE` takes the account's username and
    password and answers with a session cookie that the rest of the calls
    ride on, so the session -- not a header -- is what carries the
    credential.

    **Adding is not starting.** `ADDMAGNET` registers the torrent and leaves
    it stopped; `START` needs a folder id from `FOLDERLIST` before anything
    downloads. Skipping that is the same trap as Real-Debrid's file
    selection: the add succeeds, nothing ever finishes, and nothing says why.

    Everything is wrapped in `{"status": "OK", "error": false, "return": ...}`
    and an error arrives with HTTP 200, so the envelope is what gets checked.
    """

    name = "Linksnappy"
    LEDGER = ".romarr-linksnappy.json"
    DEFAULT_URL = "https://linksnappy.com/api"
    DONE_STATE = "FINISHED"

    def __init__(self, config, session: requests.Session | None = None):
        super().__init__(config, session)
        self._authed = False

    @property
    def configured(self) -> bool:
        return bool(self._config.username and self._config.password
                    and self._config.save_path)

    def _headers(self) -> dict:
        return {}

    @staticmethod
    def _returned(body):
        if not body or body.get("status") != "OK" or body.get("error"):
            return None
        return body.get("return")

    def _login(self) -> bool:
        if self._authed:
            return True
        body = self._get("/AUTHENTICATE", {"username": self._config.username,
                                           "password": self._config.password})
        self._authed = bool(body) and str(body.get("status")) == "OK"
        if not self._authed:
            log.warning("linksnappy rejected the account credentials")
        return self._authed

    def _call(self, path: str, params: dict | None = None):
        if not self._login():
            return None
        return self._returned(self._get(path, params))

    def reachable(self) -> bool:
        return bool(self.configured and self._login())

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self.configured:
            return False
        if not magnet_or_url.startswith("magnet:"):
            # ADDURL exists but answers with a differently shaped body keyed
            # by filename; magnets are what indexers hand out and what this
            # reads. Saying so beats parsing a shape nobody tested.
            log.warning("linksnappy only accepts magnets; got a %s link",
                        magnet_or_url.split(":", 1)[0])
            return False
        rows = self._call("/torrents/ADDMAGNET", {"magnetlinks": magnet_or_url})
        first = rows[0] if isinstance(rows, list) and rows else {}
        torrent_id = first.get("torrentid") if isinstance(first, dict) else None
        if not torrent_id:
            log.warning("linksnappy rejected the magnet: %s",
                        (first or {}).get("error"))
            return False
        folder_id = self._folder()
        if folder_id is None:
            return False
        self._call("/torrents/START", {"tid": torrent_id, "fid": folder_id})
        self._remember(torrent_id)
        return True

    def _folder(self):
        """The account's root folder id, which START will not work without."""
        folders = self._call("/torrents/FOLDERLIST")
        for row in folders or []:
            if isinstance(row, dict) and row.get("type") == "root":
                return row.get("id")
        log.warning("linksnappy has no root folder to start the torrent into")
        return None

    def _finished(self) -> list[tuple[str, list]]:
        out = []
        for entry in self._ours():
            ident = self._entry_id(entry)
            status = self._call("/torrents/STATUS", {"tid": ident}) or {}
            if str(status.get("status", "")).upper() != "FINISHED":
                continue
            files = self._call("/torrents/FILES", {"id": ident})
            out.append((status.get("name") or ident, _linksnappy_files(files)))
        return out


def _linksnappy_files(node) -> list[tuple[str, str]]:
    """Every downloadable file in a Linksnappy FILES answer.

    The answer is a nested mapping rather than a list, and only the leaves
    carry `downloadLink`, so it is walked rather than iterated.
    """
    out = []
    if isinstance(node, dict):
        if node.get("downloadLink"):
            link = node["downloadLink"]
            out.append((node.get("filename") or node.get("name")
                        or _basename_of(link), link))
        else:
            for value in node.values():
                out.extend(_linksnappy_files(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_linksnappy_files(value))
    return out


#: Every debrid service, by the type name stored in its configuration. A table
#: rather than a chain of branches in build_client because these eight differ
#: in nothing an operator types -- only in the class that reads it -- and a
#: ninth should be one line here and one entry in CLIENT_TYPES.
DEBRID_CLIENTS = {
    "realdebrid": RealDebrid,
    "alldebrid": AllDebrid,
    "premiumize": Premiumize,
    "torbox": TorBox,
    "debridlink": DebridLink,
    "offcloud": Offcloud,
    "putio": PutIo,
    "linksnappy": Linksnappy,
}


# ------------------------------------------------------------- blackhole ----


@dataclass(frozen=True)
class BlackholeConfig:
    drop_path: str = ""       # where the .torrent or .nzb is written
    watch_path: str = ""      # where the client leaves what it finished
    mode: str = "torrent"     # "torrent" | "usenet"
    magnet_extension: str = ".magnet"
    #: Seconds a finished item must sit untouched before it is reported.
    settle: float = 60.0
    base_url: str = ""        # nothing to connect to; the status row reads it
    timeout: int = 30


class Blackhole:
    """A watched directory, for every client ROMarr does not speak.

    There is no API here and nothing to connect to. ROMarr writes the .torrent
    or the .nzb into a folder, somebody else's client picks it up, and what it
    finishes appears in another folder. That is the entire protocol -- which
    is exactly why it earns its place: it is the answer for a client nobody
    has written a driver for, including one that has not been released yet.

    Three decisions worth knowing about.

    **The file is named after the release.** Not after whatever the indexer
    called it: the importer matches a finished download to its queue row by
    the release title, and a file named `1234.torrent` matches nothing.

    **A magnet is written as a one-line `.magnet` file.** It has no file to
    write otherwise. Not every client reads those; the ones that do not need a
    torrent URL from the indexer instead, and the field help says so, because
    a file that sits in a folder for ever is a worse answer than a refusal.

    **Anything modified in the last `settle` seconds is ignored.** The writer
    is somebody else's process and nothing tells us when it has finished.
    Importing a file that is still growing produces a truncated ROM that looks
    for all the world like a bad dump.
    """

    #: See hand_off. This is the client where it matters most -- the name is
    #: the only thing that survives the trip through a folder.
    TAKES_NAME = True

    def __init__(self, config: BlackholeConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        #: What reachable() last found out, for the status page.
        self.detail = ""

    @property
    def protocol(self) -> str:
        return "usenet" if self._config.mode == "usenet" else "torrent"

    @property
    def name(self) -> str:
        return ("Usenet Blackhole" if self.protocol == "usenet"
                else "Torrent Blackhole")

    @property
    def extension(self) -> str:
        return ".nzb" if self.protocol == "usenet" else ".torrent"

    @property
    def configured(self) -> bool:
        return bool(self._config.drop_path and self._config.watch_path)

    def reachable(self) -> bool:
        """Both folders exist and can be written to.

        There is nothing else to test. A blackhole whose drop folder is not
        writable fails at grab time with an OSError nobody sees, so the check
        that matters is done here where it can be reported.
        """
        if not self.configured:
            self.detail = "both folders must be set"
            return False
        for role, path in (("drop", self._config.drop_path),
                           ("watch", self._config.watch_path)):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as err:
                self.detail = f"{role} folder is not usable: {err.strerror}"
                return False
        self.detail = (f"drops {self.extension} into {self._config.drop_path}, "
                       f"imports from {self._config.watch_path}")
        return True

    def add(self, url: str, *, name: str = "") -> bool:
        if not self.configured:
            return False
        stem = _safe_stem(name) or _safe_stem(_basename_of(url)) or "romarr"
        try:
            folder = Path(self._config.drop_path)
            folder.mkdir(parents=True, exist_ok=True)
            if str(url or "").lower().startswith("magnet:"):
                payload = url.encode("utf-8")
                destination = folder / f"{stem}{self._config.magnet_extension}"
            else:
                response = self._session.get(url, timeout=self._config.timeout)
                response.raise_for_status()
                payload = response.content
                destination = folder / f"{stem}{self.extension}"
        except requests.RequestException as err:
            # Never the URL itself: an indexer's key is in it.
            log.warning("could not fetch the release for the blackhole: %s",
                        err.__class__.__name__)
            return False
        except OSError as err:
            log.warning("blackhole drop folder is not writable: %s", err)
            return False
        # Written beside and renamed into place, because the whole point of
        # the folder is that something else is watching it -- and a watcher
        # that reads a half-written .torrent reports a corrupt file.
        partial = destination.with_suffix(destination.suffix + ".partial")
        try:
            partial.write_bytes(payload)
            partial.replace(destination)
        except OSError as err:
            log.warning("could not write into the blackhole: %s", err)
            return False
        return True

    def completed(self) -> list[dict]:
        if not self.configured:
            return []
        watched = Path(self._config.watch_path)
        try:
            entries = sorted(watched.iterdir())
        except OSError:
            return []
        cutoff = time.time() - float(self._config.settle)
        out = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.suffix.lower() in _IN_PROGRESS:
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            out.append({"name": entry.stem if entry.is_file() else entry.name,
                        "content_path": str(entry),
                        "save_path": str(watched), "state": "done"})
        return out


#: Suffixes every torrent and usenet client uses for a file it is still
#: writing. Present so a settle window that is too short does not import one.
_IN_PROGRESS = {".part", ".partial", ".!qb", ".tmp", ".crdownload", ".bts"}


def _safe_stem(name: str) -> str:
    """A release title reduced to something safe to be a filename.

    A title arrives from an indexer, which makes it somebody else's text: it
    is not allowed to contain a path separator, and on Windows it is not
    allowed to contain the handful of characters that make a file unopenable.
    """
    stem = os.path.basename(str(name or "").replace("\\", "/"))
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem).strip(" .")
    return stem[:180]


# ------------------------------------------------------------ ROM sites ----
#
# Everything above this line hands a URL to somebody else's daemon and lets it
# do the fetching. The ROM sites do not work that way: they serve files over
# ordinary HTTP, there is no torrent and no NZB, and the thing that has to
# fetch the bytes is ROMarr.
#
# Two modes, because the sites divide cleanly into two kinds and only one of
# them needs anything heavy:
#
#   * `direct` -- the site serves a URL. A GET fetches it. Faster, cheaper,
#     no dependencies, and the default: a plugin asks for a browser only when
#     plain HTTP genuinely cannot answer.
#   * `browser` -- the download is a form POST carrying a per-render token, or
#     a link that only exists after the page's JavaScript has run. Vimm's Lair
#     is the case that made this necessary. A real headless Chromium loads the
#     real page and clicks the real control; see browser.py, which also
#     documents at length what this is not allowed to become.
#
# A site that can only be downloaded from by solving a CAPTCHA, defeating a
# challenge, forging a header or breaking a login is reported as unavailable,
# with the reason, and its plugin stays catalogue-only. That is the finished
# answer for such a site, not a gap.

#: Who we say we are. No version in it, deliberately: downloaders.py cannot
#: import app.py without a cycle, and a version constant duplicated here would
#: be wrong the first time one of them changed. The URL is the part that
#: matters to an operator reading their access log -- it tells them what
#: visited and where to complain.
SITE_USER_AGENT = "ROMarr (+https://github.com/BlizzHacker/romarr)"

#: Seconds between requests to one host, when robots.txt does not ask for
#: more. Five is slower than any human browsing and that is the point: this
#: fetches whole ROMs from small sites run by hobbyists.
SITE_DELAY = 5.0

#: Statuses worth waiting out. 429 and 503 both mean "later", and the server
#: usually says how much later in Retry-After.
SITE_RETRY = (429, 503)

#: The one that means stop. A 403 is a refusal, and retrying a refusal --
#: or dressing the request up until it stops being refused -- is the
#: behaviour this whole module exists to not have.
SITE_FORBIDDEN = 403


class SitePolicy:
    """robots.txt, one request at a time, and a real gap between them.

    Separate from the client because being a good guest is a property of the
    fetching, not of the mode: the browser lane loads pages from the same
    small servers the direct lane does, and it would be absurd for one of them
    to honour a crawl-delay and the other to ignore it.

    The clock is per host. A shared one would make two sites wait for each
    other for no reason, and a global "one request per five seconds" is not
    what anybody's robots.txt asked for.
    """

    #: Reading robots.txt must never cost as much as reading a ROM.
    ROBOTS_TIMEOUT = 10

    def __init__(self, *, user_agent: str = SITE_USER_AGENT,
                 delay: float = SITE_DELAY,
                 session: requests.Session | None = None, sleeper=None):
        self.user_agent = user_agent
        self.delay = float(delay)
        self._session = session or requests.Session()
        self._sleep = sleeper or time.sleep
        self._robots: dict[str, object] = {}
        self._last: dict[str, float] = {}

    @staticmethod
    def _host(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _rules(self, url: str):
        """This host's robots.txt, fetched once and kept.

        A network failure or a 5xx is treated as a refusal, which is what
        RFC 9309 asks for: a site whose robots.txt cannot be read has not
        given permission, and guessing that it would have is exactly the
        assumption a polite client does not get to make. A 404 is the
        opposite -- it is a site saying it has no rules.
        """
        host = self._host(url)
        if host in self._robots:
            return self._robots[host]
        parser = RobotFileParser()
        try:
            response = self._session.get(
                f"{host}/robots.txt", timeout=self.ROBOTS_TIMEOUT,
                headers={"User-Agent": self.user_agent})
        except requests.RequestException as err:
            log.warning("robots.txt for %s could not be read: %s", host,
                        type(err).__name__)
            self._robots[host] = None
            return None
        if response.status_code >= 500:
            self._robots[host] = None
            return None
        if response.status_code >= 400:
            parser.parse([])          # no rules is not the same as no answer
        else:
            parser.parse(response.text.splitlines())
        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> tuple[bool, str]:
        """Whether robots.txt permits this fetch, and why not when it does not."""
        rules = self._rules(url)
        if rules is None:
            return False, ("robots.txt could not be read, so this site has "
                           "given no permission to fetch from it")
        if not rules.can_fetch(self.user_agent, url):
            return False, "robots.txt disallows this path"
        return True, ""

    def wait(self, url: str) -> None:
        """Sleep until this host's turn comes round.

        The site's own crawl-delay wins when it is longer than ours; a site
        that asks for ten seconds gets ten, not five.
        """
        host = self._host(url)
        gap = self.delay
        rules = self._robots.get(host)
        asked = None
        if rules is not None:
            try:
                asked = rules.crawl_delay(self.user_agent)
            except Exception:
                asked = None
        if asked:
            gap = max(gap, float(asked))
        previous = self._last.get(host)
        now = time.monotonic()
        if previous is not None and now - previous < gap:
            self._sleep(gap - (now - previous))
        self._last[host] = time.monotonic()


@dataclass(frozen=True)
class SiteConfig:
    save_path: str
    mode: str = "direct"              # "direct" | "browser"
    #: A browser running somewhere else: `ws://` for a playwright run-server,
    #: `http://` for a bare debugging port. Empty means launch one here. Named
    #: base_url because that is the field every other client in this module
    #: carries and the status row reads by that name.
    base_url: str = ""
    #: Where an `http://` browser writes its downloads, as it sees the path.
    #: Needed only for that one shape, and only when the two hosts share a
    #: directory -- see browser._keep, which explains why ws:// does not need
    #: it and http:// cannot do without it.
    remote_download_dir: str = ""
    user_agent: str = SITE_USER_AGENT
    delay: float = SITE_DELAY
    timeout: int = 120
    #: How many files to fetch per sweep. One, deliberately: these are small
    #: sites, and a parallel downloader is the difference between a guest and
    #: a problem.
    max_active: int = 1


class SiteDownloader:
    """A ROM site as a download client.

    Shaped like every other client here -- configured / reachable / add /
    completed -- so nothing else in ROMarr needs to know a file arrived over
    HTTP rather than BitTorrent.

    `add` records the job and returns; `completed` does the fetching and then
    reports what is on disk. That is the Real-Debrid shape rather than a
    thread pool, and for the same reason: the import sweep already runs on a
    timer, an HTTP handler must not block for the length of a ROM download,
    and a queue that survives a restart has to be on disk anyway.
    """

    #: This client can label a job with the release title -- see hand_off.
    #: It matters more here than anywhere else: the importer matches a
    #: finished download to its queue row by that title, and a file named by
    #: the site rather than by the release would never match.
    TAKES_NAME = True

    #: The ledger of what this client was asked for and what it has fetched.
    #: Beside the downloads rather than in ROMarr's settings so that moving
    #: the download directory moves its queue with it.
    LEDGER = ".romarr-sites.json"

    def __init__(self, config: SiteConfig, session: requests.Session | None = None,
                 policy: SitePolicy | None = None, driver=None):
        self._config = config
        self._session = session or requests.Session()
        self._policy = policy or SitePolicy(
            user_agent=config.user_agent, delay=config.delay,
            session=self._session)
        # Injected by the tests; live use imports browser.py lazily so an
        # install without the driver never pays for it.
        self._driver = driver
        #: Hosts that answered 403. Remembered for the life of the process so
        #: a refusal is not re-asked once a minute for the rest of the day.
        self._forbidden: set[str] = set()
        #: What reachable() last found out, for the status page.
        self.detail = ""
        #: The estate's remote path mappings, needed to find a file an
        #: `http://` browser wrote on another host. Set after construction
        #: rather than carried on the config because the table is a property
        #: of the install, not of this client row -- the same list already
        #: translates every other client's completed paths.
        self.path_mappings: list | tuple = ()

    # -- identity ----------------------------------------------------------

    @property
    def protocol(self) -> str:
        """The mode is the protocol.

        A release from a plugin that needs a click is not the same kind of
        thing as one that is a URL, and routing them through `pick_client`
        keeps that distinction where every other routing decision already
        lives. It also means a site needing a browser on an install without
        one fails as "no download client configured for browser", which names
        the missing piece, instead of failing inside a driver import.
        """
        return "browser" if self._config.mode == "browser" else "direct"

    @property
    def name(self) -> str:
        return "Headless Browser" if self.protocol == "browser" else "Direct HTTP"

    @property
    def configured(self) -> bool:
        return bool(self._config.save_path)

    def reachable(self) -> bool:
        if not self.configured:
            self.detail = "no download directory set"
            return False
        root = Path(self._config.save_path)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self.detail = f"download directory is not writable: {err.strerror}"
            return False
        if self.protocol != "browser":
            self.detail = f"downloads land in {root}"
            return True
        ok, why = self._availability()
        self.detail = why
        return ok

    def _availability(self) -> tuple[bool, str]:
        if self._driver is not None:
            return self._driver.availability(self._config.base_url)
        try:
            from . import browser
        except ImportError as err:          # pragma: no cover -- packaging fault
            return False, f"browser support is missing: {err}"
        return browser.availability(self._config.base_url)

    # -- the ledger --------------------------------------------------------

    def _ledger_path(self) -> Path:
        return Path(self._config.save_path) / self.LEDGER

    def _read(self) -> list[dict]:
        try:
            rows = json.loads(self._ledger_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict)]

    def _write(self, rows: list[dict]) -> None:
        try:
            path = self._ledger_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        except OSError as err:
            log.warning("could not record the site download queue: %s", err)

    # -- the client interface ----------------------------------------------

    def add(self, url: str, *, save_path: str | None = None, name: str = "") -> bool:
        """Record a job. The bytes are fetched by the next `completed` sweep.

        Refusals that can be decided without touching the network are decided
        here, so an operator finds out at grab time rather than discovering a
        queue row that quietly never moves.
        """
        if not self.configured:
            return False
        if not str(url or "").lower().startswith(("http://", "https://")):
            log.warning("site downloader was handed a %s link, not an HTTP one",
                        str(url).split(":", 1)[0] or "blank")
            return False
        rows = self._read()
        if any(r.get("url") == url and r.get("state") in ("pending", "done")
               for r in rows):
            # Already queued or already here. Both mean the caller got what
            # it asked for, so both are a success.
            return True
        rows.append({
            "url": url,
            "name": name,
            "save_path": save_path or self._config.save_path,
            "state": "pending",
            "detail": "",
            "file": "",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self._write(rows)
        return True

    def completed(self) -> list[dict]:
        """Fetch what is pending, then report everything already here.

        Reports rather than pops: the importer sweeps repeatedly and decides
        for itself what it has already taken, exactly as it does with a
        torrent that stays in qBittorrent after finishing.
        """
        if not self.configured:
            return []
        rows = self._read()
        fetched = 0
        for row in rows:
            if row.get("state") != "pending":
                continue
            if fetched >= self._config.max_active:
                break
            fetched += 1
            self._run(row)
        if fetched:
            self._write(rows)
        out = []
        for row in rows:
            if row.get("state") != "done" or not row.get("file"):
                continue
            path = Path(row["file"])
            if not path.exists():
                continue
            out.append({
                "name": row.get("name") or path.name,
                "content_path": str(path),
                "save_path": str(path.parent),
                "state": "done",
            })
        return out

    def _run(self, row: dict) -> None:
        """Fetch one job, in place, recording why if it did not work."""
        url = row.get("url", "")
        host = SitePolicy._host(url)
        if host in self._forbidden:
            row.update(state="failed",
                       detail="this site answered 403 earlier in this session")
            return
        allowed, why = self._policy.allowed(url)
        if not allowed:
            row.update(state="failed", detail=why)
            log.warning("refusing %s: %s", host, why)
            return
        target = Path(row.get("save_path") or self._config.save_path)
        try:
            if self.protocol == "browser":
                saved = self._browser_fetch(url, target)
            else:
                saved = self._direct_fetch(url, target)
        except Exception as err:
            row.update(state="failed", detail=f"{type(err).__name__}: {err}")
            log.warning("site download failed: %s", err)
            return
        row.update(state="done", detail="", file=str(saved))

    def _direct_fetch(self, url: str, target: Path) -> Path:
        """A plain GET, streamed to disk, with the manners turned on.

        Retries only what a server asked to have retried -- 429 and 503 carry
        Retry-After and mean "later". Everything else that is not a 200 is
        reported as it stands; in particular a 403 stops this host for the
        rest of the session rather than being tried again with a different
        hat on.
        """
        target.mkdir(parents=True, exist_ok=True)
        attempts = 3
        for attempt in range(attempts):
            self._policy.wait(url)
            response = self._session.get(
                url, stream=True, timeout=self._config.timeout,
                headers={"User-Agent": self._config.user_agent})
            status = response.status_code
            if status == SITE_FORBIDDEN:
                response.close()
                self._forbidden.add(SitePolicy._host(url))
                raise RuntimeError(
                    "the site answered 403; ROMarr does not retry a refusal "
                    "or disguise the request to get round one")
            if status in SITE_RETRY and attempt < attempts - 1:
                after = response.headers.get("Retry-After", "")
                response.close()
                self._policy._sleep(_retry_after(after, self._config.delay))
                continue
            response.raise_for_status()
            return _stream_to(response, target, url)
        raise RuntimeError("the site asked us to come back later, three times")

    def _browser_fetch(self, url: str, target: Path) -> Path:
        # The same clock the plain GET waits on. A browser loading a page is a
        # request to the same small server, and honouring a crawl-delay in one
        # mode and ignoring it in the other would be absurd.
        self._policy.wait(url)
        driver = self._driver
        if driver is None:
            from . import browser as driver          # noqa: N813
        return driver.fetch(
            url, target,
            endpoint=self._config.base_url,
            ua_token=self._config.user_agent,
            timeout=self._config.timeout,
            remote_dir=self._config.remote_download_dir,
            path_mappings=self.path_mappings,
            # Checked again where it lands. `allowed` above cleared the page;
            # a click routinely crosses to a file host with its own robots.txt.
            allowed=self._policy.allowed,
        )


def _retry_after(header: str, fallback: float) -> float:
    """Seconds to wait, from a Retry-After that may be either legal form.

    Capped: a server is allowed to say "an hour", and a download client that
    then sleeps for an hour inside an import sweep looks exactly like a
    download client that has hung.
    """
    try:
        return min(float(header), 300.0)
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return fallback
    if when is None:
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), 300.0))


def _filename_for(response, url: str) -> str:
    """The name to save under: what the server said, else what the URL says.

    Content-Disposition first because that is the site telling us the file's
    real name, and the real name carries the region and revision a DAT check
    and the importer both read. Any directory component is dropped -- a
    filename arriving from someone else's server is not allowed to choose a
    path.
    """
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    name = ""
    if match:
        name = unquote(match.group(1).strip())
    if not name:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = os.path.basename(name.replace("\\", "/")).strip()
    return name or "download.bin"


def _stream_to(response, target: Path, url: str) -> Path:
    """Write the body to `target`, via a .partial so a half file never imports."""
    destination = target / _filename_for(response, url)
    partial = destination.with_suffix(destination.suffix + ".partial")
    with response:
        with open(partial, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
    partial.replace(destination)
    return destination


def hand_off(client, url: str, *, name: str = "") -> bool:
    """Give a client a release, telling the ones that can use it its title.

    The importer matches a finished download to its queue row by the release
    title, so a client that can label a job with it produces imports and one
    that cannot produces downloads nobody claims. Only some can, and handing
    the keyword to the rest is a TypeError -- hence the declaration on the
    class rather than a signature check here.
    """
    if name and getattr(client, "TAKES_NAME", False):
        return client.add(url, name=name)
    return client.add(url)


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


def _debrid_fields(token_help: str, save_default: str,
                   token_label: str = "API Token") -> list:
    """The form every debrid row has: a credential and somewhere to put files.

    Written once because the differences between these services are in their
    APIs, not in what an operator types. Seven hand-copied field lists would
    drift, and the one that drifted would be the one whose Save Path silently
    stopped being asked for.
    """
    return [
        FIELD("name", "Name"),
        FIELD("enable", "Enable", "bool", True),
        FIELD("api_token", token_label, "secret", help=token_help),
        FIELD("save_path", "Save Path", default=save_default,
              help="The service fetches the release on its own hardware; "
                   "ROMarr downloads the result into here and imports from "
                   "it. Only what ROMarr itself sent is ever touched."),
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
    "vuze": {
        "label": "Vuze",
        "protocol": "torrent",
        "default_port": 9091,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Label", default="romarr",
                  help="Needs Vuze's Transmission RPC plugin, which is not "
                       "installed by default. Vuze itself is no longer "
                       "developed -- BiglyBT is the fork that is."),
        ],
    },
    "biglybt": {
        "label": "BiglyBT",
        "protocol": "torrent",
        "default_port": 9091,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Label", default="romarr",
                  help="Needs BiglyBT's Transmission RPC plugin, which is "
                       "not installed by default."),
        ],
    },
    "utorrent": {
        "label": "uTorrent / BitTorrent",
        "protocol": "torrent",
        "default_port": 8080,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Label", default="romarr",
                  help="The Web UI cannot label a torrent added by URL, so "
                       "the label is applied afterwards using the magnet's "
                       "own infohash. A release grabbed as a .torrent link "
                       "has no hash to use and lands unlabelled."),
        ],
    },
    "aria2": {
        "label": "Aria2",
        "protocol": "torrent",
        "default_port": 6800,
        "fields": _COMMON + [
            FIELD("secret", "RPC Secret", "secret",
                  help="aria2c --rpc-secret. Sent as the first argument of "
                       "every call, which is the only place aria2 looks."),
            FIELD("save_path", "Download Directory",
                  help="Passed as aria2's `dir`. Leave blank to use the "
                       "daemon's own default."),
        ],
    },
    "flood": {
        "label": "Flood",
        "protocol": "torrent",
        "default_port": 3000,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Tag", default="romarr",
                  help="Flood has tags rather than categories; this is one"),
            FIELD("save_path", "Destination",
                  help="Where Flood tells its client to put the download. "
                       "Blank uses the client's default."),
        ],
    },
    "freebox": {
        "label": "Freebox Download",
        "protocol": "torrent",
        "default_port": 443,
        "fields": _COMMON + [
            FIELD("api_url", "API URL", default="/api/v1/",
                  help="The API base and version, as the box serves it"),
            FIELD("app_id", "App ID", default="romarr"),
            FIELD("app_token", "App Token", "secret",
                  help="Issued once, after somebody authorises the app on "
                       "the front panel of the box. ROMarr cannot do that "
                       "part."),
            FIELD("save_path", "Destination Directory",
                  help="A path as the Freebox sees it, e.g. /Disque dur/roms"),
            FIELD("category", "Subfolder", default="romarr",
                  help="The box has no labels, so a category is a subfolder "
                       "of the destination"),
        ],
    },
    "hadouken": {
        "label": "Hadouken",
        "protocol": "torrent",
        "default_port": 7070,
        "fields": _COMMON + [
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("category", "Label", default="romarr"),
        ],
    },
    "porla": {
        "label": "porla",
        "protocol": "torrent",
        "default_port": 1337,
        "fields": _COMMON + [
            FIELD("api_token", "API Token", "secret",
                  help="A porla JWT, from `porla auth token` or the web UI"),
            FIELD("save_path", "Save Path",
                  help="Blank uses porla's own default or the preset's"),
            FIELD("category", "Category", default="romarr"),
        ],
    },
    "realdebrid": {
        "label": "Real-Debrid",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "From real-debrid.com/apitoken. Magnets only -- Real-Debrid "
            "takes .torrent files as an upload, which indexers do not offer.",
            "/downloads/realdebrid"),
    },
    "alldebrid": {
        "label": "AllDebrid",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "From alldebrid.com/apikeys. Magnets and infohashes only.",
            "/downloads/alldebrid", token_label="API Key"),
    },
    "premiumize": {
        "label": "Premiumize",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "From premiumize.me/account. Takes magnets and .torrent URLs.",
            "/downloads/premiumize", token_label="API Key"),
    },
    "torbox": {
        "label": "TorBox",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "From torbox.app/settings. Download links last three hours, so "
            "ROMarr asks for one per file as it fetches it.",
            "/downloads/torbox"),
    },
    "debridlink": {
        "label": "Debrid-Link",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "From debrid-link.com/webapp/apikey. Takes magnets and "
            ".torrent URLs.",
            "/downloads/debridlink"),
    },
    "offcloud": {
        "label": "Offcloud",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "From offcloud.com account settings. Cloud downloads need the "
            "cloud add-on on the account; without it every add is refused.",
            "/downloads/offcloud", token_label="API Key"),
    },
    "putio": {
        "label": "put.io",
        "protocol": "torrent",
        "default_port": 0,
        "fields": _debrid_fields(
            "An OAuth token from put.io/account/api/apps. Takes magnets and "
            ".torrent URLs.",
            "/downloads/putio", token_label="OAuth Token"),
    },
    "linksnappy": {
        "label": "Linksnappy",
        "protocol": "torrent",
        "default_port": 0,
        "fields": [
            FIELD("name", "Name"),
            FIELD("enable", "Enable", "bool", True),
            # The one debrid service with no API key: it authenticates with
            # the account itself and answers with a session cookie.
            FIELD("username", "Username"),
            FIELD("password", "Password", "secret"),
            FIELD("save_path", "Save Path", default="/downloads/linksnappy",
                  help="Magnets only. Linksnappy adds a torrent stopped, so "
                       "ROMarr starts it into the account's root folder "
                       "before it will download anything."),
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
    "nzbvortex": {
        "label": "NZBVortex",
        "protocol": "usenet",
        "default_port": 4321,
        "fields": _COMMON + [
            FIELD("api_key", "API Key", "secret",
                  help="NZBVortex → Preferences → API. It is never sent: it "
                       "is hashed with a nonce from each side."),
            FIELD("category", "Group", default="",
                  help="NZBVortex calls categories groups. The group must "
                       "already exist."),
            FIELD("verify_tls", "Verify TLS Certificate", "bool", False,
                  help="NZBVortex serves HTTPS with a certificate it made "
                       "for itself, so this is off by default. Turn it on if "
                       "you have put a real certificate in front of it."),
        ],
    },
    # The two blackholes. No API, no connection, no driver: a folder ROMarr
    # writes into and a folder it reads out of. They are how somebody uses a
    # client this module does not speak -- including one that does not exist
    # yet.
    "torrentblackhole": {
        "label": "Torrent Blackhole",
        "protocol": "torrent",
        "default_port": 0,
        "fields": [
            FIELD("name", "Name", default="Torrent Blackhole"),
            FIELD("enable", "Enable", "bool", True),
            FIELD("drop_path", "Torrent Folder",
                  default="/downloads/blackhole/torrents",
                  help="Where the .torrent is written. Point your client's "
                       "watch directory at this."),
            FIELD("watch_path", "Watch Folder",
                  default="/downloads/blackhole/complete",
                  help="Where your client leaves what it finished. The "
                       "importer reads here."),
            FIELD("magnet_extension", "Magnet File Extension",
                  default=".magnet",
                  help="A magnet has no file to write, so it is written as "
                       "one line of text. Clients that cannot read these "
                       "need a .torrent link from the indexer instead."),
            FIELD("settle", "Settle Seconds", "int", 60,
                  help="How long a finished item must sit untouched before "
                       "it is imported. Nothing announces the end of a "
                       "write, and importing a growing file produces a "
                       "truncated ROM."),
        ],
    },
    "usenetblackhole": {
        "label": "Usenet Blackhole",
        "protocol": "usenet",
        "default_port": 0,
        "fields": [
            FIELD("name", "Name", default="Usenet Blackhole"),
            FIELD("enable", "Enable", "bool", True),
            FIELD("drop_path", "NZB Folder",
                  default="/downloads/blackhole/nzb",
                  help="Where the .nzb is written. Point your client's watch "
                       "directory at this."),
            FIELD("watch_path", "Watch Folder",
                  default="/downloads/blackhole/complete",
                  help="Where your client leaves what it finished. The "
                       "importer reads here."),
            FIELD("settle", "Settle Seconds", "int", 60,
                  help="How long a finished item must sit untouched before "
                       "it is imported."),
        ],
    },
    # The two ROM-site modes. They are separate types rather than one type
    # with a dropdown because they route differently: a plugin declares which
    # of the two its site needs, `pick_client` reads that off the release, and
    # a single row that could be either would make the routing depend on
    # something nobody configured.
    "direct": {
        "label": "Direct HTTP",
        "protocol": "direct",
        "default_port": 0,
        "fields": [
            FIELD("name", "Name", default="Direct HTTP"),
            FIELD("enable", "Enable", "bool", True),
            FIELD("save_path", "Save Path", default="/downloads/sites",
                  help="Where fetched files land; the importer reads here"),
            FIELD("delay", "Seconds between requests", "int", 5,
                  help="A site's own robots.txt crawl-delay wins when it "
                       "asks for longer"),
        ],
    },
    "browser": {
        "label": "Headless Browser",
        "protocol": "browser",
        "default_port": 0,
        "fields": [
            FIELD("name", "Name", default="Headless Browser"),
            FIELD("enable", "Enable", "bool", True),
            FIELD("save_path", "Save Path", default="/downloads/sites"),
            FIELD("host", "Browser Host",
                  help="Blank launches Chromium here. For a browser in "
                       "another container, run `playwright run-server "
                       "--host 0.0.0.0 --port 3000` on it and set Scheme to "
                       "ws -- the driver runs there and streams the finished "
                       "file back, so the two need no shared directory."),
            FIELD("port", "Browser Port", "int", 3000),
            FIELD("scheme", "Scheme", default="ws",
                  help="ws for a playwright run-server; http for a bare "
                       "`chromium --remote-debugging-port`, which can only "
                       "hand the file over through a shared directory."),
            FIELD("remote_download_dir", "Browser's Download Directory",
                  help="Only for an http endpoint, and only when both "
                       "containers can see the same directory. Translated "
                       "with the remote path mappings."),
            FIELD("delay", "Seconds between requests", "int", 5),
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
    elif kind in ("vuze", "biglybt"):
        # Both are the Transmission RPC behind a plugin, so they are built
        # with Transmission's own configuration rather than a copy of it.
        client = (Vuze if kind == "vuze" else BiglyBT)(TransmissionConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    elif kind == "utorrent":
        client = UTorrent(UTorrentConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    elif kind == "aria2":
        client = Aria2(Aria2Config(
            base_url=url, secret=cfg.get("secret", ""),
            save_path=cfg.get("save_path", "")))
    elif kind == "flood":
        client = Flood(FloodConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category,
            save_path=cfg.get("save_path", "")))
    elif kind == "freebox":
        # The API base and version are part of the address, not a path the
        # client appends, because the box serves them together and an
        # operator who has moved them needs to be able to say so.
        api = "/" + str(cfg.get("api_url") or "/api/v1/").strip("/")
        client = FreeboxDownload(FreeboxConfig(
            base_url=url.rstrip("/") + api,
            app_id=cfg.get("app_id", ""), app_token=cfg.get("app_token", ""),
            save_path=cfg.get("save_path", ""),
            category=cfg.get("category", "")))
    elif kind == "hadouken":
        client = Hadouken(HadoukenConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    elif kind == "porla":
        client = Porla(PorlaConfig(
            base_url=url, api_token=cfg.get("api_token", ""),
            save_path=cfg.get("save_path", ""), category=category))
    elif kind in DEBRID_CLIENTS:
        # Every debrid service takes the same configuration and differs only
        # in the class that reads it, so they are built from a table rather
        # than from eight indistinguishable branches.
        debrid = DEBRID_CLIENTS[kind]
        client = debrid(DebridConfig(
            api_token=cfg.get("api_token", ""),
            save_path=cfg.get("save_path", ""),
            base_url=debrid.DEFAULT_URL,
            username=cfg.get("username", ""),
            password=cfg.get("password", "")))
    elif kind in ("torrentblackhole", "usenetblackhole"):
        client = Blackhole(BlackholeConfig(
            drop_path=cfg.get("drop_path", ""),
            watch_path=cfg.get("watch_path", ""),
            mode="usenet" if kind == "usenetblackhole" else "torrent",
            magnet_extension=str(cfg.get("magnet_extension") or ".magnet"),
            settle=float(cfg.get("settle") if cfg.get("settle") is not None
                         else 60)))
    elif kind == "nzbvortex":
        client = NZBVortex(NzbVortexConfig(
            base_url=url, api_key=cfg.get("api_key", ""),
            category=cfg.get("category", ""),
            verify_tls=bool(cfg.get("verify_tls", False))))
    elif kind == "sabnzbd":
        client = SABnzbd(SabConfig(
            base_url=url, api_key=cfg.get("api_key", ""), category=category))
    elif kind == "nzbget":
        client = NZBGet(NzbgetConfig(
            base_url=url, username=cfg.get("username", ""),
            password=cfg.get("password", ""), category=category))
    elif kind in ("direct", "browser"):
        # A blank host means "launch one here", so an unset host must not be
        # assembled into `ws://localhost:3000` -- that would point the driver
        # at a machine nobody configured and fail as a connection refused
        # rather than as the local launch that was asked for.
        endpoint = ""
        if kind == "browser" and cfg.get("host"):
            # base_url_for only speaks http and https; the scheme decides how
            # the finished file gets back here, so it is a field of its own
            # rather than a guess from the port number.
            scheme = str(cfg.get("scheme") or "ws").strip().lower()
            endpoint = re.sub(r"^https?://", f"{scheme}://", url, count=1)
        client = SiteDownloader(SiteConfig(
            save_path=cfg.get("save_path", ""),
            mode=kind,
            base_url=endpoint,
            remote_download_dir=cfg.get("remote_download_dir", ""),
            delay=float(cfg.get("delay") or SITE_DELAY)))
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
