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
import pathlib
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import time

from .libraries import (
    LIBRARY_TYPES, build_library, build_library_from_config, library_counts,
    load_backend_plugins, merge_library_secrets, redact_library, route_library,
)
from .clients import QBittorrent, QbitConfig, Romm, RommConfig
from . import hub  # ROM Hub bridge -- the Cartridge plugin layer
from .dat import DatIndex, parse_dat
from .downloaders import (
    CLIENT_TYPES, NZBGet, NzbgetConfig, SABnzbd, SabConfig, build_client,
    hand_off, merge_secrets, pick_client, redact,
)
from .indexers import INDEXER_TYPES, build_indexer, redact_indexer
from .indexers import Prowlarr, ProwlarrConfig
from .library import import_rom, map_remote_path
from .collections import is_translation
from .auth import DISABLED as AUTH_DISABLED
from .auth import MIN_PASSWORD, SESSION_COOKIE, Auth, new_api_key, parse_cookies
from .capture import MAX_BODY_BYTES as CAPTURE_MAX_BODY
from .capture import Rejected as CaptureRejected
from .capture import index_dir as capture_index_dir
from .capture import ingest as capture_ingest
from .capture import status as capture_status
from .catalogue import (Submission, check_source, facets as hub_facets,
                        search as hub_search, submission_link)
from .frontends import FORMATS as FRONTEND_FORMATS
from .metadata import PROVIDERS as METADATA_PROVIDERS
from .notify import (NOTIFIERS, Message, Notifier, failed, grabbed, imported,
                     update_available)
from .profiles import Blocklist, ReleaseProfile, release_id
from .upgrade import is_upgrade, merge_tags, scan as scan_directory
from .metadata import Metadata, calendar as metadata_calendar
from .metadata import discover as metadata_discover
from .openapi import spec as openapi_spec
from .ops import (LogRing, RateLimiter, make_backup, read_backup,
                  render_metrics, to_csv)
from .platforms import PLATFORMS, resolve
from .sso import ForwardAuth
from .totp import Totp
from .playability import (
    DOWNLOAD, MOONLIGHT_KINDS, PAIRING_IS_MANUAL, PLAYERS, WOLF, MoonlightHost,
    PlayerPolicy, StreamServer, StreamSources, routes_for, routes_for_file)
from .lists import LIST_TYPES

#: List-source fields that are credentials: masked on the way out, kept on
#: edit when the form returns the placeholder.
LIST_SECRETS = ("api_key", "openxbl_key", "npsso", "itchio_key",
                "epic_code", "epic_refresh", "ea_token",
                "battlenet_cookie", "humble_cookie")
from .scheduler import Scheduler, next_search_due
from .selection import best_release, judge, score
from .store import Event, Store
from .ui import page as ui_page
from .ui import link_page as ui_link_page
from .ui import login_page as ui_login_page

log = logging.getLogger(__name__)

VERSION = "0.8.0"

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


#: How deep the DAT scan walks, and how many directory entries it will look at
#: before giving up.
#:
#: `reload_dats` used to do `root.rglob("*.dat")` over whatever DAT_PATH named.
#: Pointed at a real library that is the whole library: on a live install with
#: ~58 platforms on a network mount the walk had not finished after ten
#: minutes, so setting DAT_PATH to the obvious place -- the directory the ROMs
#: and their datfiles are both in -- made ROMarr appear to hang at startup.
#:
#: DATs sit at the top of a DAT directory, or one level down beside the
#: platform they describe. Three levels covers both with room to spare, and the
#: entry cap stops a pathological tree regardless of depth.
DAT_SCAN_DEPTH = 3
DAT_SCAN_MAX_ENTRIES = 40_000

#: Directory names never worth descending into looking for a DAT. These are
#: where the volume of files actually is.
_DAT_SKIP_DIRS = frozenset({
    ".git", ".svn", "node_modules", "__pycache__", "@eaDir",
    "#recycle", "$RECYCLE.BIN", "System Volume Information",
    "saves", "savestates", "states", "media", "images", "videos",
    "manuals", "bios", "downloaded_media", "cache",
})


def _find_dats(root: "Path") -> tuple[list["Path"], str]:
    """Candidate DAT files under `root`, without walking the world.

    Returns the paths and, when the scan stopped early, a sentence saying so --
    silence would leave an operator wondering why only some of their DATs
    loaded.
    """
    import os

    found: list[Path] = []
    seen = 0
    root = Path(root)
    base_depth = len(root.parts)

    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        depth = len(here.parts) - base_depth
        if depth >= DAT_SCAN_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs
                       if d not in _DAT_SKIP_DIRS and not d.startswith(".")]
        for name in files:
            seen += 1
            if seen > DAT_SCAN_MAX_ENTRIES:
                return found, (
                    f"looked at {DAT_SCAN_MAX_ENTRIES:,} files under {root} "
                    f"and stopped. Point DAT_PATH at a directory holding only "
                    f"DATs, rather than at your ROM library.")
            lowered = name.lower()
            # gamelist.xml is EmulationStation's, one per platform directory,
            # and never a DAT. On a real library that is dozens of files opened
            # and rejected -- and on this one, dozens of permission-denied
            # warnings that look like a problem and are not.
            if lowered in ("gamelist.xml", "miximages.xml"):
                continue
            if lowered.endswith((".dat", ".xml")):
                found.append(here / name)
    return sorted(found), ""




def _read_failure(err: Exception) -> str:
    """Why a library read failed, in words that suggest a fix.

    "HTTPError" tells somebody nothing. A 401 or 403 from a library almost
    always means the credentials ROMarr holds are wrong or have expired, and
    saying so is the difference between a five-minute fix and an evening.
    """
    status = getattr(getattr(err, "response", None), "status_code", None)
    if status in (401, 403):
        return (f"credentials rejected (HTTP {status}). Check the username, "
                f"password or API key on this library.")
    if status:
        return f"HTTP {status} from the library server."
    return err.__class__.__name__



def _claimants(filename: str) -> list[str]:
    """Every platform that claims this file's extension."""
    suffix = pathlib.PurePath(str(filename)).suffix.lower()
    if not suffix:
        return []
    return [p.slug for p in PLATFORMS if suffix in p.extensions]


#: Pairs the header genuinely cannot separate, so a difference between them is
#: not evidence of anything. Mirrors the rule in sniff.disagrees_with; kept
#: here too because this decides whether to *correct* rather than warn.
_FAMILIES = (("sms", "gamegear"), ("gb", "gbc"), ("nes", "famicom", "fds"),
             ("snes", "sfam"), ("neo-geo-pocket", "neo-geo-pocket-color"),
             ("wonderswan", "wonderswan-color"))


def _same_family(a: str, b: str) -> bool:
    return any(a in family and b in family for family in _FAMILIES)

