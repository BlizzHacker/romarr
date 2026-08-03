"""ROMarr's HTTP service and web UI.

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
  POST /api/request            request one game
  POST /api/v1/webhook         accept a request event from a front-end
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import time

from .libraries import (
    LIBRARY_TYPES, build_library, build_library_from_config,
    load_backend_plugins, merge_library_secrets, redact_library, route_library,
)
from .clients import QBittorrent, QbitConfig, Romm, RommConfig
from . import hub  # ROM Hub bridge -- the Cartridge plugin layer
from .dat import DatIndex, parse_dat
from .downloaders import (
    CLIENT_TYPES, NZBGet, NzbgetConfig, SABnzbd, SabConfig, build_client,
    merge_secrets, pick_client, redact,
)
from .indexers import INDEXER_TYPES, build_indexer, redact_indexer
from .indexers import Prowlarr, ProwlarrConfig
from .library import import_rom, map_remote_path
from .auth import DISABLED as AUTH_DISABLED
from .auth import SESSION_COOKIE, Auth, new_api_key, parse_cookies
from .catalogue import (Submission, check_source, facets as hub_facets,
                        search as hub_search, submission_link)
from .frontends import FORMATS as FRONTEND_FORMATS
from .metadata import PROVIDERS as METADATA_PROVIDERS
from .notify import NOTIFIERS, Message, Notifier, failed, grabbed, imported
from .profiles import Blocklist, ReleaseProfile, release_id
from .upgrade import is_upgrade, merge_tags, scan as scan_directory
from .metadata import Metadata, calendar as metadata_calendar
from .openapi import spec as openapi_spec
from .ops import RateLimiter, make_backup, read_backup, render_metrics, to_csv
from .platforms import PLATFORMS, resolve
from .sso import ForwardAuth
from .totp import Totp
from .playability import DOWNLOAD, StreamServer, routes_for
from .selection import best_release, judge, score
from .store import Event, Store
from .ui import page as ui_page

log = logging.getLogger(__name__)

VERSION = "0.7.0"

# What ROMarr labels its own downloads with, so its jobs are distinguishable
# from everything else in a shared client -- the same reason Radarr and Sonarr
# each use a category of their own.
DEFAULT_CATEGORY = "romarr"


def category_for(env: dict[str, str], client: str) -> str:
    """The download category for one client, e.g. SABNZBD_CATEGORY.

    Configurable per client because the clients are separate installs with
    separate category lists, and because somebody running two ROMarrs against
    one SABnzbd needs to tell their downloads apart.

    The category does not have to exist in the client beforehand. SABnzbd 5.0.4
    defines *, movies, tv, audio and software out of the box, and `romarr` is
    none of them -- but an undefined category is accepted, kept verbatim on the
    job, and still matched by the history and queue filters this service uses to
    notice a finished download. Verified against a real instance, because the
    alternative reading -- that SABnzbd silently reassigns the job to Default
    and the history filter then never matches -- would be a download that
    completes and is never imported, with nothing anywhere saying why.

    Defining it in SABnzbd is still worth doing if you want the download to land
    in a folder of its own or run a post-processing script. ROMarr does not need
    it either way: it takes the finished path from SABnzbd's own `storage`
    field rather than assuming where the category put it.
    """
    return env.get(f"{client}_CATEGORY") or DEFAULT_CATEGORY


@dataclass
class QueueItem:
    game: str
    platform: str
    release: str
    seeders: int
    state: str                # queued | grabbed | imported | failed
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class ROMarr:
    """The service. Holds config, clients, and the in-flight queue."""

    def __init__(self, env: dict[str, str] | None = None):
        e = env if env is not None else os.environ
        # Kept so reload_libraries can seed from the environment on first run
        # without the caller having to hand it back in.
        self._env = e
        self.prowlarr = Prowlarr(ProwlarrConfig(
            base_url=e.get("PROWLARR_URL", ""),
            api_key=e.get("PROWLARR_API_KEY", ""),
        ))
        self.qbit = QBittorrent(QbitConfig(
            base_url=e.get("QBITTORRENT_URL", ""),
            username=e.get("QBITTORRENT_USER", ""),
            password=e.get("QBITTORRENT_PASS", ""),
            category=category_for(e, "QBITTORRENT"),
        ))
        # Usenet is not an afterthought: Prowlarr indexes both protocols, and
        # accepting only torrents made every usenet indexer dead weight --
        # results scored fine and were then refused for having no magnet.
        self.sab = SABnzbd(SabConfig(
            base_url=e.get("SABNZBD_URL", ""),
            api_key=e.get("SABNZBD_API_KEY", ""),
            category=category_for(e, "SABNZBD"),
        ))
        self.nzbget = NZBGet(NzbgetConfig(
            base_url=e.get("NZBGET_URL", ""),
            username=e.get("NZBGET_USER", ""),
            password=e.get("NZBGET_PASS", ""),
            category=category_for(e, "NZBGET"),
        ))
        # Which game library this ROMarr feeds. RomM remains the default so an
        # install that predates the other backends keeps working untouched, and
        # the ROMM_* variables are still honoured for the same reason.
        # NOT self.library: that name is already the ROM directory Path, and
        # assigning both silently leaves whichever ran last -- the same
        # collision that forced library_view() to be renamed.
        self.game_library = build_library(e.get("LIBRARY_KIND", "romm"), e)
        # Kept under the old name so the rest of the service, and anything that
        # already reads it, does not have to care which backend is attached.
        self.romm = self.game_library
        # Every configured library, as (stored config, live backend) pairs.
        # Built from the store by reload_libraries(); the environment seeds the
        # first entry. game_library and romm above stay pointed at the default
        # one, so every existing call site keeps working.
        self.game_libraries: list[tuple[dict, object]] = []
        # Built from stored configuration, seeded from the environment once.
        self.clients: list = []
        # Torznab/Newznab indexers queried directly, alongside Prowlarr.
        self.indexers: list = []
        # Where GG Requestz lives, so the status page can show the connection
        # the same way Seerr shows Radarr.
        self.ggrequestz_url = e.get("GGREQUESTZ_URL", "")

        # Where ROMs are filed. LIBRARY_PATH is the name that matches a
        # pluggable library; ROMM_LIBRARY still works, the same way LIBRARY_URL
        # falls back to ROMM_URL, so no existing install has to be edited.
        self.library = Path(e.get("LIBRARY_PATH") or e.get("ROMM_LIBRARY", "/mnt/roms"))
        self.queue: list[QueueItem] = []
        # Releases offered by a search, keyed by search then by release id, so
        # the Search page can grab one without ever being handed a download URL
        # carrying Prowlarr's API key.
        self._candidates: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Lets a configuration change wake the background refresh instead of
        # waiting out its interval. Created before reload_libraries, which sets
        # it.
        self._refresh_now = threading.Event()
        self._started = time.monotonic()

        # History, Wanted and settings survive a restart. Without this a restart
        # lost everything you had asked for, which is the difference between a
        # tool and a demo.
        self.store = Store(e.get("ROMARR_DATA", "/opt/romarr/romarr.json"))
        # A library path saved through the UI has to win over the environment,
        # or the setting is one you can change but not apply. A *default* must
        # not, which is why the stored default is empty: otherwise it outranks
        # the environment on a fresh install and LIBRARY_PATH does nothing.
        saved = self.store.settings.get("library_path")
        if saved:
            self.library = Path(saved)
        else:
            # Record what the environment decided, so the Settings page shows
            # the path ROMs are actually filed into rather than a blank field.
            self.store.update_settings({"library_path": str(self.library)})

        # Shown on the General page so an operator can see what is wired up
        # without opening a shell. URLs only -- never a credential.
        self.store.settings["_prowlarr_url"] = e.get("PROWLARR_URL", "")
        self.store.settings["_qbit_url"] = e.get("QBITTORRENT_URL", "")
        self.store.settings["_romm_url"] = e.get("ROMM_URL", "")

        # The headless RetroArch stream server, if the operator runs one. It is
        # what plays the machines EmulatorJS has no core for -- PS2, GameCube,
        # Wii, Dreamcast, 3DS -- so whether one is configured changes the
        # honest answer to "will this play", and nothing else here.
        #
        # Optional by design: ROMarr must work identically without it, minus
        # the routes only it can offer.
        stream_url = e.get("STREAM_SERVER_URL", "")
        self.store.settings["_stream_url"] = stream_url
        self.stream = StreamServer(stream_url) if stream_url else None

        # Authentication, on unless deliberately turned off.
        #
        # The key is generated and stored on first run rather than left blank,
        # because an install that is open until somebody reads the
        # documentation is an open install. It is kept under a leading
        # underscore so `safe_settings` never has to learn about it -- that
        # method masks known credential shapes, and a key it had not been told
        # about would have gone straight into the browser.
        api_key = e.get("ROMARR_API_KEY", "") or self.store.settings.get("_api_key", "")
        if not api_key:
            api_key = new_api_key()
            log.info("generated an API key; find it under Settings -> General")
        self.store.settings["_api_key"] = api_key
        mode = e.get("ROMARR_AUTH", "").strip().lower()
        self.auth = Auth(
            api_key=api_key,
            password_hash=self.store.settings.get("_password_hash", ""),
            enabled=mode != AUTH_DISABLED,
        )
        self.auth.totp = Totp(
            secret=self.store.settings.get("_totp_secret", ""),
            backup=self.store.settings.get("_totp_backup", []) or [],
        )

        # Single sign-on, when a proxy in front is the authority.
        #
        # This is what `ROMARR_AUTH=disabled` should have been. That setting
        # is honest about what it does -- ROMarr stops checking anything, so
        # any request reaching the port is in, including one that bypassed the
        # proxy entirely. Forward mode keeps the proxy as the authority but
        # verifies the request came *through* it, learns who the user is, and
        # can require a group.
        self.limiter = RateLimiter()
        self.reload_dats(e.get("DAT_PATH", "")
                         or self.store.settings.get("dat_path", ""))
        self.reload_metadata()
        self.reload_policy()
        self.sso = None
        if mode == "forward":
            self.sso = ForwardAuth(
                provider=e.get("ROMARR_SSO_PROVIDER", "authentik"),
                trusted_proxies=[p.strip() for p
                                 in e.get("ROMARR_TRUSTED_PROXIES", "").split(",")
                                 if p.strip()],
                user_header=e.get("ROMARR_SSO_USER_HEADER", ""),
                groups_header=e.get("ROMARR_SSO_GROUPS_HEADER", ""),
                required_group=e.get("ROMARR_SSO_GROUP", ""),
            )
            if not self.sso.trusted_proxies:
                log.error(
                    "ROMARR_AUTH=forward with no ROMARR_TRUSTED_PROXIES: every "
                    "request will be refused. Set it to the proxy's address, "
                    "e.g. 192.168.0.0/24")

        self._seed_from_env(e)
        self.reload_clients()
        self.reload_libraries()

        # The library count is refreshed off the request path entirely.
        # `None` means "not known yet", which the UI shows as a dash -- an
        # honest answer, where 0 would be a claim that the library is empty.
        self._count_cache: tuple[int | None, float] = (None, 0.0)
        # The library itself is cached the same way, and for a stronger reason:
        # RomM's /api/roms is contended on a large library -- other clients
        # polling it hold the database for minutes at a time -- so a request
        # that waits its turn is a page that never paints. Fetched here,
        # retried until it lands, then served from memory.
        self._library_cache: tuple[list | None, float, str] = (None, 0.0, "")
        self._count_thread = threading.Thread(target=self._refresh_counts, daemon=True)
        self._count_thread.start()

    def _refresh_counts(self) -> None:
        """Keep the count and the library fresh without ever blocking a request.

        Backs off on failure rather than hammering a database that is already
        struggling -- retrying hard against a contended table is how one slow
        client becomes the reason nothing else can read either.
        """
        delay = 15
        while True:
            if not self.game_libraries:
                # Nothing configured. The shelf stays unknown rather than
                # becoming an empty list, because "no library is set up" and
                # "your library has no games in it" are different answers and
                # only one of them is actionable.
                cached, at, _ = self._library_cache
                self._library_cache = (cached, at, "no library configured")
                self._wait(self.COUNT_TTL)
                continue

            ok, total, shelf, failures = True, 0, [], []
            # Every library is read, and one failing does not lose the others.
            # A single unreachable server used to be indistinguishable from an
            # empty shelf; now it is one named row that did not answer.
            multiple = len(self.game_libraries) > 1
            for cfg, backend in self.game_libraries:
                label = cfg.get("name") or getattr(backend, "name", "library")
                try:
                    total += backend.count()
                except Exception as err:
                    ok = False
                    failures.append(f"{label}: {err.__class__.__name__}")
                    log.warning("library count refresh failed for %s: %s",
                                label, err.__class__.__name__)
                try:
                    games = backend.games(limit=self.LIBRARY_PAGE,
                                          timeout=backend.BACKGROUND_TIMEOUT)
                    # Tag the source only when there is more than one library,
                    # so a single-library install shows no redundant badge.
                    shelf.extend(replace(g, source=label) if multiple else g
                                 for g in games)
                except Exception as err:
                    ok = False
                    failures.append(f"{label}: {err.__class__.__name__}")
                    log.warning("library refresh failed for %s: %s",
                                label, err.__class__.__name__)

            if shelf or ok:
                self._count_cache = (total, time.monotonic())
                self._library_cache = (shelf, time.monotonic(), "; ".join(failures))
                log.info("library refreshed: %d games across %d librar%s",
                         len(shelf), len(self.game_libraries),
                         "y" if len(self.game_libraries) == 1 else "ies")
            else:
                # Keep whatever was last fetched; a transient failure should
                # not empty a shelf that was working a minute ago.
                cached, at, _ = self._library_cache
                self._library_cache = (cached, at, "; ".join(failures))

            delay = self.COUNT_TTL if ok else min(delay * 2, 300)
            self._wait(delay)

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake early when the libraries change.

        Without this, adding a library on the Libraries page left the shelf
        reading "no library configured" for up to COUNT_TTL -- five minutes of a
        message that was true when it was written and wrong by the time anybody
        read it.
        """
        self._refresh_now.wait(seconds)
        self._refresh_now.clear()

    def library_view(self) -> dict:
        """The cached library, with enough state for the page to explain itself.

        Not named `library`: that attribute is the ROM library Path and has
        been since the first commit, so a method of the same name is shadowed
        by it and the route ends up calling a PosixPath.
        """
        names = [cfg.get("name") or getattr(b, "name", "library")
                 for cfg, b in self.game_libraries]
        games, at, err = self._library_cache
        if games is None:
            where = ", ".join(names) or getattr(self.game_library, "name", "library")
            return {"items": [], "loading": True,
                    "error": err or "",
                    "libraries": names,
                    "message": f"Reading the library from {where}…"}
        return {"items": [asdict(g) for g in games], "loading": False,
                "libraries": names,
                "library": self.game_library.name,
                "age_seconds": int(time.monotonic() - at) if at else None,
                "error": err}

    # -- configuration -----------------------------------------------------

    def _seed_from_env(self, e) -> None:
        """Turn environment variables into stored clients, once.

        Clients were environment-only, so the Settings page could show them and
        not change them. Seeding rather than continuing to read the environment
        means an existing install comes up already configured and is then
        editable like any other -- without the environment silently overriding
        an edit on the next restart.
        """
        if self.store.settings.get("_seeded_clients"):
            return

        def add(kind: str, url: str, **extra):
            if not url:
                return
            from urllib.parse import urlsplit
            parts = urlsplit(url if "//" in url else "http://" + url)
            self.store.put_item("download_clients", {
                "type": kind, "name": CLIENT_TYPES[kind]["label"], "enable": True,
                "host": parts.hostname or "localhost",
                "port": parts.port or CLIENT_TYPES[kind]["default_port"],
                "use_ssl": parts.scheme == "https",
                "url_base": parts.path.strip("/"),
                "category": category_for(e, kind.upper()), **extra,
            })

        add("qbittorrent", e.get("QBITTORRENT_URL", ""),
            username=e.get("QBITTORRENT_USER", ""), password=e.get("QBITTORRENT_PASS", ""))
        add("sabnzbd", e.get("SABNZBD_URL", ""), api_key=e.get("SABNZBD_API_KEY", ""))
        add("nzbget", e.get("NZBGET_URL", ""),
            username=e.get("NZBGET_USER", ""), password=e.get("NZBGET_PASS", ""))

        if e.get("PROWLARR_URL"):
            self.store.put_item("indexers", {
                "type": "prowlarr", "name": "Prowlarr", "enable": True,
                "url": e.get("PROWLARR_URL", ""), "api_key": e.get("PROWLARR_API_KEY", ""),
            })

        self.store.settings["_seeded_clients"] = True
        self.store.save()

    def _seed_libraries_from_env(self, e) -> None:
        """Turn the single environment library into the first stored entry.

        Gated separately from _seeded_clients, and this matters: an install that
        predates multiple libraries already has _seeded_clients set, so reusing
        that flag would leave it with an empty Libraries page and nothing to
        import into -- an upgrade that silently unconfigures the thing.
        """
        if self.store.settings.get("_seeded_libraries"):
            return
        self.store.settings["_seeded_libraries"] = True

        url = e.get("LIBRARY_URL") or e.get("ROMM_URL", "")
        kind = (e.get("LIBRARY_KIND") or "romm").strip().lower()
        # A folder library has no server, so requiring a URL would silently
        # leave the one kind that needs no configuration with nothing
        # configured. Its path is the whole of it.
        if not url and not (kind == "folder" and str(self.library)):
            # Nothing to seed. Still marked as seeded, so a library added by
            # hand later is not joined by a surprise second entry on the next
            # restart if the environment gains a URL.
            self.store.save()
            return

        self.store.put_item("libraries", {
            "type": kind,
            "name": LIBRARY_TYPES.get(kind, {}).get("label", kind.title()),
            "enable": True,
            "url": url,
            "username": e.get("LIBRARY_USERNAME") or e.get("ROMM_USERNAME", ""),
            "password": e.get("LIBRARY_PASSWORD") or e.get("ROMM_PASSWORD", ""),
            "api_key": e.get("LIBRARY_API_KEY") or e.get("ROMM_API_TOKEN", ""),
            "path": str(self.library),
            "is_default": True,
            "platforms": [],
        })
        self.store.save()

    def reload_libraries(self) -> None:
        """Rebuild every live library from stored configuration.

        Mirrors reload_clients, for the same reason: a library added on the
        Libraries page has to work without a restart.
        """
        # Before anything is built, so a drop-in driver's kind is known by the
        # time stored rows are read. Reloaded on every pass rather than once at
        # import, so dropping in a driver and hitting Reload is enough -- the
        # same expectation the Libraries page already sets for everything else.
        load_backend_plugins()

        self._seed_libraries_from_env(self._env)

        built: list[tuple[dict, object]] = []
        for cfg in self.store.list_items("libraries"):
            if not cfg.get("enable", True):
                continue
            backend = build_library_from_config(cfg)
            if backend is not None:
                built.append((cfg, backend))
        self.game_libraries = built

        # Keep the single-library attributes pointed at the default, so the
        # status page, the health check and anything else reading them keep
        # reporting the library most installs have exactly one of.
        # The cached error describes a configuration that no longer exists, so
        # it must not outlive it -- and the refresh is woken rather than left to
        # finish its interval.
        cached = getattr(self, "_library_cache", None)
        if cached is not None:
            self._library_cache = (cached[0], cached[1], "")
        if getattr(self, "_refresh_now", None) is not None:
            self._refresh_now.set()

        default = self.default_library()
        if default is not None:
            cfg, backend = default
            self.game_library = backend
            self.romm = backend
            if cfg.get("path"):
                self.library = Path(cfg["path"])

    def default_library(self) -> tuple[dict, object] | None:
        """The library a request goes to when no platform rule matches."""
        cfg = route_library([c for c, _ in self.game_libraries], "")
        if cfg is None:
            return None
        for candidate, backend in self.game_libraries:
            if candidate.get("id") == cfg.get("id"):
                return candidate, backend
        return None

    def library_for(self, platform_slug: str) -> tuple[dict, object] | None:
        """Which library a request for this platform is filed into."""
        cfg = route_library([c for c, _ in self.game_libraries], platform_slug)
        if cfg is None:
            return None
        for candidate, backend in self.game_libraries:
            if candidate.get("id") == cfg.get("id"):
                return candidate, backend
        return None

    def library_root(self, cfg: dict) -> Path:
        """Where ROMs are filed for one library, falling back to the default."""
        return Path(cfg.get("path") or self.library)

    def path_hint(self, root: Path) -> str:
        """Why a library path is missing, and which fix applies.

        "ROM library: Not available /mnt/roms" is a true statement that helps
        nobody -- it was the first thing a new user reported, having mounted the
        volume somewhere else entirely. Two different mistakes produce it and
        they need opposite fixes, so this distinguishes them.

        The nastier one is second: a path stored on first run outranks the
        environment forever after, deliberately, so that a saved decision is not
        undone by a restart. The cost is that correcting LIBRARY_PATH in compose
        appears to do nothing at all. If the environment names a path that does
        exist while the stored one does not, that is almost certainly what has
        happened, and the fix is the Settings page rather than the compose file.
        """
        if root.exists():
            return ""
        env_path = self._env.get("LIBRARY_PATH") or self._env.get("ROMM_LIBRARY", "")
        if env_path and str(root) != env_path and Path(env_path).exists():
            return (f"{root} does not exist in this container, but {env_path} "
                    f"does. {root} was stored on first run, and a stored path "
                    "outranks the environment -- change it on the Settings page, "
                    "because editing the environment will not move it.")
        return (f"{root} does not exist in this container. Mount your library "
                "volume there, or change the path on the Settings page.")

    def libraries_status(self) -> list[dict]:
        """Every library, whether it answers, and where it files ROMs.

        Reported for all of them rather than only the reachable ones: "Retrom:
        not answering" is the answer to "why did my PSX request go to RomM", and
        hiding the row hides the answer.
        """
        out = []
        for cfg, backend in self.game_libraries:
            root = self.library_root(cfg)
            out.append({
                "id": cfg.get("id"),
                "name": cfg.get("name") or getattr(backend, "name", ""),
                "type": cfg.get("type", ""),
                "url": cfg.get("url", ""),
                "path": str(root),
                "path_exists": root.exists(),
                "path_hint": self.path_hint(root),
                "is_default": bool(cfg.get("is_default")),
                "platforms": cfg.get("platforms") or [],
                "ok": bool(backend.reachable()),
            })
        return out

    def reload_clients(self) -> None:
        """Rebuild the live clients from stored configuration.

        Called after any edit so a saved change takes effect immediately --
        an *arr that needs restarting to use a client you just added is not
        one anybody would call configured.
        """
        clients = []
        for cfg in self.store.list_items("download_clients"):
            if not cfg.get("enable", True):
                continue
            client = build_client(cfg)
            if client is not None:
                clients.append(client)
        self.clients = clients

        # The first enabled Prowlarr entry drives search.
        for cfg in self.store.list_items("indexers"):
            if cfg.get("type") == "prowlarr" and cfg.get("enable", True):
                self.prowlarr = Prowlarr(ProwlarrConfig(
                    base_url=cfg.get("url", ""), api_key=cfg.get("api_key", "")))
                self.store.settings["_prowlarr_url"] = cfg.get("url", "")
                break

        # Directly-configured Torznab and Newznab indexers. These were
        # storable, editable and testable but never searched, so an operator
        # could add one, watch Test pass, and never see a result from it.
        direct = []
        for cfg in self.store.list_items("indexers"):
            if not cfg.get("enable", True):
                continue
            indexer = build_indexer(cfg)
            if indexer is not None:
                direct.append(indexer)
        self.indexers = direct

    def safe_settings(self) -> dict:
        """Settings with every stored credential masked.

        The raw settings hold download client passwords and indexer API keys in
        plaintext, and this endpoint feeds a browser. Returning them verbatim
        put real credentials in a page anyone who reached the UI could read --
        including the qBittorrent password. Masking here rather than at each
        call site means a new secret field is covered the moment it is declared.
        """
        out = dict(self.store.settings)
        out["download_clients"] = [redact(c) for c in out.get("download_clients", [])]
        out["indexers"] = [redact_indexer(i) for i in out.get("indexers", [])]
        out["libraries"] = [redact_library(lib) for lib in out.get("libraries", [])]
        # The two settings that ARE the credential. Removed by name rather
        # than by a leading-underscore rule, because the other underscore keys
        # (`_prowlarr_url`, `_romm_url`, `_stream_url`) are deliberately shown
        # -- a rule would have hidden the wrong half of them.
        for secret in ("_api_key", "_password_hash"):
            out.pop(secret, None)
        return out

    # --- inbound webhooks -------------------------------------------------

    @staticmethod
    def parse_request_webhook(body: dict) -> tuple[str, str] | None:
        """Read a game and a platform out of a request front-end's webhook.

        GG Requestz posts a notification-shaped event: a human title and
        message, with the useful parts nested under `data`. Everything needed
        is in data.game_title and data.platforms; the rest is for a human to
        read in a chat client.

        Returns None rather than guessing when the payload is not a game
        request -- a webhook endpoint receives whatever anybody points at it.
        """
        if not isinstance(body, dict):
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            data = {}

        kind = str(body.get("type") or data.get("request_type") or "").lower()
        if kind and "game" not in kind:
            return None

        game = str(data.get("game_title") or body.get("game") or "").strip()
        if not game:
            # Fall back to the human title, which reads
            # "New Game Request: <title>".
            title = str(body.get("title") or "")
            if ":" in title:
                game = title.split(":", 1)[1].strip()
        if not game:
            return None

        platforms = data.get("platforms") or data.get("platform") or body.get("platform")
        if isinstance(platforms, str):
            platforms = [platforms]
        platform = str((platforms or [""])[0]).strip()
        return (game, platform)

    def handle_request_webhook(self, body: dict) -> dict:
        """Accept a request event and grab it, without making the caller wait.

        A search fans out across every indexer and can take most of a minute.
        A webhook sender is entitled to a prompt answer and will retry or log a
        failure if it does not get one -- so this acknowledges immediately and
        does the work on a thread. What actually happened lands in History,
        which is where somebody would look for it anyway.
        """
        parsed = self.parse_request_webhook(body)
        if parsed is None:
            return {"ok": False, "error": "not a game request"}
        game, platform_name = parsed

        platform = resolve(platform_name) if platform_name else None
        if platform is None:
            # Recorded rather than dropped: an unknown platform is a mapping
            # problem somebody can fix, and silence would hide it.
            self.store.record(Event(kind="failed", game=game,
                                    platform=platform_name or "unknown",
                                    detail=f"unknown platform: {platform_name!r}"))
            return {"ok": False, "error": f"unknown platform: {platform_name!r}",
                    "game": game}

        threading.Thread(target=self.request, args=(game, platform.slug),
                         daemon=True).start()
        return {"ok": True, "accepted": True, "game": game, "platform": platform.slug}

    def test_indexer(self, cfg: dict) -> dict:
        """Try an indexer configuration without saving it.

        Tested through the same client the search path uses, so a green Test
        means the search will work. The previous version issued its own ad-hoc
        caps request, which could pass against a URL the searcher would then
        have handled differently.
        """
        kind = str(cfg.get("type") or "").lower()
        url, key = cfg.get("url", ""), cfg.get("api_key", "")
        if not url:
            return {"ok": False, "message": "a URL is required"}
        try:
            if kind == "prowlarr":
                probe = Prowlarr(ProwlarrConfig(base_url=url, api_key=key))
                indexers = probe.indexers()
                private = sum(1 for i in indexers if i.get("privacy") == "private")
                return {"ok": True,
                        "message": f"Connected, {len(indexers)} indexer(s), "
                                   f"{private} private"}
            probe = build_indexer(cfg)
            if probe is None:
                return {"ok": False, "message": f"unknown indexer type: {kind!r}"}
            # Both protocols answer a caps query, which needs no search term and
            # so tests the credential without asking the site to run a query.
            probe.caps()
            return {"ok": True, "message": "Connected"}
        except Exception as err:
            import requests as _rq
            if isinstance(err, _rq.HTTPError) and err.response is not None \
                    and err.response.status_code in (401, 403):
                return {"ok": False, "message": "Rejected the API key"}
            return {"ok": False, "message": f"{type(err).__name__}: {err}"}

    def test_client(self, cfg: dict) -> dict:
        """Try a configuration without saving it, the way *arr's Test does."""
        client = build_client(cfg)
        if client is None:
            return {"ok": False, "message": f"unknown client type: {cfg.get('type')!r}"}
        try:
            ok = client.reachable()
        except Exception as err:
            return {"ok": False, "message": f"{type(err).__name__}: {err}"}
        return {"ok": bool(ok),
                "message": "Connected" if ok else "Could not connect or authenticate"}

    def test_library(self, cfg: dict) -> dict:
        """Try a library configuration without saving it.

        Reports the path separately from the connection, because they fail for
        unrelated reasons and the fix differs: an unreachable URL is a wrong
        address or a stopped server, while a missing path on a reachable library
        is almost always a volume that was never mounted into this container.
        """
        backend = build_library_from_config(cfg)
        if backend is None:
            return {"ok": False, "message": f"unknown library type: {cfg.get('type')!r}"}

        root = Path(cfg.get("path") or "")
        path_ok = bool(cfg.get("path")) and root.exists()
        try:
            ok = backend.reachable()
        except Exception as err:
            return {"ok": False, "path_ok": path_ok,
                    "message": f"{type(err).__name__}: {err}"}

        if not ok:
            return {"ok": False, "path_ok": path_ok,
                    "message": "Could not connect or authenticate"}
        if not cfg.get("path"):
            return {"ok": False, "path_ok": False,
                    "message": "Connected, but no library path is set -- "
                               "nothing can be imported"}
        if not path_ok:
            return {"ok": False, "path_ok": False,
                    "message": f"Connected, but {root} does not exist here. "
                               "Mount it into ROMarr, or correct the path."}
        return {"ok": True, "path_ok": True, "message": "Connected"}

    # -- operations --------------------------------------------------------

    def health(self) -> dict:
        libraries = self.libraries_status()
        return {
            "ok": True,
            "prowlarr": bool(self.prowlarr._config.api_key),
            # The default library, kept under the old key so anything already
            # watching /api/health -- a monitor, a healthcheck -- keeps working.
            "romm": self.romm.reachable(),
            "library": self.library.exists(),
            "library_path": str(self.library),
            "library_path_hint": self.path_hint(self.library),
            "libraries": libraries,
            "libraries_ok": sum(1 for lib in libraries if lib["ok"]),
            "libraries_total": len(libraries),
            "platforms": len(PLATFORMS),
            "queued": len(self.queue),
        }

    def search(self, game: str, platform_name: str = "") -> dict:
        """What a request for this game would find, without grabbing it.

        Runs the same search the request path runs. It previously issued only
        the narrow `"<game> <platform>"` query against Prowlarr, so the one
        endpoint an operator would reach for to ask "why did this fail" gave a
        different -- and much worse -- answer than the real request: four
        results and no pick, where the merged search finds twenty.

        `rejected` is reported because "found 21, best null" is otherwise
        unanswerable without reading the source. The top few scores say whether
        nothing matched the title, everything was the wrong platform, or a
        release was simply just below the bar.
        """
        platform = resolve(platform_name) if platform_name else None
        # Both branches have to survive an indexer failing. _search_releases
        # already isolates each source, but the no-platform branch queried
        # Prowlarr unprotected, so one aggregate search exceeding its 60s
        # timeout took the whole request down -- and an unrecognised platform
        # name silently routes here, which is how `?platform=psx` returned no
        # status code at all rather than a result or an error.
        if platform is not None:
            releases = self._search_releases(game, platform)
        else:
            try:
                releases = self.prowlarr.search(game)
            except Exception as err:
                log.warning("prowlarr search failed for %r: %s", game, err)
                releases = []
        pick = best_release(releases, game, platform)
        scored = sorted(
            ((score(r, game, platform), r) for r in releases),
            key=lambda pair: -pair[0])
        # Which indexers actually contributed, and how many of their results
        # survived scoring. This is the question you have when a tracker you
        # just configured does not seem to be doing anything: "0 of 30 kept"
        # is a scoring problem, absent from the list entirely is a search or
        # credential problem, and the two need different fixes.
        by_indexer: dict[str, dict] = {}
        for s, r in scored:
            row = by_indexer.setdefault(
                r.indexer or "(unknown)", {"found": 0, "kept": 0, "private": r.private})
            row["found"] += 1
            if s > 0:
                row["kept"] += 1

        return {
            "game": game,
            "platform": platform.slug if platform else None,
            # A name that resolves to nothing is searched without any platform
            # evidence, which quietly changes what the scores mean. Saying so
            # separates "I asked for no platform" from "I asked for psx and it
            # was not recognised" -- the second is a supported-platform question,
            # since only cartridge systems are modelled.
            "unknown_platform": platform_name if platform_name and not platform else None,
            "found": len(releases),
            "rejected": sum(1 for s, _ in scored if s <= 0),
            "private_found": sum(1 for _, r in scored if r.private),
            "best": asdict(pick) if pick else None,
            "indexers": by_indexer,
            "top": [{"title": r.title, "score": s, "seeders": r.seeders,
                     "indexer": r.indexer, "private": r.private}
                    for s, r in scored[:5]],
        }

    def _search_releases(self, game: str, platform) -> list:
        """Find releases for a game, favouring recall over a tidy query.

        Searching "<game> <platform.name>" alone was far too narrow: indexers
        match the whole string, and almost no release is named "Super Metroid
        Super Nintendo". A real search returned 21 releases; the qualified one
        returned 4, none usable -- so a game that was plainly available was
        reported as missing.

        Both queries are run and the results merged. The qualified one still
        earns its place on ambiguous titles, where it surfaces the handful of
        releases that do name the system. Precision is not weakened by the
        wider net: score() already rejects a release naming a foreign system,
        one in the wrong category, or one too large to be a cartridge.
        """
        seen: set[tuple[str, str]] = set()
        merged = []
        # Prowlarr plus every directly-configured indexer. One source failing
        # must never lose another's results -- an indexer that is down, rate
        # limited or mid-login is the normal case across a dozen trackers, not
        # an outage worth abandoning the search for.
        sources: list[tuple[str, object]] = [("prowlarr", self.prowlarr)]
        sources += [(getattr(i, "name", "indexer"), i)
                    for i in getattr(self, "indexers", [])]
        for term in (game, f"{game} {platform.name}"):
            for label, source in sources:
                try:
                    found = source.search(term)
                except Exception as err:
                    log.warning("%s search failed for %r: %s", label, term, err)
                    continue
                for release in found:
                    key = (release.title, release.download_url)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(release)
        return merged

    def request(self, game: str, platform_name: str) -> dict:
        platform = resolve(platform_name)
        if platform is None:
            return {"ok": False, "error": f"unknown platform: {platform_name!r}"}

        releases = self._search_releases(game, platform)
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

        return self.grab(pick, game, platform.slug)

    def grab(self, pick, game: str, platform_slug: str, *, manual: bool = False) -> dict:
        """Hand one release to a download client and record what happened.

        Shared by the automatic path and the Search page, deliberately: a
        release chosen by hand must be queued, recorded and fulfilled exactly
        like one the scorer picked, or Activity and Wanted start disagreeing
        with each other depending on how a game was requested.
        """
        client = pick_client(pick.protocol, self.clients)
        if client is None:
            item = QueueItem(game, platform_slug, pick.title, pick.seeders, "failed",
                             f"no download client configured for {pick.protocol}")
            with self._lock:
                self.queue.append(item)
            self.store.want(game, platform_slug)
            self.store.note_failure(game, platform_slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform_slug,
                                    release=pick.title, detail=item.detail))
            return {"ok": False, "error": item.detail}

        ok = client.add(pick.download_url)
        item = QueueItem(game, platform_slug, pick.title, pick.seeders,
                         "grabbed" if ok else "failed",
                         "" if ok else f"{client.name} rejected the release")
        with self._lock:
            self.queue.append(item)
        if ok:
            self.store.record(Event(kind="grabbed", game=game, platform=platform_slug,
                                    release=pick.title, seeders=pick.seeders,
                                    size=getattr(pick, "size", 0),
                                    indexer=getattr(pick, "indexer", ""),
                                    detail="chosen by hand" if manual else ""))
        else:
            self.store.want(game, platform_slug)
            self.store.note_failure(game, platform_slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform_slug,
                                    release=pick.title, detail=item.detail))
        return {"ok": ok, "release": pick.title, "seeders": pick.seeders}

    # -- interactive search -------------------------------------------------
    #
    # The scorer is opinionated, and every opinion in it is one somebody may
    # disagree with. Radarr and Sonarr both answer this the same way: show the
    # candidates, show why each was ranked where it was, and let a human take
    # one. Without it, a wrong pick is a bug report; with it, it is a click.

    # How many searches to keep grabbable at once. Small on purpose: this is a
    # handle for a button the user is looking at, not a cache.
    CANDIDATE_SEARCHES = 8

    def candidates(self, game: str, platform_name: str = "") -> dict:
        """Every release found, scored, with the reasoning shown.

        Download links are deliberately absent from the reply. Prowlarr's
        downloadUrl carries its API key in the query string, so it must never
        reach a browser -- the release is grabbed later by the id issued here,
        and the URL is looked up server-side.
        """
        platform = resolve(platform_name) if platform_name else None
        if platform_name and platform is None:
            return {"error": f"unknown platform: {platform_name!r}",
                    "game": game, "platform": None, "unknown_platform": platform_name,
                    "items": []}

        try:
            releases = (self._search_releases(game, platform) if platform
                        else self.prowlarr.search(game))
        except Exception as err:
            log.warning("interactive search failed for %r: %s", game, err)
            return {"error": f"search failed: {type(err).__name__}",
                    "game": game, "platform": platform.slug if platform else None,
                    "items": []}

        judged = [(judge(r, game, platform), r) for r in releases]
        judged.sort(key=lambda pair: (-pair[0].points, pair[1].size))

        key = f"{game}|{platform.slug if platform else ''}"
        held = {}
        items = []
        for n, (verdict, r) in enumerate(judged):
            rid = f"{abs(hash((key, r.title, r.size, r.indexer))):x}{n:02d}"
            held[rid] = (r, game, platform.slug if platform else "")
            items.append({
                "id": rid,
                "title": r.title,
                "size": r.size,
                "seeders": r.seeders,
                "indexer": r.indexer,
                "protocol": r.protocol,
                "private": r.private,
                "score": verdict.points,
                "accepted": verdict.accepted,
                "reasons": verdict.why(),
                "grabbable": bool(r.download_url),
            })

        with self._lock:
            self._candidates[key] = held
            # Bounded, and oldest-first: Python dicts keep insertion order, so
            # this drops the search the user is least likely to still be
            # looking at.
            while len(self._candidates) > self.CANDIDATE_SEARCHES:
                self._candidates.pop(next(iter(self._candidates)))

        return {
            "game": game,
            "platform": platform.slug if platform else None,
            "found": len(items),
            "accepted": sum(1 for i in items if i["accepted"]),
            "items": items,
        }

    def grab_candidate(self, release_id: str) -> dict:
        """Grab one release the user picked out of a search."""
        found = None
        with self._lock:
            for held in self._candidates.values():
                if release_id in held:
                    found = held[release_id]
                    break
        if found is None:
            # Searches expire, and a stale button is not an error worth a 500.
            return {"ok": False, "error": "that search has expired -- run it again"}

        release, game, platform_slug = found
        if not release.download_url:
            return {"ok": False, "error": "release offers no usable download link"}
        return self.grab(release, game, platform_slug, manual=True)

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
                "id": getattr(c, "config_id", None),
                "name": getattr(c, "display_name", getattr(c, "name", type(c).__name__)),
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
            "library_path_hint": self.path_hint(self.library),
            "libraries": self.libraries_status(),
            "platforms": len(PLATFORMS),
            "play_routes": self.play_route_counts(),
            "stream_url": self.store.settings.get("_stream_url", ""),
            "events": len(self.store.events),
            "uptime": f"{up // 3600}h {(up % 3600) // 60}m",
        }

    def reload_dats(self, directory: str = "") -> dict:
        """Load every DAT under `directory`.

        Optional and quiet. An operator with no DATs is the common case on
        day one, and it must not turn every import into an error -- the index
        simply answers `unknown`, which is a real verdict rather than a
        failure.
        """
        self.dats = DatIndex()
        self.store.settings["dat_path"] = str(directory or "")
        if not directory:
            return {"loaded": 0, "path": ""}
        root = Path(directory)
        if not root.is_dir():
            log.warning("DAT_PATH %s is not a directory", root)
            return {"loaded": 0, "path": str(root),
                    "error": f"{root} is not a directory"}
        for path in sorted(root.rglob("*.dat")) + sorted(root.rglob("*.xml")):
            try:
                self.dats.add(parse_dat(path.read_text(encoding="utf-8",
                                                       errors="replace")))
            except OSError as exc:
                log.warning("could not read %s: %s", path, exc)
        log.info("loaded %d DAT(s) from %s", len(self.dats.dats), root)
        return {"loaded": len(self.dats.dats), "path": str(root)}

    def reload_policy(self) -> None:
        """Rebuild the operator's selection policy and notification fan-out.

        Held on the service rather than rebuilt per search: a blocklist is
        read on every candidate, and re-parsing it thousands of times during
        one interactive search is work nobody asked for.
        """
        self.profile = ReleaseProfile.from_settings(self.store.settings)
        self.blocklist = Blocklist.from_items(
            self.store.list_items("blocklist"))
        self.notifier = Notifier(self.store.list_items("connections"))

    def block(self, release, reason: str = "") -> dict:
        """Never take this release again, and record why."""
        entry = self.blocklist.add(release, reason=reason)
        self.store.put_item("blocklist", entry)
        return entry

    def unblock(self, entry_id: str) -> bool:
        self.blocklist.remove(entry_id)
        return self.store.delete_item("blocklist", entry_id)

    def notify(self, message) -> list[dict]:
        """Fan out, and never let a failure here matter.

        The delivery is a courtesy; the import is the work.
        """
        try:
            return self.notifier.send(message)
        except Exception as exc:
            log.warning("notification fan-out failed: %s", exc)
            return []

    def tag(self, item_id: str, add=None, remove=None) -> list[str]:
        """Set the tags on one library item."""
        tags = self.store.settings.setdefault("_tags", {})
        merged = merge_tags(tags.get(str(item_id)) or [], add, remove)
        if merged:
            tags[str(item_id)] = merged
        else:
            tags.pop(str(item_id), None)
        self.store.save()
        return merged

    def tags_for(self, item_id: str) -> list[str]:
        return list((self.store.settings.get("_tags") or {}).get(str(item_id))
                    or [])

    def scan(self, directory: str) -> dict:
        """Manual import: what is already on disk that ROMarr could adopt."""
        result = scan_directory(directory, PLATFORMS, self.dats)
        return {
            "candidates": [vars(c) for c in result.candidates],
            "skipped": result.skipped,
            "error": result.error,
        }

    def reload_metadata(self) -> None:
        """Rebuild the provider chain from stored settings."""
        self.metadata = Metadata(self.store.list_items("metadata_providers"))

    def identify(self, filename: str = "", verification=None) -> dict:
        """Name a game, and say how confidently.

        `matched_by` travels with the answer because "we matched a DAT name"
        and "we guessed from the filename" deserve different amounts of trust.
        A UI that shows a cover without saying which is inviting somebody to
        believe the wrong one.
        """
        info = self.metadata.identify(verification=verification,
                                      filename=filename)
        return {
            "found": info.found,
            "title": info.title,
            "summary": info.summary,
            "released": info.released,
            "rating": info.rating,
            "genres": list(info.genres),
            "cover_url": info.cover_url,
            "source": info.source,
            "matched_by": info.matched_by,
        }

    def frontend_rows(self, platform: str = "") -> list[dict]:
        """The library flattened into the shape every frontend export wants.

        One shape for all three, because the differences between LaunchBox,
        Playnite and EmulationStation are in how the fields are *written*, not
        in which fields exist -- and three near-identical row builders would
        drift apart the first time one of them gained a column.
        """
        items = (self.library_view() or {}).get("items", [])
        wanted = str(platform or "").strip().lower()
        rows = []
        for item in items:
            slug = str(item.get("platform_slug") or item.get("platform") or "")
            if wanted and slug.lower() != wanted:
                continue
            name = str(item.get("file_name") or item.get("filename")
                       or item.get("name") or "")
            rows.append({
                "id": item.get("id") or item.get("rom_id") or "",
                "title": item.get("name") or item.get("title") or name,
                "platform": slug,
                "filename": name,
                "path": item.get("path") or item.get("full_path")
                        or (str(self.library_root(None) / slug / name)
                            if name and slug else ""),
                "region": item.get("region") or "",
                "verified": item.get("verified") or "",
            })
        return rows

    def metrics(self) -> str:
        """Everything a scrape needs, from state already computed."""
        up = int(time.monotonic() - self._started)
        return render_metrics({
            "platforms": len(PLATFORMS),
            "queued": len(self.queue),
            "wanted": len(self.store.missing()),
            "blocklist": len(self.store.list_items("blocklist")),
            "uptime_seconds": up,
            "dependencies": {
                "prowlarr": bool(self.prowlarr._config.api_key),
                **{lib["name"]: bool(lib.get("ok"))
                   for lib in self.libraries_status()},
            },
            "imports": self.store.settings.get("_import_verdicts") or {},
        })

    def play_route_counts(self) -> dict:
        """How many supported platforms are playable, and by which route.

        On the status page because the answer changes with the operator's own
        setup -- configuring a stream server moves five platforms out of
        `download_only` -- and because an install where that number is
        surprising is one where something is misconfigured.
        """
        counts: dict[str, int] = {}
        download_only = 0
        for platform in PLATFORMS:
            routes = routes_for(platform, stream=self.stream)
            if not routes.plays_without_downloading:
                download_only += 1
            for kind in routes.kinds:
                if kind != DOWNLOAD:
                    counts[kind] = counts.get(kind, 0) + 1
        counts["download_only"] = download_only
        counts["total"] = len(PLATFORMS)
        return counts

    def platform_directory(self) -> list[dict]:
        """Every platform with how it plays, for the API and the UI."""
        out = []
        for platform in PLATFORMS:
            routes = routes_for(platform, stream=self.stream)
            out.append({
                "slug": platform.slug,
                "name": platform.name,
                "media": platform.media,
                "extensions": list(platform.extensions),
                "max_size_mb": platform.max_size // (1024 * 1024),
                "play_routes": list(routes.kinds),
                "plays": routes.plays_without_downloading,
                "how": routes.summary(),
            })
        return out

    # How often the count and library are refreshed in the background.
    COUNT_TTL = 300
    # How much of the library is held for the grid. Enough to browse; the whole
    # of a 72,000-row library is neither renderable nor wanted in memory.
    LIBRARY_PAGE = 200

    def counts(self) -> dict:
        """Badge numbers for the nav rail.

        This never calls RomM. Caching a slow call still means somebody pays
        for it whenever the cache expires, and on a large library RomM's
        /api/roms can exceed two minutes -- so the page stalled for whoever
        happened to poll first.
        """
        games = self._count_cache[0]
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
            counted, failed = 0, []
            for cfg, backend in self.game_libraries:
                label = cfg.get("name") or getattr(backend, "name", "library")
                try:
                    counted += backend.count()
                except Exception as err:
                    # Named, because "unreachable" without saying which server
                    # is useless once there is more than one.
                    failed.append(f"{label} unreachable ({err.__class__.__name__})")
            if not self.game_libraries:
                return {"message": "no library configured"}
            where = f"{len(self.game_libraries)} librar" + \
                    ("y" if len(self.game_libraries) == 1 else "ies")
            msg = f"{counted} games across {where}"
            return {"message": msg + ("; " + ", ".join(failed) if failed else "")}
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
            # Which library this platform belongs to. A platform rule wins over
            # the default, so "PSX goes to Gaseous" is one row in the Libraries
            # page rather than a second ROMarr.
            target = self.library_for(platform.slug)
            if target is None:
                # Nothing to import into. Recorded rather than skipped: a
                # finished download with nowhere to go is a configuration
                # problem somebody can fix, and silence would leave a download
                # that completed and simply never appeared.
                detail = "no library configured to import into"
                self.store.record(Event(kind="failed", game=name,
                                        platform=platform.slug, detail=detail))
                results.append({"name": name, "ok": False, "reason": detail})
                continue
            target_cfg, target_lib = target
            label = target_cfg.get("name") or getattr(target_lib, "name", "library")

            outcome = import_rom(path, platform, self.library_root(target_cfg))
            if outcome.ok:
                if self.store.settings.get("rescan_after_import", True):
                    target_lib.rescan(platform.slug)
                self.store.record(Event(kind="imported", game=name,
                                        platform=platform.slug, release=name,
                                        library=label,
                                        detail=str(outcome.destination)))
                # It has arrived, so it is no longer wanted.
                for w in list(self.store.wanted):
                    if w.platform == platform.slug and w.game.lower() in name.lower():
                        self.store.fulfil(w.game, w.platform)
            else:
                self.store.record(Event(kind="failed", game=name,
                                        platform=platform.slug, detail=outcome.reason))
            results.append({"name": name, "ok": outcome.ok,
                            "reason": outcome.reason, "library": label})
        return results


