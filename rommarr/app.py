"""Rommarr's HTTP service and web UI.

Deliberately stdlib-only for the server itself: this runs in a 512MB LXC beside
a download client and a database, and an *arr that needs a web framework to
answer six routes is an *arr that is harder to install than the thing it
automates.

The API is shaped like the other *arr applications -- /api/v1/<noun>, with the
same nouns wherever the concept is the same -- so anything that already speaks
Radarr or Sonarr is not surprised here. Where a concept genuinely differs it is
renamed rather than faked: there is no Calendar, because a ROM has no air date,
and a Quality Profile ranks regions rather than bitrates.

Routes:
  GET  /                       the UI
  GET  /api/v1/game            the library, from RomM
  GET  /api/v1/wanted/missing  requested and not yet imported
  GET  /api/v1/queue           in flight
  GET  /api/v1/history         what happened
  GET  /api/v1/indexer         Prowlarr's indexers (read-only)
  GET  /api/v1/downloadclient  download clients and whether they answer
  GET  /api/v1/config          settings
  PUT  /api/v1/config          partial settings update
  GET  /api/v1/system/status   health of every dependency
  GET  /api/v1/system/counts   badge counts for the nav
  POST /api/v1/command         run a task
  GET  /api/v1/log             recent events
  POST /api/request            request one game (also used by GG Requestz)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import time

from .clients import QBittorrent, QbitConfig, Romm, RommConfig
from .downloaders import NZBGet, NzbgetConfig, SABnzbd, SabConfig, pick_client
from .indexers import Prowlarr, ProwlarrConfig
from .library import import_rom, map_remote_path
from .platforms import PLATFORMS, resolve
from .selection import best_release
from .store import Event, Store
from .ui import page as ui_page

log = logging.getLogger(__name__)

VERSION = "0.2.0"


@dataclass
class QueueItem:
    game: str
    platform: str
    release: str
    seeders: int
    state: str                # queued | grabbed | imported | failed
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class Rommarr:
    """The service. Holds config, clients, and the in-flight queue."""

    def __init__(self, env: dict[str, str] | None = None):
        e = env if env is not None else os.environ
        self.prowlarr = Prowlarr(ProwlarrConfig(
            base_url=e.get("PROWLARR_URL", ""),
            api_key=e.get("PROWLARR_API_KEY", ""),
        ))
        self.qbit = QBittorrent(QbitConfig(
            base_url=e.get("QBITTORRENT_URL", ""),
            username=e.get("QBITTORRENT_USER", ""),
            password=e.get("QBITTORRENT_PASS", ""),
        ))
        # Usenet is not an afterthought: Prowlarr indexes both protocols, and
        # accepting only torrents made every usenet indexer dead weight --
        # results scored fine and were then refused for having no magnet.
        self.sab = SABnzbd(SabConfig(
            base_url=e.get("SABNZBD_URL", ""),
            api_key=e.get("SABNZBD_API_KEY", ""),
        ))
        self.nzbget = NZBGet(NzbgetConfig(
            base_url=e.get("NZBGET_URL", ""),
            username=e.get("NZBGET_USER", ""),
            password=e.get("NZBGET_PASS", ""),
        ))
        self.romm = Romm(RommConfig(
            base_url=e.get("ROMM_URL", ""),
            username=e.get("ROMM_USERNAME", ""),
            password=e.get("ROMM_PASSWORD", ""),
            api_token=e.get("ROMM_API_TOKEN", ""),
        ))
        # Ordered: the first configured client that speaks a protocol wins,
        # which is how an operator expresses a preference between two usenet
        # clients without a separate setting for it.
        self.clients = [self.qbit, self.sab, self.nzbget]
        # Where GG Requestz lives, so the status page can show the connection
        # the same way Seerr shows Radarr.
        self.ggrequestz_url = e.get("GGREQUESTZ_URL", "")

        self.library = Path(e.get("ROMM_LIBRARY", "/mnt/roms"))
        self.queue: list[QueueItem] = []
        self._lock = threading.Lock()
        self._started = time.monotonic()

        # History, Wanted and settings survive a restart. Without this a restart
        # lost everything you had asked for, which is the difference between a
        # tool and a demo.
        self.store = Store(e.get("ROMMARR_DATA", "/opt/rommarr/rommarr.json"))
        # A library path saved through the UI has to win over the environment
        # default, or the setting is one you can change but not apply.
        saved = self.store.settings.get("library_path")
        if saved:
            self.library = Path(saved)

        # Shown on the General page so an operator can see what is wired up
        # without opening a shell. URLs only -- never a credential.
        self.store.settings["_prowlarr_url"] = e.get("PROWLARR_URL", "")
        self.store.settings["_qbit_url"] = e.get("QBITTORRENT_URL", "")
        self.store.settings["_romm_url"] = e.get("ROMM_URL", "")

    # -- operations --------------------------------------------------------

    def health(self) -> dict:
        return {
            "ok": True,
            "prowlarr": bool(self.prowlarr._config.api_key),
            "romm": self.romm.reachable(),
            "library": self.library.exists(),
            "library_path": str(self.library),
            "platforms": len(PLATFORMS),
            "queued": len(self.queue),
        }

    def search(self, game: str, platform_name: str = "") -> dict:
        platform = resolve(platform_name) if platform_name else None
        term = f"{game} {platform.name}" if platform else game
        releases = self.prowlarr.search(term)
        pick = best_release(releases, game, platform)
        return {
            "game": game,
            "platform": platform.slug if platform else None,
            "found": len(releases),
            "best": asdict(pick) if pick else None,
        }

    def request(self, game: str, platform_name: str) -> dict:
        platform = resolve(platform_name)
        if platform is None:
            return {"ok": False, "error": f"unknown platform: {platform_name!r}"}

        releases = self.prowlarr.search(f"{game} {platform.name}")
        pick = best_release(releases, game, platform)
        if pick is None:
            item = QueueItem(game, platform.slug, "", 0, "failed",
                             f"no usable release among {len(releases)} result(s)")
            with self._lock:
                self.queue.append(item)
            self.store.want(game, platform.slug)
            self.store.note_failure(game, platform.slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform.slug,
                                    detail=item.detail))
            return {"ok": False, "error": item.detail}

        if not pick.download_url:
            item = QueueItem(game, platform.slug, pick.title, pick.seeders, "failed",
                             "release offers no usable download link")
            with self._lock:
                self.queue.append(item)
            self.store.want(game, platform.slug)
            self.store.note_failure(game, platform.slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform.slug,
                                    release=pick.title, detail=item.detail))
            return {"ok": False, "error": item.detail}

        client = pick_client(pick.protocol, self.clients)
        if client is None:
            item = QueueItem(game, platform.slug, pick.title, pick.seeders, "failed",
                             f"no download client configured for {pick.protocol}")
            with self._lock:
                self.queue.append(item)
            self.store.want(game, platform.slug)
            self.store.note_failure(game, platform.slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform.slug,
                                    release=pick.title, detail=item.detail))
            return {"ok": False, "error": item.detail}

        ok = client.add(pick.download_url)
        item = QueueItem(game, platform.slug, pick.title, pick.seeders,
                         "grabbed" if ok else "failed",
                         "" if ok else f"{client.name} rejected the release")
        with self._lock:
            self.queue.append(item)
        if ok:
            self.store.record(Event(kind="grabbed", game=game, platform=platform.slug,
                                    release=pick.title, seeders=pick.seeders,
                                    size=getattr(pick, "size", 0)))
        else:
            self.store.want(game, platform.slug)
            self.store.note_failure(game, platform.slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform.slug,
                                    release=pick.title, detail=item.detail))
        return {"ok": ok, "release": pick.title, "seeders": pick.seeders}

    def download_clients(self) -> list[dict]:
        """Every client, what it speaks, and whether it answers.

        Reported for all of them rather than only the configured ones, because
        "SABnzbd: not configured" is the answer to "why was my usenet result
        refused" and hiding the row hides the answer.
        """
        out = []
        for c in self.clients:
            configured = getattr(c, "configured", True)
            out.append({
                "name": getattr(c, "name", type(c).__name__),
                "protocol": getattr(c, "protocol", ""),
                "url": getattr(c, "_config").base_url,
                "category": getattr(getattr(c, "_config"), "category", ""),
                "configured": configured,
                "ok": bool(configured and c.reachable()),
            })
        return out

    def ggrequestz(self) -> dict:
        """Whether GG Requestz is reachable, the way Seerr reports Radarr.

        A request front-end that cannot see its downloader is the single most
        confusing failure in this kind of stack -- requests appear to succeed
        and nothing ever arrives -- so the link is shown from this side too.
        """
        url = (self.ggrequestz_url or "").rstrip("/")
        if not url:
            return {"configured": False, "ok": False, "url": ""}
        try:
            import requests as _rq
            r = _rq.get(url, timeout=8, allow_redirects=False)
            # Behind SSO a 200 and a 302 to the login page both mean "it is
            # there"; only a connection failure means it is not.
            ok = r.status_code < 500
        except Exception as err:
            log.warning("ggrequestz unreachable: %s", err.__class__.__name__)
            ok = False
        return {"configured": True, "ok": ok, "url": url}

    def status(self) -> dict:
        """Everything the System status page reports on."""
        up = int(time.monotonic() - self._started)
        return {
            "clients": self.download_clients(),
            "ggrequestz": self.ggrequestz(),
            "version": VERSION,
            "prowlarr": bool(self.prowlarr._config.api_key),
            "prowlarr_url": self.store.settings.get("_prowlarr_url", ""),
            "qbittorrent": self.qbit.reachable(),
            "qbit_url": self.store.settings.get("_qbit_url", ""),
            "romm": self.romm.reachable(),
            "romm_url": self.store.settings.get("_romm_url", ""),
            "library": self.library.exists(),
            "library_path": str(self.library),
            "platforms": len(PLATFORMS),
            "events": len(self.store.events),
            "uptime": f"{up // 3600}h {(up % 3600) // 60}m",
        }

    # How long a library count is reused. The badge polls every 15s, so without
    # this a slow or hanging library endpoint costs its full timeout on every
    # poll -- which is the difference between a page that feels alive and one
    # that stalls four times a minute.
    COUNT_TTL = 120

    def counts(self) -> dict:
        """Badge numbers for the nav rail."""
        now = time.monotonic()
        cached, at = getattr(self, "_count_cache", (None, 0.0))
        if cached is not None and now - at < self.COUNT_TTL:
            games = cached
        else:
            try:
                games = self.romm.count()
            except Exception:
                # An unreachable RomM must not blank the whole rail, and a
                # previous good count beats showing zero.
                games = cached if cached is not None else 0
            self._count_cache = (games, now)
        with self._lock:
            queued = sum(1 for i in self.queue if i.state in ("queued", "grabbed"))
        return {"games": games, "missing": len(self.store.wanted), "queued": queued}

    def search_missing(self) -> dict:
        """Retry everything in Wanted, the way *arr's missing search does."""
        searched = grabbed = 0
        for item in list(self.store.wanted):
            searched += 1
            if self.request(item.game, item.platform).get("ok"):
                grabbed += 1
        return {"searched": searched, "grabbed": grabbed,
                "message": f"Searched {searched}, grabbed {grabbed}"}

    def run_command(self, name: str) -> dict:
        """The Tasks page. Names match how *arr labels its commands."""
        if name == "MissingGameSearch":
            return self.search_missing()
        if name == "ImportCompleted":
            done = self.import_finished()
            return {"imported": done, "message": f"Imported {len(done)}"}
        if name == "RefreshLibrary":
            try:
                return {"message": f"{self.romm.count()} games in RomM"}
            except Exception as err:
                return {"message": f"RomM unreachable: {err}"}
        return {"error": f"unknown command: {name}"}

    def import_finished(self) -> list[dict]:
        """Import anything the download client has completed."""
        results = []
        finished = []
        for client in self.clients:
            if not getattr(client, "configured", True):
                continue
            try:
                finished.extend(client.completed())
            except Exception as err:
                # One unreachable client must not stop the others importing.
                log.warning("%s completed() failed: %s", getattr(client, "name", client), err)
        for torrent in finished:
            name = torrent.get("name", "")
            # The client reports the path IT sees. When it runs in a different
            # container that path means nothing here, and the import fails with
            # "download path does not exist" while the file sits right there.
            path = map_remote_path(
                torrent.get("content_path") or torrent.get("save_path", ""),
                self.store.settings.get("remote_path_mappings"),
            )
            platform = None
            with self._lock:
                for item in self.queue:
                    if item.release == name:
                        platform = resolve(item.platform)
                        break
            if platform is None:
                continue
            outcome = import_rom(path, platform, self.library)
            if outcome.ok:
                if self.store.settings.get("rescan_after_import", True):
                    self.romm.rescan(platform.slug)
                self.store.record(Event(kind="imported", game=name,
                                        platform=platform.slug, release=name,
                                        detail=str(outcome.destination)))
                # It has arrived, so it is no longer wanted.
                for w in list(self.store.wanted):
                    if w.platform == platform.slug and w.game.lower() in name.lower():
                        self.store.fulfil(w.game, w.platform)
            else:
                self.store.record(Event(kind="failed", game=name,
                                        platform=platform.slug, detail=outcome.reason))
            results.append({"name": name, "ok": outcome.ok, "reason": outcome.reason})
        return results