def _archive_tool_path() -> str:
    """The libarchive bsdtar ROMarr found, or an empty string."""
    try:
        from .library import bsdtar_path
        return bsdtar_path() or ""
    except Exception:  # noqa: BLE001
        return ""

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
        self.retroarch = StreamServer(stream_url) if stream_url else None

        # A Moonlight host -- Wolf, Sunshine or Steam Headless -- if the
        # operator runs one. The same idea as the stream server and a much
        # weaker source: it renders somewhere else and sends video, but it is
        # a desktop rather than a platform router, so it can only be *asked*
        # what applications it has and never what those applications open.
        # `playability.MoonlightHost` is where that limit is spelled out.
        #
        # Host and kind only. The credential that reads Sunshine's app list is
        # read from the environment and never written to the store, for the
        # same reason ROMARR_API_KEY is not: a secret from outside must not
        # end up in a file that gets backed up.
        moonlight_host = e.get("MOONLIGHT_HOST", "")
        self.store.settings["_moonlight_host"] = moonlight_host
        self.store.settings["_moonlight_kind"] = e.get("MOONLIGHT_KIND", WOLF)
        self.moonlight = MoonlightHost(
            moonlight_host,
            kind=e.get("MOONLIGHT_KIND", WOLF),
            username=e.get("MOONLIGHT_USER", ""),
            password=e.get("MOONLIGHT_PASS", ""),
            socket_path=e.get("WOLF_SOCKET_PATH", ""),
            api_url=e.get("WOLF_API_URL", ""),
            desktop_url=e.get("STEAM_HEADLESS_URL", ""),
        ) if moonlight_host else None

        # Both stream tiers behind the single slot `routes_for` takes. The
        # RetroArch server goes first because it knows which cores it has,
        # while the Moonlight host is inferring from application names -- so
        # an inferred answer never displaces a known one.
        self.stream = (StreamSources(self.retroarch, self.moonlight)
                       if (self.retroarch or self.moonlight) else None)

        # Which browser players this install offers, and in what order. All
        # four unless the operator says otherwise -- see PlayerPolicy for why
        # an unset variable means "all" and `none` means none.
        #
        # Turning one off is a real decision rather than a preference: an
        # install whose library server runs with DISABLE_RUFFLE_RS set turns
        # Ruffle off here so ROMarr stops promising a button that will not be
        # there, and an install that would rather its users did not leave for
        # Archive.org turns Emularity off.
        self.players = PlayerPolicy.from_env(e)
        self.store.settings["_players"] = ",".join(self.players.order)

        # Authentication, on unless deliberately turned off.
        #
        # The key is generated and stored on first run rather than left blank,
        # because an install that is open until somebody reads the
        # documentation is an open install. It is kept under a leading
        # underscore so `safe_settings` never has to learn about it -- that
        # method masks known credential shapes, and a key it had not been told
        # about would have gone straight into the browser.
        supplied_key = e.get("ROMARR_API_KEY", "")
        api_key = supplied_key or self.store.settings.get("_api_key", "")
        generated = not api_key
        if generated:
            api_key = new_api_key()
        if supplied_key:
            # An operator-supplied key belongs to the environment and is not
            # copied into the state file. Persisting it would put a secret
            # from outside into a file that gets backed up, and would make
            # removing the variable a no-op because the stored copy would take
            # over -- which is not what unsetting something means.
            self.store.settings.pop("_api_key", None)
        else:
            self.store.settings["_api_key"] = api_key
            if generated:
                # Written now rather than left for whatever saves next. A
                # generated key that only ever exists in memory is a different
                # key after every restart, so every script authenticating with
                # it breaks and the value shown under Settings is one nobody
                # can rely on. Found on a live install whose store file was a
                # week older than the running process.
                self.store.save()
        mode = e.get("ROMARR_AUTH", "").strip().lower()
        self.auth = Auth(
            api_key=api_key,
            password_hash=self.store.settings.get("_password_hash", ""),
            enabled=mode != AUTH_DISABLED,
        )

        # A password handed in by the environment claims the install before it
        # ever serves a request, which is what a container template should do:
        # no open window at all, and no setup screen for the operator to find.
        env_password = e.get("ROMARR_PASSWORD", "")
        if env_password and not self.store.settings.get("_password_hash"):
            self.store.settings["_password_hash"] = \
                self.auth.hash_password(env_password)
            self.auth.password_hash = self.store.settings["_password_hash"]
            log.info("password set from ROMARR_PASSWORD")

        # "Claimed" means somebody can actually get in: a password exists, or
        # the operator supplied the key themselves and therefore has it. A key
        # ROMarr generated for itself is not a credential anybody holds, so an
        # install with only that is unclaimed and shows the setup screen.
        #
        # This is the whole of issue #8. Authentication was correct and the
        # browser had no way through it, so the UI rendered and then 401'd on
        # every request, while the log pointed at a Settings page that was
        # itself behind the gate.
        self.claimed = bool(self.store.settings.get("_password_hash")
                            or supplied_key)
        if self.auth.enabled and not self.claimed:
            log.warning(
                "ROMarr is unclaimed: the first visitor to the web UI sets the "
                "password. Set ROMARR_PASSWORD (or ROMARR_API_KEY) in the "
                "environment to claim it before it starts.")
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
        #: The same number, split into the two categories it was hiding:
        #: files on disk here versus rows the library server holds for things
        #: that stream from somewhere else. `None` for either means the
        #: backend could not separate them -- see libraries.library_counts.
        self._count_split: dict = {"total": None, "on_disk": None,
                                   "catalogued": None}
        #: label -> why the last read of that library failed, or absent.
        self._library_reasons: dict[str, str] = {}
        # The library itself is cached the same way, and for a stronger reason:
        # RomM's /api/roms is contended on a large library -- other clients
        # polling it hold the database for minutes at a time -- so a request
        # that waits its turn is a page that never paints. Fetched here,
        # retried until it lands, then served from memory.
        self._library_cache: tuple[list | None, float, str] = (None, 0.0, "")
        self._count_thread = threading.Thread(target=self._refresh_counts, daemon=True)
        self._count_thread.start()

        # The live log: a ring buffer behind the Logs page. Attached to the
        # root logger so every module's lines land in it, exactly as they go
        # to stdout -- the page is a tail, not a second logging system.
        # One-shot tokens for the Steam OpenID return leg. The session
        # cookie is SameSite=Strict, so Steam's cross-site redirect back
        # arrives without it; this is the credential that replaces it.
        from .connect import StateStore
        self.connect_states = StateStore()

        # Peering: the trust model, the catalogue projection, and the HTTP
        # surface two servers talk over.
        from .federation import Federation
        self.federation = Federation(
            name=e.get("ROMARR_PEER_NAME", "ROMarr"),
            url=e.get("ROMARR_PUBLIC_URL",
                      self.store.settings.get("public_url", "")))
        # Under a leading underscore: peer tokens are credentials, and that
        # prefix is what keeps them structurally outside safe_settings rather
        # than relying on a list somebody has to remember to update.
        restored = self.federation.restore(
            self.store.settings.get("_peers") or [])
        if restored:
            log.info("restored %d peer relationship(s)", restored)

        # What netplay is judged on. Kept beside the data file rather than
        # inside it: the index is one entry per ROM, and romarr.json is
        # rewritten on every recorded event.
        from .hashes import HashIndex
        self.hashes = HashIndex(
            Path(e.get("ROMARR_DATA", "/opt/romarr/romarr.json"))
            .with_name("romarr-hashes.json"))
        self.hashes.load()
        if len(self.hashes):
            log.info("hash index: %d dump(s) known to netplay", len(self.hashes))

        #: peer_id -> (rows, fetched_at). A friend's shelf, so browsing and
        #: filtering it does not bill their server per keystroke.
        self._friend_shelves: dict[str, tuple[list, float]] = {}

        self.logring = LogRing()
        logging.getLogger().addHandler(self.logring.handler())

        # The clock. Until this existed, "Import completed downloads
        # automatically" was a checkbox nothing read, and the Wanted list was
        # only ever searched when somebody pressed the button. Intervals are
        # read from settings at every tick, so changing one applies without a
        # restart; setting one to zero turns the job off.
        def _minutes(key: str, *, gate: str = "") -> object:
            def read() -> float:
                if gate and not self.store.settings.get(gate, True):
                    return 0
                return float(self.store.settings.get(key) or 0) * 60
            return read

        def _hours(key: str) -> object:
            def read() -> float:
                return float(self.store.settings.get(key) or 0) * 3600
            return read

        self.scheduler = Scheduler()
        self.scheduler.add(
            "ImportCompleted", "Import completed downloads",
            _minutes("auto_import_interval_minutes", gate="auto_import"),
            lambda: self._auto_import_summary())
        self.scheduler.add(
            "MissingGameSearch", "Search the Wanted list",
            _hours("search_missing_interval_hours"),
            lambda: self.search_missing(auto=True)["message"])
        self.scheduler.add(
            "RssSync", "Watch indexer feeds for wanted games",
            _minutes("rss_sync_interval_minutes"),
            self.rss_sync)
        self.scheduler.add(
            "ListSync", "Sync import lists into Wanted",
            _hours("list_sync_interval_hours"),
            lambda: self.list_sync()["message"])
        self.scheduler.add(
            "UpdateCheck", "Check github.com for a newer ROMarr",
            lambda: 86400 if self.store.settings.get("update_check", True) else 0,
            lambda: self.check_update()["message"])
        # Netplay is useless without hashes, and a shelf this size takes
        # minutes to read. Nobody should have to know that, or press a
        # button for it, so it runs daily and once at startup.
        self.scheduler.add(
            "HashIndex", "Read ROM hashes from the library for netplay",
            lambda: 86400 if self.store.settings.get("hash_index", True) else 0,
            lambda: self.start_hash_seed(quiet=True).get("detail", ""))
        self.scheduler.start()

        # The scheduler does not fire a daily job until a day has passed, so
        # a fresh install would have no hashes for 24 hours and every netplay
        # offer would come back "missing" with nothing to explain why. Kick
        # it once here instead.
        try:
            client = getattr(self, "game_library", None)
            if (self.store.settings.get("hash_index", True)
                    and hasattr(client, "hashes")):
                self.start_hash_seed(quiet=True)
        except Exception:                          # noqa: BLE001 - never fatal
            log.debug("startup hash seed did not start", exc_info=True)

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
            # Accumulated beside the total, because the total on its own is
            # the bug: a library that catalogues streamed entries answers
            # `count()` with the two categories added together, and every
            # surface then showed that sum as "games". Summed across
            # backends, and abandoned entirely the moment one of them cannot
            # split -- a breakdown that silently drops a library is a worse
            # answer than no breakdown.
            on_disk: int | None = 0
            catalogued: int | None = 0
            # The authoritative platform list, straight from each backend.
            # Derived-from-the-cache chips are a prefix of the truth while
            # the walk runs, and a person looking for a platform the walk
            # has not reached concludes it is unsupported.
            server_platforms: dict[str, int] = {}
            for _cfg, backend in self.game_libraries:
                lister = getattr(backend, "platforms", None)
                if not callable(lister):
                    continue
                try:
                    for row in lister():
                        name = row.get("platform") or ""
                        if name:
                            server_platforms[name] = (
                                server_platforms.get(name, 0)
                                + int(row.get("count") or 0))
                except Exception as err:      # noqa: BLE001
                    log.warning("platform list unavailable from %s: %s",
                                getattr(backend, "name", backend),
                                err.__class__.__name__)
            if server_platforms:
                self._server_platforms = [
                    {"platform": name, "count": count}
                    for name, count in sorted(server_platforms.items(),
                                              key=lambda kv: (-kv[1], kv[0]))]
            # Per library, so the Libraries page can say which one is unhappy
            # and why. A joined string was enough for a log line and no use to
            # a row that has to render one server's state.
            reasons: dict[str, str] = {}
            # Every library is read, and one failing does not lose the others.
            # A single unreachable server used to be indistinguishable from an
            # empty shelf; now it is one named row that did not answer.
            multiple = len(self.game_libraries) > 1
            for cfg, backend in self.game_libraries:
                label = cfg.get("name") or getattr(backend, "name", "library")
                try:
                    split = library_counts(backend)
                    total += int(split.get("total") or 0)
                    here, there = split.get("on_disk"), split.get("catalogued")
                    if here is None or there is None or on_disk is None:
                        on_disk = catalogued = None
                    else:
                        on_disk += int(here)
                        catalogued += int(there)
                    # Publish as soon as the server has answered, rather than
                    # after the shelf walk. The walk takes minutes on a large
                    # library, and until this moved the headline numbers fell
                    # back to the growing prefix of cached rows for all of it
                    # -- so a restart showed 60,000 games and climbing when
                    # the server could say 166,548 in three requests. All
                    # three still come from the same place, which is the
                    # property that stops them disagreeing.
                    self._count_split = {"total": total, "on_disk": on_disk,
                                         "catalogued": catalogued}
                except Exception as err:
                    ok = False
                    failures.append(f"{label}: {err.__class__.__name__}")
                    reasons[label] = _read_failure(err)
                    log.warning("library count refresh failed for %s: %s",
                                label, err.__class__.__name__)
                try:
                    # The WHOLE library, in pages, published progressively.
                    # This used to stop at one 200-row page, which made a
                    # 166,578-game install render as "about the first 100" --
                    # the grid was honest about exactly the slice it had and
                    # useless about the rest. Progressive publishing means
                    # the page improves while the fetch runs instead of
                    # showing a spinner for the minutes a big RomM takes.
                    offset = 0
                    while offset < self.LIBRARY_MAX:
                        batch = backend.games(
                            limit=self.LIBRARY_PAGE, offset=offset,
                            timeout=backend.BACKGROUND_TIMEOUT)
                        if not batch:
                            break
                        shelf.extend(
                            replace(g, source=label) if multiple else g
                            for g in batch)
                        offset += len(batch)
                        self._publish_library(shelf, "", partial=True)
                        if len(batch) < self.LIBRARY_PAGE:
                            break
                except Exception as err:
                    ok = False
                    failures.append(f"{label}: {err.__class__.__name__}")
                    reasons.setdefault(label, _read_failure(err))
                    log.warning("library refresh failed for %s: %s",
                                label, err.__class__.__name__)

            self._library_reasons = reasons
            if shelf or ok:
                self._count_cache = (total, time.monotonic())
                self._count_split = {"total": total, "on_disk": on_disk,
                                     "catalogued": catalogued}
                self._publish_library(shelf, "; ".join(failures),
                                      partial=False)
                log.info("library refreshed: %d games across %d librar%s "
                         "(%s on disk, %s catalogued elsewhere)",
                         len(shelf), len(self.game_libraries),
                         "y" if len(self.game_libraries) == 1 else "ies",
                         "?" if on_disk is None else on_disk,
                         "?" if catalogued is None else catalogued)
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

    def library_view(self, platform: str = "", q: str = "",
                     offset: int = 0, limit: int = 120, genre: str = "",
                     region: str = "", decade: str = "", origin: str = "",
                     sort: str = "", source: str = "") -> dict:
        """One page of the library, organised the way RomM organises it.

        Platform chips, alphabetical within a platform, server-side search
        and pagination -- because the whole cache is 166k rows and a grid
        that renders all of them is a browser tab that dies.

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

        rows = games
        wanted = str(platform or "").strip().lower()
        if wanted:
            rows = [g for g in rows if str(g.platform).lower() == wanted]
        needle = str(q or "").strip().lower()
        if needle:
            rows = [g for g in rows if needle in str(g.name).lower()]
        if genre:
            low = genre.lower()
            rows = [g for g in rows if any(x.lower() == low for x in g.genres)]
        if region:
            low = region.lower()
            rows = [g for g in rows if any(x.lower() == low for x in g.regions)]
        if decade:
            try:
                start = int(str(decade).rstrip("s"))
                rows = [g for g in rows if g.year and start <= g.year < start + 10]
            except ValueError:
                pass
        if origin:
            low = origin.lower()
            rows = [g for g in rows if (g.origin or "local").lower() == low]
        if source:
            low = source.lower()
            rows = [g for g in rows if (g.provenance or "local").lower() == low]

        # Sorting is over the filtered set, not the page, or "top rated"
        # would mean "the best of the 120 rows you happened to be looking
        # at" -- which is the kind of quiet lie this project exists to avoid.
        if sort == "rating":
            rows = sorted(rows, key=lambda g: (-g.rating, g.name.lower()))
        elif sort == "year":
            rows = sorted(rows, key=lambda g: (-g.year, g.name.lower()))
        elif sort == "name":
            rows = sorted(rows, key=lambda g: g.name.lower())

        offset = max(0, int(offset))
        limit = max(1, min(int(limit or 120), 500))
        page = rows[offset:offset + limit]

        # `grand_total` is how many rows are cached and browsable right now.
        # `totals` is what the library actually holds, straight from the
        # server. They differ while the walk runs and they differ forever on
        # a library past LIBRARY_MAX, and conflating them is how the shelf
        # came to report a different "games" number than the nav badge did on
        # the same screen. Both are published, each labelled for what it is.
        return {"items": [asdict(g) for g in page],
                "loading": False,
                "partial": getattr(self, "_library_partial", False),
                "total": len(rows),
                "grand_total": len(games),
                "cached_total": len(games),
                "totals": self.library_split(),
                "offset": offset,
                "platforms": getattr(self, "_library_platforms", []),
                "facets": getattr(self, "_library_facets", {}),
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

    def library_layout(self, cfg: dict) -> str:
        """The directory shape for one library: its own setting, or the global
        default. `flat` = <root>/<platform>/<rom>, `nested` = the same with a
        `roms/` level, matching RomM's Structure A and B."""
        return str((cfg or {}).get("layout")
                   or self.store.settings.get("library_layout") or "flat")

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
            name = cfg.get("name") or getattr(backend, "name", "")
            out.append({
                "id": cfg.get("id"),
                "name": name,
                "type": cfg.get("type", ""),
                "url": cfg.get("url", ""),
                "path": str(root),
                "path_exists": root.exists(),
                "path_hint": self.path_hint(root),
                "is_default": bool(cfg.get("is_default")),
                "platforms": cfg.get("platforms") or [],
                # Answering and usable are different questions, and conflating
                # them is how a library with rejected credentials showed as OK
                # while ROMarr could not read a single game out of it. The
                # heartbeat is deliberately unauthenticated so a slow server
                # does not stall the page; that makes it a liveness check, not
                # a verdict on whether the library works.
                "ok": bool(backend.reachable()),
                "readable": name not in self._library_reasons,
                "detail": self._library_reasons.get(name, ""),
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
        # `_peers` carries one bearer token per relationship. It is removed
        # here by name for the same reason as the two above: the underscore
        # is a naming convention, not a boundary, and `_romm_url` proves the
        # convention is not a reliable one to filter on.
        for secret in ("_api_key", "_password_hash", "_peers"):
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

        # The title travels with the release for the clients that can carry
        # it: the import sweep matches a finished download to its queue row by
        # that title, and a file named by the site it came from would never
        # match. See downloaders.hand_off.
        ok = hand_off(client, pick.download_url, name=pick.title)
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
            # The message other tools cannot send: what the scorer weighed.
            # A failure to explain must never fail the grab it explains.
            try:
                reasons = judge(pick, game, resolve(platform_slug)).why()
            except Exception:
                reasons = ()
            self.notify(grabbed(pick.title, platform_slug,
                                getattr(pick, "indexer", ""), reasons))
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
                # The release's page on its indexer. Unlike download_url it
                # never carries a credential, so it is the one link the
                # browser may have.
                "info_url": getattr(r, "info_url", ""),
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

    def queue_action(self, index: int, action: str) -> dict:
        """Act on one queue row: retry it, or forget it.

        The queue was a read-only list -- a download could sit "failed" with
        its reason shown and no way to do anything about it but edit the
        state file. Retry re-runs the request through the normal path;
        remove forgets a row that is done with (imported, or a failure you
        have read).
        """
        with self._lock:
            if not 0 <= index < len(self.queue):
                return {"ok": False, "error": "no such queue item"}
            item = self.queue[index]
        if action == "remove":
            with self._lock:
                self.queue = [q for n, q in enumerate(self.queue)
                              if n != index]
            return {"ok": True, "removed": True}
        if action == "retry":
            # Drop the stale row first so the retry's outcome is the only
            # one for this game, then run the same request a person would.
            with self._lock:
                self.queue = [q for n, q in enumerate(self.queue)
                              if n != index]
            return self.request(item.game, item.platform)
        return {"ok": False, "error": f"unknown action {action!r}"}

    def clear_queue(self, state: str = "") -> dict:
        """Empty the queue, or just the rows in one state.

        `clear_queue("failed")` is the common one: a sweep left twenty
        failures whose reason you have read, and removing them one by one is
        busywork.
        """
        with self._lock:
            before = len(self.queue)
            if state:
                self.queue = [q for q in self.queue if q.state != state]
            else:
                self.queue = []
            removed = before - len(self.queue)
        return {"ok": True, "removed": removed}

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
                # Set by the client during reachable() when it has something
                # to say beyond yes or no -- "the playwright driver is not
                # installed" is the difference between a fixable setup and an
                # inexplicable red dot.
                "detail": getattr(c, "detail", ""),
            })
        return out

    def browser_capability(self) -> dict:
        """Whether the browser download lane can run here, and why not.

        Its own report rather than a line on the status page because the
        answer is a setup instruction, and because it is legitimately absent:
        an install that only ever fetches plain URLs is complete, not broken.
        Reported for the configured client when there is one, and for a bare
        local launch when there is not, so the question can be asked before
        anybody adds a row.
        """
        from . import browser as browser_lane

        rows = [c for c in self.clients
                if getattr(c, "protocol", "") == "browser"]
        endpoint = getattr(getattr(rows[0], "_config", None), "base_url", "") if rows else ""
        available, reason = browser_lane.availability(endpoint)
        return {
            "configured": bool(rows),
            "available": available,
            "reason": reason,
            "endpoint": endpoint,
            "where": "remote" if endpoint else "this host",
            # Said out loud in the API, not only in the source: this lane
            # automates a real click on a real page and refuses everything
            # that would make it a bypass.
            "refuses": ["captchas", "bot-detection challenges",
                        "header spoofing", "logins ROMarr cannot pass"],
        }

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
            # DAT verification is the thing that separates ROMarr from a
            # downloader, and the status page had no way to say whether it was
            # on. Get Started could therefore only ever report DATs as not set
            # up, however many were loaded.
            # Without a libarchive bsdtar, every 7z and rar import fails on a
            # format it cannot open -- which is how the disc platforms ship.
            # The live install this was found on had been missing it silently.
            "archive_tool": _archive_tool_path(),
            "dats": len(self.dats.dats),
            "dat_names": [d.name for d in self.dats.dats if d.name],
            "dat_games": sum(len(d.games) for d in self.dats.dats),
            "play_routes": self.play_route_counts(),
            "stream_url": self.store.settings.get("_stream_url", ""),
            "moonlight": self.moonlight_status(),
            "events": len(self.store.events),
            "uptime": f"{up // 3600}h {(up % 3600) // 60}m",
        }

    @staticmethod
    def _dat_scan_limits() -> tuple[int, int]:
        """How far the DAT scan is allowed to go. Overridable for testing."""
        return DAT_SCAN_DEPTH, DAT_SCAN_MAX_ENTRIES

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
        found, stopped = _find_dats(root)
        for path in found:
            try:
                self.dats.add(parse_dat(path.read_text(encoding="utf-8",
                                                       errors="replace")))
            except (OSError, ValueError) as exc:
                log.warning("could not read %s: %s", path, exc)
        log.info("loaded %d DAT(s) from %s", len(self.dats.dats), root)
        result = {"loaded": len(self.dats.dats), "path": str(root),
                  "examined": len(found)}
        if stopped:
            result["truncated"] = stopped
            log.warning("DAT scan stopped early: %s", stopped)
        return result

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
        from .sniff import disagrees_with

        from .sniff import identify_file, looks_hollow

        result = scan_directory(directory, PLATFORMS, self.dats)
        rows = []
        for candidate in result.candidates:
            row = vars(candidate).copy()
            # One small read per candidate, and it settles two different
            # problems: a file whose name lies, and a file whose extension is
            # honest but shared.
            sniffed = identify_file(row.get("path", ""))
            guessed = row.get("platform", "")
            if sniffed is not None and sniffed.platform == guessed:
                # The guess was right, but on a shared extension it was still
                # a guess. Say what confirmed it instead of asking the operator
                # to confirm something we are now certain of.
                if len(_claimants(row.get("filename", ""))) > 1:
                    row["reason"] = sniffed.detail + " — confirmed by the header"
                    row["header_chose"] = sniffed.platform
            elif sniffed is not None:
                claimants = _claimants(row.get("filename", ""))
                if sniffed.platform in claimants:
                    # The extension is shared and detection had to pick one.
                    # `.bin` alone is claimed by twenty platforms, so the
                    # first-listed guess is barely better than a coin toss --
                    # and the header knows. Correcting is the whole point of
                    # reading it.
                    row["platform"] = sniffed.platform
                    row["reason"] = (f"{sniffed.detail} — chosen over "
                                     f"{len(claimants)} platforms sharing this "
                                     f"extension")
                    row["header_chose"] = sniffed.platform
                elif not _same_family(sniffed.platform, guessed):
                    # Not a claimant at all: the name is simply wrong. Flagged,
                    # never silently overridden -- an operator may know better.
                    row["header_says"] = sniffed.platform
                    row["header_detail"] = sniffed.detail
            # A file can be the right size, the right name and completely
            # empty. Nothing else in the pipeline notices, because every check
            # upstream is about identity rather than content.
            hollow = looks_hollow(row.get("path", ""))
            if hollow:
                row["hollow"] = hollow
            rows.append(row)
        return {
            "candidates": rows,
            "skipped": result.skipped,
            "error": result.error,
        }

    @staticmethod
    def _remove_imported(destination: Path) -> None:
        """Undo a write that turned out to need forcing.

        Verification happens against the archive before the copy, but a plain
        file on disk is verified from what landed -- so a refusal can arrive
        after the write. Leaving it there would put a rejected dump in the
        library for somebody to find later and wonder about.
        """
        import shutil
        try:
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
        except OSError as err:
            log.warning("could not roll back %s: %s", destination, err)

    def adopt(self, path: str, platform_slug: str = "", *,
              force: bool = False) -> dict:
        """Import one file the operator picked out of a manual-import scan.

        Separate from `import_finished` because the two have different
        authorities. That one acts on a download ROMarr asked for, and can
        infer the platform from the request that started it. This one acts on
        a file somebody pointed at, so the operator's platform choice wins over
        the guess -- they can see the file and ROMarr cannot.

        `force` is what makes a BAD_DUMP importable. It is deliberately a
        separate argument rather than a mode: a bad dump is a file whose hash
        does not match a known ROM of the same size, and the operator may well
        know why -- a translation patch, a hack, a modified Pokemon save
        editor's output. ROMarr does not get to decide that for them. What it
        does get to do is refuse silence: a forced import is recorded as forced,
        with the verdict that was overridden, so the library never claims a
        file was verified when it was not.
        """
        from .dat import BAD_DUMP, VERIFIED

        source = Path(path)
        if not source.exists():
            return {"ok": False, "reason": f"{path} does not exist"}

        platform = resolve(platform_slug) if platform_slug else None
        if platform is None:
            # Fall back to the scan's own guess, so a caller that trusts
            # ROMarr's detection does not have to restate it.
            guess = scan_directory(str(source.parent), PLATFORMS, self.dats)
            for candidate in guess.candidates:
                if candidate.filename == source.name:
                    platform = resolve(candidate.platform)
                    break
        if platform is None:
            return {"ok": False,
                    "reason": "could not tell which platform this belongs to; "
                              "choose one"}

        # The bytes get a say before anything is filed. A ROM renamed by hand
        # or by a release group is otherwise filed under whatever its name
        # claimed, and the symptom appears much later as a game that will not
        # boot. Reported rather than enforced: the operator picked this
        # platform and may have a reason, so this is a warning attached to the
        # result, not a refusal.
        from .sniff import disagrees_with

        mismatch = disagrees_with(source, platform.slug)

        target = self.library_for(platform.slug)
        if target is None:
            return {"ok": False,
                    "reason": "no library configured to import into"}
        target_cfg, target_lib = target
        label = target_cfg.get("name") or getattr(target_lib, "name", "library")

        outcomes = import_rom(source, platform, self.library_root(target_cfg),
                              dats=self.dats,
                              require_verified=False,
                              layout=self.library_layout(target_cfg),
                              translation=is_translation(source.name))

        imported, refused = [], []
        for outcome in outcomes:
            verdict = outcome.verification
            status = getattr(verdict, "status", None)

            # The rule that matters, and the one most likely to be got wrong:
            # UNKNOWN means "not in the DAT you loaded", which is the normal
            # state for homebrew, translations, and anything newer than your
            # DAT. It is not evidence of a problem and never needs forcing.
            # Only BAD_DUMP -- a hash mismatch against a known ROM -- does.
            if status == BAD_DUMP and not force:
                refused.append({
                    "file": outcome.destination and str(outcome.destination)
                            or source.name,
                    "verdict": status,
                    "detail": getattr(verdict, "detail", ""),
                    "reason": "bad dump: the file is the size of a known ROM "
                              "but its hash does not match. Import anyway only "
                              "if you know why -- a patch, a hack or a "
                              "translation will look exactly like this.",
                    "needs_force": True,
                })
                # Undo the write: refusing after landing the file leaves a
                # rejected dump in the library for somebody to find later.
                if outcome.ok and outcome.destination:
                    self._remove_imported(outcome.destination)
                continue

            if not outcome.ok:
                refused.append({"file": source.name, "verdict": status,
                                "reason": outcome.reason, "needs_force": False})
                continue

            forced = force and status == BAD_DUMP
            self._index_hash(verdict, platform.slug, source.stem,
                             outcome.destination)
            self.store.record(Event(
                kind="imported", game=source.stem, platform=platform.slug,
                release=source.name, library=label,
                detail=(f"manual import ({'FORCED over ' + str(status) if forced else status or 'unknown'})"
                        f": {outcome.destination}")))
            imported.append({
                "file": str(outcome.destination),
                "verdict": status,
                "forced": forced,
            })

        if imported and self.store.settings.get("rescan_after_import", True):
            target_lib.rescan(platform.slug)
        self.store.save()

        result = {
            "ok": bool(imported),
            "platform": platform.slug,
            "library": label,
            "imported": imported,
            "refused": refused,
        }
        if mismatch is not None:
            result["header_says"] = mismatch.platform
            result["header_detail"] = mismatch.detail
        return result

    # ------------------------------------------------------- collections --

    def _present_titles(self, platform_slug: str = "") -> dict[str, str]:
        """What the library already has, keyed by DAT-style name.

        Read from the platform's directory on disk when there is one, and only
        from the cached shelf otherwise. The shelf is a page -- a few hundred
        rows kept for the Library grid -- and comparing a 1,900-entry DAT
        against the first page of a 166,578-game library reports almost
        everything as missing. The directory is the whole truth for one
        platform and costs one listing.

        Matched by name, not by hash. Hashing a library that size to draw a
        progress bar would take hours, and the verdict recorded when ROMarr
        imported a file is already the better answer where it exists.
        """
        from .collections import PRESENT_UNKNOWN, PRESENT_VERIFIED, PRESENT_BAD

        verdicts: dict[str, str] = {}
        for event in self.store.events:
            if event.kind != "imported" or not event.game:
                continue
            detail = (event.detail or "").lower()
            if "verified" in detail:
                verdicts[event.game.lower()] = PRESENT_VERIFIED
            elif "bad-dump" in detail or "bad dump" in detail:
                verdicts[event.game.lower()] = PRESENT_BAD

        present: dict[str, str] = {}

        def note(name: str) -> None:
            if name:
                present[name] = verdicts.get(name.lower(), PRESENT_UNKNOWN)

        listed = False
        if platform_slug:
            for cfg, _ in self.game_libraries:
                folder = self.library_root(cfg) / platform_slug
                try:
                    entries = list(folder.iterdir())
                except OSError:
                    continue
                listed = True
                for entry in entries:
                    # A disc set lands as a directory named for the game; a
                    # cartridge as a file. Both answer to their stem.
                    note(entry.name if entry.is_dir() else entry.stem)
        if not listed:
            for item in (self.library_view() or {}).get("items", []):
                note(str(item.get("name") or ""))
        return present

    def dat_names(self) -> list[str]:
        return [d.name for d in self.dats.dats if d.name]

    def collection_plan(self, dat_name: str = "", platform: str = "",
                        **policy_kw) -> dict:
        """The set report: expected, present, missing, and why each dump won."""
        from .collections import Policy, build_plan

        dat = next((d for d in self.dats.dats if d.name == dat_name),
                   None) or (self.dats.dats[0] if self.dats.dats else None)
        if dat is None:
            return {"error": "no DAT loaded. Point DAT_PATH at a No-Intro or "
                             "Redump directory, or add one under Settings."}

        regions = tuple(r.lower() for r in
                        (policy_kw.get("regions")
                         or self.store.settings.get("preferred_regions")
                         or ("usa", "world", "europe", "japan")))
        # `.get(key, default)` returns None when the caller passed None
        # explicitly, which is exactly what the HTTP route does for an absent
        # query parameter -- and frozenset(None) is a 500. The plan worked
        # in-process and failed over HTTP for precisely this reason.
        exclude = policy_kw.get("exclude")
        if exclude is None:
            exclude = ("proto", "beta", "demo", "hack", "translation",
                       "unlicensed")
        translation_policy = (policy_kw.get("translation_policy")
                              or self.store.settings.get("translation_policy")
                              or "exclude")
        policy = Policy(
            regions=regions,
            one_game_one_rom=bool(policy_kw.get("one_game_one_rom", True)),
            exclude=frozenset(exclude),
            translation_policy=str(translation_policy),
        )
        plan = build_plan(dat, self._present_titles(platform), policy)
        return {
            "dat": plan.dat,
            "dat_version": plan.dat_version,
            "platform": platform,
            "counts": plan.counts(),
            "policy": {"regions": list(policy.regions),
                       "one_game_one_rom": policy.one_game_one_rom,
                       "exclude": sorted(policy.exclude),
                       "translation_policy": policy.translation_policy},
            "titles": [
                {"name": t.name, "parent": t.parent, "status": t.status,
                 "why": t.chosen_because,
                 "discarded": [{"name": n, "why": w} for n, w in t.discarded],
                 "outside_preference": t.outside_preference,
                 "translation": t.is_translation}
                for t in plan.titles
            ],
        }

    def _batches(self) -> dict:
        from .collections import Batch
        return {raw["id"]: Batch.from_dict(raw)
                for raw in self.store.list_items("batches")}

    def collection_start(self, dat_name: str, platform: str = "",
                         per_pass: int = 5, **policy_kw) -> dict:
        """Queue every missing title from a plan, resumably."""
        import uuid

        from .collections import Batch, BATCH_PENDING

        plan = self.collection_plan(dat_name, platform, **policy_kw)
        if plan.get("error"):
            return plan
        missing = [t["name"] for t in plan["titles"] if t["status"] == "missing"]
        if not missing:
            return {"ok": True, "message": "nothing missing", "queued": 0}

        batch = Batch(id=uuid.uuid4().hex[:12], platform=platform,
                      dat=plan["dat"], status=BATCH_PENDING,
                      queue=missing, per_pass=max(1, int(per_pass)))
        self.store.put_item("batches", batch.to_dict())
        self.store.save()
        return {"ok": True, "queued": len(missing), **batch.progress()}

    def collection_step(self, batch_id: str) -> dict:
        """Request the next slice. Deliberately one slice per call.

        Indexers and download clients are shared with whatever else the
        operator runs, and a 3,000-title set is not more important than their
        other traffic. Draining the queue in one pass would also mean losing
        the lot if the process stopped halfway.
        """
        from .collections import BATCH_DONE, BATCH_PAUSED, BATCH_RUNNING

        batches = self._batches()
        batch = batches.get(batch_id)
        if batch is None:
            return {"error": "no such batch"}
        if batch.status == BATCH_PAUSED:
            return {"paused": True, **batch.progress()}

        batch.status = BATCH_RUNNING
        for name in batch.take():
            try:
                self.request(name, batch.platform)
                batch.record(name, ok=True)
            except Exception as err:  # noqa: BLE001 - one title must not stop the set
                batch.record(name, ok=False,
                             reason=f"{err.__class__.__name__}: {err}")
        if not batch.queue:
            batch.status = BATCH_DONE
        self.store.put_item("batches", batch.to_dict())
        self.store.save()
        return batch.progress()

    def collection_control(self, batch_id: str, action: str) -> dict:
        from .collections import BATCH_PAUSED, BATCH_PENDING

        batches = self._batches()
        batch = batches.get(batch_id)
        if batch is None:
            return {"error": "no such batch"}
        if action == "pause":
            batch.status = BATCH_PAUSED
        elif action == "resume":
            batch.status = BATCH_PENDING
        elif action == "retry":
            batch.retry_failed()
        elif action == "cancel":
            self.store.delete_item("batches", batch_id)
            self.store.save()
            return {"ok": True, "cancelled": batch_id}
        else:
            return {"error": f"unknown action {action!r}"}
        self.store.put_item("batches", batch.to_dict())
        self.store.save()
        return batch.progress()

    def collection_status(self) -> dict:
        return {"batches": [b.progress() for b in self._batches().values()],
                "dats": self.dat_names()}

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
                # The default library root, not library_root(None): that
                # helper reads its argument, and handing it None was a 500
                # on every export whose backend rows carry no path -- which
                # is what RomM's cached rows look like. Caught live by
                # scripts/playnite_proof.ps1.
                "path": item.get("path") or item.get("full_path")
                        or (str(self.library / slug / name)
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
        by_player: dict[str, int] = {}
        download_only = 0
        for platform in PLATFORMS:
            routes = routes_for(platform, stream=self.stream,
                                players=self.players)
            if not routes.plays_without_downloading:
                download_only += 1
            for kind in set(routes.kinds):
                if kind != DOWNLOAD:
                    counts[kind] = counts.get(kind, 0) + 1
            # Counted per platform rather than per route so a platform two
            # players can open is one for each and not two of anything.
            for player in set(routes.players):
                by_player[player] = by_player.get(player, 0) + 1
        counts["download_only"] = download_only
        counts["total"] = len(PLATFORMS)
        counts["players"] = by_player
        return counts

    def moonlight_status(self) -> dict:
        """The Moonlight host, or an honest account of there not being one.

        Not folded into the stream-server row, because the two answer
        different questions and conflating them would hide the interesting
        one. A stream server that is down is broken. A Moonlight host that is
        up but whose app list ROMarr cannot read is *working perfectly* and
        still grants nothing -- and an operator needs to be told which of
        those they are looking at.
        """
        if not self.moonlight:
            return {
                "configured": False,
                "hint": ("set MOONLIGHT_HOST to a Wolf, Sunshine or Steam "
                         "Headless machine to see it here"),
                "kinds": list(MOONLIGHT_KINDS),
                "manual": PAIRING_IS_MANUAL,
            }
        return self.moonlight.status()

    def moonlight_pin(self, body: dict) -> dict:
        """Relay a PIN a human read off their own Moonlight client.

        The PIN never originates here and cannot: it is generated by the
        client, on the user's device, and both host implementations wait on a
        human to supply it. This endpoint exists so that the human types it
        into ROMarr instead of hunting for Wolf's PIN page in container logs
        or finding Sunshine's admin panel -- it saves a search, not a step.
        """
        if not self.moonlight:
            return {"ok": False, "detail": "no Moonlight host is configured"}
        return self.moonlight.submit_pin(
            str(body.get("pin") or ""),
            pair_secret=str(body.get("pair_secret") or ""),
            name=str(body.get("name") or "ROMarr"))

    def platform_directory(self) -> list[dict]:
        """Every platform with how it plays, for the API and the UI."""
        out = []
        for platform in PLATFORMS:
            routes = routes_for(platform, stream=self.stream,
                                players=self.players)
            out.append({
                "slug": platform.slug,
                "name": platform.name,
                "media": platform.media,
                "extensions": list(platform.extensions),
                "max_size_mb": platform.max_size // (1024 * 1024),
                "play_routes": list(routes.kinds),
                "players": list(routes.players),
                "plays": routes.plays_without_downloading,
                "how": routes.summary(),
            })
        return out

    def player_directory(self) -> dict:
        """Every browser player, on or off, with what it will and will not run.

        Served whole rather than filtered to the enabled ones. The disabled
        entries are the useful ones: an operator looking at a library of Flash
        that will not play needs to see Ruffle listed and switched off, not an
        absence.
        """
        return {
            "players": self.players.as_dict(),
            "order": list(self.players.order),
            "setting": "ROMARR_PLAYERS",
            "known": sorted(PLAYERS),
            # What each one can reach across the shelf ROMarr has walked. A
            # capability with no number next to it is a claim; this is the
            # measurement.
            "library": (getattr(self, "_library_facets", None)
                        or {}).get("players", {}),
        }

    def play_for(self, name: str, platform: str = "",
                 present: bool | None = None) -> dict:
        """How one file plays: every route, every player, and every refusal.

        `name` may be a filename or a bare extension, because both arrive --
        RomM reports `fs_extension` without a dot and a person pastes a
        filename. `present` defaults to True; pass False for a row the library
        server holds without the bytes, which is the majority of a large
        catalogued library and the case with the most misleading default.
        """
        routes = routes_for_file(name, platform,
                                 present=True if present is None else present,
                                 stream=self.stream, players=self.players)
        return {
            "file": name,
            "platform": routes.platform,
            "present": True if present is None else bool(present),
            "plays": routes.plays_without_downloading,
            "absent": routes.absent,
            "routes": [{"kind": r.kind, "player": r.player, "detail": r.detail}
                       for r in routes.routes],
            "alternatives": [{"kind": r.kind, "player": r.player,
                              "detail": r.detail}
                             for r in routes.alternatives],
            "how": routes.summary(),
            "stream_unreachable": routes.stream_unreachable,
        }

    # How often the count and library are refreshed in the background.
    COUNT_TTL = 300
    # How many rows each backend request fetches during the background walk.
    LIBRARY_PAGE = 1000
    # The ceiling on rows held in memory. A row is a name, a slug and a
    # cover URL -- ~200 bytes -- so a quarter million is ~50MB, which a
    # 512MB install carries comfortably. This replaced a 200-row page that
    # made big libraries render as "about the first 100 games".
    LIBRARY_MAX = 250_000

    def _player_tally(self, snapshot: list) -> dict:
        """How many rows each browser player can actually open, right now.

        The number the whole players model exists to produce, and the reason
        it is computed here rather than on request: it is one pass over the
        same 166k rows the facets are already walking, and answering it per
        page view would be a second pass per view.

        Memoised on (platform, extension, present) because a library of
        166,548 rows has fewer than three hundred distinct combinations of
        those, and `routes_for_file` is pure. Without it this loop is seconds;
        with it, it is noise.

        The stream server is deliberately not consulted. This counts *browser
        players*, and folding a stream route in would make a number about what
        a browser can do depend on whether a LAN service answered.
        """
        from collections import Counter

        counts: Counter = Counter()
        seen: dict[tuple, tuple] = {}
        absent = playable = 0
        for game in snapshot:
            # The slug, not the display name. See `Game.platform_slug` for
            # the three platforms where the difference is a wrong answer
            # rather than a cosmetic one.
            key = (str(game.platform_slug or game.platform or ""),
                   str(game.extension or ""),
                   (game.origin or "local") != "cloud")
            answer = seen.get(key)
            if answer is None:
                got = routes_for_file(key[1], key[0], present=key[2],
                                      players=self.players)
                answer = (tuple(sorted(set(got.players))),
                          bool(got.absent),
                          got.plays_without_downloading)
                seen[key] = answer
            players, is_absent, plays = answer
            for player in players:
                counts[player] += 1
            absent += is_absent
            playable += plays
        return {
            "by_player": dict(counts),
            # Named rather than derived, because "94,428 of these have no file
            # here" is the single most useful sentence about this library and
            # it must not be something a reader has to subtract to find.
            "no_file": absent,
            "plays_in_browser": playable,
            "rows": len(snapshot),
        }

    def _publish_library(self, shelf: list, error: str, *,
                         partial: bool) -> None:
        """Swap the served library snapshot, sorted the way a shelf reads.

        Platform first, then title, the way RomM and every folder frontend
        lay a library out. Sorted here once per publish rather than per
        request, because sorting 166k rows on every page view is a page
        that saccades.
        """
        from collections import Counter

        snapshot = sorted(shelf, key=lambda g: (str(g.platform).lower(),
                                                str(g.name).lower()))
        platforms = Counter(str(g.platform) or "unknown" for g in snapshot)
        # The same tally split the way the headline is split. A platform chip
        # reading "Browser 94,415" invited exactly the wrong conclusion --
        # every one of those is a row for something that is not here, and the
        # chip looked identical to "Nintendo DS 8,112", which is 8,112 files
        # on a disk. The split comes from the walked rows rather than the
        # server because no library server offers a per-platform breakdown of
        # it; that makes it a prefix while the walk runs, which is why
        # `cached` travels alongside and the UI says so.
        on_disk = Counter(str(g.platform) or "unknown" for g in snapshot
                          if (g.origin or "local") != "cloud")
        cached_platforms = [
            {"platform": name, "count": count,
             "on_disk": on_disk.get(name, 0),
             "catalogued": count - on_disk.get(name, 0)}
            for name, count in sorted(platforms.items(),
                                      key=lambda kv: (-kv[1], kv[0]))]
        # Prefer the server's own list: it is complete from the first
        # second, where the cached one grows a platform at a time and hides
        # everything the walk has not reached yet. `cached` rides along so
        # the UI can show how much of each platform is browsable now.
        authoritative = getattr(self, "_server_platforms", None)
        if authoritative:
            have = {p["platform"]: p for p in cached_platforms}
            self._library_platforms = [
                {**row,
                 "cached": have.get(row["platform"], {}).get("count", 0),
                 "on_disk": have.get(row["platform"], {}).get("on_disk", 0),
                 "catalogued": have.get(row["platform"], {}).get("catalogued", 0)}
                for row in authoritative]
        else:
            self._library_platforms = [{**p, "cached": p["count"]}
                                       for p in cached_platforms]

        # Every other axis worth cutting a 166k-game shelf on, counted once
        # per publish rather than per request. Genre and year only exist for
        # games the library server has identified, so `identified` travels
        # with them -- a genre list covering 8% of the shelf has to say so,
        # or it reads as "you own almost nothing".
        genres, regions, decades = Counter(), Counter(), Counter()
        franchises, companies = Counter(), Counter()
        # How much of the shelf a calendar can actually place. Counted here
        # with everything else so the Calendar page can state its own
        # coverage without a second pass over 166k rows.
        released = 0
        identified = 0
        for game in snapshot:
            if game.genres or game.year:
                identified += 1
            for genre in game.genres:
                genres[genre] += 1
            for region in game.regions:
                regions[region] += 1
            for franchise in game.franchises:
                franchises[franchise] += 1
            for company in game.companies:
                companies[company] += 1
            if game.released:
                released += 1
            if game.year:
                decades[f"{(game.year // 10) * 10}s"] += 1
        rank = lambda counter, limit: [        # noqa: E731
            {"value": name, "count": count}
            for name, count in sorted(counter.items(),
                                      key=lambda kv: (-kv[1], kv[0]))[:limit]]
        self._library_facets = {
            "genres": rank(genres, 40),
            "regions": rank(regions, 20),
            "franchises": rank(franchises, 40),
            "companies": rank(companies, 40),
            "released": released,
            "decades": sorted(
                ({"value": name, "count": count}
                 for name, count in decades.items()),
                key=lambda d: d["value"]),
            "origins": rank(Counter(g.origin or "local" for g in snapshot), 5),
            # One level finer than `origins`, and the answer to the question
            # that started this: a catalogued half of 94,428 is not one thing.
            # Ranked rather than capped at the four known values so a backend
            # that learns a fifth catalogue shows up here without a change.
            "sources": rank(
                Counter(g.provenance or "local" for g in snapshot), 8),
            "identified": identified,
            "players": self._player_tally(snapshot),
        }
        # Kept separately from the facet: the facet is a filter menu and gets
        # truncated, where this is a headline the Library and Stats pages both
        # read and must always be complete.
        self._library_sources = dict(
            Counter(g.provenance or "local" for g in snapshot))
        self._library_partial = partial
        self._library_cache = (snapshot, time.monotonic(), error)

    def counts(self) -> dict:
        """Badge numbers for the nav rail.

        This never calls RomM. Caching a slow call still means somebody pays
        for it whenever the cache expires, and on a large library RomM's
        /api/roms can exceed two minutes -- so the page stalled for whoever
        happened to poll first.
        """
        games = self._count_cache[0]
        split = self.library_split()
        with self._lock:
            queued = sum(1 for i in self.queue if i.state in ("queued", "grabbed"))
        # `games` stays the sum, because that is what the badge has always
        # meant and a nav badge that changed number overnight would read as
        # data loss. The two categories ride alongside it so the badge can
        # say which half is which instead of implying they are the same kind
        # of thing.
        return {"games": games,
                "games_on_disk": split["on_disk"],
                "games_catalogued": split["catalogued"],
                "missing": len(self.store.wanted), "queued": queued}

    def library_split(self) -> dict:
        """On disk here, catalogued elsewhere, and the total of both.

        The backend's own answer is preferred: it comes from the library
        server's database and is exact from the first refresh, where the
        walked shelf is a prefix of the truth for the minutes a large library
        takes to read. The shelf is the fallback for a backend that cannot
        split -- and when neither can answer, all three are None, which every
        surface renders as a dash rather than as zero.

        The three always come from the same place, and `counted_from` says
        which. Mixing them was tried and is wrong: a server total beside a
        cache-derived split gives three numbers that do not add up, and two
        of them move while the walk runs. Three consistent numbers that are
        only a prefix beat three authoritative-looking ones that disagree.
        """
        split = getattr(self, "_count_split", None) or {}
        total = split.get("total")
        if total is None:
            total = self._count_cache[0]
        on_disk, catalogued = split.get("on_disk"), split.get("catalogued")
        counted_from = "library server"
        if on_disk is None or catalogued is None:
            games, _, _ = self._library_cache
            if games:
                on_disk = sum(1 for g in games
                              if (g.origin or "local") != "cloud")
                catalogued = len(games) - on_disk
                total = len(games)
                counted_from = "cached rows"
            else:
                counted_from = ""
        return {"total": total, "on_disk": on_disk, "catalogued": catalogued,
                "counted_from": counted_from,
                "sources": dict(getattr(self, "_library_sources", {}) or {})}


    #: Shelves Discover builds out of the library you already have. This
    #: exists because the alternative -- RAWG or IGDB -- needs an API key
    #: nobody has on day one, so Discover was an empty page saying "add a
    #: provider". Your own library already knows its genres, ratings and
    #: years; browsing it is worth as much as browsing a storefront, and it
    #: works with nothing configured.
    LIBRARY_SHELVES = ("top-rated", "recent", "recently-added", "hidden-gems",
                       "by-genre", "by-franchise", "by-company", "multiplayer",
                       "anniversary")

    #: Which `Game` field each `by-*` shelf cuts on. One table rather than a
    #: branch each, because the shelves differ only in the field they read
    #: and the facet list they offer back.
    SHELF_FACETS = {"by-genre": "genres", "by-franchise": "franchises",
                    "by-company": "companies"}

    @staticmethod
    def _shelf_row(game) -> dict:
        """One game as Discover renders it.

        Everything here comes off the library server's own metadata. Nothing
        is looked up elsewhere and nothing is guessed, so a field that is
        empty means the server does not know it -- which is the normal case
        for most of a large, half-identified library.
        """
        return {"id": game.id, "title": game.name, "platform": game.platform,
                "cover_url": game.cover, "rating": game.rating,
                "year": game.year, "released": game.released,
                "added": game.added, "genres": list(game.genres),
                "franchises": list(game.franchises),
                "companies": list(game.companies[:3]),
                "modes": list(game.modes), "players": game.players,
                "origin": game.origin, "owned": True}

    def discover_library(self, shelf: str = "top-rated", genre: str = "",
                         value: str = "", limit: int = 40) -> dict:
        """Browse what you own, the way a storefront browses what it sells."""
        games, _, _ = self._library_cache
        if not games:
            return {"shelf": shelf, "items": [], "genres": [], "facet": [],
                    "error": "the library has not been read yet"}

        # `genre` is the older name for the same thing and still arrives from
        # bookmarked URLs; the shelves added since needed a name that was not
        # a lie for a company or a franchise.
        chosen = (value or genre or "").strip()
        rated = [g for g in games if g.rating > 0]
        facet_field = self.SHELF_FACETS.get(shelf, "")
        if shelf == "top-rated":
            rows = sorted(rated, key=lambda g: (-g.rating, g.name.lower()))
        elif shelf == "recent":
            rows = sorted((g for g in games if g.year),
                          key=lambda g: (-g.year, g.name.lower()))
        elif shelf == "recently-added":
            # What the library server took in most recently, which is a
            # different question from what came out most recently and the one
            # people actually ask after a scan finishes.
            rows = sorted((g for g in games if g.added),
                          key=lambda g: (g.added, g.name.lower()),
                          reverse=True)
        elif shelf == "hidden-gems":
            # Well-reviewed games on platforms you own few of -- the ones a
            # 166k-game shelf buries and a "top rated" list never surfaces.
            from collections import Counter
            per_platform = Counter(g.platform for g in games)
            rows = sorted(
                (g for g in rated if g.rating >= 7.5
                 and per_platform[g.platform] < 500),
                key=lambda g: (-g.rating, g.name.lower()))
        elif shelf == "multiplayer":
            # RomM records game modes, and "what can we play together" is the
            # question a shared library gets asked most.
            rows = sorted(
                (g for g in games
                 if any("player" in m.lower() or "co-op" in m.lower()
                        or "cooperative" in m.lower() or "multiplayer" in m.lower()
                        for m in g.modes)
                 and not (len(g.modes) == 1
                          and g.modes[0].lower() == "single player")),
                key=lambda g: (-g.rating, g.name.lower()))
        elif shelf == "anniversary":
            # Games that came out this day of the year, any year. The same
            # data the Calendar is built on, offered here because it is the
            # best "show me something I forgot I owned" there is.
            import datetime
            today = datetime.date.today().strftime("-%m-%d")
            rows = sorted((g for g in games if g.released.endswith(today)),
                          key=lambda g: (-g.rating, g.name.lower()))
        elif facet_field:
            low = chosen.lower()
            rows = sorted(
                (g for g in games
                 if any(x.lower() == low for x in getattr(g, facet_field))),
                key=lambda g: (-g.rating, g.name.lower())) if low else []
        else:
            return {"shelf": shelf, "items": [],
                    "error": f"unknown shelf; one of "
                             f"{', '.join(self.LIBRARY_SHELVES)}"}

        facets = getattr(self, "_library_facets", {})
        # The facet list belongs to the shelf being browsed: offering genres
        # while somebody browses franchises is a list of buttons that do
        # nothing. `genres` stays alongside it because the UI and anything
        # written against the older shape still read that key.
        facet_key = {"genres": "genres", "franchises": "franchises",
                     "companies": "companies"}.get(facet_field, "")
        return {
            "shelf": shelf,
            "genre": chosen,
            "value": chosen,
            "genres": [g["value"] for g in facets.get("genres", [])][:24],
            "facet": [g["value"] for g in facets.get(facet_key, [])][:40],
            "total": len(rows),
            "items": [self._shelf_row(g) for g in rows[:limit]],
        }

    #: What the Calendar can be pointed at. Each is a date the library server
    #: genuinely holds; there is deliberately no fifth view for "coming soon"
    #: sourced from anywhere else.
    CALENDAR_VIEWS = ("releases", "added", "updated", "upcoming")

    #: Which `Game` field each view reads. `releases` matches on month and day
    #: across every year -- an anniversary -- where the other two are real
    #: dates in a real month.
    CALENDAR_FIELDS = {"releases": "released", "added": "added",
                       "updated": "updated", "upcoming": "released"}

    def library_calendar(self, decade: str = "", view: str = "releases",
                         month: str = "", day: str = "",
                         limit: int = 200) -> dict:
        """A real calendar over the dates the library server actually holds.

        This used to be a bar chart of release years, and the Calendar page
        did not call it at all -- the page asked a metadata provider that
        needs an API key, so a fully-populated library rendered "add a
        metadata provider" forever. The dates were sitting in RomM the whole
        time.

        Three of them, and they answer different questions:

          * `releases` -- what came out on this day of the year, in any year.
            Anniversaries, because a ROM library is entirely back catalogue
            and "20 years ago today" is the only forward-looking thing it can
            honestly offer.
          * `added` -- when the library server took each file in.
          * `updated` -- when it last changed the row, which is when a
            metadata scan touched it.

        `upcoming` is the fourth and is usually empty on purpose. RomM has no
        concept of an unreleased game: every row is a file on disk or a
        catalogued entry for something that already shipped. If a row does
        carry a future release date this shows it, and if none do it says so
        rather than filling the page from somewhere else.
        """
        import calendar as calmod
        import datetime
        from collections import Counter

        games, _, _ = self._library_cache
        if not games:
            return {"years": [], "items": [], "days": [],
                    "error": "the library has not been read yet"}

        view = view if view in self.CALENDAR_VIEWS else "releases"
        field = self.CALENDAR_FIELDS[view]
        today = datetime.date.today()

        # One pass for the two things that have to be known before the month
        # can be chosen. Deliberately not a list comprehension building a
        # second 166k-element list per page view -- the shelf is already the
        # biggest thing in the process and this runs on every click.
        latest = ""
        dated = 0
        for game in games:
            stamp = getattr(game, field)
            if not stamp:
                continue
            dated += 1
            if stamp > latest:
                latest = stamp
        # Said out loud on every response. A calendar drawn over the
        # identified fraction of a library, presenting itself as the whole
        # library, is the quiet kind of lie this project exists to avoid:
        # somebody concludes their collection has no 1993 in it when really
        # nothing from 1993 was ever identified.
        coverage = {"dated": dated, "total": len(games), "field": field}

        # The month being browsed. Defaults to the one with something in it
        # rather than to today: a library imported in one batch has nothing
        # in the current month, and an empty grid reads as a broken page.
        if view == "releases":
            # Anniversaries ignore the year for matching, but the grid still
            # needs one to know how long the month is and which weekday it
            # starts on -- so it pages through the current year.
            head = f"{today.year:04d}-{today.month:02d}"
        else:
            head = latest[:7] or f"{today.year:04d}-{today.month:02d}"
        try:
            cursor = datetime.date.fromisoformat(
                f"{str(month or '').strip() or head}-01")
        except ValueError:
            cursor = datetime.date(today.year, today.month, 1)
        # Everything below matches against this rather than the caller's
        # string: a malformed ?month= falls back to a real month, and a
        # filter still reading the original would then match nothing and
        # render a working month as empty.
        month = f"{cursor.year:04d}-{cursor.month:02d}"
        first_weekday, length = calmod.monthrange(cursor.year, cursor.month)

        if view == "upcoming":
            horizon = today.isoformat()
            rows = sorted((g for g in games if g.released > horizon),
                          key=lambda g: (g.released, g.name.lower()))
            counts: Counter = Counter()
        else:
            # Matching by slice rather than by parsing a date: this walks the
            # whole shelf and a `date.fromisoformat` per row is the
            # difference between a page and a pause. `releases` compares only
            # the month segment because an anniversary is any year's.
            segment = f"{cursor.month:02d}" if view == "releases" else month
            span = slice(5, 7) if view == "releases" else slice(0, 7)
            # An undated row slices to "" and matches nothing, so it drops
            # out here rather than needing a filtered copy of the shelf.
            in_month = [g for g in games if getattr(g, field)[span] == segment]
            counts = Counter(int(getattr(g, field)[8:10] or 0)
                             for g in in_month)
            counts.pop(0, None)
            chosen = self._calendar_day(day, cursor, length, counts, today)
            rows = sorted((g for g in in_month
                           if getattr(g, field)[8:10] == f"{chosen:02d}"),
                          key=lambda g: (-g.rating, g.name.lower()))

        years = Counter(g.year for g in games if g.year)
        out = {
            "view": view,
            "views": list(self.CALENDAR_VIEWS),
            "today": today.isoformat(),
            "month": month,
            "month_name": calmod.month_name[cursor.month],
            # Monday-first, matching `calendar.monthrange`, so the UI can lay
            # a seven-column grid without recomputing the offset itself.
            "first_weekday": first_weekday,
            "days_in_month": length,
            "days": [{"day": n, "count": counts.get(n, 0)}
                     for n in range(1, length + 1)],
            "coverage": coverage,
            "decade": decade,
            "years": [{"year": y, "count": c} for y, c in sorted(years.items())],
            "decades": sorted({f"{(y // 10) * 10}s" for y in years}),
        }
        if view == "upcoming":
            out["day"] = ""
            out["items"] = [self._shelf_row(g) for g in rows[:limit]]
            if not rows:
                # Unchanged in substance now that a metadata provider can
                # answer the same question: this view is about the library,
                # the library has nothing unreleased in it, and the provider
                # shelf underneath is a different claim about different
                # games. Merging the two would be the easy lie -- a page that
                # looks like the operator has a release schedule when what
                # they have is somebody else's catalogue.
                out["note"] = ("RomM holds no unreleased titles. Every row in "
                               "a ROM library is a file, or a catalogue entry "
                               "for a game that already shipped, so there is "
                               "no release schedule to show -- and ROMarr "
                               "will not invent one. Anything under Elsewhere "
                               "came from a metadata provider's catalogue and "
                               "is owned by nobody here.")
            return out
        out["selected_day"] = chosen
        # An anniversary has no year, so it is not dressed up as a date. The
        # other two views are looking at one real month and say which.
        out["day"] = (f"{cursor.month:02d}-{chosen:02d}" if view == "releases"
                      else f"{month}-{chosen:02d}")
        if view == "releases":
            out["note"] = ("Anniversaries: what came out on this day, in any "
                           "year. Providers that only knew the year store it "
                           "as January 1st, so the first of each month is "
                           "heavier than the rest.")
        # `decade` keeps its original meaning: a decade beats the day grid,
        # because somebody who asked for the 1990s wants the 1990s.
        if decade:
            try:
                start = int(str(decade).rstrip("s"))
            except ValueError:
                start = 0
            if start:
                rows = sorted(
                    (g for g in games if g.year and start <= g.year < start + 10),
                    key=lambda g: (-g.year, -g.rating, g.name.lower()))
        out["items"] = [self._shelf_row(g) for g in rows[:limit]]
        return out

    @staticmethod
    def _calendar_day(day: str, cursor, length: int, counts, today) -> int:
        """Which square of the month to open, given what the caller asked for.

        Falls back to today, and to the busiest day in the month when today
        has nothing on it -- landing on an empty square is how a calendar
        with plenty in it looks empty. Measured against a real library that
        was not hypothetical: the Updated view opened on the 11th, which was
        blank, while the 4th of the same month held 11,155 rows.
        """
        wanted = str(day or "").strip()
        if wanted:
            # Accepts a bare day, an anniversary `MM-DD` and a full date, so
            # a link from any of the three views works in any of them.
            tail = wanted.split("-")[-1]
            if tail.isdigit() and 1 <= int(tail) <= length:
                return int(tail)
        if ((today.year, today.month) == (cursor.year, cursor.month)
                and counts.get(today.day)):
            return today.day
        if counts:
            return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        return today.day if (today.year, today.month) == (cursor.year,
                                                          cursor.month) else 1

    def peer_shelf(self, peer) -> list[dict]:
        """The projection this peer is allowed to see, from the live cache."""
        games, _, _ = self._library_cache
        return self.federation.project(peer, games or [])

    def peer_request(self, peer, title: str, platform: str) -> dict:
        """A peer asking me to acquire something.

        Allowed only for `fetch`; a `catalogue` peer sees the shelf and
        acquires through its OWN indexers, which is the safe default and
        the reason catalogue exists as a separate grant.
        """
        if peer.access != "fetch":
            return {"ok": False,
                    "error": "this peer may see the catalogue but not "
                             "request downloads"}
        return self.request(title, platform)

    # ------------------------------------------------- the outbound half --
    #
    # Everything above answers a peer. Everything below CALLS one. The two
    # halves use the same credential in opposite directions: the invitation
    # secret is stored as `token` on both sides, so no second exchange is
    # needed to talk back.

    def _index_hash(self, verdict, platform: str, fallback: str,
                    destination=None) -> None:
        """Record an imported file's hash, if verification produced one.

        Import is where new ROMs arrive, so without this the index only ever
        reflects the last audit and netplay would go stale the moment the
        acquisition pipeline did its job.
        """
        rom = getattr(verdict, "rom", None)
        sha1 = str(getattr(rom, "sha1", "") or "")
        if not sha1:
            return
        from .dat import VERIFIED
        self.hashes.add(sha1, getattr(verdict, "game", "") or fallback,
                        platform, getattr(verdict, "status", "") == VERIFIED,
                        str(destination or ""))
        self.hashes.save()

    #: Progress of the background hash seed, so the UI can show it moving
    #: instead of holding a request open for twenty minutes.
    _seed_thread = None
    _seed_state: dict = {"status": "idle", "seen": 0, "added": 0}

    def seed_status(self) -> dict:
        state = dict(ROMarr._seed_state)
        state["total"] = len(self.hashes)
        state["running"] = bool(ROMarr._seed_thread
                                and ROMarr._seed_thread.is_alive())
        return state

    def start_hash_seed(self, *, quiet: bool = False) -> dict:
        """Seed hashes in the background.

        Reading 166,578 entries out of RomM in pages is minutes of HTTP. Doing
        it inside the request meant the page sat there loading, which is not a
        thing anybody should have to wait through -- and it is the reason this
        runs itself at startup rather than waiting to be asked.
        """
        import threading as _threading

        if ROMarr._seed_thread is not None and ROMarr._seed_thread.is_alive():
            return {"ok": True, "already": True, **self.seed_status(),
                    "detail": "Already reading your library."}

        ROMarr._seed_state = {"status": "running", "seen": 0, "added": 0,
                              "started": datetime.now(timezone.utc)
                              .isoformat(timespec="seconds")}

        def run():
            try:
                result = self.index_hashes_from_library(
                    progress=ROMarr._seed_state)
                ROMarr._seed_state.update(result, status="done")
            except Exception as err:                # noqa: BLE001 - reported
                log.warning("hash seed failed: %s", err)
                ROMarr._seed_state.update(status="failed", error=str(err))

        ROMarr._seed_thread = _threading.Thread(
            target=run, name="romarr-hash-seed", daemon=True)
        ROMarr._seed_thread.start()
        if not quiet:
            log.info("reading hashes from the library in the background")
        return {"ok": True, "started": True, **self.seed_status(),
                "detail": "Reading your library in the background. This page "
                          "does not have to stay open."}

    def index_hashes_from_library(self, progress: dict | None = None) -> dict:
        """Fill the hash index from the library server, not from disk.

        RomM stores a SHA1 against every rom it has scanned and serves it on
        the normal listing. Reading that is seconds of HTTP against a library
        an audit would need hours to walk -- and on a shelf of 166,578 entries
        the audit is not a thing anybody actually runs, which meant netplay
        was theoretically supported and practically unusable.

        These entries are recorded as UNVERIFIED. RomM's hash says what the
        file is; it does not say anybody checked it against No-Intro or
        Redump. An audit still upgrades them, and `HashIndex.add` prefers the
        verified copy when one turns up.
        """
        library = getattr(self, "library_server", None) or self.romm
        client = getattr(library, "_client", library)
        if not hasattr(client, "hashes"):
            return {"ok": False,
                    "error": "the attached library server does not publish "
                             "file hashes, so netplay cannot be seeded from "
                             "it -- run an audit instead"}
        from .dat import VERIFIED

        added = offset = seen = confirmed = 0
        truncated = ""
        while offset < 500_000:
            try:
                page, raw = client.hashes(limit=500, offset=offset)
            except Exception as err:                # noqa: BLE001 - reported
                if not added:
                    return {"ok": False, "error": f"library read failed: {err}"}
                # Partial is better than nothing, but a partial read that
                # reports plain success is how 63,500 of 166,578 looked like
                # a complete answer. Say where it stopped and why.
                log.warning("hash seed stopped at offset %d of the library: "
                            "%s", offset, err)
                if progress is not None:
                    progress["stopped_early"] = True
                    progress["error"] = str(err)
                truncated = str(err)
                break
            if not raw:
                break
            seen += raw
            for row in page:
                # The library server's hash says what a file IS. Whether it
                # is a good dump is a different claim, and only a DAT can
                # make it -- so check here rather than leaving the whole
                # index unverified until somebody runs an hours-long audit
                # over a library this size. That audit still upgrades what
                # a DAT does not cover; this just stops it being the only
                # way to ever see a verified flag.
                good = row["verified"]
                if not good and self.dats is not None:
                    good = self.dats.lookup(sha1=row["sha1"]).status == VERIFIED
                    if good:
                        confirmed += 1
                if self.hashes.add(row["sha1"], row["name"], row["platform"],
                                   good, row.get("path", "")):
                    added += 1
            if progress is not None:
                progress["seen"], progress["added"] = seen, added
                # Reported live, not only in the final result. Without this
                # the page showed "0 verified" for the twenty minutes the
                # walk takes while the flags were in fact being written --
                # which reads as a broken feature rather than a running one.
                progress["verified"] = confirmed
                # Save as we go: a restart part-way through should keep the
                # work already done rather than starting from nothing. It
                # lost 30,000 hashes to a restart once, which is exactly the
                # kind of thing that makes a feature feel unreliable.
                if seen % 2_500 < 500:
                    self.hashes.save()
            # Paginate on what the SERVER returned, never on what survived
            # filtering -- most of a large library has no hash, and a short
            # filtered page is the normal case rather than the last one.
            if raw < 500:
                break
            offset += 500
        self.hashes.save()
        log.info("seeded the hash index from the library: %d dump(s) with a "
                 "hash out of %d entries; %d known in total",
                 added, seen, len(self.hashes))
        without = max(0, seen - added)
        return {"ok": True, "added": added, "seen": seen,
                "total": len(self.hashes), "verified": confirmed,
                "stopped_early": bool(truncated), "error": truncated,
                "detail": (f"Stopped after {seen:,} entries: {truncated}. "
                           f"{added:,} hashes were kept; run it again to "
                           f"continue." if truncated else
                           f"Read {seen:,} entries and found {added:,} with a "
                          f"hash. Netplay can match those. The other "
                           f"{without:,} are catalogued entries your library "
                           f"has never scanned off a disk, so there is "
                           f"nothing to match them on. "
                           + (f"{confirmed:,} matched a No-Intro or Redump "
                              f"dump and are marked verified."
                              if confirmed else
                              "None matched a DAT, so all are unverified -- "
                              "check the DAT directory under Settings."))}

    def save_peers(self) -> None:
        """Persist relationships. Called after every change to one."""
        self.store.settings["_peers"] = self.federation.dump()
        self.store.save()

    def claim_invitation(self, link: str, code: str) -> dict:
        """Finish a friend's invitation from a link and a typed claim code.

        `/peer/redeem` stores a relationship and stops -- the inviter never
        hears about it, and the first thing either side notices is a shelf
        that answers 401. That is survivable when the blob already contains
        the durable token. It is not survivable here: a claim code is worth
        nothing after it is spent, so the exchange has to happen now or the
        friend is left holding eight dead characters.

        So this one calls the server named in the link, hands over the code,
        and keeps the token that comes back. The address it calls is read
        from the link rather than typed, which is why the UI shows it before
        this runs: a link is a thing somebody else wrote.
        """
        import requests as _rq

        from .federation import Peer, parse_invite_link
        try:
            invite = parse_invite_link(link)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not str(code or "").strip():
            return {"ok": False,
                    "error": "the claim code is missing. It travels "
                             "separately from the link on purpose -- ask your "
                             "friend for the eight characters"}
        # Sent so the inviter's Friends page can call back. Read fresh from
        # settings for the same reason minting does: an operator who set a
        # public URL five minutes ago should not have to restart to use it.
        mine = str(self.store.settings.get("public_url")
                   or self.federation.url or "").strip().rstrip("/")
        try:
            response = _rq.post(
                invite["url"] + "/api/v1/peer/accept",
                json={"peer_id": invite["peer_id"], "secret": str(code),
                      "name": self.federation.name, "url": mine},
                timeout=20)
        except _rq.RequestException as err:
            return {"ok": False,
                    "error": f"could not reach {invite['url']}: {err}"}
        body = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok:
            return {"ok": False,
                    "error": str(body.get("error")
                                 or f"{invite['url']} answered HTTP "
                                    f"{response.status_code}")}
        token = str(body.get("token") or "")
        if not token:
            # An older ROMarr accepts the handshake and returns no token, so
            # the relationship would look made and authenticate as nobody
            # forever. Refuse loudly instead of storing a peer that cannot
            # work.
            return {"ok": False,
                    "error": "that server completed the handshake but sent no "
                             "token back, so there is nothing to talk to it "
                             "with. It is probably running a ROMarr from "
                             "before invitation links -- ask for the older "
                             "pasted invitation instead"}
        peer = Peer(peer_id=invite["peer_id"],
                    name=(str(body.get("name") or "") or invite["name"]
                          or invite["url"]),
                    url=str(body.get("url") or "") or invite["url"],
                    token=token,
                    # Confirmed here, like a redeemed blob: I typed the code
                    # my friend gave me, which is the consent the second step
                    # exists to establish. Their side still holds me
                    # unconfirmed until they say so.
                    confirmed=True)
        self.federation.peers[peer.peer_id] = peer
        self.save_peers()
        out = {"ok": True, "peer_id": peer.peer_id, "name": peer.name,
               "detail": f"Connected to {peer.name}. They confirm you on "
                         f"their side before you can see anything."}
        if not mine:
            out["warning"] = ("Your ROMarr has no public URL set, so your "
                              "friend's server has no address to call you "
                              "back on. Set it under Settings -> General.")
        return out

    class _RommDump:
        """One of a RomM friend's files, shaped like a hash-index entry.

        netplay_answer reads `.sha1`, `.name` and `.verified` off whatever it
        is handed. Adapting here means a RomM friend goes through the exact
        same verdict logic as a ROMarr one rather than a parallel copy that
        could drift into disagreeing about what "mismatch" means.
        """

        __slots__ = ("sha1", "name", "verified")

        def __init__(self, row):
            self.sha1 = row.get("sha1", "")
            self.name = row.get("title", "")
            self.verified = bool(row.get("verified"))

    def _romm_friend_client(self, peer):
        """A RomM client pointed at a friend's server, using their account."""
        from .clients import Romm, RommConfig
        return Romm(RommConfig(base_url=peer.url, username=peer.username,
                               password=peer.password,
                               api_token=peer.token if not peer.username
                               else "", timeout=30))

    #: A RomM friend's library is read in pages of this size. Larger than the
    #: poster grid uses because nothing here renders -- it is hashes and
    #: titles going into a cache.
    ROMM_FRIEND_PAGE = 500
    ROMM_FRIEND_MAX = 10_000

    def _romm_friend_rows(self, peer) -> tuple[list, str]:
        """A RomM friend's shelf, in the same shape a ROMarr peer projects.

        Projected to exactly the ROMarr peer fields on purpose: the rest of
        the app should not need to know which kind of friend it is looking
        at, and a RomM row carries far more (paths, ids) that has no business
        being in a friends view.
        """
        client = self._romm_friend_client(peer)
        rows, offset = [], 0
        while offset < self.ROMM_FRIEND_MAX:
            try:
                page, raw = client.hashes(limit=self.ROMM_FRIEND_PAGE,
                                          offset=offset)
            except Exception as err:            # noqa: BLE001 - reported
                if rows:
                    break                        # partial is better than none
                return [], f"could not read {peer.name}: {err}"
            if not raw:
                break
            for row in page:
                rows.append({"title": row["name"], "platform": row["platform"],
                             "year": 0, "verified": False,
                             "origin": peer.name,
                             #: Carried for netplay, not for display. It is a
                             #: fact about a file, not a credential.
                             "sha1": row["sha1"]})
            if len(page) < self.ROMM_FRIEND_PAGE:
                break
            offset += self.ROMM_FRIEND_PAGE
        return rows, ""

    def _friend(self, peer_id: str):
        """The peer I am about to call, or a reason I cannot."""
        peer = self.federation.peers.get(str(peer_id or ""))
        if peer is None:
            return None, "no such friend"
        if not peer.confirmed:
            return None, ("this friend is not confirmed yet -- confirm them "
                          "before asking their server for anything")
        if not peer.url:
            return None, ("this friend's invitation carried no address, so "
                          "there is nowhere to call. Ask them to set their "
                          "public URL and send a fresh invitation.")
        return peer, ""

    def _call_friend(self, peer, method: str, path: str, payload=None,
                     timeout: int = 20):
        """One request to a friend's server, as a server rather than a user."""
        import requests as _rq
        url = peer.url.rstrip("/") + path
        try:
            response = _rq.request(method, url, headers=peer.headers(),
                                   json=payload, timeout=timeout)
        except _rq.RequestException as err:
            return None, f"could not reach {peer.name}: {err}"
        if response.status_code == 401:
            return None, (f"{peer.name} refused the credential -- they may "
                          f"have removed you, or not confirmed you yet")
        if not response.ok:
            return None, f"{peer.name} answered HTTP {response.status_code}"
        try:
            return response.json(), ""
        except ValueError:
            return None, f"{peer.name} sent something that was not JSON"

    #: A friend's shelf is refetched at most this often. Their library does
    #: not change per keystroke, and filtering happens here.
    FRIEND_SHELF_TTL = 120.0

    def friend_shelf(self, peer_id: str, *, q: str = "", platform: str = "",
                     offset: int = 0, limit: int = 200,
                     refresh: bool = False) -> dict:
        """Browse what a friend shares with me.

        Fetched once and filtered locally: the projection is capped at 5,000
        rows and asking their server again for every keystroke would make
        somebody else's install pay for my typing.
        """
        peer, why = self._friend(peer_id)
        if peer is None:
            return {"ok": False, "error": why, "items": [], "total": 0}

        cached = self._friend_shelves.get(peer.peer_id)
        if refresh or cached is None or \
                (time.monotonic() - cached[1]) > self.FRIEND_SHELF_TTL:
            if peer.kind == "romm":
                rows, err = self._romm_friend_rows(peer)
                if err and cached is None:
                    return {"ok": False, "error": err, "items": [],
                            "total": 0}
                if not err:
                    self._friend_shelves[peer.peer_id] = (rows,
                                                          time.monotonic())
                rows = self._friend_shelves[peer.peer_id][0]
                return {**self._slice_shelf(rows, q, platform, offset, limit),
                        "ok": True, "friend": peer.name, "kind": "romm",
                        "access": "catalogue"}
            body, err = self._call_friend(peer, "GET", "/api/v1/peer/shelf")
            if body is None:
                if cached is None:
                    return {"ok": False, "error": err, "items": [], "total": 0}
                # Serve what we last saw rather than blanking the page.
                rows, at = cached
                return {**self._slice_shelf(rows, q, platform, offset, limit),
                        "ok": True, "stale": True, "error": err,
                        "friend": peer.name}
            rows = [r for r in (body.get("items") or []) if isinstance(r, dict)]
            self._friend_shelves[peer.peer_id] = (rows, time.monotonic())
        rows = self._friend_shelves[peer.peer_id][0]
        return {**self._slice_shelf(rows, q, platform, offset, limit),
                "ok": True, "friend": peer.name, "access": peer.access}

    @staticmethod
    def _slice_shelf(rows, q: str, platform: str, offset: int,
                     limit: int) -> dict:
        needle = str(q or "").strip().lower()
        slug = str(platform or "").strip().lower()
        matched = [r for r in rows
                   if (not needle or needle in str(r.get("title", "")).lower())
                   and (not slug or str(r.get("platform", "")).lower() == slug)]
        platforms = sorted({str(r.get("platform", "")) for r in rows if
                            r.get("platform")})
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 200), 1000))
        return {"items": matched[offset:offset + limit],
                "total": len(matched), "shelf_total": len(rows),
                "offset": offset, "limit": limit, "platforms": platforms}

    def friend_want(self, peer_id: str, title: str, platform: str) -> dict:
        """Add something off a friend's shelf to MY wanted list.

        This is what `catalogue` access means and why it is the default: I
        saw it at theirs, and my own indexers go and find it. Nothing is
        fetched from my friend, so seeing a shelf never turns the person
        showing it into a distributor.
        """
        peer, why = self._friend(peer_id)
        if peer is None:
            return {"ok": False, "error": why}
        if not str(title or "").strip():
            return {"ok": False, "error": "no title"}
        self.store.want(title, platform)
        self.store.record(Event(
            kind="wanted", game=title, platform=platform,
            detail=f"added from {peer.name}'s shelf"))
        self.store.save()
        return {"ok": True, "title": title, "platform": platform,
                "detail": f"added to Wanted -- your indexers will look for it"}

    def netplay_invite(self, peer_id: str, title: str,
                       platform: str = "") -> dict:
        """Offer a friend a game, and report honestly what came back.

        The offer carries the SHA1 of my copy, which is the whole point: a
        title agrees far too easily, and two servers agreeing on a title and
        not the bytes is the failure this exists to prevent.
        """
        peer, why = self._friend(peer_id)
        if peer is None:
            return {"ok": False, "status": "error", "detail": why}

        entry = self.hashes.for_game(title, platform)
        if entry is None:
            return {
                "ok": False, "status": "unhashed",
                "detail": ("ROMarr has not hashed this game yet, so it cannot "
                           "prove which dump you have. Run an audit of "
                           f"{platform or 'this platform'} under Tasks and "
                           "try again."),
            }

        from .playability import LOCAL, routes_for
        in_browser = LOCAL in routes_for(platform or entry.platform).kinds

        offer = {"title": entry.name, "platform": entry.platform,
                 "sha1": entry.sha1, "verified": entry.verified,
                 "host": self.federation.name}

        if peer.kind == "romm":
            # Their RomM cannot answer an offer, so ROMarr answers it on
            # their behalf from the hashes RomM already publishes. The
            # verdict is decided by exactly the same function, so a RomM
            # friend and a ROMarr friend cannot disagree about what the four
            # words mean.
            rows, err = (self._friend_shelves.get(peer.peer_id, ([], 0))[0],
                         "")
            if not rows:
                rows, err = self._romm_friend_rows(peer)
                if not err:
                    self._friend_shelves[peer.peer_id] = (rows,
                                                          time.monotonic())
            if err:
                return {"ok": False, "status": "error", "detail": err}
            theirs = [_RommDump(r) for r in rows if r.get("sha1")]
            body = self.federation.netplay_answer(offer, theirs)
        else:
            body, err = self._call_friend(peer, "POST",
                                          "/api/v1/peer/netplay",
                                          {"offer": offer})
            if body is None:
                return {"ok": False, "status": "error", "detail": err}

        status = str(body.get("status") or "")
        answer = {
            "ok": status in ("ready", "unverified"),
            "status": status,
            "friend": peer.name,
            "title": entry.name,
            "platform": entry.platform,
            "sha1": entry.sha1,
            "detail": str(body.get("detail") or ""),
        }
        if answer["ok"]:
            answer["room"] = self.federation.netplay_room(peer.peer_id,
                                                          entry.sha1)
            # Say plainly when the match is good but the machine has no
            # in-browser core: agreeing on the bytes does not conjure an
            # emulator, and claiming otherwise would be the same overreach
            # netplay-by-title makes.
            answer["in_browser"] = in_browser
            if not in_browser:
                answer["detail"] = (
                    f"You both have this exact dump, but EmulatorJS has no "
                    f"core for {entry.platform} -- you will need a native "
                    f"emulator that can join a room.")
        return answer

    def stats(self) -> dict:
        """The numbers page: what this install has actually done.

        Everything here is read from state already in memory -- events, the
        wanted list, the shelf metadata -- so the page costs nothing and can
        never stall behind a slow library backend.
        """
        from collections import Counter

        events = list(self.store.events)
        by_kind = Counter(e.kind for e in events)
        imported_by_platform = Counter(
            e.platform for e in events if e.kind == "imported" and e.platform)
        grabbed_by_indexer = Counter(
            e.indexer for e in events if e.kind == "grabbed" and e.indexer)
        meta = self.store.all_game_meta()
        ratings = [int(m["rating"]) for m in meta if m.get("rating")]
        statuses = Counter(m["status"] for m in meta if m.get("status"))
        games, _ = self._count_cache
        split = self.library_split()
        return {
            "version": VERSION,
            "update_available": bool(self.store.settings.get("_update_available")),
            "latest_version": self.store.settings.get("_latest_version", ""),
            "uptime_seconds": int(time.monotonic() - self._started),
            "library_games": games,
            # "Games in library" was this page's headline number and it was
            # the sum of two unlike things. It stays, because a stat page
            # that renumbered itself would be its own confusion, but it no
            # longer stands alone: these say how many of those are files
            # here, how many are catalogue rows, and which catalogues.
            "library_on_disk": split["on_disk"],
            "library_catalogued": split["catalogued"],
            "library_sources": split["sources"],
            "wanted": len(self.store.wanted),
            "events": dict(by_kind),
            "imported_by_platform": dict(imported_by_platform.most_common(15)),
            "grabbed_by_indexer": dict(grabbed_by_indexer.most_common(10)),
            "statuses": dict(statuses),
            "rated": len(ratings),
            "average_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
        }

    #: One audit at a time; the state the UI polls.
    _audit_state: dict = {}
    _audit_thread = None

    def audit_library(self, platform_slug: str) -> dict:
        """Verify what is already on the shelf, one platform at a time.

        The import path verifies files at the border; this walks files that
        were already there -- hashed against the loaded DATs, so the answer
        is the same three verdicts an import gets: verified, bad dump (the
        one worth finding: right size, wrong bytes -- bit rot or a tampered
        file), unknown (homebrew, translations, a stale DAT). Duplicates
        are reported by HASH, because two byte-identical files are a dupe
        whatever their names, and two different dumps of one game are not.

        One platform per run, in its own thread: hashing a whole 166k-game
        library in one go is a job measured in hours, and an audit that
        blocks the scheduler starves every import while it runs.
        """
        import threading as _threading

        if self._audit_thread is not None and self._audit_thread.is_alive():
            return {"error": "an audit is already running",
                    **self._audit_state}
        platform = resolve(platform_slug)
        if platform is None:
            return {"error": f"unknown platform: {platform_slug!r}"}
        if self.dats is None:
            return {"error": "no DATs loaded -- set the DAT directory under "
                             "Settings -> Media Management first"}

        root = self.library / platform.slug
        state = {"status": "running", "platform": platform.slug,
                 "scanned": 0, "verified": 0, "bad": 0, "unknown": 0,
                 "bad_files": [], "duplicates": [],
                 "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        ROMarr._audit_state = state

        def run():
            from .dat import BAD_DUMP, VERIFIED, hash_file
            seen: dict[str, str] = {}
            # Replace this platform's hashes rather than accumulating them,
            # so a re-audit after a cleanup does not leave netplay offering
            # dumps that are no longer on disk.
            self.hashes.clear_platform(platform.slug)
            try:
                files = [p for p in sorted(root.rglob("*"))
                         if p.is_file()][:50_000]
            except OSError as err:
                state.update(status="failed", error=str(err))
                return
            for path in files:
                try:
                    digest = hash_file(path)
                except OSError:
                    continue
                state["scanned"] += 1
                verdict = self.dats.lookup(**digest)
                if verdict.status == VERIFIED:
                    state["verified"] += 1
                elif verdict.status == BAD_DUMP:
                    state["bad"] += 1
                    if len(state["bad_files"]) < 200:
                        state["bad_files"].append({
                            "file": str(path.relative_to(self.library)),
                            "detail": verdict.detail or ""})
                else:
                    state["unknown"] += 1
                sha = digest.get("sha1", "")
                # Keep the hash. Netplay is decided on bytes, and this walk
                # is the only place ROMarr ever computes them; discarding
                # them here is what left an offer with nothing to carry.
                if sha:
                    self.hashes.add(
                        sha, verdict.game or path.stem, platform.slug,
                        verdict.status == VERIFIED,
                        str(path.relative_to(self.library)))
                if sha in seen and len(state["duplicates"]) < 200:
                    state["duplicates"].append({
                        "file": str(path.relative_to(self.library)),
                        "same_as": seen[sha]})
                elif sha:
                    seen[sha] = str(path.relative_to(self.library))
            self.hashes.save()
            state["status"] = "done"
            state["indexed"] = len(self.hashes)
            state["finished"] = datetime.now(timezone.utc)\
                .isoformat(timespec="seconds")
            log.info("audit of %s: %d scanned, %d verified, %d bad, "
                     "%d unknown, %d duplicate(s); %d dump(s) now known to "
                     "netplay", platform.slug,
                     state["scanned"], state["verified"], state["bad"],
                     state["unknown"], len(state["duplicates"]),
                     len(self.hashes))

        ROMarr._audit_thread = _threading.Thread(
            target=run, name="romarr-audit", daemon=True)
        ROMarr._audit_thread.start()
        return dict(state)

    def search_missing(self, *, auto: bool = False) -> dict:
        """Retry Wanted, the way *arr's missing search does.

        Manual (the Tasks page button) searches everything: pressing the
        button is consent. The scheduled sweep honours each item's backoff,
        so a title that has failed for months is retried weekly rather than
        on every sweep -- hammering indexers for games that were not there
        this morning is how trackers hand out bans.
        """
        searched = grabbed_count = skipped = 0
        for item in list(self.store.wanted):
            if auto and not next_search_due(item.attempts, item.searched_at):
                skipped += 1
                continue
            searched += 1
            if auto:
                self.store.mark_searched(item.game, item.platform)
            if self.request(item.game, item.platform).get("ok"):
                grabbed_count += 1
        message = f"Searched {searched}, grabbed {grabbed_count}"
        if skipped:
            message += f", {skipped} waiting out backoff"
        return {"searched": searched, "grabbed": grabbed_count,
                "skipped": skipped, "message": message}

    def rss_sync(self) -> str:
        """Watch what is new instead of asking again for everything.

        Torznab's convention: a search with no query is the indexer's latest
        releases -- its RSS feed. Every feed item is thrown at the same
        scorer a real search uses, per wanted title, so RSS can never grab
        something a search would have refused. This is what closes the gap
        between missing-search sweeps: a release that appears an hour after
        you asked is grabbed within the hour, not at the next sweep.
        """
        wanted = list(self.store.wanted)
        if not wanted:
            return "nothing wanted, nothing to match"
        releases, sources = [], 0
        candidates: list[tuple[str, object]] = []
        if self.prowlarr._config.base_url:
            candidates.append(("prowlarr", self.prowlarr))
        candidates += [(getattr(i, "name", "indexer"), i)
                       for i in getattr(self, "indexers", [])]
        for label, source in candidates:
            try:
                releases += source.search("", limit=100)
                sources += 1
            except Exception as err:
                log.warning("%s feed read failed: %s", label,
                            err.__class__.__name__)
        if not sources:
            return "no indexer could be read"
        grabbed_count = 0
        for item in wanted:
            platform = resolve(item.platform)
            if platform is None:
                continue
            pick = best_release(releases, item.game, platform)
            if pick is None or not pick.download_url:
                continue
            if self.grab(pick, item.game, platform.slug).get("ok"):
                grabbed_count += 1
        return (f"{len(releases)} feed item(s) from {sources} source(s), "
                f"grabbed {grabbed_count}")

    #: Where release news comes from. Only ever read, never written to.
    RELEASES_URL = "https://api.github.com/repos/BlizzHacker/romarr/releases/latest"

    def check_update(self) -> dict:
        """Ask github.com whether a newer ROMarr exists. Telling somebody is
        the entire feature -- nothing is downloaded or applied, because an
        *arr that updates itself is an *arr that restarts mid-import."""
        import requests as _requests
        try:
            response = _requests.get(
                self.RELEASES_URL, timeout=10,
                headers={"Accept": "application/vnd.github+json"})
            response.raise_for_status()
            body = response.json()
        except Exception as err:
            return {"ok": False,
                    "message": f"could not reach github.com: {err.__class__.__name__}"}
        latest = str(body.get("tag_name") or "").lstrip("vV")
        url = str(body.get("html_url") or "")
        if not latest:
            return {"ok": False, "message": "github.com answered without a tag"}

        def parts(version: str) -> tuple:
            try:
                return tuple(int(p) for p in version.split("."))
            except ValueError:
                return (0,)

        newer = parts(latest) > parts(VERSION)
        self.store.settings["_latest_version"] = latest
        self.store.settings["_update_available"] = newer
        if newer and self.store.settings.get("_update_notified") != latest:
            # Once per version, not once per day: the daily check repeating
            # the same news is an alarm everybody learns to ignore.
            self.store.settings["_update_notified"] = latest
            self.notify(update_available(VERSION, latest, url))
        self.store.save()
        message = (f"{latest} is available (running {VERSION})" if newer
                   else f"up to date ({VERSION})")
        return {"ok": True, "latest": latest, "update_available": newer,
                "message": message}

    def _auto_import_summary(self) -> str:
        done = self.import_finished(retry_failed=False)
        if not done:
            return "nothing finished"
        ok = sum(1 for d in done if d.get("ok"))
        return f"imported {ok} of {len(done)} finished download(s)"

    def connect_steam(self, steam_id: str) -> dict:
        """Save a verified SteamID64 as a list, and sync it immediately.

        Called only after `connect.steam_verify` has checked the assertion
        with Steam, so the id here is one Steam vouched for rather than one
        a caller supplied.
        """
        existing = next((cfg for cfg in self.store.list_items("import_lists")
                         if cfg.get("type") == "steam"
                         and str(cfg.get("steam_id")) == str(steam_id)), None)
        cfg = existing or {"name": "Steam library", "type": "steam",
                           "platform": "", "enable": True}
        cfg["steam_id"] = str(steam_id)
        cfg["profile"] = str(steam_id)      # the keyless public-profile path
        cfg["source"] = "owned"
        saved = self.store.put_item("import_lists", cfg)
        result = self.list_sync()
        return {"ok": True, "id": saved.get("id"), "steam_id": steam_id,
                "message": f"Steam connected. {result.get('message', '')}".strip()}

    def scan_launchers(self) -> dict:
        """Every game the launchers on THIS machine have installed.

        Useful directly when ROMarr runs on the gaming PC, which is a common
        Windows install; when it runs on a server,
        `scripts/connect_launchers.py` performs the same scan on the PC and
        pushes the result here. Either way no store credential is involved:
        the launchers already wrote their libraries to disk.
        """
        from .launchers import scan_all

        try:
            games = scan_all()
        except Exception as exc:            # noqa: BLE001 - best effort
            log.warning("launcher scan failed: %s", exc)
            return {"items": [], "error": f"scan failed: {exc.__class__.__name__}"}
        counts: dict[str, int] = {}
        for game in games:
            counts[game.launcher] = counts.get(game.launcher, 0) + 1
        return {
            "items": [{"name": g.name, "launcher": g.launcher, "path": g.path}
                      for g in games],
            "counts": counts,
            "total": len(games),
        }

    def connect_launchers(self, name: str = "Local launchers",
                          platform: str = "") -> dict:
        """Scan this machine's launchers and save the result as an import list."""
        found = self.scan_launchers()
        if found.get("error"):
            return found
        if not found["items"]:
            return {"ok": False, "added": 0,
                    "message": "no launcher libraries found on this machine"}
        content = "\n".join(g["name"] for g in found["items"])
        saved = self.store.put_item("import_lists", {
            "name": name, "type": "paste", "platform": platform,
            "content": content, "enable": True,
        })
        return {"ok": True, "id": saved.get("id"), "total": found["total"],
                "counts": found["counts"],
                "message": f"connected {found['total']} game(s) from "
                           f"{len(found['counts'])} launcher(s)"}

    def list_sync(self) -> dict:
        """Sync every enabled import list into Wanted.

        Each list carries a ledger of what it already added. A title is added
        once, ever: a list is an instruction to acquire, not a state to
        enforce, and without the ledger every re-sync would resurrect titles
        that were acquired, imported and fulfilled -- a slow loop
        re-downloading its own history.
        """
        from .lists import fetch_entries
        lists = self.store.list_items("import_lists")
        if not lists:
            return {"added": 0, "message": "no lists configured"}
        added = known = unknown = failed_lists = 0
        failures: list[dict] = []
        for cfg in lists:
            if not cfg.get("enable", True):
                continue
            from .store import now_iso
            try:
                entries = fetch_entries(cfg)
                cfg["last_sync"] = {"at": now_iso(),
                                    "fetched": len(entries)}
                self.store.put_item("import_lists", cfg)
            except Exception as err:
                # Which list failed matters, and so does WHY: "2 list(s)
                # unreadable" sends somebody hunting through logs for a
                # sentence the connector already wrote. A ValueError here
                # is a message written for the operator; anything else is
                # named by type.
                reason = (str(err) if isinstance(err, ValueError)
                          else f"{err.__class__.__name__} talking to the store")
                log.warning("import list %r failed: %s", cfg.get("name"), reason)
                failures.append({"id": cfg.get("id"),
                                 "name": cfg.get("name"), "reason": reason})
                cfg["last_sync"] = {"at": now_iso(), "error": reason}
                self.store.put_item("import_lists", cfg)
                failed_lists += 1
                continue
            # Epic swaps its single-use code for a refresh token during the
            # fetch; persisting that is what makes the next sync silent.
            if cfg.get("epic_refresh") and not cfg.get("_epic_saved"):
                cfg["epic_code"] = ""
                cfg["_epic_saved"] = True
                self.store.put_item("import_lists", cfg)
            ledger = set(cfg.get("added") or [])
            changed = False
            # Titles with no ROM platform -- a Battle.net or PSN library is
            # mostly modern games -- are kept on the list, visibly, rather
            # than discarded. "133 skipped" with no way to see which 133 is
            # how somebody concludes the connector is broken.
            unmatched: list[str] = []
            for entry in entries:
                platform = resolve(entry.platform or cfg.get("platform") or "")
                if platform is None:
                    unknown += 1
                    if entry.game not in unmatched:
                        unmatched.append(entry.game)
                    continue
                key = f"{platform.slug}/{entry.game.strip().lower()}"
                if key in ledger:
                    known += 1
                    continue
                ledger.add(key)
                changed = True
                self.store.want(entry.game, platform.slug)
                added += 1
            if changed or unmatched != (cfg.get("unmatched") or []):
                cfg["added"] = sorted(ledger)
                cfg["unmatched"] = unmatched[:500]
                self.store.put_item("import_lists", cfg)
        message = f"added {added} to Wanted"
        if known:
            message += f", {known} already added before"
        if unknown:
            message += (f", {unknown} kept aside with no ROM platform "
                        "(normal for modern store titles -- see each "
                        "list's Edit view)")
        if failed_lists:
            # Name them, with the reason. The count alone is the least
            # useful half of what is known here.
            message += "; " + "; ".join(
                f"{f['name']}: {f['reason']}" for f in failures)
        return {"added": added, "known": known, "unknown": unknown,
                "failed_lists": failed_lists, "failures": failures,
                "message": message}

    def run_command(self, name: str) -> dict:
        """The Tasks page. Names match how *arr labels its commands."""
        if name == "MissingGameSearch":
            return self.search_missing()
        if name == "ImportCompleted":
            done = self.import_finished()
            return {"imported": done, "message": f"Imported {len(done)}"}
        if name == "RssSync":
            return {"message": self.rss_sync()}
        if name == "ListSync":
            return self.list_sync()
        if name == "UpdateCheck":
            return self.check_update()
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

    def import_finished(self, *, retry_failed: bool = True) -> list[dict]:
        """Import anything the download client has completed.

        Runs on the scheduler as well as from the Tasks page, so it has to be
        quiet about work already done: a queue item that imported (or failed
        to) is marked and skipped on later sweeps, or a one-minute timer
        would re-attempt every finished download forever and fill History
        with "already in the library". The manual button passes
        `retry_failed=True`, which clears the failure marks first -- pressing
        it after fixing a path mapping is exactly how a stuck import should
        be retried.
        """
        if retry_failed:
            with self._lock:
                for item in self.queue:
                    if item.state == "import-failed":
                        item.state = "grabbed"
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
            queue_item = None
            with self._lock:
                for item in self.queue:
                    if item.release == name:
                        if item.state in ("imported", "import-failed"):
                            queue_item = item
                            break
                        platform = resolve(item.platform)
                        queue_item = item
                        break
            if queue_item is not None and queue_item.state in ("imported", "import-failed"):
                continue
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
                if queue_item is not None:
                    queue_item.state = "import-failed"
                continue
            target_cfg, target_lib = target
            label = target_cfg.get("name") or getattr(target_lib, "name", "library")

            outcomes = import_rom(
                path, platform, self.library_root(target_cfg),
                layout=self.library_layout(target_cfg),
                translation=is_translation(name))
            if not outcomes:
                self.store.record(Event(kind="failed", game=name,
                                        platform=platform.slug,
                                        detail="no ROMs found in download"))
                results.append({"name": name, "ok": False,
                                "reason": "no ROMs found", "library": label})
                if queue_item is not None:
                    queue_item.state = "import-failed"
                continue

            any_ok = any(o.ok for o in outcomes)
            for outcome in outcomes:
                if outcome.ok:
                    self.store.record(Event(kind="imported", game=name,
                                            platform=platform.slug, release=name,
                                            library=label,
                                            detail=str(outcome.destination)))
                    self.notify(imported(name, platform.slug,
                                         outcome.verification))
                else:
                    self.store.record(Event(kind="failed", game=name,
                                            platform=platform.slug,
                                            detail=outcome.reason))
                    self.notify(failed(name, str(outcome.reason)))
            if queue_item is not None:
                # Marked so the scheduled sweep never re-attempts it; the
                # Tasks page button clears failure marks to retry.
                queue_item.state = "imported" if any_ok else "import-failed"
            if any_ok:
                if self.store.settings.get("rescan_after_import", True):
                    target_lib.rescan(platform.slug)
                for w in list(self.store.wanted):
                    if w.platform == platform.slug and w.game.lower() in name.lower():
                        self.store.fulfil(w.game, w.platform)
            results.append({"name": name, "ok": any_ok,
                            "reason": "" if any_ok else str(outcomes[0].reason),
                            "library": label})
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
        # `/login` renders the sign-in screen and `/api/v1/setup` performs the
        # first-run claim, so both have to answer somebody with no credential
        # -- that is the entire point of them. `/setup` guards itself: it
        # refuses once the install is claimed.
        #: Reachable without the session cookie. The Steam return is here
        #: because it CANNOT carry one: it arrives as a cross-site redirect
        #: from Steam and the cookie is SameSite=Strict. Its single-use,
        #: short-lived `state` -- minted by an authenticated request -- is
        #: what authorises it instead.
        #: `/link` is where an invitation link lands. It is open because the
        #: person opening it is somebody else's operator with no account
        #: here -- that is the entire situation. It answers with a CONSTANT:
        #: it takes no parameters, reads no invitation, touches no state and
        #: returns identical bytes to every caller. The invitation id is in
        #: the URL fragment, which never reaches this server at all.
        OPEN_PATHS = ("/", "/login", "/link", "/api/health", "/api/v1/login",
                      "/api/v1/setup", "/api/v1/connect/steam/return",
                      # Peer-facing: authenticated by peer id + token in
                      # headers, which is a DIFFERENT credential from the
                      # operator's session. Open to the session gate and
                      # closed to anyone without a valid peer token.
                      "/api/v1/peer/accept", "/api/v1/peer/shelf",
                      "/api/v1/peer/netplay")

        def _send_session(self, token: str, payload: dict):
            """Answer with a session cookie set.

            Shared by login and first-run setup so the cookie's flags are
            stated once. HttpOnly so a script cannot read it; SameSite=Strict
            so another site cannot ride it; Path=/ so it covers the API. Not
            Secure: ROMarr is normally plain http on a LAN, and a Secure
            cookie would simply never be stored there.
            """
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={service.auth.session_seconds}")
            raw = json.dumps(payload).encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)

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

        #: How much of an over-sized body is drained before the socket is
        #: simply closed. Generous, because draining costs one buffer rather
        #: than the whole body -- but not unbounded, or a caller declaring ten
        #: gigabytes holds a worker for as long as it cares to send them.
        DISCARD_CEILING = 16 << 20

        def _discard(self, length: int) -> None:
            """Throw a body away as it arrives, without ever holding it.

            `_drain` reads the body in one call, which is right for the small
            refusals it was written for and wrong for a body being refused
            precisely *because* it is enormous: `read(length)` allocates
            whatever the caller declared. This reads a chunk at a time and
            keeps none of it, so the memory cost is one buffer regardless.
            """
            remaining = min(max(length, 0), self.DISCARD_CEILING)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (ValueError, OSError):
                pass
            if length > self.DISCARD_CEILING:
                # More still coming than is worth waiting for. The reply is
                # already on its way; the connection goes rather than the
                # worker sitting through the rest.
                self.close_connection = True

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
            if route.path == "/link":
                # Served before the sign-in check on purpose, and without
                # consulting anything: the visitor is a stranger holding a
                # link, and the page's whole job is to tell them what they
                # are holding and get them onto their OWN server with it.
                return self._send(200, ui_link_page().encode("utf-8"),
                                  "text/html; charset=utf-8")
            if route.path in ("/", "/login"):
                # The app shell only for somebody who is already in. Serving
                # it to everybody is what made issue #8 so confusing: the UI
                # rendered, looked healthy, and then every request it made
                # came back 401 with nowhere to go and fix it.
                if service.auth.enabled and not self._authorised():
                    return self._send(
                        200,
                        ui_login_page(claimed=service.claimed,
                                      totp=service.auth.totp.enabled)
                        .encode("utf-8"),
                        "text/html; charset=utf-8")
                if route.path == "/login":
                    # Already signed in; nothing to log into.
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._send(200, ui_page().encode("utf-8"),
                                  "text/html; charset=utf-8")

            # --- *arr-shaped API ---------------------------------------
            if route.path == "/api/v1/game":
                # Served from the background cache. Calling RomM here meant the
                # page waited behind whatever else was querying that table.
                return self._json(200, service.library_view(
                    platform=(query.get("platform") or [""])[0],
                    q=(query.get("q") or [""])[0],
                    offset=int((query.get("offset") or ["0"])[0] or 0),
                    limit=int((query.get("limit") or ["120"])[0] or 120),
                    genre=(query.get("genre") or [""])[0],
                    region=(query.get("region") or [""])[0],
                    decade=(query.get("decade") or [""])[0],
                    origin=(query.get("origin") or [""])[0],
                    source=(query.get("source") or [""])[0],
                    sort=(query.get("sort") or [""])[0]))
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
            if route.path == "/api/v1/log/tail":
                return self._json(200, service.logring.tail(
                    since=int((query.get("since") or ["0"])[0] or 0),
                    level=(query.get("level") or [""])[0],
                    limit=int((query.get("limit") or ["200"])[0])))
            if route.path == "/api/v1/discover/library":
                return self._json(200, service.discover_library(
                    shelf=(query.get("shelf") or ["top-rated"])[0],
                    genre=(query.get("genre") or [""])[0],
                    value=(query.get("value") or [""])[0],
                    limit=int((query.get("limit") or ["40"])[0] or 40)))
            if route.path == "/api/v1/calendar/library":
                return self._json(200, service.library_calendar(
                    decade=(query.get("decade") or [""])[0],
                    view=(query.get("view") or ["releases"])[0],
                    month=(query.get("month") or [""])[0],
                    day=(query.get("day") or [""])[0],
                    limit=int((query.get("limit") or ["200"])[0] or 200)))
            if route.path == "/api/v1/discover":
                return self._json(200, metadata_discover(
                    service.store.list_items("metadata_providers"),
                    shelf=(query.get("shelf") or ["popular"])[0],
                    limit=int((query.get("limit") or ["40"])[0])))
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
            if route.path == "/api/v1/downloadclient/browser":
                return self._json(200, service.browser_capability())
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
            if route.path == "/api/v1/connection":
                secrets = {"url", "token", "password", "key"}
                items = []
                for cfg in service.store.list_items("connections"):
                    items.append({k: ("********" if k in secrets and v else v)
                                  for k, v in cfg.items()})
                return self._json(200, {"items": items})
            if route.path == "/api/v1/connection/schema":
                # Both shapes: the provider cards read the flat list, and
                # the generic editor reads per-type field definitions. A
                # webhook URL IS the credential for most of these, so it is
                # a secret field.
                types = {}
                for name, spec in NOTIFIERS.items():
                    types[name] = {
                        "label": spec["label"], "protocol": "notification",
                        "fields": [
                            {"name": "name", "label": "Name", "type": "text",
                             "default": spec["label"]},
                            {"name": "enable", "label": "Enable",
                             "type": "bool", "default": True},
                        ] + [
                            {"name": f, "label": f.replace("_", " ").title(),
                             "type": ("secret" if f in ("url", "token",
                                                        "password", "key")
                                      else "text"),
                             "default": "", "help": spec["help"]}
                            for f in spec["fields"]
                        ] + [
                            {"name": "events", "label": "Events", "type": "list",
                             "help": "grab, import, upgrade, failure, "
                                     "bad-dump, update. Empty means all."},
                        ],
                    }
                return self._json(200, {"types": types, "list": [
                    {"name": name, "label": spec["label"],
                     "fields": list(spec["fields"]), "help": spec["help"]}
                    for name, spec in NOTIFIERS.items()]})
            if route.path == "/api/v1/tag":
                return self._json(200, {
                    "tags": service.store.settings.get("_tags") or {}})
            if route.path == "/api/v1/manualimport":
                return self._json(200, service.scan(
                    query.get("path", [""])[0]))
            if route.path == "/api/v1/collection/plan":
                return self._json(200, service.collection_plan(
                    query.get("dat", [""])[0],
                    query.get("platform", [""])[0],
                    one_game_one_rom=query.get("onegame", ["1"])[0] != "0",
                    regions=[r for r in query.get("regions", [""])[0].split(",")
                             if r] or None,
                    translation_policy=query.get("translation_policy",
                                                 [""])[0] or None,
                    exclude=[e for e in query.get("exclude", [""])[0].split(",")
                             if e] if "exclude" in query else None))
            if route.path == "/api/v1/collection":
                return self._json(200, service.collection_status())
            if route.path == "/api/v1/openapi.json":
                return self._json(200, openapi_spec(VERSION))
            if route.path == "/api/v1/metadataprovider":
                from .metadata import PROVIDERS as _MP
                secrets = {"api_key", "token", "client_id"}
                items = []
                for cfg in service.store.list_items("metadata_providers"):
                    items.append({k: ("********" if k in secrets and v else v)
                                  for k, v in cfg.items()})
                return self._json(200, {"items": items})
            if route.path == "/api/v1/metadataprovider/schema":
                # The generic editor's shape: per-type field definitions,
                # secrets marked so they render as password inputs and are
                # kept on edit.
                from .metadata import PROVIDERS as _MP
                types = {}
                for name, spec in _MP.items():
                    types[name] = {
                        "label": spec["label"],
                        "protocol": "metadata",
                        "fields": [
                            {"name": "name", "label": "Name", "type": "text",
                             "default": spec["label"]},
                            {"name": "enable", "label": "Enable",
                             "type": "bool", "default": True},
                        ] + [
                            # `labels` where the storage key and the thing
                            # the operator is holding have drifted apart:
                            # IGDB's `token` is a Twitch client secret, and
                            # a box labelled "Token" next to help text about
                            # a secret is how somebody pastes the wrong half
                            # of the credential.
                            {"name": f,
                             "label": spec.get("labels", {}).get(
                                 f, f.replace("_", " ").title()),
                             "type": "secret", "default": "",
                             "help": spec.get("help", "")}
                            for f in spec["fields"]
                        ],
                    }
                return self._json(200, {"types": types})
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
            if route.path == "/api/v1/system/apikey":
                # Deliberately its own authenticated route rather than part
                # of safe_settings, which exists to strip credentials.
                return self._json(200, {"api_key": service.auth.api_key})
            if route.path == "/api/v1/system/tasks":
                return self._json(200, {"items": service.scheduler.status()})
            if route.path == "/api/v1/capture/status":
                # What the extension's options page calls to prove its
                # settings work. Authenticated, so a wrong key answers 401 --
                # which is the entire value of the test. Pointing it at
                # /api/health instead would have reported success for a key
                # the server had rejected, because health answers strangers.
                return self._json(200, capture_status(
                    capture_index_dir(service._env, service.store.path)))
            if route.path == "/api/v1/peer/shelf":
                # Peer-facing. Authenticated by peer id + token, never by
                # the operator's session -- a peer is not a user here.
                peer = service.federation.authenticate(
                    self.headers.get("X-Peer-Id", ""),
                    self.headers.get("X-Peer-Token", ""))
                if peer is None:
                    return self._json(401, {"error": "unknown peer"})
                return self._json(200, {"items": service.peer_shelf(peer),
                                        "origin": service.federation.name})
            if route.path == "/api/v1/hashes":
                # What netplay can actually prove. Without this an operator
                # has no way to tell "we disagree on the dump" apart from
                # "I never audited that platform".
                return self._json(200, {
                    "count": len(service.hashes),
                    "platforms": service.hashes.platforms(),
                    "seed": service.seed_status(),
                    "detail": "Read from your library server's own hashes, "
                              "and upgraded to verified by an audit or a "
                              "verified import."})
            if route.path == "/api/v1/friends/shelf":
                # The other direction: I browse THEIR library. Operator
                # session, because this one is a person at a screen.
                return self._json(200, service.friend_shelf(
                    (query.get("peer_id") or [""])[0],
                    q=(query.get("q") or [""])[0],
                    platform=(query.get("platform") or [""])[0],
                    offset=int((query.get("offset") or ["0"])[0] or 0),
                    limit=int((query.get("limit") or ["200"])[0] or 200),
                    refresh=(query.get("refresh") or [""])[0] == "1"))
            if route.path == "/api/v1/peer":
                return self._json(200, {
                    "name": service.federation.name,
                    "url": service.federation.url,
                    "peers": service.federation.status()})
            if route.path == "/api/v1/ecosystem":
                from .ecosystem import as_dict
                return self._json(200, {"categories": as_dict()})
            if route.path == "/api/v1/audit":
                return self._json(200, dict(service._audit_state)
                                  or {"status": "never run"})
            if route.path == "/api/v1/stats":
                return self._json(200, service.stats())
            if route.path == "/api/v1/game/meta":
                platform = (query.get("platform") or [""])[0]
                game = (query.get("game") or [""])[0]
                if platform and game:
                    return self._json(200, service.store.game_meta(platform, game))
                return self._json(200, {"items": service.store.all_game_meta()})
            if route.path == "/api/v1/importlist":
                items = []
                for cfg in service.store.list_items("import_lists"):
                    # The ledger is bookkeeping, not configuration; its size
                    # is the interesting part.
                    ledger = cfg.pop("added", None) or []
                    for secret in LIST_SECRETS:
                        if cfg.get(secret):
                            cfg[secret] = "********"
                    items.append({**cfg, "added_count": len(ledger),
                                  "unmatched_count": len(cfg.get("unmatched")
                                                         or [])})
                return self._json(200, {"items": items})
            if route.path == "/api/v1/importlist/schema":
                from .lists import NO_API_STORES
                return self._json(200, {"types": LIST_TYPES,
                                        "no_api": NO_API_STORES})
            if route.path == "/api/v1/launchers":
                return self._json(200, service.scan_launchers())
            if route.path == "/api/v1/connect/sources":
                from .connect import PASTE_SOURCES, TOKEN_SOURCES
                return self._json(200, {"token_sources": TOKEN_SOURCES,
                                        "paste_sources": PASTE_SOURCES})
            if route.path == "/api/v1/connect/steam":
                # Step one: bounce the browser to Steam. The return URL is
                # built from the request's own host so it works on a LAN
                # address and behind a proxy alike, without configuration.
                from .connect import steam_login_url

                host = self.headers.get("X-Forwarded-Host") \
                    or self.headers.get("Host") or "localhost"
                scheme = self.headers.get("X-Forwarded-Proto") or "http"
                base = f"{scheme}://{host}"
                state = service.connect_states.issue()
                self.send_response(303)
                self.send_header(
                    "Location",
                    steam_login_url(
                        f"{base}/api/v1/connect/steam/return?state={state}",
                        realm=base))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if route.path == "/api/v1/connect/steam/return":
                # Step two: Steam has sent the browser back. Verify what it
                # asserted with Steam itself before believing any of it.
                from .connect import steam_verify

                if not service.connect_states.spend(
                        (query.get("state") or [""])[0]):
                    body = ("<h2>That sign-in link has expired</h2>"
                            "<p>Start again from Lists. "
                            "<a href='/#lists'>Back</a></p>")
                    return self._send(400, body.encode(),
                                      "text/html; charset=utf-8")
                steam_id = steam_verify(query)
                if not steam_id:
                    body = ("<h2>Steam sign-in could not be verified</h2>"
                            "<p>Nothing was connected. "
                            "<a href='/#lists'>Back to Lists</a></p>")
                    return self._send(400, body.encode(),
                                      "text/html; charset=utf-8")
                out = service.connect_steam(steam_id)
                body = (f"<h2>Steam connected</h2><p>{out['message']}</p>"
                        "<p><a href='/#lists'>Back to Lists</a></p>"
                        "<script>location.replace('/#lists')</script>")
                return self._send(200, body.encode(),
                                  "text/html; charset=utf-8")
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
            if route.path == "/api/v1/players":
                return self._json(200, service.player_directory())
            if route.path == "/api/v1/play":
                # `file` is the only required parameter, and a bare extension
                # is a legitimate value for it -- that is the shape a library
                # server reports. `missing=1` asks the question that matters
                # on a catalogued library: the row exists, the bytes do not.
                name = (query.get("file") or [""])[0].strip()
                if not name:
                    return self._json(400, {"error": "file is required"})
                missing = (query.get("missing") or [""])[0].strip().lower()
                return self._json(200, service.play_for(
                    name, (query.get("platform") or [""])[0],
                    present=missing not in ("1", "true", "yes")))
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
            if route.path == "/api/v1/moonlight":
                return self._json(200, service.moonlight_status())
            if route.path == "/api/v1/hub/status":
                # Whether plugins are confined is the most useful thing to
                # know before installing one, so it travels with the
                # availability answer instead of hiding in a startup log.
                confined, why = hub.sandbox_state()
                return self._json(200, {"available": hub.available(),
                                        "sandboxed": confined,
                                        "sandbox_detail": why})
            if route.path == "/api/search":
                game = (query.get("game") or [""])[0]
                if not game:
                    return self._json(400, {"error": "game is required"})
                return self._json(200, service.search(game, (query.get("platform") or [""])[0]))
            return self._json(404, {"error": "not found"})

        def _post(self):
            route = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            # The capture body is assembled by a web page, so its size is
            # checked before it is read rather than after. `read(length)` on a
            # declared gigabyte allocates a gigabyte first and refuses second,
            # which is not a refusal. Only this route is bounded here: a
            # restore legitimately posts a large backup, and a blanket cap
            # would break it.
            if (route.path == "/api/v1/capture"
                    and length > CAPTURE_MAX_BODY):
                # Drained in chunks and thrown away rather than read whole.
                # That is the distinction that matters: discarding as it
                # arrives costs one buffer no matter what was declared, while
                # `read(length)` costs whatever the caller claimed. Draining
                # at all is `_drain`'s reasoning -- a POST has already sent its
                # body, and replying without reading it aborts the connection
                # so the caller never sees the refusal. Verified: skipping
                # this gave the poster a dropped socket instead of a 413.
                self._discard(length)
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                refusal = json.dumps({
                    "error": "capture too large",
                    "limit_bytes": CAPTURE_MAX_BODY,
                    "detail": "post the page in smaller batches",
                }).encode()
                self.send_header("Content-Length", str(len(refusal)))
                self.end_headers()
                return self.wfile.write(refusal)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})

            if route.path == "/api/v1/capture":
                # Catalogue rows the operator's own browser saw, from a site no
                # HTTP client can read. Authenticated by the same gate as every
                # other route and deliberately NOT on OPEN_PATHS: the extension
                # holds the operator's API key because the operator pasted it
                # in, which is a credential rather than an exemption.
                try:
                    report = capture_ingest(
                        body, directory=capture_index_dir(
                            service._env, service.store.path))
                except CaptureRejected as exc:
                    # Malformed input is refused whole. Storing the good half
                    # of a payload that lied about the rest would put rows in
                    # the index that nobody could account for.
                    return self._json(400, {"ok": False, "error": str(exc)})
                return self._json(200, report)
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
            if route.path == "/api/v1/collection/start":
                return self._json(200, service.collection_start(
                    str(body.get("dat") or ""),
                    str(body.get("platform") or ""),
                    int(body.get("per_pass") or 5),
                    one_game_one_rom=bool(body.get("one_game_one_rom", True)),
                    regions=body.get("regions") or None,
                    translation_policy=body.get("translation_policy") or None,
                    exclude=body.get("exclude")
                    if body.get("exclude") is not None else None))
            if route.path == "/api/v1/collection/step":
                return self._json(200, service.collection_step(
                    str(body.get("id") or "")))
            if route.path == "/api/v1/collection/control":
                return self._json(200, service.collection_control(
                    str(body.get("id") or ""), str(body.get("action") or "")))
            if route.path == "/api/v1/manualimport":
                # The action half of Manual Import. GET scans and reports;
                # this adopts one file the operator picked, with their
                # platform choice winning over ROMarr's guess.
                return self._json(200, service.adopt(
                    str(body.get("path") or ""),
                    str(body.get("platform") or ""),
                    force=bool(body.get("force"))))
            if route.path == "/api/v1/metadataprovider":
                secrets = {"api_key", "token", "client_id"}
                cfg = dict(body or {})
                if cfg.get("id"):
                    old_cfg = service.store.get_item("metadata_providers",
                                                     str(cfg["id"])) or {}
                    for key in secrets:
                        # A key that is absent counts the same as one sent
                        # back masked. `put_item` replaces the whole entry,
                        # so without this a caller renaming a provider wipes
                        # the credential it was renaming -- and the provider
                        # then reports itself as unconfigured, which is the
                        # exact confusion this endpoint keeps producing.
                        if cfg.get(key, "") in ("********", ""):
                            cfg[key] = old_cfg.get(key, "")
                saved = service.store.put_item("metadata_providers", cfg)
                service.reload_metadata()
                return self._json(200, {k: ("********" if k in secrets and v
                                            else v)
                                        for k, v in saved.items()})
            if route.path == "/api/v1/totp/enroll":
                from .totp import backup_codes, new_secret, provisioning_uri
                secret = new_secret()
                codes = backup_codes()
                service.store.settings["_totp_secret"] = secret
                service.store.settings["_totp_backup"] = codes
                service.store.save()
                from .totp import Totp as _Totp
                service.auth.totp = _Totp(secret=secret, backup=codes)
                return self._json(200, {
                    "secret": secret,
                    "uri": provisioning_uri(secret, account="romarr"),
                    "backup_codes": codes,
                    "note": "Scan the URI or enter the secret in any TOTP "
                            "app. Codes are asked for at sign-in from now "
                            "on; the backup codes are single-use."})
            if route.path == "/api/v1/totp/disable":
                service.store.settings["_totp_secret"] = ""
                service.store.settings["_totp_backup"] = []
                service.store.save()
                from .totp import Totp as _Totp
                service.auth.totp = _Totp(secret="", backup=[])
                return self._json(200, {"ok": True, "enabled": False})
            if route.path == "/api/v1/connection":
                secrets = {"url", "token", "password", "key"}
                cfg = dict(body or {})
                if cfg.get("id"):
                    old_cfg = service.store.get_item("connections",
                                                     str(cfg["id"])) or {}
                    for key in secrets:
                        if cfg.get(key) in ("********", ""):
                            cfg[key] = old_cfg.get(key, "")
                if not cfg.get("events"):
                    cfg.pop("events", None)   # absent means every event
                saved = service.store.put_item("connections", cfg)
                service.reload_policy()
                return self._json(200, {k: ("********" if k in secrets and v
                                            else v)
                                        for k, v in saved.items()})
            if route.path == "/api/v1/metadataprovider/test":
                from .metadata import PROVIDERS as _MP
                spec = _MP.get(str(body.get("type") or "").lower())
                if spec is None:
                    return self._json(400, {"ok": False,
                                            "message": "unknown provider"})
                cfg = dict(body or {})
                if cfg.get("id"):
                    old_cfg = service.store.get_item("metadata_providers",
                                                     str(cfg["id"])) or {}
                    for key in ("api_key", "token", "client_id"):
                        # Absent as well as masked, so testing a saved
                        # provider by id alone reports on the credential
                        # that is stored rather than on an empty one.
                        if cfg.get(key, "") in ("********", ""):
                            cfg[key] = old_cfg.get(key, "")
                try:
                    info = spec["lookup"](cfg, "Chrono Trigger")
                except Exception as exc:
                    return self._json(200, {
                        "ok": False,
                        "message": f"lookup failed: {exc.__class__.__name__}"})
                found = bool(getattr(info, "found", False))
                return self._json(200, {
                    "ok": found,
                    "message": ("found Chrono Trigger -- the key works"
                                if found else
                                "the provider answered but found nothing -- "
                                "check the key")})
            if route.path == "/api/v1/peer/invite":
                # Pick up a public URL set since startup, so the operator
                # does not have to restart to mint a usable invitation.
                configured = str(
                    service.store.settings.get("public_url") or "").strip()
                if configured:
                    service.federation.url = configured.rstrip("/")
                invite = service.federation.invite(
                    name=str(body.get("name") or service.federation.name))
                payload = {
                    # The link is safe to send anywhere a link goes, because
                    # it authorises nothing. The code is the credential and
                    # is deliberately NOT in the link.
                    "link": invite.link(),
                    "code": invite.code_display,
                    "code_expires_in": invite.code_expires_in(),
                    "invite": invite.blob(),
                    "note": "Send the link however you like -- it carries no "
                            "secret. Send the code separately if you can: by "
                            "voice, by text, in another app. It works once, "
                            "expires in 15 minutes, and you still confirm the "
                            "peer here before they can see anything."}
                if not service.federation.url:
                    # An invitation with no address is a dead end: the
                    # friend's server has nowhere to call back to, and the
                    # failure would surface much later as a silent nothing.
                    payload["warning"] = (
                        "This invitation carries no address for your server, "
                        "so your friend's ROMarr cannot call you back. Set "
                        "your public URL under Settings -> General, then mint "
                        "a fresh invitation.")
                return self._json(200, payload)
            if route.path == "/api/v1/peer/redeem":
                try:
                    peer = service.federation.redeem(body.get("invite") or {})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                service.save_peers()
                return self._json(200, {"peer_id": peer.peer_id,
                                        "name": peer.name})
            if route.path == "/api/v1/peer/claim":
                # The link-and-code path, from the redeeming side. Unlike
                # /redeem this one goes over the network, because a claim
                # code is not a token: it has to be exchanged with the
                # server that minted it before anything is worth storing.
                result = service.claim_invitation(
                    str(body.get("link") or ""), str(body.get("code") or ""))
                return self._json(200 if result.get("ok") else 400, result)
            if route.path == "/api/v1/peer/accept":
                # Called BY a peer redeeming my invitation, so it carries
                # the one-time secret -- or the short claim code, which is
                # the same invitation reached by the other door -- rather
                # than a session.
                try:
                    peer = service.federation.accept(
                        str(body.get("peer_id") or ""),
                        str(body.get("secret") or ""),
                        name=str(body.get("name") or "peer"),
                        url=str(body.get("url") or ""))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                service.save_peers()
                return self._json(200, {
                    "peer_id": peer.peer_id,
                    # The long token, handed back to the caller that just
                    # proved it holds this invitation. The blob path already
                    # had it; the claim-code path arrives with eight
                    # characters and needs the durable credential from
                    # somewhere, and this exchange -- short code in, long
                    # token out, once, over the wire -- is that somewhere.
                    # It is not a leak: nobody reaches this line without a
                    # credential for this exact invitation, and the token it
                    # returns opens nothing until the operator confirms.
                    "token": peer.token,
                    "confirmed": peer.confirmed,
                    "name": service.federation.name,
                    "url": service.federation.url,
                    "note": "held until the operator confirms it"})
            if route.path == "/api/v1/peer/confirm":
                try:
                    peer = service.federation.confirm(
                        str(body.get("peer_id") or ""))
                except ValueError as exc:
                    return self._json(404, {"error": str(exc)})
                service.save_peers()
                return self._json(200, {"peer_id": peer.peer_id,
                                        "confirmed": True})
            if route.path == "/api/v1/peer/policy":
                from .federation import ACCESS, SCOPES
                peer = service.federation.peers.get(
                    str(body.get("peer_id") or ""))
                if peer is None:
                    return self._json(404, {"error": "no such peer"})
                scope = str(body.get("scope") or peer.scope)
                access = str(body.get("access") or peer.access)
                if scope not in SCOPES or access not in ACCESS:
                    return self._json(400, {
                        "error": "unknown scope or access",
                        "scopes": list(SCOPES), "access": list(ACCESS)})
                peer.scope, peer.access = scope, access
                if body.get("platforms") is not None:
                    peer.platforms = tuple(body.get("platforms") or ())
                if body.get("delegate_users") is not None:
                    peer.delegate_users = bool(body.get("delegate_users"))
                service.save_peers()
                return self._json(200, {"peer_id": peer.peer_id,
                                        "scope": peer.scope,
                                        "access": peer.access})
            if route.path == "/api/v1/peer/netplay":
                # A peer proposing a session. The answer is about bytes.
                peer = service.federation.authenticate(
                    self.headers.get("X-Peer-Id", ""),
                    self.headers.get("X-Peer-Token", ""))
                if peer is None:
                    return self._json(401, {"error": "unknown peer"})
                # Judged against the hash index, not the shelf: a library
                # server's game object has no SHA1, so answering from it
                # could only ever say "missing".
                return self._json(200, service.federation.netplay_answer(
                    body.get("offer") or {}, service.hashes.entries()))
            if route.path == "/api/v1/hashes/seed":
                # Netplay from the library server's own hashes, so it works
                # on a shelf too large to audit. Backgrounded: this is
                # minutes of HTTP against a large library.
                return self._json(200, service.start_hash_seed())
            if route.path == "/api/v1/peer/romm":
                # Befriend somebody running a plain RomM. No handshake:
                # there is nobody on the far side who speaks this protocol.
                try:
                    peer = service.federation.add_romm(
                        str(body.get("url") or ""),
                        str(body.get("username") or ""),
                        str(body.get("password") or ""),
                        name=str(body.get("name") or ""),
                        token=str(body.get("token") or ""))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                rows, err = service._romm_friend_rows(peer)
                if err:
                    # Do not keep a friend we could not read: a row that
                    # never works is worse than a clear failure now.
                    service.federation.revoke(peer.peer_id)
                    return self._json(400, {"error": err})
                service._friend_shelves[peer.peer_id] = (rows, time.monotonic())
                service.save_peers()
                return self._json(200, {
                    "peer_id": peer.peer_id, "name": peer.name,
                    "titles": len(rows),
                    "detail": f"Connected to {peer.name}: {len(rows)} title(s) "
                              f"with hashes ROMarr can match on."})
            if route.path == "/api/v1/friends/want":
                # Something off a friend's shelf, into MY wanted list. My
                # indexers fetch it; nothing comes from my friend.
                return self._json(200, service.friend_want(
                    str(body.get("peer_id") or ""),
                    str(body.get("title") or ""),
                    str(body.get("platform") or "")))
            if route.path == "/api/v1/friends/netplay":
                # I offer a friend a game. The offer carries my SHA1.
                return self._json(200, service.netplay_invite(
                    str(body.get("peer_id") or ""),
                    str(body.get("title") or ""),
                    str(body.get("platform") or "")))
            if route.path == "/api/v1/audit":
                return self._json(200, service.audit_library(
                    str(body.get("platform") or "")))
            if route.path == "/api/v1/queue/action":
                return self._json(200, service.queue_action(
                    int(body.get("index", -1)), str(body.get("action") or "")))
            if route.path == "/api/v1/queue/clear":
                return self._json(200, service.clear_queue(
                    str(body.get("state") or "")))
            if route.path == "/api/v1/game/meta":
                platform = str(body.get("platform") or "")
                game = str(body.get("game") or "")
                if not platform or not game:
                    return self._json(400, {"error": "platform and game are required"})
                try:
                    meta = service.store.set_game_meta(
                        platform, game,
                        status=body.get("status"),
                        rating=body.get("rating"),
                        notes=body.get("notes"))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, meta)
            if route.path == "/api/v1/importlist":
                cfg = {k: body.get(k, "") for k in
                       ("id", "name", "type", "platform", "content", "url",
                        "steam_id", "profile", "api_key", "source",
                        "gog_username", "openxbl_key", "npsso", "itchio_key",
                        "epic_code", "ea_token", "battlenet_json")}
                cfg["enable"] = bool(body.get("enable", True))
                cfg["type"] = str(cfg["type"] or "paste").lower()
                if cfg["type"] not in LIST_TYPES:
                    return self._json(400, {
                        "error": f"unknown list type {cfg['type']!r}",
                        "known": sorted(LIST_TYPES)})
                # Take the whole document a person pasted, not just the
                # bare value: every token page is JSON and select-all-copy
                # is what actually happens.
                from .connect import extract_value
                for secret in ("npsso", "ea_token", "epic_code",
                               "openxbl_key", "itchio_key", "gog_username"):
                    if cfg.get(secret) and cfg[secret] != "********":
                        cfg[secret] = extract_value(secret, cfg[secret])
                if not cfg.get("id"):
                    cfg.pop("id", None)
                else:
                    # An edit must not wipe the ledger of what this list
                    # already added -- losing it would resurrect every
                    # fulfilled title on the next sync. The same rule keeps
                    # every stored credential when the form sends back its
                    # placeholder.
                    old = service.store.get_item("import_lists", cfg["id"])
                    if old:
                        cfg["added"] = old.get("added") or []
                        for secret in LIST_SECRETS:
                            if cfg.get(secret) in ("********", ""):
                                cfg[secret] = old.get(secret, "")
                saved = service.store.put_item("import_lists", cfg)
                saved.pop("added", None)
                for secret in LIST_SECRETS:
                    if saved.get(secret):
                        saved[secret] = "********"
                return self._json(200, saved)
            if route.path == "/api/v1/launchers/connect":
                return self._json(200, service.connect_launchers(
                    str(body.get("name") or "Local launchers"),
                    str(body.get("platform") or "")))
            if route.path == "/api/v1/importlist/preview":
                from .lists import fetch_entries as _fetch_entries
                preview_cfg = {k: body.get(k) or ""
                               for k in ("type", "content", "url", "steam_id",
                                         "profile", "api_key", "source",
                                         "gog_username", "openxbl_key",
                                         "npsso", "itchio_key", "epic_code",
                                         "ea_token", "battlenet_json",
                                         "humble_cookie")}
                preview_cfg["type"] = preview_cfg["type"] or "paste"
                if body.get("id"):
                    stored = service.store.get_item("import_lists",
                                                    str(body["id"])) or {}
                    for secret in LIST_SECRETS:
                        if preview_cfg.get(secret) == "********":
                            preview_cfg[secret] = stored.get(secret, "")
                try:
                    entries = _fetch_entries(preview_cfg)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except Exception as exc:
                    return self._json(502, {
                        "error": f"could not fetch the list: {exc.__class__.__name__}"})
                default = str(body.get("platform") or "")
                out = []
                for entry in entries[:500]:
                    platform = resolve(entry.platform or default)
                    out.append({"game": entry.game,
                                "platform": platform.slug if platform else "",
                                "unresolved": platform is None})
                return self._json(200, {"items": out, "total": len(entries)})
            if route.path == "/api/v1/setup":
                # First-run claim. Open only while unclaimed, and it closes the
                # moment it succeeds -- so this is a one-shot, not a standing
                # unauthenticated way to reset the password.
                if service.claimed:
                    return self._json(409, {
                        "error": "already set up",
                        "detail": "This ROMarr already has a password. Sign in "
                                  "instead.",
                    })
                password = str(body.get("password") or "")
                if len(password) < MIN_PASSWORD:
                    return self._json(400, {
                        "error": "weak password",
                        "detail": f"Use at least {MIN_PASSWORD} characters.",
                    })
                service.store.settings["_password_hash"] = \
                    service.auth.hash_password(password)
                service.auth.password_hash = \
                    service.store.settings["_password_hash"]
                service.claimed = True
                service.store.save()
                log.info("this install has been claimed; setup is now closed")
                token = service.auth.issue_session()
                return self._send_session(token, {"ok": True})
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
                return self._send_session(token, {"ok": True})
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
            if route.path == "/api/v1/moonlight/pin":
                return self._json(200, service.moonlight_pin(body))
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
            if route.path.startswith("/api/v1/importlist/"):
                item_id = route.path[len("/api/v1/importlist/"):]
                removed = service.store.delete_item("import_lists", item_id)
                return self._json(200 if removed else 404, {"deleted": removed})
            if route.path.startswith("/api/v1/peer/"):
                peer_id = route.path[len("/api/v1/peer/"):]
                gone = service.federation.revoke(peer_id)
                if gone:
                    service.save_peers()
                return self._json(200 if gone else 404, {"revoked": gone})
            if route.path.startswith("/api/v1/connection/"):
                item_id = route.path[len("/api/v1/connection/"):]
                removed = service.store.delete_item("connections", item_id)
                service.reload_policy()
                return self._json(200 if removed else 404, {"deleted": removed})
            if route.path.startswith("/api/v1/metadataprovider/"):
                item_id = route.path[len("/api/v1/metadataprovider/"):]
                removed = service.store.delete_item("metadata_providers",
                                                    item_id)
                service.reload_metadata()
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
                if "dat_path" in body:
                    # A stored path nothing re-reads is a setting that lies;
                    # reload_dats both stores and applies it.
                    service.reload_dats(str(body.get("dat_path") or ""))
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

    # Native HTTPS, for installs with no reverse proxy in front. Both
    # variables or neither: half a cert is a typo, and refusing to boot over
    # a typo would take the whole service down -- so it logs, serves HTTP,
    # and the operator reads why.
    e = env if env is not None else os.environ
    cert = e.get("ROMARR_SSL_CERT", "")
    key = e.get("ROMARR_SSL_KEY", "")
    scheme = "http"
    if cert or key:
        import ssl
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert, keyfile=key or None)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        except (OSError, ssl.SSLError) as err:
            log.error("could not load ROMARR_SSL_CERT/ROMARR_SSL_KEY (%s); "
                      "serving plain HTTP", err)

    log.info("ROMarr listening on %s://0.0.0.0:%d, library=%s",
             scheme, port, service.library)
    httpd.serve_forever()