# -- HTTP ------------------------------------------------------------------

def make_handler(service: ROMarr):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ROMarr"

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload):
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _guard(self, handler):
            """Turn an unhandled failure into a reply instead of a dead socket.

            Without this, an exception propagates to BaseHTTPRequestHandler,
            which logs a traceback and closes the connection having sent
            nothing. The caller sees no status code at all -- curl reports 000 --
            so there is nothing to search for and no way to tell a crash from a
            network problem. A search that exceeded Prowlarr's 60s timeout did
            exactly this.

            Extended past the two verbs it was written for: a PUT that saves
            settings and a DELETE that removes a library can fail the same way,
            and a dead socket is no more diagnosable there.
            """
            try:
                return handler()
            except Exception as exc:
                log.exception("%s %s failed", self.command, self.path)
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        #: Paths that answer without a credential, and nothing else.
        #:
        #: `/` is the UI shell, which holds no data and is where somebody logs
        #: in. `/api/v1/login` is how they do it. `/api/health` is the
        #: container's HEALTHCHECK, which has no credential to offer -- it
        #: answers, but see `_get`: unauthenticated it returns one bit rather
        #: than the library paths and client URLs it used to hand out for free.
        OPEN_PATHS = ("/", "/api/health", "/api/v1/login")

        def _drain(self) -> None:
            """Read the request body before refusing it.

            A POST has already sent its body; replying without reading leaves
            those bytes in the socket and the client sees an aborted
            connection rather than the refusal. The refusal has to arrive to
            be useful.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass

        def _authorised(self) -> bool:
            # Single sign-on first, when configured. `self.client_address` is
            # the socket peer -- the only trustworthy source. Reading it from
            # X-Forwarded-For would let anybody claim to be the proxy, which
            # is the bypass the whole arrangement exists to close.
            if service.sso is not None:
                identity = service.sso.identify(
                    self.headers, peer=self.client_address[0])
                if identity.ok:
                    return True
                log.debug("sso refused: %s", identity.reason)
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            query = parse_qs(urlparse(self.path).query)
            return service.auth.authorised(self.headers, query, cookies)

        def _gate(self, handler):
            """Refuse before doing anything, unless the path is open.

            Wrapped around every verb rather than checked inside each route:
            a route added later is protected by default, and forgetting to
            call a per-route check is exactly how the endpoints that queue
            downloads end up answering strangers.
            """
            path = urlparse(self.path).path
            # Rate limit before authenticating, and key on the caller's
            # address: login is the one endpoint where guessing is the attack,
            # so it has to be limited for callers who have not authenticated
            # -- which is all of them, at that point.
            category = RateLimiter.category_for(path)
            allowed, retry = service.limiter.check(
                category, self.client_address[0])
            if not allowed:
                # Drain first, for the same reason the 401 does: a POST has
                # already sent its body, and replying without reading it
                # aborts the connection so the caller never sees the 429 --
                # which is precisely the caller who most needs to be told to
                # slow down, and who will otherwise retry immediately.
                self._drain()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(retry))
                body = json.dumps({"error": "rate limited",
                                   "retry_after": retry}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if path in self.OPEN_PATHS or self._authorised():
                return self._guard(handler)
            self._drain()
            return self._json(401, {
                "error": "unauthorised",
                "detail": "Send your key as the X-Api-Key header, as "
                          "Authorization: Bearer, or as ?apikey=. Find it "
                          "under Settings -> General, or set ROMARR_API_KEY.",
            })

        def do_GET(self):
            return self._gate(self._get)

        def do_POST(self):
            return self._gate(self._post)

        def do_PUT(self):
            return self._gate(self._put)

        def do_DELETE(self):
            return self._gate(self._delete)

        def _get(self):
            route = urlparse(self.path)
            query = parse_qs(route.query)
            if route.path == "/":
                return self._send(200, ui_page().encode("utf-8"),
                                  "text/html; charset=utf-8")

            # --- *arr-shaped API ---------------------------------------
            if route.path == "/api/v1/game":
                # Served from the background cache. Calling RomM here meant the
                # page waited behind whatever else was querying that table.
                return self._json(200, service.library_view())
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
                # Configured entries first, then whatever Prowlarr proxies --
                # the latter are managed there and shown read-only, the way an
                # *arr shows an indexer it did not define.
                items = [redact(i) for i in service.store.list_items("indexers")]
                proxied, err = [], None
                try:
                    proxied = service.prowlarr.indexers()
                except Exception as exc:
                    err = str(exc)
                return self._json(200, {"items": items, "proxied": proxied,
                                        "error": err})
            if route.path == "/api/v1/downloadclient":
                return self._json(200, {"items": service.download_clients()})
            if route.path == "/api/v1/downloadclient/schema":
                return self._json(200, {"types": CLIENT_TYPES})
            if route.path == "/metrics":
                # Authenticated like everything else. A metrics endpoint that
                # is open while the rest of the app is not is a hole with a
                # Grafana dashboard attached: it names every dependency, the
                # queue depth and the library size.
                return self._send(200, service.metrics().encode(),
                                  "text/plain; version=0.0.4; charset=utf-8")
            if route.path == "/api/v1/backup":
                include = query.get("secrets", ["0"])[0] in ("1", "true", "yes")
                return self._json(200, make_backup(service.store.settings,
                                                   include_secrets=include))
            if route.path == "/api/v1/export":
                what = (query.get("what", ["library"])[0] or "library").lower()
                rows = {
                    "library": service.library_view().get("items", []),
                    "wanted": service.store.missing(),
                    "blocklist": service.store.list_items("blocklist"),
                }.get(what)
                if rows is None:
                    return self._json(400, {"error": "unknown export"})
                if query.get("format", ["json"])[0].lower() == "csv":
                    return self._send(200, to_csv(rows).encode(),
                                      "text/csv; charset=utf-8")
                return self._json(200, {"items": rows})
            if route.path == "/api/v1/hub/catalogue":
                catalogue = hub.plugins()
                items = catalogue.get("items") or []
                found = hub_search(
                    items,
                    query.get("q", [""])[0],
                    capability=query.get("capability", [""])[0],
                    platform=query.get("platform", [""])[0],
                    installed={"1": True, "0": False}.get(
                        query.get("installed", [""])[0]),
                )
                return self._json(200, {
                    "items": found,
                    "facets": hub_facets(items),
                    "total": len(items),
                    "matched": len(found),
                    "error": catalogue.get("error"),
                })
            if route.path == "/api/v1/calendar":
                return self._json(200, metadata_calendar(
                    service.store.list_items("metadata_providers"),
                    days_back=int(query.get("back", ["30"])[0] or 30),
                    days_ahead=int(query.get("ahead", ["60"])[0] or 60)))
            if route.path == "/api/v1/blocklist":
                return self._json(200, {"items": service.blocklist.as_items()})
            if route.path == "/api/v1/connection/schema":
                return self._json(200, {"types": [
                    {"name": name, "label": spec["label"],
                     "fields": list(spec["fields"]), "help": spec["help"]}
                    for name, spec in NOTIFIERS.items()]})
            if route.path == "/api/v1/tag":
                return self._json(200, {
                    "tags": service.store.settings.get("_tags") or {}})
            if route.path == "/api/v1/manualimport":
                return self._json(200, service.scan(
                    query.get("path", [""])[0]))
            if route.path == "/api/v1/openapi.json":
                return self._json(200, openapi_spec(VERSION))
            if route.path == "/api/v1/metadata/schema":
                return self._json(200, {"providers": [
                    {"name": name, "label": spec["label"],
                     "fields": list(spec["fields"]), "help": spec["help"]}
                    for name, spec in METADATA_PROVIDERS.items()]})
            if route.path == "/api/v1/metadata/lookup":
                return self._json(200, service.identify(
                    filename=query.get("filename", [""])[0]))
            if route.path == "/api/v1/frontend/formats":
                return self._json(200, {"formats": [
                    {"name": name, "label": spec["label"],
                     "filename": spec["filename"]}
                    for name, spec in FRONTEND_FORMATS.items()]})
            if route.path == "/api/v1/frontend/export":
                name = (query.get("format", ["launchbox"])[0] or "").lower()
                spec = FRONTEND_FORMATS.get(name)
                if spec is None:
                    return self._json(400, {
                        "error": "unknown format",
                        "known": sorted(FRONTEND_FORMATS)})
                rows = service.frontend_rows(query.get("platform", [""])[0])
                return self._send(200, spec["render"](rows).encode("utf-8"),
                                  spec["content_type"])
            if route.path == "/api/v1/indexer/schema":
                return self._json(200, {"types": INDEXER_TYPES})
            if route.path == "/api/v1/library":
                return self._json(200, {"items": service.libraries_status()})
            if route.path == "/api/v1/library/config":
                return self._json(200, {"items": [
                    redact_library(lib) for lib in service.store.list_items("libraries")]})
            if route.path == "/api/v1/library/schema":
                return self._json(200, {"types": LIBRARY_TYPES})
            if route.path == "/api/v1/config":
                return self._json(200, service.safe_settings())
            if route.path == "/api/v1/system/status":
                return self._json(200, service.status())
            if route.path == "/api/v1/system/counts":
                return self._json(200, service.counts())
            if route.path == "/api/health" and not self._authorised():
                # Liveness needs one bit. The full report names library paths,
                # client URLs and counts, and this endpoint is reachable
                # without a credential so the container HEALTHCHECK works --
                # which made all of that free reconnaissance for anyone who
                # could reach the port.
                return self._json(200, {"ok": True})
            if route.path == "/api/health":
                return self._json(200, service.health())
            if route.path == "/api/platforms":
                return self._json(200, service.platform_directory())
            if route.path == "/api/queue":
                return self._json(200, [asdict(i) for i in service.queue])
            if route.path == "/api/v1/release":
                game = (query.get("game") or [""])[0].strip()
                if not game:
                    return self._json(400, {"error": "game is required"})
                return self._json(200, service.candidates(
                    game, (query.get("platform") or [""])[0]))
            if route.path == "/api/v1/hub/plugins":
                return self._json(200, hub.plugins())
            if route.path == "/api/v1/hub/status":
                return self._json(200, {"available": hub.available()})
            if route.path == "/api/search":
                game = (query.get("game") or [""])[0]
                if not game:
                    return self._json(400, {"error": "game is required"})
                return self._json(200, service.search(game, (query.get("platform") or [""])[0]))
            return self._json(404, {"error": "not found"})

        def _post(self):
            route = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})

            if route.path == "/api/v1/blocklist":
                # A release is blocked by identity, so a title the indexer
                # rewrites tomorrow is still the same block.
                from .selection import Release as _Release

                release = _Release(
                    title=str(body.get("title") or ""),
                    size=int(body.get("size") or 0), seeders=0,
                    categories=(), download_url=str(body.get("url") or ""),
                    protocol="torrent",
                    indexer=str(body.get("indexer") or ""))
                return self._json(200, service.block(
                    release, str(body.get("reason") or "")))
            if route.path == "/api/v1/restore":
                try:
                    settings, warning = read_backup(body)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                service.store.settings.update(settings)
                service.store.save()
                service.reload_clients()
                service.reload_libraries()
                return self._json(200, {"ok": True, "warning": warning})
            if route.path == "/api/v1/login":
                # Exchange a credential for a session, so a browser presents
                # the key once instead of carrying it in every request and
                # holding it in the page where any script can read it.
                supplied = str(body.get("apikey") or "")
                password = str(body.get("password") or "")
                if not (service.auth.check_key(supplied)
                        or service.auth.check_password(password)):
                    return self._json(401, {"error": "unauthorised"})
                # The second factor gates the session, not the API key: a key
                # is already a high-entropy secret and a script cannot be
                # prompted. What 2FA protects is the interactive login.
                if service.auth.totp.enabled and not supplied:
                    if not service.auth.totp.verify(body.get("totp")):
                        return self._json(401, {"error": "unauthorised",
                                                "detail": "two-factor code "
                                                          "required or wrong"})
                token = service.auth.issue_session()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # HttpOnly so a script cannot read it; SameSite=Strict so
                # another site cannot ride it; Path=/ so it covers the API.
                # Not Secure: ROMarr is normally plain http on a LAN, and a
                # Secure cookie would simply never be stored there.
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; "
                    f"Path=/; Max-Age={service.auth.session_seconds}")
                payload = json.dumps({"ok": True}).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                return self.wfile.write(payload)
            if route.path == "/api/v1/connection/test":
                got = service.notify(Message(
                    "grab", "ROMarr test notification",
                    body="If you can read this, the connection works.",
                    reasons=("+50 this is a test",)))
                return self._json(200, {"results": got})
            if route.path == "/api/v1/tag":
                merged = service.tag(str(body.get("id") or ""),
                                     add=body.get("add"),
                                     remove=body.get("remove"))
                return self._json(200, {"tags": merged})
            if route.path == "/api/v1/hub/submit":
                # Prepared, never sent. Publishing under somebody's name is
                # their decision; a tool that posts because a button was
                # clicked in a settings page has made it for them.
                submission = Submission(
                    slug=str(body.get("slug") or "").strip(),
                    name=str(body.get("name") or "").strip(),
                    repository=str(body.get("repository") or "").strip(),
                    author=str(body.get("author") or "").strip(),
                    description=str(body.get("description") or "").strip(),
                    capabilities=list(body.get("capabilities") or []),
                    platforms=list(body.get("platforms") or []),
                )
                problems = submission.problems(
                    service.store.settings.get("plugin_hosts") or None)
                if problems:
                    return self._json(400, {"problems": problems})
                return self._json(200, {
                    "entry": submission.as_entry(),
                    "submit_url": submission_link(submission),
                    "note": "ROMarr does not post this for you. Open the link "
                            "to review and submit it yourself.",
                })
            if route.path == "/api/v1/hub/source/check":
                got = check_source(
                    str(body.get("url") or ""),
                    service.store.settings.get("plugin_hosts") or None)
                return self._json(200 if got.ok else 400, {
                    "ok": got.ok, "url": got.url, "host": got.host,
                    "reason": got.reason,
                })
            if route.path == "/api/v1/hub/plugin":
                slug = (body.get("slug") or "").strip()
                action = (body.get("action") or "").strip()
                if not slug or action not in ("install", "enable", "disable", "uninstall"):
                    return self._json(400, {"error": "slug and a valid action are required"})
                fn = {"install": hub.install, "enable": hub.enable,
                      "disable": hub.disable, "uninstall": hub.uninstall}[action]
                result = fn(slug)
                return self._json(200 if result.get("ok") else 500, result)
            if route.path == "/api/request":
                game = (body.get("game") or "").strip()
                platform = (body.get("platform") or "").strip()
                if not game or not platform:
                    return self._json(400, {"error": "game and platform are required"})
                return self._json(200, service.request(game, platform))
            if route.path == "/api/v1/release/grab":
                release_id = (body.get("id") or "").strip()
                if not release_id:
                    return self._json(400, {"error": "id is required"})
                return self._json(200, service.grab_candidate(release_id))
            if route.path == "/api/import":
                return self._json(200, {"imported": service.import_finished()})
            if route.path in ("/api/v1/downloadclient", "/api/v1/indexer"):
                key = "download_clients" if "downloadclient" in route.path else "indexers"
                existing = service.store.get_item(key, body.get("id")) if body.get("id") else None
                saved = service.store.put_item(key, merge_secrets(dict(body), existing))
                service.reload_clients()
                return self._json(200, redact(saved))
            if route.path == "/api/v1/library":
                existing = (service.store.get_item("libraries", body.get("id"))
                            if body.get("id") else None)
                incoming = merge_library_secrets(dict(body), existing)
                # Exactly one default. Marking a second would make routing
                # depend on stored order, which is not something anybody can
                # see, let alone reason about.
                if incoming.get("is_default"):
                    for other in service.store.list_items("libraries"):
                        if other.get("id") != incoming.get("id") and other.get("is_default"):
                            other["is_default"] = False
                            service.store.put_item("libraries", other)
                saved = service.store.put_item("libraries", incoming)
                service.reload_libraries()
                return self._json(200, redact_library(saved))
            if route.path == "/api/v1/library/test":
                existing = (service.store.get_item("libraries", body.get("id"))
                            if body.get("id") else None)
                return self._json(200, service.test_library(
                    merge_library_secrets(dict(body), existing)))
            if route.path == "/api/v1/downloadclient/test":
                # Tested against the submitted form, with any untouched secret
                # filled back in, so Test reflects what Save would store.
                existing = service.store.get_item("download_clients", body.get("id"))                     if body.get("id") else None
                return self._json(200, service.test_client(merge_secrets(dict(body), existing)))
            if route.path == "/api/v1/indexer/test":
                existing = service.store.get_item("indexers", body.get("id"))                     if body.get("id") else None
                return self._json(200, service.test_indexer(merge_secrets(dict(body), existing)))
            if route.path in ("/api/v1/webhook", "/api/v1/webhook/ggrequestz"):
                return self._json(200, service.handle_request_webhook(body))
            if route.path == "/api/v1/command":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json(400, {"error": "name is required"})
                return self._json(200, service.run_command(name))
            return self._json(404, {"error": "not found"})

        def _delete(self):
            route = urlparse(self.path)
            if route.path.startswith("/api/v1/blocklist/"):
                # Lifting a block is a decision, so it is its own verb rather
                # than a flag on an update -- and the reason the entry carried
                # is what the operator read before making it.
                from urllib.parse import unquote

                entry_id = unquote(route.path[len("/api/v1/blocklist/"):])
                removed = service.unblock(entry_id)
                return self._json(200 if removed else 404, {"deleted": removed})
            if route.path.startswith("/api/v1/library/"):
                item_id = route.path[len("/api/v1/library/"):]
                removed = service.store.delete_item("libraries", item_id)
                service.reload_libraries()
                return self._json(200 if removed else 404, {"deleted": removed})
            for prefix, key in (("/api/v1/downloadclient/", "download_clients"),
                                ("/api/v1/indexer/", "indexers")):
                if route.path.startswith(prefix):
                    item_id = route.path[len(prefix):]
                    removed = service.store.delete_item(key, item_id)
                    service.reload_clients()
                    return self._json(200 if removed else 404, {"deleted": removed})
            return self._json(404, {"error": "not found"})

        def _put(self):
            route = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})

            if route.path == "/api/v1/config":
                updated = service.store.update_settings(body)
                updated = service.safe_settings()
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


def serve(port: int = 6868, env: dict[str, str] | None = None):
    service = ROMarr(env)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
    log.info("ROMarr listening on :%d, library=%s", port, service.library)
    httpd.serve_forever()
