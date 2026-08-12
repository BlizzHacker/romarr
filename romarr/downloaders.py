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
import os
import re
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
    #: The CDP endpoint of a browser running somewhere else. Empty means
    #: launch one here. Named base_url because that is the field every other
    #: client in this module carries and the status row reads by that name.
    base_url: str = ""
    #: Where THAT browser writes its downloads, as it sees the path. Only
    #: meaningful with base_url set, and only when the two hosts share a
    #: directory -- see browser._keep.
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
        driver = self._driver
        if driver is None:
            from . import browser as driver          # noqa: N813
        return driver.fetch(
            url, target,
            cdp_url=self._config.base_url,
            ua_token=self._config.user_agent,
            timeout=self._config.timeout,
            remote_dir=self._config.remote_download_dir,
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
