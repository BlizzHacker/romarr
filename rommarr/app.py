"""Rommarr's HTTP service and web UI.

Deliberately stdlib-only for the server itself: this runs in a 512MB LXC beside
a download client and a database, and an *arr that needs a web framework to
answer six routes is an *arr that is harder to install than the thing it
automates.

Routes:
  GET  /            the UI
  GET  /api/health  liveness, plus whether each dependency is reachable
  GET  /api/search  search indexers, ranked, without grabbing anything
  POST /api/request grab the best release for a game and queue it for import
  GET  /api/queue   what is in flight
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

from .clients import QBittorrent, QbitConfig, Romm, RommConfig
from .indexers import Prowlarr, ProwlarrConfig
from .library import import_rom
from .platforms import PLATFORMS, resolve
from .selection import best_release

log = logging.getLogger(__name__)


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
        self.romm = Romm(RommConfig(
            base_url=e.get("ROMM_URL", ""),
            username=e.get("ROMM_USERNAME", ""),
            password=e.get("ROMM_PASSWORD", ""),
            api_token=e.get("ROMM_API_TOKEN", ""),
        ))
        self.library = Path(e.get("ROMM_LIBRARY", "/mnt/roms"))
        self.queue: list[QueueItem] = []
        self._lock = threading.Lock()

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
            return {"ok": False, "error": item.detail}

        if not pick.download_url:
            # Prowlarr's own links carry its API key, so a release without a
            # plain magnet has to be grabbed server-side by Prowlarr itself.
            item = QueueItem(game, platform.slug, pick.title, pick.seeders, "failed",
                             "release has no plain magnet; grab via Prowlarr")
            with self._lock:
                self.queue.append(item)
            return {"ok": False, "error": item.detail}

        ok = self.qbit.add(pick.download_url)
        item = QueueItem(game, platform.slug, pick.title, pick.seeders,
                         "grabbed" if ok else "failed",
                         "" if ok else "download client rejected the magnet")
        with self._lock:
            self.queue.append(item)
        return {"ok": ok, "release": pick.title, "seeders": pick.seeders}

    def import_finished(self) -> list[dict]:
        """Import anything the download client has completed."""
        results = []
        for torrent in self.qbit.completed():
            name = torrent.get("name", "")
            path = Path(torrent.get("content_path") or torrent.get("save_path", ""))
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
                self.romm.rescan(platform.slug)
            results.append({"name": name, "ok": outcome.ok, "reason": outcome.reason})
        return results


# -- HTTP ------------------------------------------------------------------

UI = """<!doctype html><meta charset=utf-8><title>Rommarr</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0d11;--panel:#151a22;--line:#252d3a;--fg:#e9edf3;--dim:#8b96a8;--accent:#f2a33c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:28px 22px 70px}
h1{font-size:26px;letter-spacing:-.02em;margin:0 0 4px}h1 span{color:var(--accent)}
.sub{color:var(--dim);margin:0 0 26px}
form{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 22px}
input,select{padding:10px 13px;background:var(--panel);color:var(--fg);
border:1px solid var(--line);border-radius:10px;outline:none}
input{flex:1;min-width:200px}
button{padding:10px 20px;background:var(--accent);color:#20130a;font-weight:650;
border:0;border-radius:10px;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.s{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid var(--line)}
.s.grabbed{color:#5eead4;border-color:#2dd4bf}.s.failed{color:#f87171;border-color:#f87171}
.s.imported{color:#a3e635;border-color:#a3e635}
.hb{display:flex;gap:16px;flex-wrap:wrap;color:var(--dim);font-size:13px;margin:0 0 26px}
.hb b{color:var(--fg);font-weight:600}
</style>
<div class=wrap>
<h1>Romm<span>arr</span></h1>
<p class=sub>Request a game. It searches your indexers, grabs the healthiest release, and files the ROM into RomM.</p>
<div class=hb id=hb>checking…</div>
<form id=f>
  <input id=game placeholder="Game, e.g. Super Mario World" required>
  <select id=plat></select>
  <button>Request</button>
</form>
<table><thead><tr><th>Game</th><th>Platform</th><th>Release</th><th>Seeders</th><th>State</th></tr></thead>
<tbody id=q><tr><td colspan=5 style="color:var(--dim)">Nothing queued yet.</td></tr></tbody></table>
</div>
<script>
const j=(u,o)=>fetch(u,o).then(r=>r.json());
j('/api/platforms').then(p=>{plat.innerHTML=p.map(x=>`<option value="${x.slug}">${x.name}</option>`).join('')});
function health(){j('/api/health').then(h=>{hb.innerHTML=
 `Prowlarr <b>${h.prowlarr?'configured':'missing key'}</b>`+
 `RomM <b>${h.romm?'reachable':'unreachable'}</b>`.replace(/^/,'&nbsp;&nbsp;•&nbsp;&nbsp;')+
 `Library <b>${h.library?'mounted':'missing'}</b>`.replace(/^/,'&nbsp;&nbsp;•&nbsp;&nbsp;')+
 `<b>${h.platforms}</b> platforms`.replace(/^/,'&nbsp;&nbsp;•&nbsp;&nbsp;')})}
function queue(){j('/api/queue').then(items=>{
 q.innerHTML=items.length?items.slice().reverse().map(i=>
  `<tr><td>${i.game}</td><td>${i.platform}</td><td>${i.release||'—'}</td>
   <td>${i.seeders||'—'}</td><td><span class="s ${i.state}">${i.state}</span>
   ${i.detail?`<div style="color:var(--dim);font-size:12px">${i.detail}</div>`:''}</td></tr>`).join('')
  :'<tr><td colspan=5 style="color:var(--dim)">Nothing queued yet.</td></tr>'})}
f.onsubmit=e=>{e.preventDefault();
 j('/api/request',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({game:game.value,platform:plat.value})}).then(()=>{game.value='';queue()})};
health();queue();setInterval(queue,5000);
</script>"""


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
                return self._send(200, UI.encode(), "text/html; charset=utf-8")
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
            return self._json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(port: int = 7878, env: dict[str, str] | None = None):
    service = Rommarr(env)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
    log.info("rommarr listening on :%d, library=%s", port, service.library)
    httpd.serve_forever()