# -- HTTP ------------------------------------------------------------------

def make_handler(service: Rommarr):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Rommarr"

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload):
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self):
            route = urlparse(self.path)
            query = parse_qs(route.query)
            if route.path == "/":
                return self._send(200, ui_page().encode("utf-8"),
                                  "text/html; charset=utf-8")

            # --- *arr-shaped API ---------------------------------------
            if route.path == "/api/v1/game":
                try:
                    limit = min(int((query.get("limit") or ["60"])[0]), 200)
                    offset = int((query.get("offset") or ["0"])[0])
                    return self._json(200, {
                        "items": service.romm.games(limit=limit, offset=offset),
                        "total": service.romm.count(),
                    })
                except Exception as err:
                    return self._json(200, {"items": [], "error": str(err)})
            if route.path == "/api/v1/wanted/missing":
                return self._json(200, {"items": service.store.missing()})
            if route.path == "/api/v1/queue":
                return self._json(200, {"items": [asdict(i) for i in service.queue]})
            if route.path == "/api/v1/history":
                limit = int((query.get("limit") or ["100"])[0])
                return self._json(200, {"items": service.store.history(limit)})
            if route.path == "/api/v1/log":
                limit = int((query.get("limit") or ["200"])[0])
                return self._json(200, {"items": service.store.history(limit)})
            if route.path == "/api/v1/indexer":
                try:
                    return self._json(200, {"items": service.prowlarr.indexers()})
                except Exception as err:
                    return self._json(200, {"items": [], "error": str(err)})
            if route.path == "/api/v1/downloadclient":
                return self._json(200, {"items": service.download_clients()})
            if route.path == "/api/v1/config":
                return self._json(200, service.store.settings)
            if route.path == "/api/v1/system/status":
                return self._json(200, service.status())
            if route.path == "/api/v1/system/counts":
                return self._json(200, service.counts())
            if route.path == "/api/health":
                return self._json(200, service.health())
            if route.path == "/api/platforms":
                return self._json(200, [{"slug": p.slug, "name": p.name} for p in PLATFORMS])
            if route.path == "/api/queue":
                return self._json(200, [asdict(i) for i in service.queue])
            if route.path == "/api/search":
                game = (query.get("game") or [""])[0]
                if not game:
                    return self._json(400, {"error": "game is required"})
                return self._json(200, service.search(game, (query.get("platform") or [""])[0]))
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            route = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})

            if route.path == "/api/request":
                game = (body.get("game") or "").strip()
                platform = (body.get("platform") or "").strip()
                if not game or not platform:
                    return self._json(400, {"error": "game and platform are required"})
                return self._json(200, service.request(game, platform))
            if route.path == "/api/import":
                return self._json(200, {"imported": service.import_finished()})
            if route.path == "/api/v1/command":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json(400, {"error": "name is required"})
                return self._json(200, service.run_command(name))
            return self._json(404, {"error": "not found"})

        def do_PUT(self):
            route = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})

            if route.path == "/api/v1/config":
                updated = service.store.update_settings(body)
                # A library path is only really changed once the running
                # service files ROMs there, not once it is stored.
                path = updated.get("library_path")
                if path:
                    service.library = Path(path)
                return self._json(200, updated)
            return self._json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(port: int = 7878, env: dict[str, str] | None = None):
    service = Rommarr(env)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
    log.info("rommarr listening on :%d, library=%s", port, service.library)
    httpd.serve_forever()
