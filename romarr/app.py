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

import inspect
import json
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import copy, deepcopy
import logging
import os
import pathlib
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse, urlsplit, urlunsplit

import time

import requests

from .libraries import (
    FolderScanError, LIBRARY_TYPES, RETROBAT_FOLDERS, build_library, build_library_from_config, library_counts,
    load_backend_plugins, merge_library_secrets, redact_library, route_library,
)
from .clients import QBittorrent, QbitConfig, QbitAuthenticationError, Romm, RommConfig
from . import hub  # ROM Hub bridge -- the Cartridge plugin layer
from . import clamav, scanners, nup, mdm
from .emulators import EmulatorHub
from .libretro import CoreHub
from .emulator_catalog import resource_catalog
from .dat import DatIndex, parse_dat
from .decompress import run_batch as run_decompress_batch, scan_compressed
from .titled import TitleDBIndex, build_index as build_titled_index, validate_switch_rom
from .downloaders import (
    Blackhole, CLIENT_TYPES, DebridClient, HubDownloader, NZBGet, NzbgetConfig, SABnzbd, SabConfig, SiteDownloader, build_client,
    hand_off_with_id, match_queue_item, merge_secrets, pick_client, redact,
)
from .indexers import INDEXER_TYPES, build_indexer, merge_indexer_secrets, redact_indexer
from .indexers import Prowlarr, ProwlarrConfig, ProwlarrDownloadError
from .library import _custom_platform_folder, import_rom, map_remote_path, platform_dir
from .selective_import import selected_import_path
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
                     FIELD_HELP, FIELD_LABELS, send_test,
                     update_available)
from .profiles import Blocklist, ReleaseProfile, release_id
from .upgrade import is_upgrade, merge_tags, scan as scan_directory
from .metadata import Metadata, calendar as metadata_calendar
from .metadata import discover as metadata_discover
from .openapi import spec as openapi_spec
from .ops import (LogRing, RateLimiter, make_backup, read_backup,
                  render_metrics, to_csv)
from .platforms import PLATFORMS, by_slug, resolve, romm_fs_slug
from .sso import ForwardAuth
from .totp import Totp
from .playability import (
    DOWNLOAD, MOONLIGHT_KINDS, NCI_DEFAULT_PORT, PAIRING_IS_MANUAL, PLAYERS,
    WOLF, MoonlightHost, PlayerPolicy, RetroArchNCI, StreamServer,
    StreamSources, routes_for, routes_for_file)
from .lists import LIST_TYPES

#: List-source fields that are credentials: masked on the way out, kept on
#: edit when the form returns the placeholder.
LIST_SECRETS = ("api_key", "openxbl_key", "npsso", "itchio_key",
                "epic_code", "epic_refresh", "ea_token",
                "battlenet_cookie", "humble_cookie")
from .scheduler import Scheduler, next_search_due
from .selection import (Release, best_release, infer_platform, judge, score,
                        collection_content_kind, collection_content_refusal)
from .store import Event, QueueItem, SeerrRequest, Store
from .ui import favicon_svg as ui_favicon_svg
from .ui import page as ui_page
from .ui import link_page as ui_link_page
from .ui import login_page as ui_login_page
from .version import compare_key, derive_version, is_unknown
from .build_info import get_build_info

log = logging.getLogger(__name__)

# Derived, never typed here. A number that has to be retyped on every release is
# how v1.7.0 shipped calling itself 1.6.0 while the Update page offered an
# upgrade to the build already running. See version.py.
VERSION = derive_version()


def _comparable_version(value: object) -> bool:
    """Only well-formed, named versions may be ordered against release tags."""
    text = str(value or "").strip()
    if text[:1].lower() == "v":
        text = text[1:]
    # Local build metadata (+gSHA, +dirty) is not a stable release identity.
    # Use only the release/pre/post spellings supported by compare_key.
    return not is_unknown(value) and bool(re.fullmatch(
        r"\d+(?:\.\d+)*(?:(?:[._-]?(?:dev|alpha|beta|preview|pre|rc|a|b|c)\.?\d*)|(?:[._-]?post\.?\d+))*",
        text, flags=re.IGNORECASE))

# What ROMarr labels its own downloads with, so its jobs are distinguishable
# from everything else in a shared client -- the same reason Radarr and Sonarr
# each use a category of their own.
DEFAULT_CATEGORY = "romarr"


def redact_query_credentials(value: str) -> str:
    """Hide URL credentials while preserving a useful HTTP log line."""
    def redact(match: re.Match) -> str:
        marker, name, _ = match.groups()
        if unquote_plus(name).casefold() == "apikey":
            return f"{marker}{name}=[REDACTED]"
        return match.group(0)

    # Decode the parameter *name* for the decision, because parse_qs does too:
    # `api%6bey=` authenticates and therefore has to be redacted just as the
    # ordinary spelling is. Leave every non-credential parameter byte-for-byte
    # intact so the access log remains useful.
    return re.sub(r"([?&])([^=&\s\"]+)=([^&\s\"]*)", redact, str(value))


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


# QueueItem is defined in .store, beside the other things that survive a
# restart, and re-exported here because that is where callers have always
# imported it from.


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


def _dat_version_key(version: str) -> tuple:
    """Order numeric version components numerically, including dotted dates."""
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r"(\d+)", version or "") if part)


def _find_dats(root: "Path") -> tuple[list["Path"], str]:
    """Candidate DAT files under `root`, without walking the world.

    Returns the paths and, when the scan stopped early, a sentence saying so --
    silence would leave an operator wondering why only some of their DATs
    loaded.

    Also finds .zip archives containing DAT files, since NoIntro distributes
    their DATs as ZIP files.
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
            if lowered.endswith((".dat", ".xml", ".zip")):
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
    if isinstance(err, FolderScanError):
        failures = err.folder_failures
        return (f"Library read incomplete: {len(failures)} filesystem errors "
                f"({', '.join(sorted(set(failures)))}). Readable games remain available. "
                "Check the mount and read permissions, then retry.")
    if isinstance(err, BlockingIOError):
        return ("Storage is temporarily busy after bounded retries. Check the NAS "
                "mount and Docker file sharing, then retry the library refresh.")
    if isinstance(err, PermissionError):
        return "Library access denied. Check the container's mount and read permissions."
    if isinstance(err, FileNotFoundError):
        return "Library folder is missing. Check that the NAS is mounted inside the container."
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


_WORD = re.compile(r"[a-z0-9]+")


def _title_key(value: str) -> str:
    """Normalise a title to lowercase alphanumeric words for comparison.

    Keeping tokens separate makes sequel numbers distinct (for example, 3 is
    not part of 3D).
    """
    return " ".join(_WORD.findall(str(value or "").lower()))



def _fulfil_identities(name: str, outcomes) -> set[str]:
    """Return raw names for the completed download and verified import.

    Keep separators so matching can distinguish a delimited subtitle from a
    sequel number.
    """
    identities = {str(name or "")}
    for outcome in outcomes or []:
        if not getattr(outcome, "ok", False):
            continue
        verification = getattr(outcome, "verification", None)
        game = getattr(verification, "game", "") or ""
        if game:
            identities.add(str(game))
        dest = getattr(outcome, "destination", None)
        if dest is not None:
            destination_name = dest.name if dest.is_dir() else dest.stem
            identities.add(str(destination_name))
    return identities



_REGION_TAG = re.compile(r"\([^)]*\)")
_TITLE_STOPWORDS = {"the"}


def _significant_tokens(title: str) -> list[str]:
    """Meaningful words of a title: region/lang tags stripped, stopwords out."""
    cleaned = _REGION_TAG.sub(" ", title or "")
    return [t for t in re.findall(r"[a-z0-9]+", cleaned.lower())
            if t not in _TITLE_STOPWORDS]


def _subtitle_prefix_matches(short_title: str, full_title: str) -> bool:
    """Match a short title only when the full name marks a subtitle boundary.

    An ordinary following word can be a sequel marker (World 2), so a
    whole-word substring alone is not enough to clear a Wanted entry.
    """
    short_key = _title_key(_REGION_TAG.sub(" ", short_title or ""))
    if len(_significant_tokens(short_title)) < 2:
        return False
    cleaned = _REGION_TAG.sub(" ", full_title or "").strip()
    parts = re.split(r"\s+[-–—]\s+|:\s+", cleaned, maxsplit=1)
    if len(parts) != 2 or _title_key(parts[0]) != short_key:
        return False
    suffix = _title_key(parts[1]).split()
    return bool(suffix) and not suffix[0].isdigit()


def _titles_are_same(title_a: str, title_b: str) -> bool:
    """Compare game titles without treating a sequel or series name as its game.

    Region tags and punctuation do not matter. Short catalog names may match a
    longer DAT name when a dash or colon explicitly starts a subtitle, while
    reordered aliases require the same significant words on both sides.
    """
    a = _title_key(_REGION_TAG.sub(" ", title_a or ""))
    b = _title_key(_REGION_TAG.sub(" ", title_b or ""))
    if not a or not b:
        return False
    if a == b:
        return True
    if (_subtitle_prefix_matches(title_a, title_b)
            or _subtitle_prefix_matches(title_b, title_a)):
        return True
    return sorted(_significant_tokens(title_a)) == sorted(
        _significant_tokens(title_b))



class ProxyResponse(tuple):
    """3-tuple subclass holding response headers so callers can unpack as (code, ctype, raw) or inspect .headers."""
    headers: dict[str, str]

    def __new__(cls, code: int, ctype: str, raw: bytes, headers: dict | None = None):
        instance = super().__new__(cls, (code, ctype, raw))
        instance.headers = headers or {}
        return instance


class ROMarr:
    """The service. Holds config, clients, and the in-flight queue."""

    QUEUE_PROBE_TIMEOUT = 5.0

    def __init__(self, env: dict[str, str] | None = None):
        """Initialize the service state, download/library clients, and persistent store."""
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
        # Releases offered by a search, keyed by search then by release id, so
        # the Search page can grab one without ever being handed a download URL
        # carrying Prowlarr's API key.
        self._candidates: dict[str, dict] = {}
        self._lock = threading.Lock()
        # A manual check and the daily sweep must never overlap or auto-apply twice.
        self._update_check_lock = threading.Lock()
        self._collection_lock = threading.RLock()
        self._probe_lock = threading.Lock()
        self._inflight_probes: dict[int, tuple[object, threading.Event, dict[str, object]]] = {}
        # Background sweeps overlap, and client I/O happens before the queue row
        # exists. Reserve a game during that gap, then let the queue row take
        # over after the handoff.
        self._automatic_grab_reservations: set[tuple[str, str]] = set()
        self._dat_reload_lock = threading.Lock()
        # Coordinates provider writes, reloads and revision-bearing reads.
        self._metadata_lock = threading.RLock()
        self._collection_source_lock = threading.Lock()
        self._mdm_lock = threading.Lock()
        # Lets a configuration change wake the background refresh instead of
        # waiting out its interval. Created before reload_libraries, which sets
        # it.
        self._refresh_now = threading.Event()
        self._started = time.monotonic()

        # History, Wanted and settings survive a restart. Without this a restart
        # lost everything you had asked for, which is the difference between a
        # tool and a demo.
        self.store = Store(e.get("ROMARR_DATA", "/opt/romarr/romarr.json"))
        self._remember_collection_owners()
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

        # Real RetroArch, as opposed to a custom HTTP server in front of one.
        # The Network Control Interface is a UDP socket -- 55355 by default,
        # off unless `network_cmd_enable = "true"` -- that answers VERSION,
        # GET_STATUS and GET_CONFIG_PARAM and nothing ROMarr would send
        # anyway (the client is read-only by allowlist, see
        # `playability.RetroArchNCI`). It reports what is playing, never
        # which cores are installed, so it grants a stream route to exactly
        # one platform at a time: the one whose game is loaded right now.
        # Weak on its own, and still worth wiring, because for that platform
        # the evidence is a running core rather than a guess.
        nci_host = e.get("RETROARCH_NCI_HOST", "")
        try:
            nci_port = int(e.get("RETROARCH_NCI_PORT", "") or NCI_DEFAULT_PORT)
        except ValueError:
            nci_port = NCI_DEFAULT_PORT
        self.store.settings["_retroarch_nci"] = (
            f"{nci_host}:{nci_port}" if nci_host else "")
        self.retroarch_nci = RetroArchNCI(nci_host, port=nci_port) \
            if nci_host else None

        # All the stream tiers behind the single slot `routes_for` takes. The
        # HTTP stream server goes first because it knows which cores it has,
        # then real RetroArch's NCI -- which knows what is *loaded*, a proof
        # for one platform -- and last the Moonlight host, which is inferring
        # from application names. Confidence order, so an inferred answer
        # never displaces a known one.
        self.stream = (StreamSources(self.retroarch, self.retroarch_nci,
                                     self.moonlight)
                       if (self.retroarch or self.retroarch_nci
                           or self.moonlight) else None)

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
        # The environment seeds a new install. An explicit saved selection
        # (including the Setup Guide) must survive the next restart.
        self.reload_dats(self.store.settings.get("dat_path", "")
                         or e.get("DAT_PATH", ""))
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
        # OIDC sign-in (Authentik or any IdP). Independent of forward mode:
        # this is ROMarr being the relying party itself, so it works on a
        # directly-reachable install with no proxy in front.
        self.oidc = None
        self.reload_oidc()

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
        self._library_scan: dict = {}
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

        #: (path, archive_is_the_rom, size, mtime_ns, ctime_ns) -> tuple of
        #: streamed member hash dicts for reconcile_platform (#161).
        self._reconcile_hash_cache: OrderedDict[
            tuple[str, bool, int, int, int], tuple[dict, ...]] = OrderedDict()
        self._reconcile_cache_lock = threading.Lock()

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
        # Often enough that a dead grab is replaced the same evening, rarely
        # enough that the replacement searches it triggers are not a hammering.
        self.scheduler.add(
            "FailedDownloads", "Retire dead downloads and grab a replacement",
            lambda: (900 if self.store.settings.get(
                "blocklist_failed_downloads", True) else 0),
            lambda: self.retire_dead_downloads()["message"])
        self.scheduler.add(
            "ListSync", "Sync import lists into Wanted",
            _hours("list_sync_interval_hours"),
            lambda: self.list_sync()["message"])
        self.scheduler.add(
            "UpdateCheck", "Check github.com for a newer ROMarr",
            lambda: 86400 if self.store.settings.get("update_check", True) else 0,
            lambda: self.check_update()["message"])
        # Plugins update on the same daily clock, gated by their own setting:
        # each re-installs from the source its registry entry recorded, so
        # "latest from GitHub" is the Hub's own install path, not a git
        # command this process invents.
        self.scheduler.add(
            "PluginUpdate", "Update ROM Hub plugins from their sources",
            lambda: (86400 if self.store.settings.get(
                "auto_update_plugins", False) else 0),
            lambda: hub.update_plugins()["message"])
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

            self._library_scan = {"running": True, "started": time.monotonic(),
                                  "read": 0, "stage": "counting", "library": "",
                                  "limit_reached": False}
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
                self._library_scan.update(library=label, stage="counting")
                streamer = getattr(backend, "iter_games", None)
                if callable(streamer):
                    # Folder backends can yield each file once. Counting then
                    # rewalking for every page is quadratic over a NAS tree.
                    from itertools import islice

                    self._library_scan["stage"] = "reading"
                    stream = None
                    try:
                        stream = iter(streamer(limit=self.LIBRARY_MAX))
                        read = 0
                        while read < self.LIBRARY_MAX:
                            batch = []
                            read_error = None
                            try:
                                # extend keeps items yielded before an error;
                                # constructing a list would discard that tail.
                                batch.extend(islice(stream, min(self.LIBRARY_PAGE,
                                                                self.LIBRARY_MAX - read)))
                            except Exception as err:
                                read_error = err
                            if batch:
                                shelf.extend(replace(g, source=label) if multiple else g
                                             for g in batch)
                                read += len(batch)
                                total += len(batch)
                                if on_disk is not None:
                                    on_disk += len(batch)
                                self._library_scan["read"] = len(shelf)
                                self._count_split = {"total": total, "on_disk": on_disk,
                                                     "catalogued": catalogued}
                                self._publish_library(shelf, "", partial=True)
                            if read_error is not None:
                                raise read_error
                            if not batch:
                                break
                        if read >= self.LIBRARY_MAX:
                            self._library_scan["limit_reached"] = True
                    except Exception as err:
                        ok = False
                        failures.append(f"{label}: {err.__class__.__name__}")
                        reasons[label] = _read_failure(err)
                        log.warning("library refresh failed for %s: %s",
                                    label, err.__class__.__name__)
                    finally:
                        close = getattr(stream, "close", None)
                        if callable(close):
                            close()
                    continue
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
                    self._library_scan["stage"] = "reading"
                    while offset < self.LIBRARY_MAX:
                        batch = backend.games(
                            limit=min(self.LIBRARY_PAGE, self.LIBRARY_MAX - offset),
                            offset=offset, timeout=backend.BACKGROUND_TIMEOUT)
                        if not batch:
                            break
                        shelf.extend(
                            replace(g, source=label) if multiple else g
                            for g in batch)
                        offset += len(batch)
                        self._library_scan["read"] = len(shelf)
                        self._publish_library(shelf, "", partial=True)
                        if len(batch) < self.LIBRARY_PAGE:
                            break
                    if offset >= self.LIBRARY_MAX:
                        self._library_scan["limit_reached"] = True
                except Exception as err:
                    ok = False
                    failures.append(f"{label}: {err.__class__.__name__}")
                    reasons.setdefault(label, _read_failure(err))
                    log.warning("library refresh failed for %s: %s",
                                label, err.__class__.__name__)

            self._library_reasons = reasons
            self._library_scan.update(running=False, finished=time.monotonic(),
                                      stage="complete" if ok else "error")
            if shelf or ok:
                self._count_cache = (total, time.monotonic())
                self._count_split = {"total": total, "on_disk": on_disk,
                                     "catalogued": catalogued}
                self._publish_library(shelf, "; ".join(failures),
                                      partial=not ok or self._library_scan["limit_reached"])
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

    def _library_scan_status(self) -> tuple[dict, str]:
        """Expose scan progress without paths, credentials, or filesystem reads."""
        state = dict(getattr(self, "_library_scan", {}))
        if not state:
            return {}, ""
        elapsed = max(0, int(state.get("finished", time.monotonic()) - state["started"]))
        status = {k: state[k] for k in ("running", "read", "stage", "library", "limit_reached")
                  if k in state}
        status["elapsed_seconds"] = elapsed
        label = state.get("library") or "the library"
        read = int(state.get("read") or 0)
        if state.get("running"):
            action = "Getting the library count" if state.get("stage") == "counting" else "Reading games"
            message = f"{action} from {label}: {read:,} read, {elapsed}s elapsed."
        elif state.get("stage") == "error":
            message = f"Library read incomplete: {read:,} games read. Check the library connection or mount."
        elif state.get("limit_reached"):
            message = f"Scan limit reached after {read:,} games; more files may remain."
        else:
            message = f"Read {read:,} games in {elapsed}s."
        return status, message

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
        scan, scan_message = self._library_scan_status()
        if games is None:
            where = ", ".join(names) or getattr(self.game_library, "name", "library")
            return {"items": [], "loading": True,
                    "error": err or "",
                    "libraries": names,
                    "scan": scan, "scan_message": scan_message,
                    "message": scan_message or f"Reading the library from {where}…"}

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
                "scan": scan, "scan_message": scan_message,
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
        if (cfg or {}).get("type") == "retrobat":
            return "flat"
        store = getattr(self, "store", None)
        fallback = store.settings.get("library_layout") if store is not None else "flat"
        return str((cfg or {}).get("layout") or fallback or "flat")

    def platform_folders(self, cfg: dict | None = None) -> dict[str, str]:
        """Per-platform folder name overrides, merging global and library settings."""
        merged: dict[str, str] = {}
        store = getattr(self, "store", None)
        global_folders = store.settings.get("platform_folders") if store is not None else {}
        for source in (global_folders,
                       RETROBAT_FOLDERS if (cfg or {}).get("type") == "retrobat" else {},
                       (cfg or {}).get("platform_folders")):
            if isinstance(source, dict):
                for k, v in source.items():
                    if k and v:
                        merged[str(k)] = str(v)
            elif isinstance(source, list):
                for row in source:
                    if isinstance(row, dict):
                        k = row.get("platform") or row.get("slug")
                        v = row.get("folder") or row.get("name")
                        if k and v:
                            merged[str(k)] = str(v)
        return merged

    def _rescan_library(self, target_lib, target_cfg: dict | None,
                        platform_slug: str, *,
                        platform_folders: dict[str, str] | None = None) -> None:
        """Trigger a library rescan, forwarding custom `platform_folders` when supported."""
        rescan = getattr(target_lib, "rescan", None)
        if not callable(rescan):
            return
        folders = (platform_folders if platform_folders is not None
                   else self.platform_folders(target_cfg))
        custom = _custom_platform_folder(platform_slug, folders)
        if custom:
            try:
                params = inspect.signature(rescan).parameters
                accepts_fs_slug = ("fs_slug" in params or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in params.values()))
            except (TypeError, ValueError):
                accepts_fs_slug = False
            if accepts_fs_slug:
                rescan(platform_slug, fs_slug=custom)
                return
        rescan(platform_slug)

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

    def _nzb_allowed_origins(self) -> tuple[str, ...]:
        """Return configured Prowlarr and direct indexer origins allowed for NZB fetches."""
        urls: list[str] = []
        prowlarr_cfg = getattr(self.prowlarr, "_config", None)
        if prowlarr_cfg is not None:
            url = str(getattr(prowlarr_cfg, "base_url", "") or "").strip()
            if url:
                urls.append(url)
        stored_prowlarr = str(self.store.settings.get("_prowlarr_url") or "").strip()
        if stored_prowlarr:
            urls.append(stored_prowlarr)
        for cfg in self.store.list_items("indexers"):
            if not cfg.get("enable", True):
                continue
            url = str(cfg.get("url") or "").strip()
            if url:
                urls.append(url)
        return tuple(dict.fromkeys(urls))

    def _build_client(self, cfg: dict):
        """Instantiate a configured client, preserving legacy 1-arg build_client mocks."""
        params = inspect.signature(build_client).parameters.values()
        if any(p.name == "data_dir" or p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            client = build_client(cfg, data_dir=str(self.store.path.parent))
        else:
            client = build_client(cfg)
        if client is not None and hasattr(client, "allowed_origins_provider"):
            client.allowed_origins_provider = self._nzb_allowed_origins
        return client

    def reload_clients(self) -> None:
        """Rebuild the live clients from stored configuration.

        Called after any edit so a saved change takes effect immediately --
        an *arr that needs restarting to use a client you just added is not
        one anybody would call configured.
        """
        with self._metadata_lock:
            clients = []
            for cfg in self.store.list_items("download_clients"):
                if not cfg.get("enable", True):
                    continue
                client = self._build_client(cfg)
                if client is not None:
                    client.romarr_id = str(cfg.get("id") or "")
                    client.romarr_name = str(cfg.get("name") or "")
                    # A client that has to READ a file another host wrote needs
                    # the translation table, and the table belongs to the install
                    # rather than to any one client row -- so it is handed over
                    # here, where both are in scope, instead of being duplicated
                    # into every configuration that might need it.
                    if hasattr(client, "path_mappings"):
                        client.path_mappings = (
                            self.store.settings.get("remote_path_mappings") or [])
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
        # General Settings shows the configured, enabled qBittorrent clients.
        # The legacy _qbit_url is only the startup environment value.
        qbit_urls = []
        for client in self.clients:
            if not isinstance(client, QBittorrent):
                continue
            url = str(client._config.base_url or "")
            if not url:
                continue
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            # A pasted URL can contain embedded credentials; never expose them.
            netloc = parts.netloc.rsplit("@", 1)[-1]
            qbit_urls.append(urlunsplit((parts.scheme, netloc, parts.path, "", "")))
        out["_qbit_urls"] = qbit_urls
        # The two settings that ARE the credential. Removed by name rather
        # than by a leading-underscore rule, because the other underscore keys
        # (`_prowlarr_url`, `_romm_url`, `_stream_url`) are deliberately shown
        # -- a rule would have hidden the wrong half of them.
        # `_peers` carries one bearer token per relationship. It is removed
        # here by name for the same reason as the two above: the underscore
        # is a naming convention, not a boundary, and `_romm_url` proves the
        # convention is not a reliable one to filter on.
        for secret in ("_api_key", "_password_hash", "_peers",
                       "_oidc_client_secret"):
            out.pop(secret, None)
        return out

    def setup_status(self) -> dict:
        """A quick setup checklist, without probing services or returning secrets."""
        def rows(key):
            return [{"id": str(row.get("id") or ""),
                     "name": str(row.get("name") or row.get("type") or "Unnamed"),
                     "type": str(row.get("type") or ""),
                     "enabled": bool(row.get("enable", True))}
                    for row in self.store.list_items(key)]

        libraries = rows("libraries")
        clients = rows("download_clients")
        indexers = rows("indexers")
        guide = self.store.settings.get("_setup_guide") or {}
        established = bool(libraries or clients or indexers
                           or self.store.events or self.store.wanted)
        return {
            "suggested": not established and not bool(guide.get("dismissed")),
            "dismissed": bool(guide.get("dismissed")),
            "mode": guide.get("mode", "organize"),
            "library_path": str(self.library),
            "libraries": libraries,
            "download_clients": clients,
            "indexers": indexers,
            "dat_path": str(self.store.settings.get("dat_path") or ""),
            "dat_loaded": len(self.dats.dats),
            "dat_games": sum(len(dat.games) for dat in self.dats.dats),
            "hub_available": hub.available(),
        }

    def finish_setup_guide(self, mode: str = "organize") -> dict:
        """Remember Skip or Finish; the guide remains available in Settings."""
        self.store.settings["_setup_guide"] = {
            "dismissed": True,
            "mode": mode if mode in {"organize", "download"} else "organize",
        }
        self.store.save()
        return {"ok": True}

    def setup_dats(self, directory: str) -> dict:
        """Validate the optional DAT folder and explain an empty result."""
        directory = str(directory or "").strip()
        if not directory:
            return {"ok": False, "error": "Choose the folder containing your DAT files."}
        if not Path(directory).is_dir():
            return {"ok": False,
                    "error": "That folder is not available to ROMarr. Check the path and, "
                             "if you use Docker, its folder mount."}
        with self._dat_reload_lock:
            result = self._reload_dats(directory, preserve_on_empty=True)
            if result.get("error") or not result.get("loaded"):
                # A rejected candidate was never published to active readers.
                if result.get("error"):
                    return {"ok": False, **result}
                return {"ok": False, **result,
                        "error": "No readable DATs were found. Choose a folder containing "
                                 "No-Intro or Redump .dat, .xml, or .zip files."}
            self.store.save()
            return {"ok": True, **result,
                    "message": f"Loaded {result['loaded']} DAT(s). You can now verify files "
                               "and compare your collection."}

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
            log.warning("GG Requestz webhook rejected: not a game request")
            return {"ok": False, "error": "not a game request"}
        game, platform_name = parsed

        platform = resolve(platform_name) if platform_name else None
        if platform is None:
            # Recorded rather than dropped: an unknown platform is a mapping
            # problem somebody can fix, and silence would hide it.
            self.store.record(Event(kind="failed", game=game,
                                    platform=platform_name or "unknown",
                                    detail=f"unknown platform: {platform_name!r}"))
            log.warning("GG Requestz webhook rejected: unknown platform %r "
                        "for %r", platform_name, game)
            return {"ok": False, "error": f"unknown platform: {platform_name!r}",
                    "game": game}

        log.info("GG Requestz webhook accepted: %r for %s", game, platform.slug)
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
        if key == "********":
            # The placeholder reached here with nothing stored to merge it
            # against -- testing eight asterisks would produce a 401 and a
            # misleading "Rejected the API key".
            return {"ok": False,
                    "message": "the API key field still holds the masked "
                               "placeholder -- type the real key"}
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
        client = self._build_client(cfg)
        if client is None:
            return {"ok": False, "message": f"unknown client type: {cfg.get('type')!r}"}
        try:
            ok = client.reachable()
        except Exception as err:
            return {"ok": False, "message": f"{type(err).__name__}: {err}"}
        detail = str(getattr(client, "detail", "") or "").strip()
        if ok:
            msg = f"Connected ({detail})" if detail else "Connected"
        else:
            msg = detail or "Could not connect or authenticate"
        return {"ok": bool(ok), "message": msg}

    def _library_folder_warnings(self, backend, cfg: dict,
                                 root: Path) -> list[str]:
        """Warn when RomM folder bindings or disk folders diverge from `platform_folders`."""
        warnings: list[str] = []
        seen: set[str] = set()

        def _add(msg: str) -> None:
            if msg not in seen:
                seen.add(msg)
                warnings.append(msg)

        folders = self.platform_folders(cfg)
        known_romm = {p.slug: p for p in PLATFORMS}
        known_romm.update({romm_fs_slug(p.slug): p for p in PLATFORMS})
        lister = (getattr(backend, "platform_bindings", None)
                  or getattr(backend, "platforms", None))
        if callable(lister):
            try:
                rows = lister() or []
            except Exception as err:
                log.debug("romm binding check skipped: %s", err)
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_slug = str(row.get("slug") or "").strip()
                fs_slug = str(row.get("fs_slug") or "").strip()
                if not raw_slug or not fs_slug:
                    continue
                label = str(row.get("platform") or raw_slug)
                plat = known_romm.get(raw_slug.lower())
                if plat is None and int(row.get("count") or 0) <= 0:
                    continue
                if plat is None:
                    plat = resolve(raw_slug)
                canon = plat.slug if plat is not None else raw_slug.lower()
                custom = _custom_platform_folder(canon, folders)
                default_slug = romm_fs_slug(canon)
                if custom:
                    # Only compare populated rows or canonical slug entries so an empty
                    # sibling platform in RomM (e.g. `nintendo-dsi`) does not falsely
                    # flag an `nds` custom folder override.
                    if fs_slug != custom and (raw_slug.lower() in {canon, default_slug}
                                              or int(row.get("count") or 0) > 0):
                        _add(f"RomM binds '{label}' ({raw_slug}) to '{fs_slug}', "
                             f"while ROMarr's platform_folders uses '{custom}'")
                elif plat is not None and fs_slug not in {default_slug, canon, raw_slug.lower()}:
                    _add(f"RomM binds '{label}' ({raw_slug}) to '{fs_slug}', "
                         f"while ROMarr will write to '{default_slug}' without a "
                         "platform_folders override")

        checked_canon: set[str] = set()
        for raw_key in folders:
            plat = resolve(raw_key) or by_slug(str(raw_key).strip().lower())
            if plat is None or plat.slug in checked_canon:
                continue
            checked_canon.add(plat.slug)
            custom = _custom_platform_folder(plat.slug, folders)
            default_slug = romm_fs_slug(plat.slug)
            if custom and custom != default_slug and (root / default_slug).is_dir():
                _add(f"Default folder '{default_slug}' exists alongside "
                     f"platform_folders '{custom}' and may cause duplicate "
                     "platforms in RomM")
        return warnings

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
        if str(cfg.get("type") or "romm").lower() == "romm":
            try:
                warnings = self._library_folder_warnings(backend, cfg, root)
            except Exception as err:
                log.debug("library folder warning check failed: %s", err)
                warnings = []
            if warnings:
                summary = "; ".join(warnings[:3])
                if len(warnings) > 3:
                    summary += f" (+{len(warnings) - 3} more)"
                return {"ok": True, "path_ok": True,
                        "message": f"Connected -- Warning: {summary}",
                        "warnings": warnings}
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
            # The hub lane joins here too, or this endpoint answers a
            # different question than the request path it claims to mirror.
            releases = releases + self._hub_releases(game, None)
        pick = best_release(releases, game, platform,
                           profile=self.profile_for(platform), blocklist=self.blocklist)
        scored = sorted(
            ((score(r, game, platform, profile=self.profile_for(platform)), r) for r in releases),
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

    def _hub_enabled(self) -> bool:
        """Whether the hub acquisition lane is wired up.

        Both halves are required: an enabled plugin that declares `search`
        (something to ask) and a configured ROM Hub download client
        (somewhere to stage what comes back). Results that cannot be grabbed
        are noise on a search page, so the lane stays quiet until both exist.
        """
        if not any(getattr(c, "protocol", "") == "hub"
                   and getattr(c, "configured", False)
                   for c in self.clients):
            return False
        return bool(hub.searchable_plugins())

    def _hub_releases(self, game: str, platform=None) -> list:
        """Turn ROM Hub search rows into releases for the shared grab flow.

        Magnet rows from ordinary torrent plugins use the configured torrent
        client. MiNERVA rows carry one exact path inside a collection torrent;
        ROMarr preserves that path and requires qBittorrent to pause the new
        torrent, verify the path, deselect every other file, and only then
        resume. Rows missing the resolved magnet or exact path are omitted
        rather than offered as a full-collection download.

        Non-torrent plugin rows keep their hub download URL and are fetched
        through ROM Hub's bounded FetchPlan lane.
        """
        if not self._hub_enabled():
            return []
        from .selection import Release
        try:
            rows, err = hub.search(game,
                                   platform=platform.slug if platform else None)
        except Exception as exc:
            log.warning("rom hub search failed for %r: %s", game, exc)
            return []
        if err:
            log.warning("rom hub search warning: %s", err)
            if not rows:
                return []
        # Magnet-bearing rows get their plugin's `torrent` capability run
        # over them first -- host-gated, tracker-augmented. One grouped pass,
        # never one subprocess per row; see hub.resolve_torrents.
        try:
            rows = hub.resolve_torrents(rows)
        except Exception as exc:  # defensive: resolution must never kill a search
            log.warning("hub torrent resolution failed: %s", exc)
        out = []
        for row in rows:
            if not row.get("title"):
                continue
            plugin_slug = str(row.get("plugin") or "")
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            is_minerva = plugin_slug == "minerva-archive"
            magnet = str(row.get("magnet") or "")
            file_path = str(extra.get("path") or "")
            if is_minerva:
                # MiNERVA results are individual files in a collection
                # torrent. If either the resolved magnet or exact source path
                # is missing, omit the result rather than offer the whole set.
                if not magnet.startswith("magnet:") or not file_path:
                    continue
                out.append(Release(
                    title=row["title"],
                    size=int(row.get("size_bytes") or 0),
                    seeders=1,
                    categories=(1000,),
                    download_url=magnet,
                    protocol="torrent",
                    indexer=f"hub:{plugin_slug}",
                    info_url=str(row.get("url") or ""),
                    torrent_file_path=file_path,
                    content_kind=collection_content_kind(file_path),
                    file_selection_required=True,
                ))
                continue
            if magnet.startswith("magnet:"):
                out.append(Release(
                    title=row["title"],
                    size=int(row.get("size_bytes") or 0),
                    # A torrent source publishes no seeder count; the same
                    # reasoning as the RSS and MiNERVA drivers: 0 reads as
                    # "dead" to the scorer, and unknown is not dead.
                    seeders=1,
                    categories=(1000,),
                    download_url=magnet,
                    protocol="torrent",
                    indexer=f"hub:{plugin_slug or 'plugin'}",
                    info_url=str(row.get("url") or ""),
                ))
                continue
            out.append(Release(
                title=row["title"],
                size=int(row.get("size_bytes") or 0),
                seeders=0,
                # A plugin result is a game by definition -- it came from a
                # ROM source. 1000 is the console range the scorer accepts;
                # inventing a category would make `is_game_release` reject
                # every row the Hub returned.
                categories=(1000,),
                download_url=hub.encode_result(row),
                protocol="hub",
                indexer=f"hub:{row.get('plugin') or 'plugin'}",
                info_url=str(row.get("url") or ""),
            ))
        return out

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
        # Prowlarr plus every directly-configured indexer. One source failing
        # must never lose another's results -- an indexer that is down, rate
        # limited or mid-login is the normal case across a dozen trackers, not
        # an outage worth abandoning the search for.
        prowlarr = self.prowlarr
        sources: list[tuple[str, object]] = [("prowlarr", prowlarr)]
        sources += [(getattr(i, "name", "indexer"), i)
                    for i in getattr(self, "indexers", [])]

        def search_term(term):
            """Query all captured indexer sources for one search string."""
            releases = []
            with ExitStack() as sessions:
                for label, source in sources:
                    client = source
                    try:
                        if isinstance(source, Prowlarr) or getattr(source, "_owns_session", False):
                            client = copy(source)
                        if getattr(source, "_owns_session", False):
                            client._session = sessions.enter_context(requests.Session())
                        if isinstance(client, Prowlarr) and source is prowlarr:
                            client._privacy = privacy
                        releases.extend(client.search(term))
                    except Exception as err:
                        log.warning("%s search failed for %r: %s", label, term, err)
            return releases

        # Enabled ROM Hub plugins are searched alongside the indexers: the
        # same titles, scored by the same rules, grabbable from the same
        # Interactive Search page. One query, not per-term: the Hub's own
        # fan-out already merges every plugin's answer for it.
        with ThreadPoolExecutor(max_workers=3) as pool:
            hub_search = pool.submit(self._hub_releases, game, platform)
            privacy = {}
            if isinstance(prowlarr, Prowlarr):
                try:
                    privacy = prowlarr._private_ids()
                except Exception as err:
                    log.warning("prowlarr privacy lookup failed: %r", err)
            searches = [pool.submit(search_term, game),
                        pool.submit(search_term, f"{game} {platform.name}"),
                        hub_search]
        seen: set[tuple[str, str]] = set()
        merged = []
        for search in searches:
            try:
                releases = search.result()
            except Exception as err:
                log.warning("release search task failed: %r", err)
                continue
            for release in releases:
                key = (release.title, release.download_url)
                if key not in seen:
                    seen.add(key)
                    merged.append(release)
        return merged

    def request(self, game: str, platform_name: str, external_request_id: str = "") -> dict:
        """Request a game. If external_request_id is set, the row is tracked.

        The same search, scoring, and handoff pipeline answers an internal
        request and an external one (Cartridge). The ``id`` the
        request was issued with is threaded into the ``QueueItem`` so
        state can be reconciled across a restart. Returns ``ok`` on
        handoff, or error string.
        """
        platform = resolve(platform_name)
        if platform is None:
            return {"ok": False, "error": f"unknown platform: {platform_name!r}"}

        releases = self._search_releases(game, platform)
        pick = best_release(releases, game, platform,
                           profile=self.profile_for(platform), blocklist=self.blocklist)
        if pick is None:
            item = QueueItem(game, platform.slug, "", 0, "failed",
                             f"no usable release among {len(releases)} result(s)",
                             external_request_id=external_request_id)
            self.store.enqueue(item)
            self.store.want(game, platform.slug)
            self.store.note_failure(game, platform.slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform.slug,
                                    detail=item.detail))
            if external_request_id:
                self.store.update_seerr_request(external_request_id,
                                                error=item.detail, status="failed")
            return {"ok": False, "error": item.detail}

        if not pick.download_url:
            # The release's own fault, and worth retiring: nothing about the
            # next sweep would choose differently, so without the blocklist
            # this result is picked again every twelve hours forever.
            item = QueueItem(game, platform.slug, pick.title, pick.seeders, "failed",
                             "release offers no usable download link",
                             release_id=release_id(pick),
                             indexer=getattr(pick, "indexer", ""),
                             size=getattr(pick, "size", 0),
                             release_fault=True,
                             external_request_id=external_request_id)
            self.store.enqueue(item)
            self.store.want(game, platform.slug)
            self.store.note_failure(game, platform.slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform.slug,
                                    release=pick.title, detail=item.detail))
            if external_request_id:
                self.store.update_seerr_request(external_request_id,
                                                error=item.detail, status="failed")
            return {"ok": False, "error": item.detail}

        return self.grab(pick, game, platform.slug,
                          external_request_id=external_request_id)

    def _reserve_automatic_grab(self, game: str, platform_slug: str):
        """Reserve one game's automatic handoff without holding the lock over
        indexer or download-client I/O."""
        key = (game.strip().casefold(), platform_slug.strip().casefold())
        with self._lock:
            if key in self._automatic_grab_reservations:
                return None
            if any(q.game.strip().casefold() == key[0]
                   and q.platform.strip().casefold() == key[1]
                   and q.state in ("queued", "grabbed", "import-review")
                   for q in self.queue):
                return None
            self._automatic_grab_reservations.add(key)
        return key

    def _automatic_request(self, game: str, platform_name: str,
                           external_request_id: str = "") -> dict:
        """Request a replacement from a background sweep at most once."""
        platform = resolve(platform_name)
        if platform is None:
            return self.request(game, platform_name, external_request_id)
        key = self._reserve_automatic_grab(game, platform.slug)
        if key is None:
            return {"ok": False, "skipped": True,
                    "error": "a download is already in flight"}
        try:
            if external_request_id:
                return self.request(game, platform.slug,
                                    external_request_id=external_request_id)
            return self.request(game, platform.slug)
        finally:
            with self._lock:
                self._automatic_grab_reservations.discard(key)

    def _automatic_grab(self, pick, game: str, platform_slug: str) -> dict:
        """Hand an RSS-selected release off only if no download is in flight."""
        key = self._reserve_automatic_grab(game, platform_slug)
        if key is None:
            return {"ok": False, "skipped": True,
                    "error": "a download is already in flight"}
        try:
            return self.grab(pick, game, platform_slug)
        finally:
            with self._lock:
                self._automatic_grab_reservations.discard(key)

    def _remember_collection_owners(self):
        from .retained_collection import owner_records
        ledger = self.store.settings.setdefault("retained_collection_owners", [])
        known = {r.get("download_id") for r in ledger}
        changed = False
        for record in owner_records(self.store.queue):
            if record["download_id"] not in known:
                ledger.append(record)
                known.add(record["download_id"])
                changed = True
        if changed:
            self.store.save()

    def _retained_owners_readonly(self, items: list[QueueItem] | None = None) -> list[dict]:
        """Return retained-collection ownership records without mutating or saving store state."""
        from .retained_collection import owner_records
        ledger = list(self.store.settings.get("retained_collection_owners", []))
        known = {r.get("download_id") for r in ledger}
        queue_items = self.queue if items is None else items
        return ledger + [
            record for record in owner_records(queue_items)
            if record["download_id"] not in known
        ]

    def _retained_owners(self):
        self._remember_collection_owners()
        return list(self.store.settings.get("retained_collection_owners", []))

    def grab(self, pick, game: str, platform_slug: str, *, manual: bool = False,
             external_request_id: str = "") -> dict:
        # Serialize collection mutations through durable queue persistence.
        if getattr(pick, "file_selection_required", False):
            with self._collection_lock:
                return self._grab_impl(pick, game, platform_slug, manual=manual,
                                       external_request_id=external_request_id)
        return self._grab_impl(pick, game, platform_slug, manual=manual,
                               external_request_id=external_request_id)

    def _grab_impl(self, pick, game: str, platform_slug: str, *, manual: bool = False,
                   external_request_id: str = "") -> dict:
        """Hand one release to a download client and record what happened.

        Shared by the automatic path and the Search page, deliberately: a
        release chosen by hand must be queued, recorded and fulfilled exactly
        like one the scorer picked, or Activity and Wanted start disagreeing
        with each other depending on how a game was requested.
        """
        platform = resolve(platform_slug) if isinstance(platform_slug, str) else None
        if platform is None:
            return {"ok": False,
                    "error": "Select a valid platform and search again before grabbing. "
                             "A download without a platform cannot be imported."}
        platform_slug = platform.slug
        refusal = collection_content_refusal(pick)
        if refusal:
            return {"ok": False, "error": refusal}
        client = pick_client(pick.protocol, self.clients)
        if client is None:
            # Not the release's fault -- blocklisting it here would punish a
            # perfectly good torrent because the operator has not set up a
            # client yet, and the block would outlive the mistake.
            item = QueueItem(game, platform_slug, pick.title, pick.seeders, "failed",
                             f"no download client configured for {pick.protocol}",
                             release_id=release_id(pick),
                             indexer=getattr(pick, "indexer", ""),
                             size=getattr(pick, "size", 0),
                             external_request_id=external_request_id)
            self.store.enqueue(item)
            self.store.want(game, platform_slug)
            self.store.note_failure(game, platform_slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform_slug,
                                    release=pick.title, detail=item.detail))
            if external_request_id:
                self.store.update_seerr_request(external_request_id,
                                                error=item.detail, status="failed")
            return {"ok": False, "error": item.detail}

        # The title travels with the release for the clients that can carry
        # it: the import sweep matches a finished download to its queue row by
        # that title, and a file named by the site it came from would never
        # match. See downloaders.hand_off.
        # The peer may have cancelled while the search ran; the store says so
        # by refusing the dispatching claim, and nothing is handed off.
        if (external_request_id
                and not self.store.mark_seerr_dispatching(external_request_id)):
            return {"ok": False, "error": "the request was cancelled"}
        failure_detail = f"{client.name} rejected the release"
        release_fault = True
        required_file = bool(getattr(pick, "file_selection_required", False))
        selected_file = str(getattr(pick, "torrent_file_path", "") or "")
        local_fetch = getattr(self.prowlarr, "is_prowlarr_download_url", None)
        supports_selective = isinstance(client, QBittorrent) or bool(
            getattr(client, "supports_selective", False))
        supports_torrent_file = isinstance(client, QBittorrent) or bool(
            getattr(client, "supports_torrent_file", False))
        if required_file and not selected_file:
            ok, download_id = False, ""
            release_fault = False
            failure_detail = "the source did not provide the exact file path"
        elif required_file and not supports_selective:
            ok, download_id = False, ""
            release_fault = False
            failure_detail = (
                "this collection source requires qBittorrent or Standalone MDM CLI for exact file selection")
        elif required_file:
            ok = client.add_magnet_selective(
                pick.download_url, selected_file, owned_selections=self._retained_owners())
            download_id = client.last_job_id if ok else ""
            release_fault = False
            if not ok:
                failure_detail = (client.last_error or
                                  "could not select the exact file")
        elif ((supports_torrent_file or getattr(client, "magnet_only", False))
                and callable(local_fetch)
                and local_fetch(pick.download_url)):
            # Prowlarr's authenticated URL is LAN-only and contains its API
            # key. Fetch it here, then upload only the .torrent bytes to the
            # download client; never give an external client that URL.
            try:
                torrent = self.prowlarr.fetch_torrent(pick.download_url)
            except ProwlarrDownloadError as err:
                ok, download_id = False, ""
                release_fault = False
                failure_detail = str(err)
            else:
                # Prowlarr may answer a .torrent request with a terminal magnet
                # redirect. Keep the safe local fetch path, then pass that URI
                # to the client instead of treating it as torrent bytes.
                if isinstance(torrent, str):
                    ok = client.add(torrent)
                elif supports_torrent_file:
                    ok = client.add_torrent_file(torrent)
                else:
                    ok = False
                download_id = (getattr(client, "last_job_id", "") or "") if ok else ""
                release_fault = False
                if not ok:
                    client_label = getattr(client, "display_name", getattr(client, "name", "Download client"))
                    failure_detail = (
                        f"{client_label} requires a magnet; Prowlarr returned a torrent file"
                        if not isinstance(torrent, str) and not supports_torrent_file
                        else (getattr(client, "last_error", "")
                              or f"{client_label} did not confirm the torrent")
                    )
        else:
            ok, download_id = hand_off_with_id(client, pick.download_url,
                                               name=pick.title)
        item = QueueItem(game, platform_slug, pick.title, pick.seeders,
                         "grabbed" if ok else "failed",
                         "" if ok else failure_detail,
                         release_id=release_id(pick),
                         indexer=getattr(pick, "indexer", ""),
                         size=getattr(pick, "size", 0),
                         release_fault=release_fault if not ok else False,
                         external_request_id=external_request_id,
                         download_id=download_id,
                         download_client=getattr(client, "romarr_id", ""),
                         manual=manual,
                         file_selection_required=required_file,
                         selected_file=(getattr(client, "last_selected_file", {})
                                        if required_file and ok else {}),
                         retained_torrent_hash=(getattr(client, "last_retained_hash", "")
                                                if required_file else ""))
        self.store.enqueue(item)
        if ok and required_file:
            self._remember_collection_owners()
        if ok:
            if external_request_id:
                self.store.update_seerr_request(external_request_id,
                                                status="downloading")
            self.store.record(Event(kind="grabbed", game=game, platform=platform_slug,
                                    release=pick.title, seeders=pick.seeders,
                                    size=getattr(pick, "size", 0),
                                    indexer=getattr(pick, "indexer", ""),
                                    detail="chosen by hand" if manual else ""))
            # The message other tools cannot send: what the scorer weighed.
            # A failure to explain must never fail the grab it explains.
            try:
                reasons = judge(pick, game, resolve(platform_slug),
                                profile=self.profile_for(platform_slug)).why()
            except Exception:
                reasons = ()
            self.notify(grabbed(pick.title, platform_slug,
                                getattr(pick, "indexer", ""), reasons))
        else:
            self.store.want(game, platform_slug)
            self.store.note_failure(game, platform_slug, item.detail)
            self.store.record(Event(kind="failed", game=game, platform=platform_slug,
                                    release=pick.title, detail=item.detail))
            if external_request_id:
                self.store.update_seerr_request(external_request_id,
                                                error=item.detail, status="failed")
        result = {"ok": ok, "release": pick.title, "seeders": pick.seeders}
        if not ok:
            result["error"] = failure_detail
        return result

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
                        else self.prowlarr.search(game)
                        + self._hub_releases(game, None))
        except Exception as err:
            log.warning("interactive search failed for %r: %s", game, err)
            return {"error": f"search failed: {type(err).__name__}",
                    "game": game, "platform": platform.slug if platform else None,
                    "items": []}

        judged = []
        for r in releases:
            inferred = platform or infer_platform(r)
            plat_prof = self.profile_for(inferred)
            judged.append((judge(r, game, inferred,
                                 profile=plat_prof, blocklist=self.blocklist), r))
        judged.sort(key=lambda pair: (-pair[0].points, pair[1].size))

        key = f"{game}|{platform.slug if platform else ''}"
        held = {}
        items = []
        for n, (verdict, r) in enumerate(judged):
            rid = f"{abs(hash((key, r.title, r.size, r.indexer))):x}{n:02d}"
            inferred = platform or infer_platform(r)
            plat_slug = inferred.slug if inferred is not None else ""
            held[rid] = (r, game, plat_slug)
            items.append({
                "id": rid,
                "title": r.title,
                "platform": plat_slug,
                "size": r.size,
                "seeders": r.seeders,
                "indexer": r.indexer,
                "protocol": r.protocol,
                "private": r.private,
                # The release's page on its indexer. Unlike download_url it
                # never carries a credential, so it is the one link the
                # browser may have.
                "info_url": getattr(r, "info_url", ""),
                "torrent_file_path": getattr(r, "torrent_file_path", ""),
                "score": verdict.points,
                "accepted": verdict.accepted,
                "reasons": verdict.why(),
                "grabbable": bool(r.download_url) and not collection_content_refusal(r),
                "grab_refusal": collection_content_refusal(r),
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

    @staticmethod
    def _accepts_timeout_arg(fn) -> bool:
        """Return True when `fn` declares an explicit `timeout` parameter."""
        try:
            return "timeout" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    def _call_with_timeout(self, client, fn, timeout: float):
        """Run a completion probe within a wall-clock wait, capping to one in-flight probe per client."""
        lock = getattr(self, "_probe_lock", None)
        inflight = getattr(self, "_inflight_probes", None)
        if lock is None or inflight is None:
            lock = threading.Lock()
            inflight = {}
            self._probe_lock = lock
            self._inflight_probes = inflight
        key = id(client)
        with lock:
            existing = inflight.get(key)
            if existing is not None and existing[0] is client:
                entry = existing
                spawn = False
            else:
                entry = (client, threading.Event(), {})
                inflight[key] = entry
                spawn = True
        if spawn:
            def _runner():
                """Execute the client probe and publish its outcome to its single-flight entry."""
                _, done_ev, box = entry
                try:
                    box["value"] = fn()
                except BaseException as exc:
                    box["exc"] = exc
                finally:
                    with lock:
                        done_ev.set()
                        if inflight.get(key) is entry:
                            inflight.pop(key, None)

            threading.Thread(target=_runner, daemon=True).start()
        _, done_ev, box = entry
        if not done_ev.wait(timeout=max(0.01, float(timeout))):
            raise TimeoutError("client completed() probe timed out")
        if "exc" in box:
            raise box["exc"]  # type: ignore[misc]
        return box["value"]

    def _completed_queue_downloads(self, items, *, ownership=None, timeout: float | None = None):
        """Yield `(client, completed_rows)` for queue preview checks without fetch or repair writes."""
        if ownership is None:
            with self._lock:
                ownership = self._retained_owners_readonly(list(self.queue))
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        for client in self.clients:
            if not getattr(client, "configured", True):
                continue
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            budget_exhausted = remaining is not None and remaining <= 0
            try:
                if isinstance(client, QBittorrent) or getattr(client, "supports_selective", False):
                    if budget_exhausted:
                        continue
                    raw_timeout = getattr(getattr(client, "_config", None), "timeout", 5) or 5
                    probe_timeout = min(float(raw_timeout), self.QUEUE_PROBE_TIMEOUT)
                    if remaining is not None:
                        probe_timeout = min(probe_timeout, remaining)
                    bounded = max(0.01, probe_timeout)

                    def _probe_selective(c=client, pt=bounded):
                        """Query selective and general torrent completions within `pt`."""
                        local_deadline = time.monotonic() + pt
                        selective = c.selective_completed(
                            items,
                            ownership=ownership,
                            update_cache=False,
                            timeout=pt,
                        )
                        if deadline is not None:
                            left = local_deadline - time.monotonic()
                            if left <= 0:
                                return selective
                            next_timeout = max(0.01, min(pt, left))
                        else:
                            next_timeout = pt
                        try:
                            if isinstance(c, QBittorrent):
                                return selective + c.completed(repair=False, timeout=next_timeout)
                            return selective + c.completed(timeout=next_timeout)
                        except Exception as exc:
                            log.warning("%s completed() failed: %s", getattr(c, "name", c), exc)
                            return selective

                    candidates = (
                        self._call_with_timeout(client, _probe_selective, bounded)
                        if deadline is not None else _probe_selective()
                    )
                elif isinstance(client, DebridClient):
                    if budget_exhausted:
                        candidates = client.completed(fetch=False, timeout=0.0)
                    else:
                        raw_timeout = getattr(getattr(client, "_config", None), "timeout", 5) or 5
                        probe_timeout = min(float(raw_timeout), self.QUEUE_PROBE_TIMEOUT)
                        if remaining is not None:
                            probe_timeout = min(probe_timeout, remaining)
                        bounded = max(0.01, probe_timeout)
                        try:
                            candidates = (
                                self._call_with_timeout(
                                    client,
                                    lambda c=client, pt=bounded: c.completed(fetch=False, timeout=pt),
                                    bounded,
                                )
                                if deadline is not None
                                else client.completed(fetch=False, timeout=bounded)
                            )
                        except TimeoutError as exc:
                            log.warning(
                                "%s completed() timed out; using cached files: %s",
                                getattr(client, "name", client),
                                exc,
                            )
                            candidates = client.completed(fetch=False, timeout=0.0)
                elif isinstance(client, (SiteDownloader, HubDownloader)):
                    candidates = client.completed(fetch=False)
                elif isinstance(client, Blackhole):
                    candidates = client.completed()
                else:
                    if budget_exhausted:
                        continue
                    completed_fn = getattr(client, "completed", None)
                    if not callable(completed_fn):
                        continue
                    raw_timeout = getattr(getattr(client, "_config", None), "timeout", 5) or 5
                    probe_timeout = min(float(raw_timeout), self.QUEUE_PROBE_TIMEOUT)
                    if remaining is not None:
                        probe_timeout = min(probe_timeout, remaining)
                    bounded = max(0.01, probe_timeout)
                    probe_call = (
                        (lambda fn=completed_fn, pt=bounded: fn(timeout=pt))
                        if self._accepts_timeout_arg(completed_fn)
                        else completed_fn
                    )
                    candidates = (
                        self._call_with_timeout(client, probe_call, bounded)
                        if deadline is not None else probe_call()
                    )
            except Exception as exc:
                log.warning("%s completed() failed: %s", getattr(client, "name", client), exc)
                continue
            yield client, candidates

    def queue_rows(self) -> list[dict]:
        """Return queue items annotated with read-only `can_preview_import` flags."""
        with self._lock:
            items = list(self.queue)
            rows = [asdict(item) for item in items]
            ownership = self._retained_owners_readonly(items)
        pending = [item for item in items if item.state not in ("imported", "failed")]
        completed = (
            [(client, download)
             for client, downloads in self._completed_queue_downloads(
                 pending, ownership=ownership, timeout=self.QUEUE_PROBE_TIMEOUT)
             for download in downloads]
            if pending else []
        )
        for item, row in zip(items, rows):
            row["can_preview_import"] = row["state"] not in ("imported", "failed") and any(
                match_queue_item(
                    items,
                    download,
                    client_id=str(getattr(client, "romarr_id", "") or getattr(client, "config_id", "") or ""),
                    client_protocol=str(getattr(client, "protocol", "") or ""),
                ) is item
                and not download.get("_romarr_selection_error")
                for client, download in completed
            )
        return rows

    def queue_import_plan(self, index: int, body: dict) -> dict:
        """Build or confirm an interactive import plan for one queued release."""
        from .import_intent import preview_import, choose_import, queue_identity
        from .romm_bundle import decorate_preview, route_selection
        from .library import MissingArchiveTool
        from requests import RequestException
        import zipfile
        import subprocess
        with self._lock:
            items = list(self.queue)
            if not 0 <= index < len(items):
                return {"error": "no such queue item"}
            item = items[index]
            if item.state == "imported":
                return {"error": "this item has already imported"}
            ownership = self._retained_owners_readonly(items)
        platform = resolve(item.platform)
        target = self.library_for(item.platform) if platform else None
        if target is None:
            return {"error": "configure a platform and library first"}
        torrent = None
        source_client = None
        for client, candidates in self._completed_queue_downloads(
                [item], ownership=ownership, timeout=self.QUEUE_PROBE_TIMEOUT):
            client_id = str(getattr(client, "romarr_id", "") or getattr(client, "config_id", "") or "")
            client_protocol = str(getattr(client, "protocol", "") or "")
            for candidate in candidates:
                if match_queue_item(items, candidate, client_id=client_id, client_protocol=client_protocol) is item:
                    torrent = candidate
                    source_client = client
                    break
            if torrent is not None:
                break
        if torrent is None:
            return {"error": "download is not complete or cannot be found"}
        if torrent.get("_romarr_selection_error"):
            return {"error": torrent["_romarr_selection_error"]}
        path = map_remote_path(torrent.get("content_path") or torrent.get("save_path", ""),
                               self.store.settings.get("remote_path_mappings"))
        try:
            if (getattr(item, "file_selection_required", False) or getattr(item, "selected_file", None)
                    or "minerva" in item.indexer.lower()):
                from .selective_import import selected_import_path
                path = selected_import_path(item, torrent, source_client,
                                            self.store.settings.get("remote_path_mappings"))
            preview = preview_import(path, platform, self.library_root(target[0]),
                                     layout=self.library_layout(target[0]),
                                     platform_folders=self.platform_folders(target[0]),
                                     queue_identity=queue_identity(item))
            preview = decorate_preview(preview, platform, self.library_root(target[0]),
                                       kind=target[0].get("type", ""), layout=self.library_layout(target[0]),
                                       platform_folders=self.platform_folders(target[0]),
                                       translation=is_translation(str(torrent.get("name") or "")))
            intent = None
            if body.get("choices") is not None:
                intent = choose_import(preview, body.get("choices"), body.get("mode", "single"), body.get("existing_game", ""))
                routes = route_selection(preview, intent)
                if routes:
                    intent["routes"] = routes
                    preview["planned_destinations"] = {primary: str(Path(preview["platform_root"]) / route) for primary, route in routes.items()}

            if body.get("confirm"):
                if body.get("token") != preview["token"]:
                    return {"error": "download changed; refresh the preview before importing"}
                if intent is None:
                    raise ValueError("Select payloads before confirmation")
                if intent.get("routes") and body.get("reviewed_destinations") != preview.get("planned_destinations"):
                    raise ValueError("Review the final destination tree before confirming")
                with self._lock:
                    item.import_intent = intent
                    item.state = "grabbed"
                    item.detail = "Import selection saved; source download will be retained"
                    item.import_preview = preview
                self.store.save()
            return {"ok": True, "preview": preview}
        except (OSError, ValueError, RuntimeError, TypeError, MissingArchiveTool, RequestException, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
            return {"error": str(exc)}

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
        if action == "resume-retained":
            with self._collection_lock:
                client = next((c for c in self.clients if isinstance(c, QBittorrent)
                               and getattr(c, "romarr_id", "") == item.download_client), None)
                identity = item.selected_file or {"hash": item.retained_torrent_hash}
                if client is None or not identity.get("hash"):
                    return {"ok": False, "error": "retained collection identity is missing; review required"}
                try:
                    ok = client.resume_retained_collection(identity, self._retained_owners())
                except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": bool(ok), "message": "Retained torrent resumed. Retry the pending grab." if ok else "Downloader refused to resume"}
        if action == "remove":
            with self._lock:
                self.queue = [q for n, q in enumerate(self.queue)
                              if n != index]
            return {"ok": True, "removed": True}
        if action == "retry" and (item.import_intent or item.state in ("import-failed", "import-review")):
            item.state = "grabbed"
            self.store.save()
            return {"ok": True, "import_retry": True}
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

    def grab_candidate(self, release_id: str, platform_name: str = "") -> dict:
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
        chosen_raw = str(platform_name or platform_slug or "").strip()
        resolved = resolve(chosen_raw) if chosen_raw else infer_platform(release)
        if resolved is None:
            return {
                "ok": False,
                "needs_platform": True,
                "error": ("could not determine which platform this release "
                          "belongs to -- choose a platform and grab again"),
            }
        return self.grab(release, game, resolved.slug, manual=True)

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

    def mdm_capability(self) -> dict:
        """Report local/managed Mister Download Manager availability and clients."""
        rows = [c for c in self.clients
                if type(c).__name__ in ("MdmTorrentClient", "MdmDirectClient")]
        configured_path = next(
            (getattr(getattr(c, "_config", None), "cli_path", "") for c in rows
             if getattr(getattr(c, "_config", None), "cli_path", "")),
            "",
        )
        info = mdm.availability(configured_path, data_dir=self.store.path.parent)
        info["configured"] = bool(rows)
        info["usenet_builtin"] = any(
            type(c).__name__ == "BuiltinNzbClient" and getattr(c, "configured", False)
            for c in self.clients
        )
        return info

    def mdm_install(self, version: str = "") -> dict:
        """Download, SHA-256 verify, and install the companion MDM CLI locally."""
        clean_version = str(version or mdm.MDM_VERSION).strip()
        if not re.fullmatch(r"v?\d+\.\d+\.\d+", clean_version):
            raise ValueError("invalid MDM version format")
        with self._mdm_lock:
            result = mdm.install_mdm(
                target_dir=mdm.default_install_dir(self.store.path.parent),
                version=clean_version,
            )
            self.reload_clients()
            return result

    def scan_installation(self, path: Path) -> None:
        """Apply enabled malware scanners before publishing an emulator package."""
        findings, _ = scanners.scan(self.store.settings, self._env, path)
        if findings:
            raise ValueError("Emulator package was refused by an enabled malware scanner")

    def security_inspect(self, path: str, engine: str) -> dict:
        """Send one explicitly chosen shared file to the user's MWM or Arkana lab."""
        target = Path(path)
        # Inspection accepts the same paths as Manual Import and never runs samples.
        if not target.is_file() or target.is_symlink():
            raise ValueError("Choose a regular file visible to both ROMarr and MWM")
        commands = {"mwm": "lab_analyze", "arkana": "arkana_analyze"}
        if engine not in commands:
            raise ValueError("Choose MWM or Arkana")
        report = scanners.mwm_call(self.store.settings, self._env, commands[engine],
                                   {"path": str(target.resolve())})
        return {"engine": engine, "report": report,
                "note": "Arkana jobs can be followed in MWM's Malware Lab."}

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
            "build": get_build_info(VERSION),
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
            # up, not many were loaded.
            # Without a libarchive bsdtar, every 7z and rar import fails on a
            # format it cannot open -- which is how the disc platforms ship.
            # The live install this was found on had been missing it silently.
            "archive_tool": _archive_tool_path(),
            "dats": len(self.dats.dats),
            "dat_names": [d.name for d in self.dats.dats if d.name],
            "dat_games": sum(len(d.games) for d in self.dats.dats),
            "titled": getattr(self, 'titled', None) and len(getattr(self, 'titled', TitleDBIndex()).titles) or 0,
            "play_routes": self.play_route_counts(),
            "stream_url": self.store.settings.get("_stream_url", ""),
            "retroarch_nci": self.retroarch_nci_status(),
            "moonlight": self.moonlight_status(),
            "clamav": self.clamav_status(),
            "events": len(self.store.events),
            "uptime": f"{up // 3600}h {(up % 3600) // 60}m",
        }

    @staticmethod
    def _dat_scan_limits() -> tuple[int, int]:
        """How far the DAT scan is allowed to go. Overridable for testing."""
        return DAT_SCAN_DEPTH, DAT_SCAN_MAX_ENTRIES

    def reload_dats(self, directory: str = "") -> dict:
        with self._dat_reload_lock:
            return self._reload_dats(directory)

    def _reload_dats(self, directory: str = "", *, preserve_on_empty: bool = False) -> dict:
        """Load every DAT under `directory`.

        Optional and quiet. An operator with no DATs is the common case on
        day one, and it must not turn every import into an error -- the index
        simply answers `unknown`, which is a real verdict rather than a
        failure.

        Handles both standalone .dat/.xml files and .zip archives containing
        DAT files (NoIntro distributes their DATs as ZIP files).
        """
        import zipfile

        if not directory:
            if preserve_on_empty:
                return {"loaded": 0, "path": ""}
            self.store.settings["dat_path"] = ""
            self.store.settings["dat_catalog_platforms"] = {}
            self.dats = DatIndex()
            return {"loaded": 0, "path": ""}
        root = Path(directory)
        if not root.is_dir():
            if not preserve_on_empty:
                # An unavailable mounted directory is not a new selection:
                # preserve the operator's mapping until the mount returns.
                if str(directory) != str(self.store.settings.get("dat_path") or ""):
                    self.store.settings["dat_catalog_platforms"] = {}
                self.store.settings["dat_path"] = str(directory)
                self.dats = DatIndex()
            log.warning("DAT_PATH %s is not a directory", root)
            return {"loaded": 0, "path": str(root),
                    "error": f"{root} is not a directory"}
        found, stopped = _find_dats(root)
        loaded_by_name = {}
        for path in found:
            try:
                # Handle ZIP archives containing DAT files
                if path.suffix.lower() == ".zip":
                    try:
                        with zipfile.ZipFile(path, 'r') as zf:
                            # Look for .dat or .xml files inside the zip
                            dat_names = [n for n in zf.namelist() 
                                        if n.lower().endswith(('.dat', '.xml'))
                                        and not n.startswith('__MACOSX/')
                                        and not n.startswith('.')]
                            for dat_name in dat_names:
                                with zf.open(dat_name) as dat_file:
                                    content = dat_file.read().decode('utf-8', errors='replace')
                                    parsed = parse_dat(content)
                                    if parsed.games:
                                        key = parsed.name.casefold() or dat_name.casefold()
                                        previous = loaded_by_name.get(key)
                                        if previous is None or _dat_version_key(parsed.version) > _dat_version_key(previous.version):
                                            loaded_by_name[key] = parsed
                    except (zipfile.BadZipFile, OSError) as zip_err:
                        log.warning("could not read zip %s: %s", path, zip_err)
                        continue
                else:
                    # Regular .dat or .xml file
                    parsed = parse_dat(path.read_text(encoding="utf-8",
                                                      errors="replace"))
                    if parsed.games:
                        key = parsed.name.casefold() or path.name.casefold()
                        previous = loaded_by_name.get(key)
                        if previous is None or _dat_version_key(parsed.version) > _dat_version_key(previous.version):
                            loaded_by_name[key] = parsed
            except (OSError, ValueError) as exc:
                log.warning("could not read %s: %s", path, exc)
        index = DatIndex()
        for parsed in loaded_by_name.values():
            index.add(parsed)
        result = {"loaded": len(index.dats), "path": str(root),
                  "examined": len(found)}
        if stopped:
            result["truncated"] = stopped
            log.warning("DAT scan stopped early: %s", stopped)
        
        # Build the candidate index entirely off to the side. Setup may reject
        # an empty/unreadable selection without exposing it to import readers.
        if preserve_on_empty and not index.dats:
            return result
        # A new DAT directory has different provenance. Never carry an old
        # catalog/platform assertion into an unrelated source selection.
        if str(directory) != str(self.store.settings.get("dat_path") or ""):
            self.store.settings["dat_catalog_platforms"] = {}
        self.store.settings["dat_path"] = str(directory)
        self.dats = index
        log.info("loaded %d DAT(s) from %s", len(index.dats), root)

        # Also initialize TitleDB for Switch validation
        self.reload_titled()
        
        return result
    
    def reload_titled(self) -> dict:
        """Initialize TitleDB index for Nintendo Switch validation.

        TitleDB provides the title catalogue for Switch games since
        NoIntro/Redump publish no Switch DAT. Best-effort by design: the
        fetch is cached on disk under ROMARR_DATA/titledb and every failure
        mode degrades to an empty index that answers "not in database"
        rather than stopping startup. Names (a 90MB region file) stay off
        unless TITLED_NAMES=1 -- see titled.py for the reasoning.
        """
        try:
            self.titled = build_titled_index()
            count = len(self.titled.titles) if self.titled else 0
            log.info("TitleDB index loaded with %d Switch title IDs", count)
            return {"loaded": count, "names": self.titled.names_loaded,
                    "status": "ok"}
        except Exception as exc:
            log.warning("TitleDB initialization failed: %s", exc)
            self.titled = TitleDBIndex()
            return {"loaded": 0, "status": "error", "error": str(exc)}

    # -- the queue ----------------------------------------------------------

    @property
    def queue(self) -> list[QueueItem]:
        """Activity, which lives in the store so it survives a restart.

        A property rather than a plain list because every caller already says
        `self.queue`, and a second list kept in step with the stored one by
        hand is precisely the drift this removes.
        """
        return self.store.queue

    @queue.setter
    def queue(self, items) -> None:
        self.store.set_queue(items)

    # -- wanted ---------------------------------------------------------------

    def drop_request(self, game: str, platform_name: str) -> bool:
        """Take something off Wanted without pretending it arrived.

        `fulfil` is what an import calls, and it means the game is in the
        library now. This is the other reason a request leaves the list -- it
        was a typo, or you changed your mind -- and somebody reading History
        later has to be able to tell those apart.

        The platform is resolved so the API is as forgiving as every other
        one here ("Super Nintendo" and "snes" are the same request), falling
        back to the raw string so an entry stored under a slug this build no
        longer recognises can still be removed.
        """
        platform = resolve(platform_name) if platform_name else None
        slug = (platform.slug if platform is not None
                else str(platform_name or "").strip())
        removed = self.store.unwant(game, slug)
        if not removed and not slug:
            # Fallback if an entry was stored under whitespace or None
            removed = self.store.unwant(game, "")
        if removed:
            self.store.record(Event(kind="ignored", game=game, platform=slug,
                                    detail="request dropped"))
        return removed

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

    def profile_for(self, platform: Any = None) -> ReleaseProfile:
        """Resolve the release profile for a given platform, with fallback to global."""
        if not platform:
            return self.profile
        return ReleaseProfile.from_settings(self.store.settings, platform=platform)

    def block(self, release, reason: str = "") -> dict:
        """Never take this release again, and record why."""
        entry = self.blocklist.add(release, reason=reason)
        self.store.put_item("blocklist", entry)
        return entry

    def block_id(self, entry_id: str, *, title: str = "", indexer: str = "",
                 size: int = 0, reason: str = "") -> dict:
        """Block one release identity, for callers holding a queue row."""
        entry = self.blocklist.add_entry(entry_id, title=title, indexer=indexer,
                                         size=size, reason=reason)
        self.store.put_item("blocklist", entry)
        return entry

    #: How many replacement grabs one sweep may make. A dead release is
    #: retired the moment it is noticed, but every replacement costs a full
    #: indexer search, and twenty of them at once is how a tracker decides
    #: ROMarr is a scraper.
    MAX_REGRABS_PER_SWEEP = 3

    def retire_dead_downloads(self) -> dict:
        with self._collection_lock:
            return self._retire_dead_downloads()

    def _retire_dead_downloads(self) -> dict:
        """Blocklist downloads that died, then grab the next best release.

        The Blocklist has existed since 0.7 and nothing ever added to it on
        its own, which made a failed download a loop: `best_release` is
        deterministic, so removing the dead torrent by hand and letting the
        next sweep run scored the same results the same way and grabbed the
        same dead file again. Retiring the release is the half that makes the
        retry land somewhere new.

        Four things count as dead, and one thing deliberately does not:
        a client that refused the release, a download the client reports it
        gave up on for a reason that blames the release, a download that
        finished with no ROMs in it, and one that has sat unfinished past the
        stall timeout. A failure that is the *install's* fault -- no download
        client configured for the protocol -- is not the release's doing and
        is left alone, or the operator would come back to a blocklist full of
        perfectly good torrents.
        """
        if not self.store.settings.get("blocklist_failed_downloads", True):
            return {"blocklisted": 0, "regrabbed": 0,
                    "message": "failed download handling is off"}

        try:
            stall_minutes = int(self.store.settings.get("stalled_timeout_minutes") or 0)
        except (TypeError, ValueError):
            stall_minutes = 0

        # Keyed by the reporting client and its job id, never by title: the
        # same release name often comes from several indexers, and an old
        # failure must not be pinned on a new grab that happens to share it.
        failures = {(job["client_id"], job["id"]): job
                    for job in self._failed_jobs() if job.get("id")}
        now = datetime.now(timezone.utc)
        dead: list[QueueItem] = []
        with self._lock:
            rows = list(self.queue)
        for row in rows:
            probe_identity = None
            with self._lock:
                if (not any(current is row for current in self.queue)
                        or row.blocklisted or not row.release_id):
                    continue
                failure = (failures.get((row.download_client, row.download_id))
                           if row.download_client and row.download_id else None)
                if (row.state == "grabbed" and failure is not None
                        and failure.get("release_fault")):
                    row.state = "failed"
                    row.release_fault = True
                    row.detail = (f"{failure['client']} failed the download: "
                                  f"{failure['detail']}")
                if row.state in ("failed", "import-failed") and row.release_fault:
                    dead.append(row)
                    continue
                if row.state == "grabbed" and stall_minutes > 0:
                    age = self._row_age_minutes(row, now)
                    if age is not None and age >= stall_minutes:
                        probe_identity = (row.release_id, row.download_client,
                                          row.download_id, row.at)

            if probe_identity is None:
                continue
            # Do not hold the queue lock during network calls. Retire each
            # inactive row before polling the next client or queue row.
            if self._is_download_active_or_complete(row):
                continue
            with self._lock:
                if (not any(current is row for current in self.queue)
                        or row.blocklisted or row.state != "grabbed"):
                    continue
                if (row.release_id, row.download_client,
                        row.download_id, row.at) != probe_identity:
                    continue
                age = self._row_age_minutes(row, now)
                if age is None or age < stall_minutes:
                    continue
                row.state = "failed"
                row.release_fault = True
                row.detail = f"stalled: no import after {int(age)} minutes"
                dead.append(row)

        blocklisted = 0
        regrabbed = 0
        retried: set[tuple[str, str]] = set()
        for row in dead:
            self._remove_dead_from_client(row)
            reason = row.detail or f"download {row.state}"
            self.block_id(row.release_id, title=row.release,
                          indexer=row.indexer, size=row.size,
                          reason=reason)
            row.blocklisted = True
            blocklisted += 1
            self.store.record(Event(kind="ignored", game=row.game,
                                    platform=row.platform, release=row.release,
                                    indexer=row.indexer,
                                    detail=f"blocklisted -- {reason}"))
            # For a row grabbed by hand (manual=True), do not replace it automatically
            # and do not put the search text on Wanted (#119).
            if getattr(row, "manual", False):
                continue
            key = (row.game.lower(), row.platform)
            if key in retried or regrabbed >= self.MAX_REGRABS_PER_SWEEP:
                continue
            retried.add(key)
            # Back on the Wanted list first: the replacement search can fail
            # too, and a request that has lost its dead download and never
            # reached Wanted is one nothing will ever look for again.
            self.store.want(row.game, row.platform)
            try:
                if self._automatic_request(
                        row.game, row.platform,
                        external_request_id=row.external_request_id).get("ok"):
                    regrabbed += 1
            except Exception as err:      # one bad row must not stop the rest
                log.warning("replacement grab for %r failed: %s", row.game, err)
        self.store.save()
        message = (f"retired {blocklisted} dead download(s), "
                   f"grabbed {regrabbed} replacement(s)")
        return {"blocklisted": blocklisted, "regrabbed": regrabbed,
                "message": message}

    def _failed_jobs(self) -> list[dict]:
        """Jobs a download client reports it gave up on, tagged with its name.

        Only clients with `failed_jobs` answer. For every other client this is
        empty, and the stall timeout stays the only way a dead download is
        noticed, as it always was.
        """
        jobs = []
        for client in self.clients:
            report = getattr(client, "failed_jobs", None)
            if report is None or not getattr(client, "configured", True):
                continue
            name = getattr(client, "name", "the download client")
            client_id = getattr(client, "romarr_id", "")
            try:
                jobs.extend({**job, "client": name, "client_id": client_id}
                            for job in report())
            except Exception as err:
                # One unreachable client must not stop the sweep.
                log.warning("%s failed_jobs() failed: %s", name, err)
        return jobs

    def _is_download_active_or_complete(self, row: QueueItem) -> bool:
        """Return True if a download client reports `row` is completed or actively transferring (#108).

        `row.at` is the grab timestamp, not the last-activity timestamp. A large
        release that takes longer than `stalled_timeout_minutes` to finish at a
        healthy speed -- or a completed download waiting for import -- must not
        be retired as stalled.
        """
        for client in self.clients:
            if not getattr(client, "configured", True):
                continue
            client_id = str(getattr(client, "romarr_id", "") or getattr(client, "config_id", "") or "")
            client_protocol = str(getattr(client, "protocol", "") or "")
            try:
                completed = getattr(client, "completed", None)
                if callable(completed):
                    for item in completed():
                        if match_queue_item([row], item, client_id=client_id, client_protocol=client_protocol) is not None:
                            return True
            except Exception:
                pass
            try:
                active_fn = (getattr(client, "active_downloads", None)
                             or getattr(client, "torrents", None))
                if callable(active_fn):
                    for item in active_fn():
                        if match_queue_item([row], item, client_id=client_id, client_protocol=client_protocol) is None:
                            continue
                        progress = float(item.get("progress", 0) or 0)
                        if progress >= 1.0 or item.get("worker_active") is True:
                            return True
                        dlspeed = int(item.get("dlspeed", 0) or 0)
                        state = str(item.get("state") or "")
                        if dlspeed > 0 or state in (
                            "downloading", "forcedDL", "checkingDL",
                            "checkingUP", "allocating", "moving",
                            "uploading", "stalledUP", "queuedUP", "forcedUP",
                        ):
                            if state == "downloading" and dlspeed == 0 and int(item.get("num_seeds", 0) or 0) == 0:
                                continue
                            return True
            except Exception:
                pass
        return False

    def _remove_dead_from_client(self, row: QueueItem) -> bool:
        """Best-effort removal of a retired/stalled download from its client (#108)."""
        removed = False
        for client in self.clients:
            if not getattr(client, "configured", True):
                continue
            client_id = getattr(client, "romarr_id", "") or getattr(client, "config_id", "")
            if row.download_client and client_id and str(row.download_client) != str(client_id):
                continue
            remover = getattr(client, "remove_download", None)
            if callable(remover) and row.download_id:
                try:
                    if remover(row.download_id):
                        removed = True
                        continue
                except Exception as err:
                    log.warning("could not remove dead download %r from %s: %s",
                                row.download_id, getattr(client, "name", client), err)
            client_protocol = str(getattr(client, "protocol", "") or "")
            deleter = getattr(client, "delete_torrent", None)
            torrents_fn = getattr(client, "torrents", None)
            if callable(deleter) and callable(torrents_fn):
                try:
                    for item in torrents_fn():
                        if match_queue_item([row], item, client_id=str(client_id), client_protocol=client_protocol) is not None:
                            thash = str(item.get("hash") or "").strip()
                            if thash and deleter(thash):
                                removed = True
                except Exception:
                    pass
        return removed

    @staticmethod
    def _row_age_minutes(row, now) -> float | None:
        """How long this row has been waiting, or None if it will not say."""
        try:
            stamped = datetime.fromisoformat(row.at)
        except (TypeError, ValueError):
            return None
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        return (now - stamped).total_seconds() / 60.0

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

    @contextmanager
    def _scanned_manual_source(self, source: Path, platform):
        """Scan private copies of every selected loose disc member before import."""
        import shutil
        import tempfile
        from .library import _PathSource
        enabled = any(self.store.settings.get(key) for key in (
            "clamav_enabled", "scanner_defender_enabled", "scanner_mwm_enabled",
            "scanner_vt_enabled"))
        if not enabled:
            yield source, None
            return
        with tempfile.TemporaryDirectory(prefix=".manual-scan-", dir=self.store.path.parent) as scratch:
            stage = Path(scratch) / source.name
            if source.suffix.lower() in (".cue", ".gdi") and source.is_file():
                origin = _PathSource(source.parent)
                chosen = next((group for group in origin.rom_sets(platform)
                               if group.primary == source.name), None)
                if chosen is None:
                    raise ValueError("Could not resolve the selected disc set")
                stage.mkdir()
                root = source.parent.resolve()
                for member in chosen.members:
                    original = source.parent / member
                    if not original.resolve().is_relative_to(root) or original.is_symlink():
                        raise ValueError("Disc member escapes its source directory")
                    destination = stage / member
                    if not destination.resolve().is_relative_to(stage.resolve()):
                        raise ValueError("Disc member escapes its staging directory")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, destination)
            elif source.is_dir():
                if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
                    raise ValueError("Manual scan source cannot contain symbolic links")
                shutil.copytree(source, stage)
            else:
                if source.is_symlink():
                    raise ValueError("Manual scan source cannot be a symbolic link")
                shutil.copyfile(source, stage)
            refusal = self._virus_check(stage, source.name, platform, None)
            yield stage, refusal

    def adopt(self, path: str, platform_slug: str = "", *,
              force: bool = False, require_verified: bool = False) -> dict:
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
        target_folders = self.platform_folders(target_cfg)

        try:
            with self._scanned_manual_source(source, platform) as (staged_source, scan_refusal):
                if scan_refusal is not None:
                    return {**scan_refusal, "refused": [{
                        "file": source.name, "reason": scan_refusal["reason"],
                        "needs_force": False}], "imported": []}
                outcomes = import_rom(staged_source, platform, self.library_root(target_cfg),
                                      dats=self.dats,
                                      require_verified=require_verified,
                                      rename=bool(self.store.settings.get("rename_on_import", True)),
                        prefer_hardlinks=staged_source == source and bool(self.store.settings.get("prefer_hardlinks", False)),
                                      layout=self.library_layout(target_cfg),
                                      translation=is_translation(source.name),
                                      platform_folders=target_folders)
        except (OSError, ValueError) as err:
            reason = f"could not prepare scanned import: {err}"
            return {"ok": False, "reason": reason, "imported": [], "refused": [
                {"file": source.name, "reason": reason, "needs_force": False}]}

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
                        f": {outcome.destination} ({outcome.import_method or 'copy'}) {outcome.fallback_reason}")))
            imported.append({
                "file": str(outcome.destination),
                "verdict": status,
                "forced": forced,
                "import_method": outcome.import_method,
                "fallback_reason": outcome.fallback_reason,
            })

        if imported and self.store.settings.get("rescan_after_import", True):
            self._rescan_library(target_lib, target_cfg, platform.slug,
                                 platform_folders=target_folders)
        self.store.save()

        result = {
            "ok": bool(imported) and not refused,
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
                folder = platform_dir(self.library_root(cfg), platform_slug,
                                      layout=self.library_layout(cfg),
                                      platform_folders=self.platform_folders(cfg))
                try:
                    # Recursive on purpose. iterdir() sees one level: a game
                    # filed in a subfolder ("Nintendo/Chrono Trigger.zip", a
                    # disc-set's inner tracks, anything under Translations/)
                    # surfaced as its PARENT DIRECTORY's name and the title
                    # itself read as missing -- so collection import queued a
                    # fresh download of a game already on disk. Walk the tree;
                    # a directory named for a game (a disc set) still answers
                    # to its own name via its files' stems below.
                    # is_dir() guard first: rglob on a MISSING directory
                    # yields nothing instead of raising, which would mark the
                    # platform as listed and skip the shelf fallback below.
                    if not folder.is_dir():
                        continue
                    entries = [p for p in folder.rglob("*") if p.is_file()]
                except OSError:
                    continue
                listed = True
                for entry in entries:
                    # A cartridge zip answers to its stem. A disc-set file
                    # inside a game-named directory answers to BOTH its own
                    # stem and the directory name, because that is how the
                    # set is filed and how the DAT names the game.
                    note(entry.stem)
                    for parent in entry.relative_to(folder).parents:
                        if str(parent) not in (".", ""):
                            note(parent.name)
        if not listed:
            for item in (self.library_view() or {}).get("items", []):
                note(str(item.get("name") or ""))
        return present

    def dat_names(self) -> list[str]:
        return [d.name for d in self.dats.dats if d.name]

    _RECONCILE_CACHE_MAX = 50_000

    def _reconcile_file_hashes(self, path: Path, platform) -> tuple[dict, ...]:
        """Return streamed DAT hashes for every member of ``path``.

        Caches computed hash dicts by ``(path, archive_is_the_rom, size,
        mtime_ns, ctime_ns)`` so repeated reconciles skip rehashing unchanged
        library files while still re-evaluating ``self.dats`` on DAT reloads.
        """
        from .library import _source_for

        st = path.stat()
        archive_as_rom = bool(getattr(platform, "archive_is_the_rom", False))
        sig = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        cache_key = (str(path), archive_as_rom, *sig)

        cache = getattr(self, "_reconcile_hash_cache", None)
        lock = getattr(self, "_reconcile_cache_lock", None)
        if cache is None or lock is None:
            cache = OrderedDict()
            lock = threading.Lock()
            self._reconcile_hash_cache = cache
            self._reconcile_cache_lock = lock

        with lock:
            cached = cache.get(cache_key)
            if cached is not None:
                cache.move_to_end(cache_key)
                return cached

        source = _source_for(path, platform)
        computed: list[dict] = []
        complete = True
        for member in source.names():
            suffix = Path(member.replace("\\", "/")).suffix
            hashes = source.hash(member, suffix=suffix)
            if hashes is None:
                complete = False
                continue
            computed.append(hashes)
        result = tuple(computed)

        st_after = path.stat()
        if complete and (st_after.st_size, st_after.st_mtime_ns, st_after.st_ctime_ns) == sig:
            with lock:
                cache[cache_key] = result
                cache.move_to_end(cache_key)
                while len(cache) > self._RECONCILE_CACHE_MAX:
                    cache.popitem(last=False)
        return result

    def reconcile_platform(self, platform_slug: str = "") -> dict:
        """Drop Wanted entries whose verified dump is already in the library.

        Closes the aliasing gap (#85/#87): a wanted title can differ from the
        imported file's name in punctuation, word order or region tags, so it
        survives every import and stays on the Missing tab even though the
        bytes are present and hash-verified. Walks the platform's library dir,
        stream-verifies each file against the loaded DATs, and fulfils any
        same-platform wanted title that maps to a verified dump. Runs after
        each import (scoped to the importing platform) and via
        ``POST /api/v1/collection/reconcile``.
        """
        from .dat import VERIFIED as _VERIFIED

        platform = resolve(platform_slug)
        if platform is None:
            return {"ok": False,
                    "error": f"unknown platform {platform_slug!r}",
                    "platform": platform_slug, "dropped": 0}

        if self.dats is None or (hasattr(self.dats, "dats") and not self.dats.dats):
            return {"ok": True, "platform": platform_slug,
                    "verified": 0, "dropped": [], "count": 0}

        verified_titles: set[str] = set()
        for cfg, _ in self.game_libraries:
            folder = platform_dir(self.library_root(cfg), platform_slug,
                                  layout=self.library_layout(cfg),
                                  platform_folders=self.platform_folders(cfg))
            if not folder.is_dir():
                continue
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    for hashes in self._reconcile_file_hashes(path, platform):
                        match = self.dats.lookup(**hashes)
                        if match.status == _VERIFIED and match.game:
                            verified_titles.add(match.game)
                except Exception:  # noqa: BLE001 - one bad file must not stop it
                    continue

        dropped: list[list[str]] = []
        for w in list(self.store.wanted):
            if w.platform != platform_slug:
                continue
            wanted_key = _title_key(w.game)
            if not wanted_key:
                continue
            if any(_titles_are_same(w.game, vt) for vt in verified_titles):
                self.store.fulfil(w.game, w.platform)
                dropped.append([w.game, w.platform])
        if dropped:
            self.store.save()
        return {"ok": True, "platform": platform_slug,
                "verified": len(verified_titles), "dropped": dropped,
                "count": len(dropped)}

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
        translation_langs = (policy_kw.get("translation_langs")
                             or self.store.settings.get("translation_langs")
                             or ("en",))
        if isinstance(translation_langs, str):
            translation_langs = [c for c in translation_langs.split(",") if c]
        from .collections import normalise_lang
        translation_langs = tuple(dict.fromkeys(
            normalise_lang(c) for c in translation_langs if str(c).strip()))
        region_mode = str(policy_kw.get("region_mode")
                          or self.store.settings.get("region_mode")
                          or "prefer").lower()
        policy = Policy(
            regions=regions,
            one_game_one_rom=bool(policy_kw.get("one_game_one_rom", True)),
            exclude=frozenset(exclude),
            translation_policy=str(translation_policy),
            translation_langs=translation_langs,
            region_mode=region_mode if region_mode in ("prefer", "strict")
            else "prefer",
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
                       "translation_policy": policy.translation_policy,
                       "translation_langs": list(policy.translation_langs),
                       "region_mode": policy.region_mode},
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

    def _source_client(self, client_id: str = ""):
        clients = [c for c in self.clients
                   if isinstance(c, QBittorrent)]
        if client_id:
            return next((c for c in clients
                         if getattr(c, "romarr_id", "") == client_id), None)
        return clients[0] if len(clients) == 1 else None

    def collection_source_torrents(self) -> dict:
        rows, errors = [], []
        for client in self.clients:
            if not isinstance(client, QBittorrent):
                continue
            try:
                for torrent in client.torrents():
                    rows.append({
                        "client_id": getattr(client, "romarr_id", ""),
                        "client_name": getattr(client, "romarr_name", "qBittorrent"),
                        "hash": str(torrent.get("hash") or ""),
                        "name": str(torrent.get("name") or ""),
                        "size": int(torrent.get("size") or 0),
                        "state": str(torrent.get("state") or ""),
                        "category": str(torrent.get("category") or ""),
                    })
            except Exception as err:  # noqa: BLE001 - report client status to UI
                detail = ("authentication failed" if isinstance(err, QbitAuthenticationError)
                          else err.__class__.__name__)
                name = getattr(client, "romarr_name", "qBittorrent")
                log.warning("could not list collection torrents from %s: %s", name, detail)
                errors.append(f"{name}: {detail}")
        return {"torrents": rows, "errors": errors,
                "jobs": self.store.list_items("collection_sources")}

    def _collection_source_plan(self, client_id: str, torrent_hash: str,
                                dat_name: str, platform: str,
                                cap_bytes: int, **policy_kw) -> dict:
        from .collection_torrent import plan_source

        client = self._source_client(client_id)
        if client is None:
            return {"error": "select one configured qBittorrent client"}
        try:
            torrent = next((t for t in client.torrents()
                            if str(t.get("hash") or "").lower()
                            == torrent_hash.lower()), None)
            if torrent is None:
                return {"error": "source torrent no longer exists"}
            listing = client.torrent_files(torrent_hash)
        except Exception as err:  # noqa: BLE001 - client failures are actionable
            detail = ("qBittorrent authentication failed"
                      if isinstance(err, QbitAuthenticationError) else err.__class__.__name__)
            return {"error": f"could not read the source torrent: {detail}"}
        plan = self.collection_plan(dat_name, platform, **policy_kw)
        if plan.get("error"):
            return plan
        dat = next((d for d in self.dats.dats if d.name == plan["dat"]), None)
        missing = [t["name"] for t in plan["titles"]
                   if t["status"] == "missing"]
        result = plan_source(missing, dat, listing, cap_bytes,
                             int(torrent.get("piece_size") or 0))
        if result.get("error"):
            return result
        result.update({
            "client_id": getattr(client, "romarr_id", ""),
            "client_name": getattr(client, "romarr_name", "qBittorrent"),
            "torrent_hash": torrent_hash,
            "torrent_name": str(torrent.get("name") or ""),
            "dat": plan["dat"], "platform": platform,
        })
        return result

    def collection_source_start(self, client_id: str, torrent_hash: str,
                                dat_name: str, platform: str,
                                cap_bytes: int, **policy_kw) -> dict:
        with self._collection_source_lock:
            return self._collection_source_start(client_id, torrent_hash,
                                                 dat_name, platform,
                                                 cap_bytes, **policy_kw)

    def _collection_source_start(self, client_id: str, torrent_hash: str,
                                 dat_name: str, platform: str,
                                 cap_bytes: int, **policy_kw) -> dict:
        import shutil
        import uuid

        platform = str(platform or "").strip()
        if not platform:
            return {"error": "select a platform before starting source acquisition"}
        selected_platform = resolve(platform)
        if selected_platform is None:
            return {"error": "select a valid platform before starting source acquisition"}
        platform = selected_platform.slug
        client = self._source_client(client_id)
        if client is None:
            return {"error": "select one configured qBittorrent client"}
        client_key = getattr(client, "romarr_id", "")
        for existing in self.store.list_items("collection_sources"):
            if (existing.get("client_id") == client_key
                    and str(existing.get("torrent_hash") or "").lower() == torrent_hash.lower()
                    and existing.get("status") in {"running", "restore-needed"}):
                return {"error": "this torrent already has an active source job; finish or cancel it first"}
        plan = self._collection_source_plan(client_id, torrent_hash, dat_name,
                                            platform, cap_bytes, **policy_kw)
        if plan.get("error"):
            return plan
        if not plan["batches"]:
            return {"error": "no missing DAT files matched this torrent"}
        try:
            torrent = next(t for t in client.torrents()
                           if str(t.get("hash") or "").lower()
                           == torrent_hash.lower())
            source_root = map_remote_path(
                torrent.get("save_path") or torrent.get("content_path") or "",
                self.store.settings.get("remote_path_mappings"))
            if not source_root or not Path(source_root).is_dir():
                return {"error": "qBittorrent source path is not mounted here"}
            stage_root = self.library / ".romarr-collection-spool"
            stage_root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(stage_root).free
            if free < int(plan["cap_bytes"]):
                return {"error": "less free space than the selected staging cap"}
            listing = client.torrent_files(torrent_hash)
            original = {str(int(f.get("index"))): int(f.get("priority") or 0)
                        for f in listing if f.get("index") is not None}
            was_paused = "paused" in str(torrent.get("state") or "").lower()
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id, "client_id": getattr(client, "romarr_id", ""),
                "torrent_hash": torrent_hash, "torrent_name": plan["torrent_name"],
                "save_path": str(source_root), "platform": platform,
                "dat": plan["dat"], "cap_bytes": int(plan["cap_bytes"]),
                "reserve_bytes": int(plan["reserve_bytes"]),
                "original_priorities": original, "was_paused": was_paused,
                "batches": plan["batches"], "batch": 0, "status": "running",
                "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "errors": [],
            }
            self.store.put_item("collection_sources", job)
            try:
                self._prepare_collection_source_batch(client, job, force=True)
            except Exception:
                restored = self._restore_source_priorities(client, job)
                job["status"] = "cancelled" if restored else "restore-needed"
                if not restored:
                    job["restore_status"] = "cancelled"
                self.store.put_item("collection_sources", job)
                raise
            self.store.put_item("collection_sources", job)
            return {"ok": True, **self._source_job_progress(job)}
        except Exception as err:  # noqa: BLE001
            log.exception("could not start selective collection torrent")
            return {"error": f"could not start source torrent: {err.__class__.__name__}"}

    @staticmethod
    def _source_job_progress(job: dict) -> dict:
        batches = job.get("batches") or []
        current = int(job.get("batch") or 0)
        total_titles = sum(len(b.get("groups") or []) for b in batches)
        done = sum(1 for b in batches for g in b.get("groups") or []
                   if g.get("status") == "imported")
        return {
            "id": job.get("id"), "torrent_name": job.get("torrent_name"),
            "dat": job.get("dat"), "platform": job.get("platform"),
            "status": job.get("status"), "batch": current + 1,
            "batches": len(batches), "done": done, "total": total_titles,
            "errors": job.get("errors") or [],
            "stage_path": job.get("stage_path") or "",
        }

    def _prepare_collection_source_batch(self, client, job: dict, *,
                                        force: bool = False) -> bool:
        batches = job.get("batches") or []
        index = int(job.get("batch") or 0)
        if index >= len(batches):
            return False
        files = client.torrent_files(job["torrent_hash"])
        ids = {int(f["index"]) for f in files if f.get("index") is not None}
        priorities = {i: 0 for i in ids}
        wanted = {int(f["index"])
                  for group in batches[index].get("groups") or []
                  for f in group.get("files") or []}
        if not wanted or not wanted.issubset(ids):
            raise ValueError("planned source file list changed")
        expected = {i: (1 if i in wanted else 0) for i in ids}
        current = {int(f["index"]): int(f.get("priority") or 0)
                   for f in files if f.get("index") is not None}
        if not force and all(current.get(i) == p for i, p in expected.items()):
            return True
        if not client.pause_torrent(job["torrent_hash"]):
            raise RuntimeError("qBittorrent refused to pause before file selection")
        if not client.set_file_priorities(job["torrent_hash"], priorities):
            raise RuntimeError("qBittorrent refused to deselect unwanted files")
        if not client.set_file_priorities(job["torrent_hash"],
                                          {i: 1 for i in wanted}):
            raise RuntimeError("qBittorrent refused to select planned files")
        if not client.resume_torrent(job["torrent_hash"]):
            raise RuntimeError("qBittorrent refused to resume selected files")
        return True

    def collection_source_step(self, job_id: str) -> dict:
        import shutil
        from pathlib import PurePosixPath

        job = self.store.get_item("collection_sources", job_id)
        if not job:
            return {"error": "no such source batch"}
        if job.get("status") != "running":
            return self._source_job_progress(job)
        client = self._source_client(job.get("client_id", ""))
        if client is None:
            return {"error": "the qBittorrent client for this batch is unavailable"}
        batch_index = int(job.get("batch") or 0)
        batches = job.get("batches") or []
        if batch_index >= len(batches):
            job["status"] = "done"
            if not self._restore_source_priorities(client, job):
                job["status"] = "restore-needed"
                job["restore_status"] = "done"
            self.store.put_item("collection_sources", job)
            return self._source_job_progress(job)
        try:
            self._prepare_collection_source_batch(client, job)
            files = client.torrent_files(job["torrent_hash"])
            by_index = {int(f["index"]): f for f in files
                        if f.get("index") is not None}
            batch = batches[batch_index]
            waiting = []
            for group in batch.get("groups") or []:
                if group.get("status") == "imported":
                    continue
                for planned in group.get("files") or []:
                    row = by_index.get(int(planned["index"]))
                    if row is None or float(row.get("progress") or 0) < 1:
                        waiting.append(planned["name"])
            if waiting:
                return {"ok": True, "waiting": True,
                        "message": f"{len(waiting)} file(s) are still downloading",
                        **self._source_job_progress(job)}
            stage_root = self.library / ".romarr-collection-spool" / job_id
            stage_root.mkdir(parents=True, exist_ok=True)
            job["stage_path"] = str(stage_root)
            for group in batch.get("groups") or []:
                if group.get("status") == "imported":
                    continue
                game_dir = stage_root / str(group["id"])
                game_dir.mkdir(parents=True, exist_ok=True)
                for planned in group.get("files") or []:
                    rel = PurePosixPath(str(planned["name"]))
                    if rel.is_absolute() or ".." in rel.parts:
                        raise ValueError("qBittorrent returned an unsafe file path")
                    source = (Path(job["save_path"]) / Path(*rel.parts)).resolve(strict=True)
                    source.relative_to(Path(job["save_path"]).resolve())
                    dest = game_dir / Path(*rel.parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        if source.stat().st_size != int(planned["size"]):
                            raise ValueError(f"source size changed: {planned['name']}")
                        used = sum(p.stat().st_size for p in stage_root.rglob("*")
                                   if p.is_file())
                        if used + source.stat().st_size + int(job["reserve_bytes"]) > int(job["cap_bytes"]):
                            raise ValueError("staging cap would be exceeded")
                        shutil.copy2(source, dest)
                candidates = [p for p in game_dir.rglob("*") if p.is_file()]
                if not candidates:
                    raise ValueError(f"no staged file for {group['title']}")
                preferred = (".zip", ".7z", ".rar", ".chd", ".iso", ".cue",
                             ".gdi", ".m3u", ".cso", ".pbp")
                candidates.sort(key=lambda p: (preferred.index(p.suffix.lower())
                                               if p.suffix.lower() in preferred
                                               else len(preferred), p.name.lower()))
                result = self.adopt(str(candidates[0]), job["platform"],
                                    require_verified=True)
                if not result.get("ok"):
                    group["status"] = "failed"
                    group["error"] = "; ".join(
                        r.get("reason", "") for r in result.get("refused", []))
                    job["errors"] = [f"{group['title']}: {group['error']}"]
                    self.store.put_item("collection_sources", job)
                    return {"error": group["error"] or "verification/import failed",
                            **self._source_job_progress(job)}
                group["status"] = "imported"
                group["imported"] = result.get("imported") or []
                shutil.rmtree(game_dir, ignore_errors=True)
                self.store.put_item("collection_sources", job)
            if batch_index + 1 < len(batches):
                job["batch"] = batch_index + 1
                self._prepare_collection_source_batch(client, job)
            else:
                job["status"] = "done"
                if not self._restore_source_priorities(client, job):
                    job["status"] = "restore-needed"
                    job["restore_status"] = "done"
            self.store.put_item("collection_sources", job)
            return {"ok": True, **self._source_job_progress(job)}
        except Exception as err:  # noqa: BLE001 - persisted progress permits retry
            job["errors"] = [f"{err.__class__.__name__}: {err}"]
            self.store.put_item("collection_sources", job)
            return {"error": job["errors"][0], **self._source_job_progress(job)}

    def _restore_source_priorities(self, client, job: dict) -> bool:
        try:
            if not client.pause_torrent(job["torrent_hash"]):
                return False
            original = {int(i): int(p) for i, p in
                        (job.get("original_priorities") or {}).items()}
            if not client.set_file_priorities(job["torrent_hash"], original):
                return False
            if not job.get("was_paused") and not client.resume_torrent(job["torrent_hash"]):
                return False
            return True
        except Exception as err:  # noqa: BLE001
            log.warning("could not restore source torrent priorities: %s",
                        err.__class__.__name__)
            return False

    def collection_source_control(self, job_id: str, action: str) -> dict:
        job = self.store.get_item("collection_sources", job_id)
        if not job:
            return {"error": "no such source batch"}
        if action == "cancel" and job.get("status") == "running":
            client = self._source_client(job.get("client_id", ""))
            if client is None:
                return {"error": "the qBittorrent client for this batch is unavailable"}
            restored = self._restore_source_priorities(client, job)
            if restored:
                job["status"] = "cancelled"
            else:
                job["status"] = "restore-needed"
                job["restore_status"] = "cancelled"
            self.store.put_item("collection_sources", job)
        elif action == "restore":
            client = self._source_client(job.get("client_id", ""))
            if client is None:
                return {"error": "the qBittorrent client for this batch is unavailable"}
            restored = self._restore_source_priorities(client, job)
            if restored:
                job["status"] = job.pop("restore_status", "cancelled")
            self.store.put_item("collection_sources", job)
        elif action == "retry":
            for batch in job.get("batches") or []:
                for group in batch.get("groups") or []:
                    if group.get("status") == "failed":
                        group.pop("error", None)
                        group["status"] = ""
            job["errors"] = []
            job["status"] = "running"
            self.store.put_item("collection_sources", job)
        else:
            return {"error": "unknown action"}
        return self._source_job_progress(job)

    def _prepare_metadata(self, providers: list[dict]) -> tuple[Metadata, list[dict]]:
        """Build a provider chain and a deep snapshot without publishing either."""
        return Metadata(deepcopy(providers)), deepcopy(providers)

    def reload_metadata(self) -> None:
        """Publish a complete Metadata chain, snapshot and revision together."""
        with self._metadata_lock:
            providers = self.store.list_items("metadata_providers")
            if (hasattr(self, "metadata")
                    and providers == getattr(self, "_metadata_provider_snapshot", None)):
                return
            metadata, snapshot = self._prepare_metadata(providers)
            revision = uuid.uuid4().hex
            self.metadata, self._metadata_provider_snapshot, self.metadata_revision = (
                metadata, snapshot, revision)

    def save_metadata_provider(self, cfg: dict) -> dict:
        """Validate a complete candidate before saving or publishing a provider."""
        with self._metadata_lock:
            providers = self.store.list_items("metadata_providers")
            candidate = dict(cfg)
            incoming_id = str(candidate.get("id") or "")
            if not incoming_id:
                # Mirror Store's short random identifier, before staging the
                # prospective chain, so saved and prepared rows are identical.
                existing_ids = {str(p.get("id")) for p in providers}
                while True:
                    incoming_id = uuid.uuid4().hex[:12]
                    if incoming_id not in existing_ids:
                        break
                candidate["id"] = incoming_id
            for index, existing in enumerate(providers):
                if str(existing.get("id")) == incoming_id:
                    providers[index] = candidate
                    break
            else:
                if cfg.get("id"):
                    raise ValueError("metadata provider no longer exists")
                providers.append(candidate)
            changed = providers != getattr(self, "_metadata_provider_snapshot", None)
            if changed:
                metadata, snapshot = self._prepare_metadata(providers)
                revision = uuid.uuid4().hex
            # Store.put_item also writes the settings file; no published chain
            # changes if preflight cannot build both candidate representations.
            saved = self.store.put_item("metadata_providers", candidate,
                                        require_persist=True)
            if changed:
                self.metadata, self._metadata_provider_snapshot, self.metadata_revision = (
                    metadata, snapshot, revision)
            return saved

    def delete_metadata_provider(self, provider_id: str) -> bool:
        """Publish a provider removal only after it has been saved."""
        with self._metadata_lock:
            providers = self.store.list_items("metadata_providers")
            remaining = [p for p in providers if str(p.get("id")) != provider_id]
            if len(remaining) == len(providers):
                return False
            metadata, snapshot = self._prepare_metadata(remaining)
            if not self.store.delete_item("metadata_providers", provider_id,
                                          require_persist=True):
                return False
            self.metadata, self._metadata_provider_snapshot, self.metadata_revision = (
                metadata, snapshot, uuid.uuid4().hex)
            return True

    def identify(self, filename: str = "", verification=None) -> dict:
        """Name a game, and say how confidently.

        `matched_by` travels with the answer because "we matched a DAT name"
        and "we guessed from the filename" deserve different amounts of trust.
        A UI that shows a cover without saying which is inviting somebody to
        believe the wrong one.
        """
        with self._metadata_lock:
            metadata = self.metadata
        info = metadata.identify(verification=verification,
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

    def retroarch_nci_status(self) -> dict:
        """The real-RetroArch source, or an honest account of there not being one.

        Its own row rather than folded into the stream server's, because the
        two answer different questions: `STREAM_SERVER_URL` is a custom HTTP
        router that knows its installed cores, while the NCI is stock
        RetroArch's UDP interface that knows only what is loaded right now.
        Host and port are shown -- never a credential, and the NCI has none,
        which is precisely why ROMarr sends it reads only.
        """
        if not self.retroarch_nci:
            return {
                "configured": False,
                "hint": ("set RETROARCH_NCI_HOST to a RetroArch running with "
                         "network_cmd_enable to see it here"),
            }
        return self.retroarch_nci.status()

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
            queued = sum(1 for i in self.queue if i.state in ("queued", "grabbed", "import-review"))
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
        path = ""
        if destination:
            try:
                routed = self.library_for(platform)
                root = self.library_root(routed[0]) if routed else self.library
                path = Path(destination).resolve().relative_to(
                    Path(root).resolve()).as_posix()
            except (OSError, ValueError):
                # Keep an unusable external path out of the library index.
                path = ""
        self.hashes.add(sha1, getattr(verdict, "game", "") or fallback,
                        platform, getattr(verdict, "status", "") == VERIFIED,
                        path)
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
        # Normalized to an absolute URL before it leaves: a scheme-less
        # public_url stored on THEIR side would break their callback to us the
        # same way a scheme-less peer URL breaks ours.
        from .federation import ensure_scheme
        mine = ensure_scheme(str(self.store.settings.get("public_url")
                                 or self.federation.url or ""))
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
        """A RomM client pointed at a friend's server, using their account.

        Normalized at use as well as at store: peer rows written before the
        normalization existed may still hold a scheme-less address, and a
        RommConfig handed one would fail the same un-sendable way.
        """
        from .clients import Romm, RommConfig
        from .federation import ensure_scheme
        return Romm(RommConfig(base_url=ensure_scheme(peer.url),
                               username=peer.username,
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
        """One request to a friend's server, as a server rather than a user.

        Their address may have been typed without a scheme, so every candidate
        `url_candidates` offers is tried in order (https first, then http for
        a scheme-less address) and the first that answers wins. A
        `MissingSchema` -- the old failure -- cannot happen, because a
        scheme-less address now produces two absolute URLs instead of one
        un-sendable string.
        """
        import requests as _rq
        from .federation import url_candidates

        candidates = url_candidates(peer.url)
        if not candidates:
            return None, (f"{peer.name}'s address {peer.url!r} is not a URL "
                          "ROMarr can call -- set a public address like "
                          "https://their.server")
        last_err = ""
        for base in candidates:
            url = base.rstrip("/") + path
            try:
                response = _rq.request(method, url, headers=peer.headers(),
                                       json=payload, timeout=timeout)
            except _rq.RequestException as err:
                # This candidate did not answer. Try the next scheme before
                # giving up -- an https-only guess that hits a plain-http LAN
                # box is exactly the case this loop exists for.
                last_err = f"could not reach {peer.name}: {err}"
                continue
            if response.status_code == 401:
                return None, (f"{peer.name} refused the credential -- they may "
                              f"have removed you, or not confirmed you yet")
            if not response.ok:
                return None, f"{peer.name} answered HTTP {response.status_code}"
            try:
                return response.json(), ""
            except ValueError:
                return None, f"{peer.name} sent something that was not JSON"
        return None, last_err or f"could not reach {peer.name}"

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
            "update_available": self._update_check_view()["update_available"],
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

        library_root = self.library
        root = platform_dir(library_root, platform,
                            platform_folders=self.platform_folders())
        state = {"status": "running", "platform": platform.slug,
                 "scanned": 0, "verified": 0, "bad": 0, "unknown": 0,
                 "bad_files": [], "duplicates": [],
                 "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        ROMarr._audit_state = state

        def run():
            from .dat import BAD_DUMP, VERIFIED, hash_file
            from .quarantine import raw_sha1
            seen: dict[tuple[str, int], str] = {}
            # Replace this platform's hashes rather than accumulating them,
            # so a re-audit after a cleanup does not leave netplay offering
            # dumps that are no longer on disk.
            self.hashes.clear_platform(platform.slug)
            from .libraries import FolderLibrary
            allowed_extensions = {
                ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                for ext in (set(platform.extensions) | set(FolderLibrary.ARCHIVES))
            }
            try:
                files = [p for p in sorted(root.rglob("*"))
                         if p.is_file() and not p.is_symlink()
                         and p.suffix.lower() in allowed_extensions][:50_000]
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
                            "file": path.relative_to(library_root).as_posix(),
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
                        path.relative_to(library_root).as_posix())
                # DAT verification strips copier headers on some platforms.
                # Quarantine candidates must match every physical byte instead.
                try:
                    raw_sha = raw_sha1(path)
                    raw_size = path.stat().st_size
                except OSError:
                    continue
                key = (raw_sha, raw_size)
                if key in seen:
                    state["duplicates"].append({
                        "file": path.relative_to(library_root).as_posix(),
                        "same_as": seen[key], "sha1": raw_sha,
                        "size": raw_size})
                else:
                    seen[key] = path.relative_to(library_root).as_posix()
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

    def duplicate_quarantine_state(self) -> dict:
        from .quarantine import manifests, preview
        audit = dict(self._audit_state or {})
        return {"preview": preview(audit.get("duplicates") or []),
                "manifests": manifests(self.library)}

    def quarantine_duplicates(self, platform_slug: str,
                              candidates: list[dict]) -> dict:
        from .quarantine import quarantine
        state = self._audit_state or {}
        if state.get("status") != "done" or state.get("platform") != platform_slug:
            return {"error": "run a completed audit for this platform first"}
        root = platform_dir(self.library, platform_slug,
                            platform_folders=self.platform_folders())
        if not root.is_dir():
            return {"error": "audited platform directory is unavailable"}
        try:
            return quarantine(self.library, root, candidates,
                              state.get("duplicates") or [])
        except (OSError, ValueError) as err:
            return {"error": str(err)}

    def restore_duplicate(self, manifest_id: str, entry_id: str) -> dict:
        from .quarantine import restore
        try:
            return restore(self.library, manifest_id, entry_id)
        except (OSError, ValueError) as err:
            return {"error": str(err)}

    def _library_rename_target(self, platform_slug: str):
        """Resolve the same routed library and layout for every rename step."""
        configured = getattr(self, "game_libraries", None) or []
        if not configured:
            return Path(self.library).resolve(), "flat", ""
        routed = self.library_for(platform_slug)
        if routed is None:
            raise ValueError("no library is configured for this platform")
        cfg, _backend = routed
        return (self.library_root(cfg).resolve(), self.library_layout(cfg),
                str(cfg.get("id") or ""))

    def _library_rename_folders(self, platform_slug: str) -> dict[str, str]:
        """Return effective `platform_folders` overrides for the routed rename library."""
        configured = getattr(self, "game_libraries", None) or []
        if not configured:
            return self.platform_folders()
        routed = self.library_for(platform_slug)
        if routed is None:
            return self.platform_folders()
        cfg, _backend = routed
        return self.platform_folders(cfg)

    def _library_rename_roots(self) -> list[Path]:
        roots = [Path(self.library)]
        roots.extend(self.library_root(cfg)
                     for cfg, _backend in (getattr(self, "game_libraries", None)
                                           or []))
        previous = getattr(self, "_library_rename_root", None)
        if previous is not None:
            roots.append(Path(previous))
        unique = {}
        for root in roots:
            try:
                resolved = Path(root).resolve()
            except OSError:
                continue
            unique[str(resolved)] = resolved
        return list(unique.values())

    def preview_library_renames(self, platform_slug: str, *,
                                scope: str = "", offset: int = 0) -> dict:
        """Build a DAT-verified, read-only preview in a background thread."""
        import threading as _threading
        from .library_rename import plan, resolve_platform_folder

        active = getattr(self, "_library_rename_thread", None)
        if active is not None and active.is_alive():
            return dict(self._library_rename_state)
        platform = resolve(platform_slug)
        if platform is None:
            return {"error": f"unknown platform: {platform_slug!r}"}
        if (self.dats is None
                or not getattr(self.dats, "dats", None)):
            return {"error": "load DATs under Settings -> Media Management first"}
        try:
            library_root, layout, library_id = self._library_rename_target(platform.slug)
            folders = self._library_rename_folders(platform.slug)
            expected_folder = resolve_platform_folder(platform.slug, folders)
        except (OSError, ValueError) as err:
            return {"error": str(err)}
        dats = self.dats
        state = {"status": "running", "platform": platform.slug,
                 "folder": expected_folder,
                 "library_id": library_id, "layout": layout,
                 "scanned": 0, "entries": []}
        self._library_rename_root = library_root
        self._library_rename_state = state

        def run():
            try:
                kwargs = {"platform_folders": folders} if folders else {}
                state.update(plan(library_root, platform.slug, dats,
                                   scope=scope, offset=offset, layout=layout,
                                   **kwargs))
            except (OSError, ValueError) as err:
                state.update(status="failed", error=str(err))

        self._library_rename_thread = _threading.Thread(
            target=run, name="romarr-library-rename-preview", daemon=True)
        self._library_rename_thread.start()
        return dict(state)

    def library_rename_state(self) -> dict:
        from .library_rename import manifests
        all_manifests = []
        for root in self._library_rename_roots():
            all_manifests.extend(manifests(root))
        all_manifests.sort(
            key=lambda item: str(item.get("created_at") or
                                 item.get("timestamp") or ""), reverse=True)
        return {"preview": dict(getattr(self, "_library_rename_state", {}) or
                                {"status": "never run"}),
                "manifests": all_manifests[:10]}

    def apply_library_renames(self, platform_slug: str,
                              files: list[str]) -> dict:
        import uuid as _uuid
        from .library_rename import apply, manifest_for, resolve_platform_folder
        state = getattr(self, "_library_rename_state", {}) or {}
        if state.get("status") != "done" or state.get("platform") != platform_slug:
            return {"error": "run a completed preview for this platform first"}
        preview_root = getattr(self, "_library_rename_root", None)
        if preview_root is None:
            return {"error": "library path changed since preview; run a new preview"}
        manifest_id = _uuid.uuid4().hex
        try:
            current_root, current_layout, current_id = self._library_rename_target(platform_slug)
            current_folders = self._library_rename_folders(platform_slug)
            current_folder = resolve_platform_folder(platform_slug, current_folders)
            if (current_root != Path(preview_root).resolve()
                    or current_layout != state.get("layout", "flat")
                    or current_id != state.get("library_id", "")
                    or current_folder != state.get("folder", current_folder)):
                return {"error": "library route, folder, or layout changed since preview; run a new preview"}
            apply_kwargs = ({"platform_folders": current_folders}
                            if current_folders else {})
            result = apply(preview_root, platform_slug, files,
                           state.get("entries") or [], manifest_id=manifest_id,
                           **apply_kwargs)
            self.hashes.move_paths({
                entry["original"]: entry["target"]
                for entry in result.get("entries", [])
                if entry.get("status") == "moved"},
                root=preview_root)
            self._library_rename_state = {
                "status": "applied", "platform": platform_slug,
                "folder": current_folder,
                "library_id": current_id, "layout": current_layout,
                "entries": result.get("entries", [])}
            ROMarr._audit_state = {
                "status": "stale", "platform": platform_slug,
                "error": "The library changed; run Audit again to refresh it."}
            return result
        except (OSError, ValueError) as err:
            partial = manifest_for(preview_root, manifest_id)
            moved = [entry for entry in (partial or {}).get("entries", [])
                     if entry.get("status") == "moved"]
            if moved:
                self.hashes.move_paths({
                    entry["original"]: entry["target"] for entry in moved},
                    root=preview_root)
                self._library_rename_state = {
                    "status": "failed", "platform": platform_slug,
                    "error": str(err), "entries": moved}
                ROMarr._audit_state = {
                    "status": "stale", "platform": platform_slug,
                    "error": "The library changed; run Audit again to refresh it."}
                return {"error": str(err), "manifest_id": manifest_id,
                        "entries": moved}
            return {"error": str(err)}

    def rollback_library_renames(self, manifest_id: str) -> dict:
        from .library_rename import rollback, manifest_for, ROOT_NAME
        matches = []
        for root in self._library_rename_roots():
            path = root / ROOT_NAME / f"manifest-{manifest_id}.json"
            if path.is_file() and not path.is_symlink():
                matches.append(root)
        if len(matches) != 1:
            return {"error": "rename journal is unavailable or ambiguous"}
        library_root = matches[0]
        journal = manifest_for(library_root, manifest_id) or {}
        platform_slug = str(journal.get("platform") or "")
        try:
            result = rollback(library_root, manifest_id)
            restored = [entry for entry in result.get("entries", [])
                        if entry.get("status") == "rolled_back"]
            self.hashes.move_paths({
                entry["target"]: entry["original"] for entry in restored},
                root=library_root)
            self._library_rename_state = {
                "status": "rolled_back", "entries": result.get("entries", [])}
            if restored:
                ROMarr._audit_state = {
                    "status": "stale",
                    "platform": platform_slug,
                    "error": "The library changed; run Audit again to refresh it."}
            return result
        except (OSError, ValueError) as err:
            partial = manifest_for(library_root, manifest_id)
            restored = [entry for entry in (partial or {}).get("entries", [])
                        if entry.get("status") == "rolled_back"]
            if restored:
                self.hashes.move_paths({
                    entry["target"]: entry["original"] for entry in restored},
                    root=library_root)
                self._library_rename_state = {
                    "status": "failed", "error": str(err), "entries": restored}
                ROMarr._audit_state = {
                    "status": "stale",
                    "platform": (partial or {}).get("platform", ""),
                    "error": "The library changed; run Audit again to refresh it."}
                return {"error": str(err), "entries": restored}
            return {"error": str(err)}

    def search_missing(self, *, auto: bool = False) -> dict:
        """Retry Wanted, the way *arr's missing search does.

        Manual (the Tasks page button) searches everything: pressing the
        button is consent. The scheduled sweep honours each item's backoff,
        so a title that has failed for months is retried weekly rather than
        on every sweep -- hammering indexers for games that were not there
        this morning is how trackers hand out bans.
        """
        searched = grabbed_count = skipped = skipped_in_flight = 0
        for item in list(self.store.wanted):
            if auto and not next_search_due(item.attempts, item.searched_at):
                skipped += 1
                continue
            result = (self._automatic_request(item.game, item.platform)
                      if auto else self.request(item.game, item.platform))
            if auto and result.get("skipped"):
                skipped_in_flight += 1
                continue
            searched += 1
            if auto:
                self.store.mark_searched(item.game, item.platform)
            if result.get("ok"):
                grabbed_count += 1
        message = f"Searched {searched}, grabbed {grabbed_count}"
        if skipped:
            message += f", {skipped} waiting out backoff"
        if skipped_in_flight:
            message += f", {skipped_in_flight} already downloading"
        return {"searched": searched, "grabbed": grabbed_count,
                "skipped": skipped, "skipped_in_flight": skipped_in_flight,
                "message": message}

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
        grabbed_count = skipped_in_flight = 0
        for item in wanted:
            platform = resolve(item.platform)
            if platform is None:
                continue
            pick = best_release(releases, item.game, platform,
                               profile=self.profile_for(platform), blocklist=self.blocklist)
            if pick is None or not pick.download_url:
                continue
            result = self._automatic_grab(pick, item.game, platform.slug)
            if result.get("skipped"):
                skipped_in_flight += 1
            elif result.get("ok"):
                grabbed_count += 1
        suffix = (f", skipped {skipped_in_flight} already downloading"
                  if skipped_in_flight else "")
        return (f"{len(releases)} feed item(s) from {sources} source(s), "
                f"grabbed {grabbed_count}{suffix}")

    #: Where release news comes from. Only ever read, never written to.
    RELEASES_URL = "https://api.github.com/repos/BlizzHacker/romarr/releases/latest"

    def reload_oidc(self) -> None:
        """(Re)build the OIDC client from stored settings + environment.

        Called at startup and after any settings save that touches an oidc_*
        key, so the login button appears the moment the configuration is
        complete -- no restart, same contract as every other live setting.
        """
        from .oidc import client_from_settings
        self.oidc = client_from_settings(self.store.settings, self._env)
        if self.oidc is not None:
            problems = self.oidc.problems()
            if problems:
                log.warning("OIDC is configured but incomplete: %s",
                            "; ".join(problems))
                self.oidc = None
            else:
                log.info("OIDC sign-in enabled via %s", self.oidc.issuer)

    def oidc_status(self) -> dict:
        """What the login page and Settings need to know about OIDC. No
        secrets: the button label and whether a client secret is set, never
        the secret."""
        if self.oidc is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "display_name": self.oidc.display_name,
            "issuer": self.oidc.issuer,
            "client_id": self.oidc.client_id,
            "has_secret": bool(self.oidc.client_secret),
            "required_group": self.oidc.required_group,
            "verify_signature": self.oidc.verify_signature,
        }

    def oidc_redirect_uri(self, request_host: str,
                          forwarded_proto: str = "") -> str:
        """The callback URL registered with the IdP.

        public_url wins when set -- that is the address the IdP was told
        about, and a request Host behind a proxy may be the internal one. The
        Host header is the fallback for an install that never set public_url
        and is reached directly; there, X-Forwarded-Proto from a proxy (or
        https as the safe default -- a wrong guess is visible as a
        redirect_uri mismatch at the IdP, not a silent failure) picks the
        scheme.
        """
        base = str(self.store.settings.get("public_url") or "").rstrip("/")
        if not base and request_host:
            scheme = str(forwarded_proto or "").strip().lower() or "https"
            if scheme not in ("http", "https"):
                scheme = "https"
            base = f"{scheme}://{request_host}"
        return f"{base}/oidc/callback" if base else "/oidc/callback"

    def _record_update_check(self, state: str, reason: str, *,
                             checked_channel: str,
                             latest: str = "", error: str = "") -> None:
        """Persist one completed check without erasing the last good comparison.

        The legacy Boolean is cleared for unknown/error outcomes, rather than
        presenting a cached availability flag as a new successful check.
        """
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            settings = self.store.settings
            settings["_update_state"] = state
            settings["_update_reason"] = reason
            settings["_update_last_attempt"] = stamp
            settings["_update_error"] = error
            settings["_update_checked_version"] = VERSION
            settings["_update_checked_channel"] = checked_channel
            settings["_update_available"] = state == "available"
            if latest:
                settings["_latest_version"] = latest
            if state in ("current", "available"):
                settings["_update_last_success"] = stamp
            self.store.save()

    def _update_check_view(self) -> dict:
        """Snapshot the check verdict; invalidate it after a build/channel change."""
        with self._lock:
            settings = dict(self.store.settings)
        channel = str(settings.get("update_channel") or "stable").lower()
        state = str(settings.get("_update_state") or "never_checked")
        reason = str(settings.get("_update_reason") or "not_checked")
        if state not in ("never_checked", "available", "current", "unknown", "error"):
            state, reason = "unknown", "invalid_saved_status"
        elif state in ("available", "current") and not _comparable_version(VERSION):
            state, reason = "unknown", "running_version_unknown"
        elif state in ("available", "current") and (
                get_build_info(VERSION).get("type") == "development"):
            state, reason = "unknown", "development_build"
        elif state != "never_checked" and (
                settings.get("_update_checked_version") != VERSION
                or settings.get("_update_checked_channel") != channel):
            state, reason = "unknown", "build_or_channel_changed"
        return {
            "state": state,
            "reason": reason,
            "channel": channel,
            "last_attempt_at": settings.get("_update_last_attempt") or "",
            "last_success_at": settings.get("_update_last_success") or "",
            "last_error": (settings.get("_update_error") or ""
                           if state == "error" else ""),
            "latest_version": settings.get("_latest_version") or "",
            "update_available": state == "available",
            "in_progress": self._update_check_lock.locked(),
        }

    def check_update(self, *, manual: bool = False) -> dict:
        """Check the stable release without concurrent requests or stale writes.

        The explicit command is check-only; the daily scheduler may notify or
        apply under the existing opt-in. Nightly revision discovery is separate.
        """
        import requests as _requests

        # A scheduled daily sweep must not be skipped because a manual check
        # already owns the request; wait for it and perform scheduled actions.
        # Manual callers return busy immediately rather than block the UI.
        if not self._update_check_lock.acquire(blocking=not manual):
            return {"ok": False, "busy": True, "update_available": False,
                    "message": "an update check is already in progress"}
        try:
            with self._lock:
                channel = str(self.store.settings.get("update_channel")
                              or "stable").lower()
            if channel != "stable":
                # A stable release endpoint cannot answer whether the Git
                # source revision on the nightly channel has advanced.
                reason = ("nightly_revision_not_checked" if channel == "nightly"
                          else "unsupported_update_channel")
                self._record_update_check("unknown", reason,
                                          checked_channel=channel)
                message = ("nightly revision checking is not yet supported; "
                           "no stable-release request was made" if channel == "nightly"
                           else f"unknown update channel {channel!r}; no check was made")
                return {"ok": True, "state": "unknown", "latest": "",
                        "indeterminate": True, "update_available": False,
                        "message": message}

            def failed(reason: str, message: str) -> dict:
                """Record a failed attempt while retaining the last good release."""
                self._record_update_check("error", reason,
                                          checked_channel=channel,
                                          error=message)
                return {"ok": False, "state": "error", "reason": reason,
                        "update_available": False, "message": message}

            headers = {"Accept": "application/vnd.github+json"}
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                response = _requests.get(self.RELEASES_URL, timeout=10,
                                         headers=headers)
                if response.status_code in (403, 429):
                    reset = response.headers.get("X-RateLimit-Reset") or ""
                    if response.status_code == 429 or not token:
                        message = ("github.com rate-limited this address "
                                   "(unauthenticated API: 60 requests/hour "
                                   "per IP, shared across your network). "
                                   "Set GITHUB_TOKEN in the environment to "
                                   "lift it to 5,000/hour" if not token else
                                   "github.com rate-limited the update check")
                        if reset:
                            message += f"; limit resets at {reset}"
                        return failed("rate_limited", message)
                    return failed("request_denied",
                                  "github.com denied the update check (HTTP 403)")
                response.raise_for_status()
                body = response.json()
            except Exception as err:
                return failed("request_failed",
                              f"could not reach github.com: {err.__class__.__name__}")

            if not isinstance(body, dict):
                return failed("invalid_response", "github.com returned invalid release data")
            latest = str(body.get("tag_name") or "").lstrip("vV")
            url = str(body.get("html_url") or "")
            if not _comparable_version(latest):
                return failed("invalid_release",
                              "github.com answered without a valid release tag")

            with self._lock:
                active_channel = str(self.store.settings.get("update_channel")
                                     or "stable").lower()
            if active_channel != channel:
                self._record_update_check("unknown", "channel_changed_during_check",
                                          latest=latest, checked_channel=channel)
                return {"ok": True, "state": "unknown", "latest": latest,
                        "indeterminate": True, "update_available": False,
                        "message": "update channel changed during the check; no action taken"}
            development = get_build_info(VERSION).get("type") == "development"
            if not _comparable_version(VERSION) or development:
                reason = ("development_build" if development
                          else "running_version_unknown")
                self._record_update_check("unknown", reason,
                                          latest=latest, checked_channel=channel)
                message = (f"latest stable release is {latest}; this is a "
                           "development build, so stable update availability "
                           "is unknown and nothing was changed" if development else
                           f"latest is {latest}; this build does not know its "
                           "own version, so it cannot say whether it is "
                           "behind, and nothing was changed")
                return {"ok": True, "state": "unknown", "latest": latest,
                        "indeterminate": True, "update_available": False,
                        "message": message}

            newer = compare_key(latest) > compare_key(VERSION)
            state = "available" if newer else "current"
            self._record_update_check(state, "stable_release_version",
                                      latest=latest, checked_channel=channel)

            if newer and not manual:
                with self._lock:
                    # Disabled scheduled checks must not send notifications
                    # or consume the one-per-version notification marker.
                    should_notify = (
                        str(self.store.settings.get("update_channel")
                            or "stable").lower() == channel
                        and self.store.settings.get("update_check", True)
                        and self.store.settings.get("_update_notified") != latest)
                    if should_notify:
                        self.store.settings["_update_notified"] = latest
                        self.store.save()
                if should_notify:
                    self.notify(update_available(VERSION, latest, url))

                if (self.store.settings.get("update_check", True)
                        and self.store.settings.get("auto_update", False)):
                    applied = self.apply_update(expected_channel=channel,
                                                expected_release=latest)
                    if applied.get("ok"):
                        return {"ok": True, "state": state, "latest": latest,
                                "update_available": True,
                                "message": applied["message"]}
                    return {"ok": True, "state": state, "latest": latest,
                            "update_available": True,
                            "message": (f"{latest} is available; auto-update did "
                                        f"not run: {applied.get('error', '')}")}

            message = (f"{latest} is available (running {VERSION})" if newer
                       else f"up to date ({VERSION})")
            return {"ok": True, "state": state, "latest": latest,
                    "update_available": newer, "message": message}
        finally:
            self._update_check_lock.release()

    def apply_update(self, *, expected_channel: str = "",
                     expected_release: str = "") -> dict:
        """Launch an explicit Git update; scheduled calls pin the checked target.

        The optional expectation is only passed by a completed stable check.
        Manual ApplyUpdate retains its original setting-driven behavior. The
        channel, queue and target are validated under one lock through launch.
        """
        from . import selfupdate

        env = selfupdate.detect()
        if env.kind == "docker":
            return selfupdate.docker_guidance()
        if not env.can_apply:
            return {"ok": False, "error": env.detail}
        with self._lock:
            channel = str(self.store.settings.get("update_channel")
                          or "stable").lower()
            if channel not in ("stable", "nightly"):
                return {"ok": False, "error": f"unknown update channel {channel!r}"}
            if expected_channel and (
                    channel != expected_channel
                    or not self.store.settings.get("auto_update", False)
                    or not self.store.settings.get("update_check", True)
                    or self.store.settings.get("_latest_version") != expected_release):
                return {"ok": False,
                        "error": "update channel, release or scheduled update preference "
                                 "changed after the check; update not launched"}
            try:
                stall = int(self.store.settings.get(
                    "stalled_timeout_minutes") or 0)
            except (TypeError, ValueError):
                stall = 0
            if selfupdate.queue_busy(self.queue, stall_minutes=stall):
                return {"ok": False, "deferred": True,
                        "error": "downloads or imports are in flight; the update "
                                 "will apply on the next daily check"}
            latest = (expected_release if expected_channel else
                      str(self.store.settings.get("_latest_version") or ""))
            if channel == "nightly":
                latest = latest or VERSION
            target, why = selfupdate.resolve_target(env, channel, latest)
            if not target:
                return {"ok": False, "error": why}
            # The same lock protects HTTP settings writes. The target remains
            # pinned to the checked release, never re-read as origin/main.
            result = selfupdate.launch(env, target)
        if result.get("ok"):
            self.store.record(Event(kind="update", game="", platform="",
                                    release=VERSION,
                                    detail=f"self-update to {target} started "
                                           f"from {env.commit}"))
            self.store.save()
        return result

    def update_status(self) -> dict:
        """Expose the current verdict separately from cached release and errors."""
        from . import selfupdate

        env = selfupdate.detect()
        check = self._update_check_view()
        configured = bool(self.store.settings.get("auto_update", False))
        scheduled = bool(self.store.settings.get("update_check", True))
        development = get_build_info(VERSION).get("type") == "development"
        effective = (configured and scheduled and env.can_apply
                     and check["channel"] == "stable"
                     and _comparable_version(VERSION) and not development)
        if not configured:
            capability_reason = "disabled"
        elif not scheduled:
            capability_reason = "update_check_disabled"
        elif not env.can_apply:
            capability_reason = "install_cannot_self_update"
        elif check["channel"] != "stable":
            capability_reason = "nightly_revision_check_not_supported"
        elif not _comparable_version(VERSION):
            capability_reason = "running_version_unknown"
        elif development:
            capability_reason = "development_build"
        else:
            capability_reason = "enabled"
        return {
            "running_version": VERSION,
            "build": get_build_info(VERSION),
            "latest_version": check["latest_version"],
            "update_available": check["update_available"],
            "check_status": {key: value for key, value in check.items()
                             if key not in ("latest_version", "update_available")},
            "install": {"kind": env.kind, "detail": env.detail,
                        "branch": env.branch, "commit": env.commit,
                        "repo": env.repo, "can_apply": env.can_apply},
            "settings": {
                "update_check": scheduled,
                "auto_update": configured,
                "auto_update_effective": effective,
                "auto_update_reason": capability_reason,
                "update_channel": check["channel"],
                "auto_update_plugins": bool(
                    self.store.settings.get("auto_update_plugins", False)),
            },
            "plugins": hub.plugin_sources(),
            "docker_guidance": (selfupdate.docker_guidance()["guidance"]
                                if env.kind == "docker" else []),
            "update_log": selfupdate.read_log(env),
        }

    def fetch_asset(self, url: str) -> tuple[int, str, bytes]:
        """One library image, fetched server-side for the browser.

        Exists for the mixed-content case: a library on http:// behind an
        https proxy cannot hand its covers to a secure page directly, so
        ROMarr — which talks to the library over the internal network anyway
        — relays the bytes. The response is same-origin with the UI, which
        is a scheme the browser already trusts.

        NOT an open proxy. The URL's origin must equal a configured
        library's origin, and nothing else is ever fetched: without that
        rule this endpoint would turn every session holder into a scanner
        of ROMarr's own network. Returns (status, content-type, body).
        """
        import requests as _requests
        from urllib.parse import urlsplit

        wanted = str(url or "").strip()
        parts = urlsplit(wanted)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return 400, "application/json", b'{"error":"http(s) URL required"}'
        origin = f"{parts.scheme}://{parts.netloc}"
        allowed = set()
        for cfg, backend in self.game_libraries:
            base = str(cfg.get("url") or "") or str(
                getattr(getattr(backend, "_config", None), "base_url", "") or "")
            b = urlsplit(base)
            if b.scheme and b.netloc:
                allowed.add(f"{b.scheme}://{b.netloc}")
        if origin not in allowed:
            return 403, "application/json", \
                b'{"error":"not a configured library origin"}'
        session = getattr(self, "_asset_session", None)
        if session is None:
            session = _requests.Session()
            adapter = _requests.adapters.HTTPAdapter(
                pool_connections=64,
                pool_maxsize=64,
                max_retries=3,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._asset_session = session
        try:
            response = session.get(wanted, timeout=20, stream=True)
        except _requests.RequestException as err:
            log.warning("asset proxy fetch failed for %s: %s", origin, err)
            return 502, "application/json", b'{"error":"library unreachable"}'
        with response:
            if response.status_code != 200:
                return response.status_code, "application/json", \
                    b'{"error":"library did not serve the asset"}'
            # Covers are small; screenshots are not tiny. 10 MB bounds a
            # mistake (an HTML error page, a video) without bounding art.
            body = response.content[:10 * 1024 * 1024]
            ctype = response.headers.get("Content-Type") or "image/png"
            return 200, ctype.split(";")[0].strip(), body


    def _portal_targets(self) -> dict[str, dict]:
        """Allowed library and request-frontend origins mapped to stored credentials."""
        from urllib.parse import urlsplit

        targets: dict[str, dict] = {}
        env_user = str(self._env.get("LIBRARY_USERNAME") or self._env.get("ROMM_USERNAME") or "")
        env_pass = str(self._env.get("LIBRARY_PASSWORD") or self._env.get("ROMM_PASSWORD") or "")
        env_key = str(self._env.get("LIBRARY_API_KEY") or self._env.get("ROMM_API_TOKEN") or "")

        def register(raw_url: str, cfg: dict | None = None) -> None:
            b = urlsplit(str(raw_url or "").strip())
            if b.scheme not in ("http", "https") or not b.netloc:
                return
            origin = f"{b.scheme}://{b.netloc}"
            c = cfg or {}
            existing = targets.get(origin) or {}
            username = str(c.get("username") or existing.get("username") or env_user)
            password = str(c.get("password") or existing.get("password") or env_pass)
            api_key = str(c.get("api_key") or existing.get("api_key") or env_key)
            if username == "********":
                username = existing.get("username") or env_user
            if password == "********":
                password = existing.get("password") or env_pass
            if api_key == "********":
                api_key = existing.get("api_key") or env_key
            targets[origin] = {
                "origin": origin,
                "username": username,
                "password": password,
                "api_key": api_key,
            }

        for cfg in self.store.list_items("libraries"):
            register(str(cfg.get("url") or ""), cfg)
        for cfg, backend in self.game_libraries:
            base = str(cfg.get("url") or "") or str(
                getattr(getattr(backend, "_config", None), "base_url", "") or "")
            register(base, cfg)
        for extra in (
            getattr(self, "ggrequestz_url", ""),
            self.store.settings.get("_romm_url", ""),
            self._env.get("ROMM_URL", ""),
            self._env.get("ROMM_PUBLIC_URL", ""),
            self._env.get("GGREQUESTZ_URL", ""),
        ):
            if extra:
                register(str(extra))
        if "moveweight.com" in str(self._env.get("GGREQUESTZ_URL", "")) or "moveweight.com" in str(self._env.get("ROMM_URL", "")):
            register("https://romm.moveweight.com")
        return targets

    def active_portal_url(self) -> str:
        """The currently active portal origin, or the first configured library origin."""
        active = str(getattr(self, "_active_portal_url", "") or "").strip()
        targets = self._portal_targets()
        if active and active in targets:
            return active
        if targets:
            first = next(iter(targets))
            self._active_portal_url = first
            return first
        return ""

    @staticmethod
    def _inject_portal_shim(html_bytes: bytes, origin: str, initial_path: str = "/") -> bytes:
        """Inject a same-origin history + fetch/XHR rewrite shim into proxied HTML."""
        try:
            text = html_bytes.decode("utf-8", errors="replace")
        except Exception:
            return html_bytes
        import re
        text = re.sub(r'\s+crossorigin(?:="[^"]*")?', '', text)
        safe_origin = json.dumps(origin)
        safe_path = json.dumps(initial_path if initial_path.startswith("/") else "/" + initial_path)
        shim = (
            "<script>(function(){"
            f"var TARGET={safe_origin};"
            f"var INIT_PATH={safe_path};"
            "var PROXY='/api/v1/portal/proxy?url='+encodeURIComponent(TARGET)+'&path=';"
            "try{if(location.pathname.indexOf('/api/v1/portal/proxy')===0){"
            "history.replaceState(null,'',INIT_PATH);}}catch(e){}"
            "function rw(u){"
            "if(typeof u!=='string'||!u)return u;"
            "if(u.indexOf('/api/v1/portal/proxy')===0)return u;"
            "if(u.indexOf(location.origin+'/')===0)u=u.slice(location.origin.length);"
            "if(u.indexOf('api/')===0)u='/'+u;"
            "if(u.charAt(0)==='/'&&u.charAt(1)!=='/')return PROXY+encodeURIComponent(u);"
            "if(u.indexOf(TARGET)===0)return PROXY+encodeURIComponent(u.slice(TARGET.length)||'/');"
            "return u;}"
            "var _f=window.fetch;"
            "if(_f){window.fetch=function(i,init){"
            "if(typeof i==='string')i=rw(i);"
            "else if(i&&typeof i.url==='string'){try{i=new Request(rw(i.url),i);}catch(e){}}"
            "return _f.call(this,i,init);};}"
            "var _o=XMLHttpRequest.prototype.open;"
            "XMLHttpRequest.prototype.open=function(m,u){"
            "var a=Array.prototype.slice.call(arguments);a[1]=rw(u);"
            "return _o.apply(this,a);};"
            "})();</script>"
        )
        lower = text.lower()
        idx = lower.find("<head>")
        if idx != -1:
            pos = idx + len("<head>")
            text = text[:pos] + shim + text[pos:]
        elif "<html" in lower:
            text = shim + text
        else:
            text = shim + text
        return text.encode("utf-8")

    def proxy_portal(self, url: str = "", path: str = "", *,
                     method: str = "GET", body: bytes | None = None,
                     content_type: str = "", accept: str = "",
                     range_header: str = "") -> tuple[int, str, bytes]:
        """Same-origin authenticated reverse proxy for the Supportive Library Shell.

        Relays HTML, API calls, and assets to a configured library server
        (RomM, Retrom, Gaseous, Gameyfin, or GG Requestz), injecting stored
        library credentials server-side so cross-origin cookies, OIDC
        X-Frame-Options blocks, and HTTPS->HTTP mixed-content blocks never
        break the embedded shell. Restricted strictly to configured origins.
        """
        import requests as _requests
        from urllib.parse import urlsplit

        targets = self._portal_targets()
        wanted = str(url or "").strip() or self.active_portal_url()
        parts = urlsplit(wanted)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return 400, "application/json", b'{"error":"http(s) URL required"}'
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in targets:
            return 403, "application/json", b'{"error":"not a configured library origin"}'

        self._active_portal_url = origin
        creds = targets[origin]
        sub_path = str(path or "").strip()
        if not sub_path:
            sub_path = parts.path or "/"
            if parts.query:
                sub_path = f"{sub_path}?{parts.query}"
        if not sub_path.startswith("/"):
            sub_path = "/" + sub_path

        sessions = getattr(self, "_portal_sessions", None)
        if sessions is None:
            sessions = {}
            self._portal_sessions = sessions
        session = sessions.get(origin)
        if session is None:
            session = _requests.Session()
            adapter = _requests.adapters.HTTPAdapter(
                pool_connections=64,
                pool_maxsize=64,
                max_retries=3,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            sessions[origin] = session

        auth = None
        if creds.get("username") and creds.get("password"):
            auth = (creds["username"], creds["password"])
        session.auth = auth

        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        if range_header:
            headers["Range"] = range_header
        if creds.get("api_key") and not auth:
            headers["X-Api-Key"] = creds["api_key"]
            headers["Authorization"] = f"Bearer {creds['api_key']}"
        csrf = session.cookies.get("romm_csrftoken")
        if csrf:
            headers["X-CSRFToken"] = csrf

        upstream_url = origin + sub_path
        try:
            response = session.request(
                method.upper(),
                upstream_url,
                data=body if method.upper() in ("POST", "PUT", "PATCH", "DELETE") else None,
                headers=headers,
                auth=auth,
                timeout=30,
                allow_redirects=True,
                stream=True,
            )
        except _requests.RequestException as err:
            log.warning("portal proxy fetch failed for %s: %s", upstream_url, err)
            return 502, "application/json", b'{"error":"library unreachable"}'
        with response:
            ctype = response.headers.get("Content-Type") or "application/octet-stream"
            if "text/html" in ctype.lower() and method.upper() == "GET":
                raw = response.content[:10 * 1024 * 1024]
                raw = self._inject_portal_shim(raw, origin, sub_path)
                ctype = "text/html; charset=utf-8"
            else:
                raw = response.content[:500 * 1024 * 1024]
            return ProxyResponse(response.status_code, ctype, raw, dict(response.headers))

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
        if name == "FailedDownloads":
            return self.retire_dead_downloads()
        if name == "ListSync":
            return self.list_sync()
        if name == "UpdateCheck":
            return self.check_update(manual=True)
        if name == "ApplyUpdate":
            return self.apply_update()
        if name == "PluginUpdate":
            return hub.update_plugins()
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
        if name == "Decompress":
            return self.decompress_batch()
        return {"error": f"unknown command: {name}"}

    def decompress_batch(self, directory: str = "", *,
                         delete_originals: bool = False,
                         dry_run: bool = False) -> dict:
        """Batch decompression: extract, validate against DATs, then delete.

        The workflow issue #22 asked for. Compressatorium does the extraction
        (CHD, RVZ, Z3DS, NSZ, CSO, 7z, zip -- every format it speaks); ROMarr
        does the verification, because ROMarr holds the DAT index and the
        question "is this the published dump" is answered here, not by the
        extractor.

        Deletion is off by default and gated on verification: a file is
        deleted only when every output it produced matched a DAT entry.
        An "unknown" output is kept -- no DAT does not mean bad file, and
        deleting on unknown would destroy homebrew and translations.
        """
        root = directory or str(self.store.settings.get("decompress_path") or "")
        if not root:
            return {"error": "no directory given -- pass 'directory' in the "
                             "request body or set decompress_path in settings"}
        url = self._env.get("COMPRESSATORIUM_URL", "")
        if not url and not dry_run:
            return {"error": "COMPRESSATORIUM_URL is not configured. Start a "
                             "compressatorium container (pacnpal/compressatorium) "
                             "and set the variable, or run with dry_run=true "
                             "to see what would be processed."}
        report = run_decompress_batch(
            root,
            compressatorium_url=url,
            compressatorium_api_key=self._env.get("COMPRESSATORIUM_API_KEY", ""),
            dats=self.dats,
            delete_originals=bool(delete_originals),
            dry_run=bool(dry_run),
        )
        result = report.to_dict()
        # Recorded as an event so the History page shows the run, like every
        # other destructive operation here does.
        self.store.record(Event(
            kind="decompress", game=f"batch: {root}",
            platform="",
            detail=(f"scanned={result['scanned']} "
                    f"decompressed={result['decompressed']} "
                    f"verified={result['verified']} "
                    f"kept={result['kept']} "
                    f"failed={result['failed']} "
                    f"deleted={result['deleted']}"
                    + (" (dry run)" if dry_run else ""))))
        return result

    def clamav_status(self) -> dict:
        """Whether downloads are scanned before import, and whether clamd answers."""
        address = str(self.store.settings.get("clamav_address") or "").strip()
        out = {"configured": bool(self.store.settings.get("clamav_enabled")),
               "address": address, "ok": False, "version": "", "problem": ""}
        if out["configured"]:
            try:
                out["version"] = clamav.version(address) if address else ""
                out["ok"] = bool(address)
                out["problem"] = "" if address else "no ClamAV address set"
            except clamav.ScanUnavailable as err:
                out["problem"] = str(err)
        return out

    def _virus_check(self, path: Path, name: str, platform, queue_item) -> dict | None:
        """None when a finished download may be imported, else why not.

        Scanning is fail-closed: a download ClamAV could not judge waits for
        the next sweep, untouched, rather than going in unscanned. Only an
        infection marks the row, as the release's fault, so failed download
        handling replaces it.
        """
        enabled = any(self.store.settings.get(key) for key in (
            "clamav_enabled", "scanner_defender_enabled", "scanner_mwm_enabled",
            "scanner_vt_enabled"))
        if not enabled:
            return None
        try:
            found, _ = scanners.scan(self.store.settings, self._env, path)
        except (scanners.ScanUnavailable, OSError) as err:
            log.warning("%s waits for a virus scan: %s", name, err)
            label = "ClamAV" if self.store.settings.get("clamav_enabled") else "malware scanner"
            return {"name": name, "ok": False, "reason": f"waiting for {label}: {err}"}
        if not found:
            return None
        detail = "Malware scan: " + "; ".join(
            f"{f.signature} in {Path(f.path).name}" for f in found)
        self.store.record(Event(kind="failed", game=name, platform=platform.slug,
                                detail=detail))
        self.notify(failed(name, detail))
        if queue_item is not None:
            queue_item.state = "import-failed"
            queue_item.release_fault = True
            queue_item.detail = detail
        return {"name": name, "ok": False, "reason": detail}

    def _nup_gate(self, path: Path, name: str, platform, queue_item, *,
                  inferred: bool, selected_primaries=None) -> tuple:
        """Check a download's loose `.nup` files against its platform.

        A NUP's header is a validated table of contents, so unlike a release
        title it can be trusted. Returns `(platform, None)` to carry on, with
        the platform moved to Switch 2 when it was only inferred and a 0x04
        title says otherwise, or `(platform, result)` when the import stops:

          * an invalid NUP fails the import, but not as the release's fault --
            a truncated file looks the same as an incomplete download;
          * a Switch 2 title in a row the operator requested for Switch goes to
            review, because overriding their choice silently is worse than
            asking.

        A 0x01 title ID is never evidence: Switch 2 Editions keep their Switch
        1 base ID. Archives are not opened; their platform stands as requested.
        """
        if platform.slug not in ("switch", "switch-2"):
            return platform, None
        if path.is_file():
            loose = [path] if path.suffix.lower() == ".nup" else []
        elif path.is_dir():
            loose = sorted(p for p in path.rglob("*")
                           if p.suffix.lower() == ".nup" and p.is_file())
        else:
            loose = []
        if selected_primaries is not None:
            selected = {str(primary).replace(chr(92), "/") for primary in selected_primaries}
            loose = [p for p in loose
                     if (p.relative_to(path).as_posix() if path.is_dir() else p.name) in selected]
        game = queue_item.game if queue_item is not None else name
        for nup_path in loose:
            try:
                header = nup.read_header(nup_path)
            except (nup.NupError, OSError) as err:
                detail = f"{nup_path.name} is not a valid NUP: {err}"
                self.store.record(Event(kind="failed", game=game,
                                        platform=platform.slug, detail=detail))
                if queue_item is not None:
                    queue_item.state = "import-failed"
                    queue_item.detail = detail
                return platform, {"name": name, "ok": False, "reason": detail}
            if not (header.is_switch2 and platform.slug == "switch"):
                continue
            title_id = header.primary.title_id
            if inferred:
                log.info("%s: NUP title %s is Switch 2; importing under switch-2",
                         name, title_id)
                platform = by_slug("switch-2")
                if queue_item is not None:
                    queue_item.platform = platform.slug
                continue
            detail = (f"{nup_path.name} is a Switch 2 title ({title_id}) but was "
                      "requested for Switch. Request it for Switch 2, or file it "
                      "with Manual Import, which reads the header.")
            if queue_item is not None:
                # Review rows are re-checked every sweep; record the event once.
                if (queue_item.state, queue_item.detail) != ("import-review", detail):
                    self.store.record(Event(kind="failed", game=game,
                                            platform=platform.slug, detail=detail))
                queue_item.state = "import-review"
                queue_item.detail = detail
            return platform, {"name": name, "ok": False, "reason": detail}
        return platform, None

    def import_finished(self, *, retry_failed: bool = True) -> list[dict]:
        with self._collection_lock:
            return self._import_finished(retry_failed=retry_failed)

    def _import_finished(self, *, retry_failed: bool = True) -> list[dict]:
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
            self.store.save()
        results = []
        finished = []
        for client in self.clients:
            if not getattr(client, "configured", True):
                continue
            try:
                if isinstance(client, QBittorrent) or getattr(client, "supports_selective", False):
                    finished.extend(dict(row, _romarr_client=client)
                                    for row in client.selective_completed(list(self.queue), ownership=self._retained_owners()))
                finished.extend(dict(row, _romarr_client=client)
                                for row in client.completed())
            except Exception as err:
                # One unreachable client must not stop the others importing.
                log.warning("%s completed() failed: %s", getattr(client, "name", client), err)
        for torrent in finished:
            name = torrent.get("name", "")
            platform = None
            platform_inferred = False
            queue_item = None
            source_client = torrent.get("_romarr_client")
            client_id = str(
                getattr(source_client, "romarr_id", "")
                or getattr(source_client, "config_id", "")
                or torrent.get("_romarr_client_id")
                or ""
            )
            client_protocol = str(getattr(source_client, "protocol", "") or "")
            with self._lock:
                match = match_queue_item(
                    self.queue,
                    torrent,
                    client_id=client_id,
                    client_protocol=client_protocol,
                )
                if match is not None:
                    queue_item = match
                    if match.state not in ("imported", "import-failed"):
                        platform = resolve(match.platform) if match.platform else None
                        if platform is None:
                            # Fallback for legacy Any-platform grabs queued with
                            # an empty platform string (#109).
                            inferred = infer_platform(Release(
                                title=name or match.release or match.game,
                                download_url="",
                                size=int(torrent.get("size") or match.size or 0),
                                seeders=0,
                                indexer=match.indexer,
                                categories=(),
                                protocol="torrent",
                            ))
                            if inferred is not None:
                                platform = inferred
                                platform_inferred = True
            if queue_item is not None and queue_item.state in ("imported", "import-failed"):
                continue
            if platform is None:
                warned = getattr(self, "_warned_unmatched", None)
                if warned is None:
                    warned = set()
                    self._warned_unmatched = warned
                if queue_item is not None:
                    wkey = ("no-platform", name)
                    if wkey not in warned:
                        warned.add(wkey)
                        log.warning(
                            "completed download %r matched queue row for %r "
                            "with no platform set; import skipped",
                            name, queue_item.game)
                    continue
                category = str(torrent.get("category") or "")
                romarr_categories = {
                    str(getattr(getattr(client, "_config", None), "category", ""))
                    for client in self.clients
                }
                if category and category in romarr_categories:
                    wkey = (name, category)
                    if wkey not in warned:
                        warned.add(wkey)
                        log.warning("completed download %r in category %r could "
                                    "not be matched to a ROMarr queue row; import "
                                    "skipped", name, category)
                continue
            if torrent.get("_romarr_selection_error") and queue_item is not None:
                detail = str(torrent["_romarr_selection_error"])
                queue_item.state = "import-review"
                queue_item.detail = detail
                self.store.record(Event(kind="failed", game=queue_item.game,
                                        platform=platform.slug, detail=detail))
                results.append({"name": name, "ok": False, "reason": detail})
                continue
            # The client reports the path IT sees. Mapped only after matching a
            # pending queue row so already-imported or foreign downloads do not
            # trigger path mapping logs on every sweep (#110).
            path = map_remote_path(
                torrent.get("content_path") or torrent.get("save_path", ""),
                self.store.settings.get("remote_path_mappings"),
            )
            if (queue_item is not None and (
                    queue_item.file_selection_required
                    or queue_item.selected_file
                    or "minerva" in queue_item.indexer.lower())):
                try:
                    path = selected_import_path(
                        queue_item, torrent, torrent.get("_romarr_client"),
                        self.store.settings.get("remote_path_mappings"))
                except (TypeError, ValueError, OSError, requests.RequestException) as err:
                    detail = f"selected collection file needs review: {err}"
                    queue_item.state = "import-failed"
                    queue_item.detail = detail
                    self.store.record(Event(kind="failed", game=queue_item.game,
                                            platform=platform.slug, detail=detail))
                    results.append({"name": name, "ok": False, "reason": detail})
                    continue
            refused = self._virus_check(path, name, platform, queue_item)
            if refused is not None:
                if platform_inferred and queue_item is not None and queue_item.state == "import-failed":
                    queue_item.platform = platform.slug
                results.append(refused)
                continue
            # A reviewed selection is validated below before its NUPs are gated.
            if not (queue_item and queue_item.import_intent):
                platform, held = self._nup_gate(path, name, platform, queue_item,
                                                inferred=platform_inferred)
                if held is not None:
                    if platform_inferred and queue_item is not None:
                        queue_item.platform = platform.slug
                    results.append(held)
                    continue
            if platform_inferred and queue_item is not None:
                queue_item.platform = platform.slug
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

            target_folders = self.platform_folders(target_cfg)
            selected_primaries = None
            bundle_routes = None
            if queue_item is not None and path.exists():
                from .import_intent import preview_import, queue_identity
                from .romm_bundle import decorate_preview, route_selection
                from .library import MissingArchiveTool
                import zipfile
                import subprocess
                try:
                    preview = preview_import(path, platform, self.library_root(target_cfg),
                                             layout=self.library_layout(target_cfg),
                                             platform_folders=target_folders,
                                             queue_identity=queue_identity(queue_item))
                    preview = decorate_preview(preview, platform, self.library_root(target_cfg),
                                               kind=target_cfg.get("type", ""), layout=self.library_layout(target_cfg),
                                               platform_folders=target_folders, translation=is_translation(name))
                except (OSError, ValueError, RuntimeError, MissingArchiveTool,
                        zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                    # A bad archive belongs to this row, not to every completed
                    # job behind it. Preserve sources and make the failure visible.
                    queue_item.state = "import-failed"
                    queue_item.detail = f"could not preview import: {exc}"
                    self.store.record(Event(kind="failed", game=queue_item.game,
                                            platform=platform.slug, detail=queue_item.detail))
                    results.append({"name": name, "ok": False, "reason": queue_item.detail})
                    continue
                intent = queue_item.import_intent
                if (preview["requires_review"] and not intent) or (intent and intent.get("token") != preview["token"]):
                    queue_item.import_preview = preview
                    queue_item.state = "import-review"
                    queue_item.detail = "Review bundle contents in Queue before importing"
                    results.append({"name": name, "ok": False, "reason": queue_item.detail})
                    continue
                if intent:
                    selected_primaries = [c["primary"] for c in intent["choices"]]
                    if intent.get("routes"):
                        try:
                            bundle_routes = route_selection(preview, intent)
                            if bundle_routes != intent["routes"]:
                                raise ValueError("Reviewed destination routes changed")
                        except ValueError as exc:
                            queue_item.state = "import-review"
                            queue_item.detail = str(exc)
                            results.append({"name": name, "ok": False, "reason": str(exc)})
                            continue

            if selected_primaries is not None:
                platform, held = self._nup_gate(
                    path, name, platform, queue_item, inferred=False,
                    selected_primaries=selected_primaries)
                if held is not None:
                    results.append(held)
                    continue

            outcomes = import_rom(
                path, platform, self.library_root(target_cfg),
                dats=self.dats,
                rename=bool(self.store.settings.get("rename_on_import", True)),
                prefer_hardlinks=bool(self.store.settings.get("prefer_hardlinks", False)),
                layout=self.library_layout(target_cfg),
                translation=is_translation(name),
                platform_folders=target_folders,
                **({"selected_primaries": selected_primaries} if selected_primaries is not None else {}),
                **({"bundle_routes": bundle_routes} if bundle_routes is not None else {}))
            game_title = (queue_item.game if queue_item and getattr(queue_item, "game", "") else name)
            if not outcomes:
                self.store.record(Event(kind="failed", game=game_title,
                                        platform=platform.slug,
                                        detail="no ROMs found in download"))
                results.append({"name": name, "ok": False,
                                "reason": "no ROMs found", "library": label})
                if queue_item is not None:
                    queue_item.state = "import-failed"
                    # The one import failure that is the release's doing: it
                    # downloaded fine and held no game. A path that does not
                    # exist or a full disc is the install's problem and must
                    # not cost the release a place on the blocklist.
                    queue_item.release_fault = True
                continue

            all_ok = all(o.ok for o in outcomes)
            fulfils_base = not (queue_item and queue_item.import_intent.get("existing_game"))
            for outcome in outcomes:
                if outcome.ok:
                    self.store.record(Event(kind="imported", game=game_title,
                                            platform=platform.slug, release=name,
                                            library=label,
                                            detail=f"{outcome.destination} ({outcome.import_method or 'copy'}) {outcome.fallback_reason}"))
                    if all_ok and fulfils_base:
                        self.notify(imported(game_title, platform.slug,
                                             outcome.verification))
                else:
                    self.store.record(Event(kind="failed", game=game_title,
                                            platform=platform.slug,
                                            detail=outcome.reason))
                    self.notify(failed(game_title, str(outcome.reason)))
            if queue_item is not None:
                # Marked so the scheduled sweep never re-attempts it; the
                # Tasks page button clears failure marks to retry.
                queue_item.state = "imported" if all_ok else "import-failed"
            if all_ok and queue_item is not None:
                # If this was a partial collection grab (e.g. MiNERVA selective grab with qbit-tag:),
                # clean up the partial collection torrent and its files from the client so it does not
                # block subsequent grabs from the same collection (#115).
                shared_collection = bool(queue_item.selected_file) and sum(
                    bool(q.selected_file) and q.download_client == queue_item.download_client
                    and q.selected_file.get("hash") == queue_item.selected_file.get("hash")
                    for q in self.queue) > 1
                if (not shared_collection and not queue_item.import_intent
                        and not self.store.settings.get("retain_downloads_after_import", True)
                        and str(queue_item.download_id or "").startswith(("qbit-tag:", "mdm-tag:", "mdm-remote:"))):
                    for client in self.clients:
                        client_id = str(getattr(client, "romarr_id", "")
                                        or getattr(client, "config_id", ""))
                        if source_client is not client and (
                                not queue_item.download_client
                                or client_id != str(queue_item.download_client)):
                            continue
                        remover = getattr(client, "remove_download", None)
                        if callable(remover):
                            try:
                                remover(queue_item.download_id, delete_files=True)
                            except Exception as err:
                                log.warning("could not clean up collection torrent %r: %s",
                                            queue_item.download_id, err)
            if all_ok:
                if self.store.settings.get("rescan_after_import", True):
                    self._rescan_library(target_lib, target_cfg, platform.slug,
                                         platform_folders=target_folders)
                # The absolute destinations this release was imported to.
                # Cartridge streams them back to the player; external
                # platforms poll the assets endpoint.
                if queue_item is not None:
                    imported_paths = [str(o.destination) for o in outcomes if o.ok]
                    if imported_paths:
                        queue_item.imported_paths = imported_paths
                        if queue_item.external_request_id and fulfils_base:
                            self.store.update_seerr_request(
                                queue_item.external_request_id,
                                assets=imported_paths,
                                status="available")
                for w in (list(self.store.wanted) if fulfils_base else []):
                    if w.platform != platform.slug:
                        continue
                    wanted_key = _title_key(w.game)
                    if not wanted_key:
                        continue
                    # A wanted title may not substring-match the release it
                    # arrived under (punctuation, region tags, or a short
                    # IGDB/GG title versus the full no-intro name). What the
                    # bytes *are* is known by hash verification, so match the
                    # wanted title against the verified identities as well as
                    # the client-reported download name.
                    if any(_titles_are_same(w.game, identity)
                           for identity in _fulfil_identities(name, outcomes)):
                        self.store.fulfil(w.game, w.platform)
                # Close the aliasing gap on this platform: drop wanted titles
                # that are already present and verified in the library but were
                # never fulfilled (word-order/alias titles, pre-existing stale
                # entries). Gated by setting so the hash walk can be disabled.
                if fulfils_base and self.store.settings.get("reconcile_after_import", True):
                    try:
                        self.reconcile_platform(platform.slug)
                    except Exception:  # noqa: BLE001
                        log.warning("reconcile after import failed for %s",
                                    platform.slug, exc_info=True)
            results.append({"name": name, "ok": all_ok,
                            "reason": "" if all_ok else next((str(o.reason) for o in outcomes if not o.ok), ""),
                            "library": label, "transfers": [{"method": o.import_method, "fallback_reason": o.fallback_reason} for o in outcomes if o.ok]})
        # The sweep marks rows in place; without this the marks live only in
        # memory and a restart re-imports everything it had already done.
        self.store.save()
        return results


# -- HTTP ------------------------------------------------------------------

def integration_view(row: SeerrRequest) -> dict:
    """One integration request as the external platform reads it."""
    return {"request_id": row.external_request_id, "name": row.name or row.game,
            "platform": row.platform, "status": row.status,
            "assets": list(row.assets), "error": row.error}


def is_portal_asset(path: str) -> bool:
    """Whether `path` is a static asset or manifest requested by the embedded portal shell."""
    if path.startswith(("/assets/", "/_next/", "/static/", "/emulatorjs/")):
        return True
    if path in ("/site.webmanifest", "/manifest.webmanifest", "/manifest.json",
                "/apple-touch-icon.png", "/safari-pinned-tab.svg",
                "/favicon-32x32.png", "/favicon-16x16.png",
                "/android-chrome-192x192.png", "/android-chrome-512x512.png",
                "/sw.js"):
        return True
    if path.startswith("/workbox-"):
        return True
    return False


def is_portal_subpath(path: str) -> bool:
    """Whether `path` is a static asset or library-API subresource requested by the embedded portal shell."""
    if is_portal_asset(path):
        return True
    if path.startswith(("/api/config", "/api/users", "/api/heartbeat",
                        "/api/roms", "/api/collections", "/api/firmware",
                        "/api/devices", "/api/token", "/api/oauth",
                        "/api/stats", "/api/tasks", "/api/playlists",
                        "/api/saves", "/api/states", "/api/screenshots",
                        "/api/platforms/", "/api/platform", "/api/activity",
                        "/api/client_tokens", "/api/play_sessions",
                        "/api/recommendations", "/api/music", "/api/sync",
                        "/api/feeds", "/api/logs", "/api/export",
                        "/api/netplay", "/api/permissions", "/api/memory_cards",
                        "/api/streaming", "/api/login", "/api/logout",
                        "/ws/", "/socket.io/", "/netplay")):
        return True
    if (path.startswith(("/rom/", "/platform/", "/console/"))
            or path in ("/collections", "/stats", "/activity")):
        return True
    return False


def make_handler(service: ROMarr):
    """Build the HTTP request handler class bound to `service`."""
    class Handler(BaseHTTPRequestHandler):
        """HTTP handler serving the ROMarr UI and REST API endpoints."""
        server_version = "ROMarr"
        _is_portal_subpath = staticmethod(is_portal_subpath)

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Navigation can cancel an in-flight fetch before its reply.
                self.close_connection = True

        def _json(self, code: int, payload):
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _install_hub_source(self, url: str):
            try:
                got = check_source(url, service.store.settings.get("plugin_hosts") or None)
            except ValueError:
                return self._json(400, {
                    "ok": False, "error": "That repository URL is not valid. Check the address and try again."})
            if not got.ok:
                return self._json(400, {"ok": False, "error": got.reason})
            parsed = urlparse(got.url)
            if parsed.username or parsed.password:
                return self._json(400, {
                    "ok": False,
                    "error": "Use a repository URL without a username or password."})
            result = dict(hub.install(got.url))
            if not result.get("ok"):
                result["error"] = (result.get("error") or result.get("err")
                                   or result.get("reason")
                                   or "The plugin could not be installed. Check ROM Hub availability and try again.")
            return self._json(200 if result.get("ok") else 500, result)

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
                log.exception("%s %s failed", self.command,
                              redact_query_credentials(self.path))
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
                      # The OIDC round trip: /oidc/login starts it (no session
                      # yet — that is the point), and /oidc/callback arrives as
                      # a cross-site redirect from the IdP, which the
                      # SameSite=Strict session cookie deliberately does not
                      # ride. Its own credentials are the state/nonce/PKCE
                      # cookies (SameSite=Lax, short-lived) and the code the
                      # IdP issued; see oidc.py.
                      "/oidc/login", "/oidc/callback",
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

        def _oidc_error(self, message: str, code: int = 400):
            """A login failure the person can read, with a way back.

            Not JSON: this arrives in a browser mid-redirect, and a bare 400
            body is a dead end. The reason is escaped — it can carry values
            echoed by the IdP.
            """
            from html import escape
            body = ("<!doctype html><meta charset='utf-8'><title>Sign-in "
                    "failed</title><body style='font:15px/1.5 system-ui;"
                    "max-width:34em;margin:4em auto;padding:0 1em'>"
                    "<h2>Sign-in did not complete</h2>"
                    f"<p>{escape(str(message))}</p>"
                    "<p><a href='/login'>Back to the sign-in page</a></p>")
            return self._send(code, body.encode("utf-8"),
                              "text/html; charset=utf-8")

        def _oidc_callback(self, query: dict):
            """Finish the OIDC round trip: verify, then issue a session.

            Every value the IdP returned is untrusted until checked. The
            state must match the cookie this browser got at /oidc/login, the
            PKCE verifier proves the code was issued to THIS client, and the
            nonce inside the id_token binds it to this login. oidc.authenticate
            raises OidcError — with a reason fit for the person waiting — on
            any failure; nothing here trusts anything it did not check.
            """
            from .oidc import (COOKIE_NONCE, COOKIE_STATE, COOKIE_VERIFIER,
                               OidcError)
            if service.oidc is None:
                return self._oidc_error("OIDC sign-in is not configured")
            # The IdP sends ?error=... when the person cancelled or it refused.
            error = (query.get("error") or [""])[0]
            if error:
                detail = (query.get("error_description") or [""])[0]
                return self._oidc_error(
                    f"The sign-in provider refused the request "
                    f"({error})" + (f": {detail}" if detail else ""))
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            verifier = cookies.get(COOKIE_VERIFIER, "")
            expected_state = cookies.get(COOKIE_STATE, "")
            nonce = cookies.get(COOKIE_NONCE, "")
            if not verifier or not expected_state:
                return self._oidc_error(
                    "The sign-in session expired or the cookies were lost. "
                    "Start the sign-in again — a bookmarked callback URL will "
                    "not work.")
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code:
                return self._oidc_error("The provider returned no code")
            host = self.headers.get("X-Forwarded-Host") \
                or self.headers.get("Host") or ""
            proto = self.headers.get("X-Forwarded-Proto") or ""
            redirect_uri = service.oidc_redirect_uri(host, proto)
            try:
                identity = service.oidc.authenticate(
                    code, redirect_uri, verifier, state, expected_state, nonce)
            except OidcError as exc:
                log.warning("oidc login refused: %s", exc)
                return self._oidc_error(str(exc), code=403)
            except Exception as exc:              # noqa: BLE001
                log.warning("oidc login failed: %s: %s",
                            exc.__class__.__name__, exc)
                return self._oidc_error(
                    f"The sign-in could not be completed "
                    f"({exc.__class__.__name__})")
            # In. The session is the same signed cookie a password login gets;
            # who the person is rides inside it, so every later request knows
            # the account without asking the IdP again.
            token = service.auth.issue_session(subject=identity.user)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={service.auth.session_seconds}")
            # Clear the flow cookies: they are single-use and a stale verifier
            # left in the jar would only ever confuse the next login.
            for name in (COOKIE_VERIFIER, COOKIE_STATE, COOKIE_NONCE):
                self.send_header("Set-Cookie",
                                 f"{name}=; HttpOnly; SameSite=Lax; Path=/; "
                                 "Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            log.info("oidc sign-in: %s", identity.user)
            return None

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
            # Static portal assets, image relays, and library portal traffic are
            # exempt so cover grids and emulator cores never trip HTTP 429.
            if not (is_portal_asset(path)
                    or path.startswith(("/api/v1/asset", "/api/v1/portal/proxy"))
                    or is_portal_subpath(path)):
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
            if (path in self.OPEN_PATHS
                    or path in ("/favicon.ico", "/favicon.svg")
                    or is_portal_asset(path)
                    or self._authorised()):
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
            """Serve UI HTML pages and JSON API GET endpoints."""
            route = urlparse(self.path)
            query = parse_qs(route.query)
            if route.path in ("/favicon.ico", "/favicon.svg"):
                return self._send(200, ui_favicon_svg().encode("utf-8"),
                                  "image/svg+xml")
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
                                      totp=service.auth.totp.enabled,
                                      oidc=service.oidc_status())
                        .encode("utf-8"),
                        "text/html; charset=utf-8")
                if route.path == "/login":
                    if (self.headers.get("Sec-Fetch-Dest") == "iframe"
                            or "portal" in self.headers.get("Referer", "")
                            or "proxy" in self.headers.get("Referer", "")):
                        return self._serve_portal_proxy(
                            target="", sub_path="/", method="GET")
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
            if route.path == "/api/v1/queue/import-plan":
                return self._json(200, service.queue_import_plan(int((query.get("index") or ["-1"])[0]), {}))
            if route.path == "/api/v1/queue":
                return self._json(200, {"items": service.queue_rows()})
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
                # redact_indexer, not the downloaders redact: its secret set
                # covers passkey (torrentrss), which downloaders does not
                # declare and would have shipped to the browser in the clear.
                items = [redact_indexer(i)
                         for i in service.store.list_items("indexers")]
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
            if route.path == "/api/v1/emulators":
                try:
                    result = EmulatorHub(service.store.path.parent).catalog(
                        (query.get("q") or [""])[0], int((query.get("offset") or ["0"])[0]),
                        int((query.get("limit") or ["60"])[0]))
                    return self._json(200, result)
                except (ValueError, OSError):
                    return self._json(400, {"error": "Could not load this catalog page"})
            if route.path == "/api/v1/emulators/resources":
                try:
                    return self._json(200, resource_catalog(
                        (query.get("q") or [""])[0], int((query.get("offset") or ["0"])[0]),
                        int((query.get("limit") or ["60"])[0])))
                except (ValueError, OSError):
                    return self._json(400, {"error": "Could not load this resource page"})
            if route.path == "/api/v1/emulators/forks":
                try:
                    result = EmulatorHub(service.store.path.parent).inventory.forks(
                        (query.get("id") or [""])[0], int((query.get("page") or ["1"])[0]))
                    return self._json(200, result)
                except (ValueError, OSError, requests.RequestException):
                    return self._json(400, {"error": "Could not load this fork page. Open the source repository to browse forks."})
            if route.path in ("/api/v1/emulators/cores", "/api/v1/emulators/cores/readiness"):
                try:
                    core_hub = CoreHub(service.store.path.parent)
                    if route.path.endswith("/readiness"):
                        result = core_hub.readiness((query.get("id") or [""])[0])
                    else:
                        result = core_hub.catalog((query.get("q") or [""])[0],
                            int((query.get("offset") or ["0"])[0]), int((query.get("limit") or ["60"])[0]))
                    return self._json(200, result)
                except (ValueError, OSError):
                    return self._json(400, {"error": "Could not load this core"})
            if route.path == "/api/v1/emulators/release":
                try:
                    result = EmulatorHub(service.store.path.parent).release(
                        (query.get("id") or [""])[0])
                    return self._json(200, result)
                except (ValueError, requests.RequestException):
                    return self._json(400, {"error": "Could not check this upstream release. Use its downloads page."})
            if route.path == "/api/v1/downloadclient/mdm":
                return self._json(200, service.mdm_capability())
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
                            {"name": f, "label": FIELD_LABELS.get(f, f.replace("_", " ").title()),
                             "type": ("secret" if f in ("url", "token",
                                                        "password", "key")
                                      else "text"),
                             "default": "",
                             "help": (spec["help"] if f == "url" else
                                      FIELD_HELP.get(f, ""))}
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
            if route.path == "/api/v1/update":
                return self._json(200, service.update_status())
            if route.path == "/api/v1/asset":
                # The mixed-content relay for library images (issue #49).
                # Auth is the session gate every route here already passed --
                # an <img> tag carries the cookie. The origin check inside
                # fetch_asset is what keeps this from being an open proxy.
                target = (query.get("url") or [""])[0]
                code, ctype, body = service.fetch_asset(target)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if code == 200:
                    # Covers repeat on every shelf render; a day of browser
                    # cache is the difference between a poster grid that
                    # loads and one that hammers the library server.
                    self.send_header("Cache-Control",
                                     "private, max-age=86400")
                self.end_headers()
                return self.wfile.write(body)
            if route.path == "/api/v1/portal/proxy":
                return self._serve_portal_proxy(
                    target=(query.get("url") or [""])[0],
                    sub_path=(query.get("path") or [""])[0],
                    method="GET",
                )
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
                    translation_langs=[
                        c for c in query.get("translation_langs", [""])[0]
                        .split(",") if c] or None,
                    region_mode=query.get("region_mode", [""])[0] or None,
                    exclude=[e for e in query.get("exclude", [""])[0].split(",")
                             if e] if "exclude" in query else None))
            if route.path == "/api/v1/collection":
                return self._json(200, service.collection_status())
            if route.path == "/api/v1/collection/source":
                return self._json(200, service.collection_source_torrents())
            if route.path == "/api/v1/openapi.json":
                return self._json(200, openapi_spec(VERSION))
            if route.path == "/api/v1/metadataprovider":
                from .metadata import PROVIDERS as _MP
                secrets = {"api_key", "token", "client_id"}
                with service._metadata_lock:
                    providers = service.store.list_items("metadata_providers")
                    revision = service.metadata_revision
                items = []
                for cfg in providers:
                    items.append({k: ("********" if k in secrets and v else v)
                                  for k, v in cfg.items()})
                return self._json(200, {"items": items,
                                        "revision": revision})
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
            if route.path == "/api/v1/setup/status":
                return self._json(200, service.setup_status())
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
            if route.path == "/api/v1/audit/quarantine":
                return self._json(200, service.duplicate_quarantine_state())
            if route.path == "/api/v1/audit/renames":
                return self._json(200, service.library_rename_state())
            if route.path == "/api/v1/stats":
                return self._json(200, service.stats())
            if route.path == "/api/v1/integration/info":
                return self._json(200, {
                    "name": "Cartridge (ROMarr)",
                    "version": VERSION,
                    "description": "ROMarr is a complete Cartridge implementation for game libraries",
                    "supports_resume": True,
                    "supports_multi_platform": True,
                })
            if route.path == "/api/v1/integration/requests":
                rows = (service.store.get_seerr_request(row["id"])
                        for row in list(service.store.settings.get("integration_requests") or []))
                return self._json(200, {"requests": [
                    integration_view(row) for row in rows if row is not None]})
            if route.path == "/api/v1/integration/requests/current":
                current = service.store.latest_seerr_request()
                if current is None:
                    return self._json(404, {"error": "no tracked requests"})
                return self._json(200, integration_view(current))
            if route.path == "/api/v1/game/roulette":
                raw = (query.get("status") or ["shelved"])[0]
                try:
                    pick = service.store.roulette(
                        raw.split(","), (query.get("platform") or [""])[0])
                except ValueError as err:
                    return self._json(400, {"error": str(err)})
                if pick is None:
                    return self._json(404, {"error": f"nothing on the shelf as {raw}"})
                return self._json(200, pick)
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
            if route.path == "/oidc/login":
                # Step one: bounce the browser to the IdP. The three flow
                # values (PKCE verifier, state, nonce) go into short-lived
                # http-only cookies rather than server state, because the
                # callback arrives as a cross-site redirect that the
                # SameSite=Strict session cookie does not ride -- these are
                # Lax so they do. Generated here, never read from the caller.
                if service.oidc is None:
                    return self._oidc_error("OIDC sign-in is not configured")
                from .oidc import (COOKIE_NONCE, COOKIE_STATE, COOKIE_TTL,
                                   COOKIE_VERIFIER, generate_pkce, new_random)
                host = self.headers.get("X-Forwarded-Host") \
                    or self.headers.get("Host") or ""
                proto = self.headers.get("X-Forwarded-Proto") or ""
                redirect_uri = service.oidc_redirect_uri(host, proto)
                verifier, challenge = generate_pkce()
                state, nonce = new_random(), new_random()
                try:
                    location = service.oidc.login_url(
                        redirect_uri, state, nonce, challenge)
                except Exception as exc:          # noqa: BLE001
                    return self._oidc_error(str(exc))
                self.send_response(303)
                self.send_header("Location", location)
                for name, value in ((COOKIE_VERIFIER, verifier),
                                    (COOKIE_STATE, state),
                                    (COOKIE_NONCE, nonce)):
                    self.send_header(
                        "Set-Cookie",
                        f"{name}={value}; HttpOnly; SameSite=Lax; Path=/; "
                        f"Max-Age={COOKIE_TTL}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if route.path == "/oidc/callback":
                # Step two: the IdP sent the browser back with a code.
                return self._oidc_callback(query)
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
            if route.path == "/api/v1/hub/igir":
                return self._json(200, hub.igir_status())
            if route.path == "/api/v1/hub/exo2romm":
                return self._json(200, hub.exo2romm_status())
            if route.path == "/api/v1/hub/fleet":
                return self._json(200, hub.fleet_profiles())
            if route.path == "/api/v1/mcp":
                return self._json(200, hub.mcp_manifest())
            if route.path == "/api/v1/moonlight":
                return self._json(200, service.moonlight_status())
            if route.path == "/api/v1/hub/status":
                # Whether plugins are confined is the most useful thing to
                # know before installing one, so it travels with the
                # availability answer instead of hiding in a startup log.
                confined, why = hub.sandbox_state()
                searchable = hub.searchable_plugins()
                lane_ready = service._hub_enabled()
                return self._json(200, {"available": hub.available(),
                                        "sandboxed": confined,
                                        "sandbox_detail": why,
                                        "archive_org_auth": hub.archive_org_auth_status(),
                                        "searchable_plugins": searchable,
                                        # The Plugins page says out loud what
                                        # enabling a plugin now does: its
                                        # results join Interactive Search,
                                        # provided the ROM Hub download client
                                        # is configured to stage the fetches.
                                        "acquisition": ("ready" if lane_ready
                                                        else ("no ROM Hub download client configured"
                                                              if searchable else
                                                              "no enabled plugin declares search"))})
            if route.path == "/api/search":
                game = (query.get("game") or [""])[0]
                if not game:
                    return self._json(400, {"error": "game is required"})
                return self._json(200, service.search(game, (query.get("platform") or [""])[0]))
            if is_portal_subpath(route.path):
                return self._serve_portal_proxy(
                    target="", sub_path=self.path, method="GET")
            return self._json(404, {"error": "not found"})

        def _serve_portal_proxy(self, target: str, sub_path: str,
                                method: str = "GET", body: bytes | None = None):
            res = service.proxy_portal(
                target,
                sub_path,
                method=method,
                body=body,
                content_type=self.headers.get("Content-Type", ""),
                accept=self.headers.get("Accept", ""),
                range_header=self.headers.get("Range", ""),
            )
            code, ctype, raw = res[0], res[1], res[2]
            resp_headers = getattr(res, "headers", {})
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for hdr in ("Content-Range", "Accept-Ranges", "ETag", "Last-Modified",
                        "Content-Disposition", "Access-Control-Allow-Origin"):
                if hdr in resp_headers:
                    self.send_header(hdr, resp_headers[hdr])
            if code == 200 and (sub_path.startswith("/assets/") or "/loader.js" in sub_path):
                self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            return self.wfile.write(raw)

        def _post(self):
            """Dispatch POST requests, coordinating configuration writes and reloads."""
            route = urlparse(self.path)
            query = parse_qs(route.query)
            length = int(self.headers.get("Content-Length") or 0)
            if route.path == "/api/v1/portal/proxy":
                raw_body = self.rfile.read(min(length, 25 * 1024 * 1024)) if length > 0 else b""
                return self._serve_portal_proxy(
                    target=(query.get("url") or [""])[0],
                    sub_path=(query.get("path") or [""])[0],
                    method="POST",
                    body=raw_body,
                )
            if is_portal_subpath(route.path):
                raw_body = self.rfile.read(min(length, 25 * 1024 * 1024)) if length > 0 else b""
                return self._serve_portal_proxy(
                    target="", sub_path=self.path, method="POST", body=raw_body)
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
                with service._metadata_lock:
                    # Keep restore preferences serialized with update launch.
                    with service._lock:
                        service.store.settings.update(settings)
                        service.store.save()
                    service.reload_metadata()
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
                    translation_langs=body.get("translation_langs") or None,
                    region_mode=body.get("region_mode") or None,
                    exclude=body.get("exclude")
                    if body.get("exclude") is not None else None))
            if route.path == "/api/v1/collection/source/plan":
                return self._json(200, service._collection_source_plan(
                    str(body.get("client_id") or ""),
                    str(body.get("torrent_hash") or ""),
                    str(body.get("dat") or ""),
                    str(body.get("platform") or ""),
                    int(body.get("cap_bytes") or 0)))
            if route.path == "/api/v1/collection/source/start":
                return self._json(200, service.collection_source_start(
                    str(body.get("client_id") or ""),
                    str(body.get("torrent_hash") or ""),
                    str(body.get("dat") or ""),
                    str(body.get("platform") or ""),
                    int(body.get("cap_bytes") or 0)))
            if route.path == "/api/v1/collection/source/step":
                return self._json(200, service.collection_source_step(
                    str(body.get("id") or "")))
            if route.path == "/api/v1/collection/source/control":
                return self._json(200, service.collection_source_control(
                    str(body.get("id") or ""),
                    str(body.get("action") or "")))
            if route.path == "/api/v1/collection/step":
                return self._json(200, service.collection_step(
                    str(body.get("id") or "")))
            if route.path == "/api/v1/collection/control":
                return self._json(200, service.collection_control(
                    str(body.get("id") or ""), str(body.get("action") or "")))
            if route.path == "/api/v1/collection/reconcile":
                return self._json(200, service.reconcile_platform(
                    str(body.get("platform") or "")))
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
                if not isinstance(body, dict):
                    return self._json(400, {"error": "invalid provider configuration"})
                with service._metadata_lock:
                    cfg = dict(body)
                    old_cfg = None
                    if cfg.get("id"):
                        old_cfg = service.store.get_item("metadata_providers",
                                                         str(cfg["id"]))
                        if old_cfg is None:
                            return self._json(404, {"error": "metadata provider no longer exists"})
                    for key in secrets:
                        supplied = cfg.get(key, "")
                        if supplied == "********":
                            if not old_cfg or old_cfg.get(key, "") in ("", "********"):
                                return self._json(400, {
                                    "error": "masked credential has no saved value"})
                            cfg[key] = old_cfg[key]
                        elif supplied == "" and old_cfg:
                            # The editor omits/clears redacted secrets when
                            # editing a different field: preserve saved values.
                            cfg[key] = old_cfg.get(key, "")
                    try:
                        saved = service.save_metadata_provider(cfg)
                    except (RecursionError, TypeError, ValueError, OverflowError):
                        return self._json(400, {
                            "error": "invalid metadata provider configuration"})
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
                # ensure_scheme: a Settings value typed without "https://"
                # still mints a link parse_invite_link will accept.
                from .federation import ensure_scheme
                configured = ensure_scheme(str(
                    service.store.settings.get("public_url") or ""))
                if configured:
                    service.federation.url = configured
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
            if route.path == "/api/v1/audit/quarantine/move":
                return self._json(200, service.quarantine_duplicates(
                    str(body.get("platform") or ""),
                    list(body.get("files") or [])))
            if route.path == "/api/v1/audit/renames/preview":
                return self._json(200, service.preview_library_renames(
                    str(body.get("platform") or ""),
                    scope=str(body.get("scope") or ""),
                    offset=max(0, int(body.get("offset") or 0))))
            if route.path == "/api/v1/audit/renames/apply":
                return self._json(200, service.apply_library_renames(
                    str(body.get("platform") or ""),
                    list(body.get("files") or [])))
            if route.path == "/api/v1/audit/renames/rollback":
                return self._json(200, service.rollback_library_renames(
                    str(body.get("manifest_id") or "")))
            if route.path == "/api/v1/audit/restore":
                return self._json(200, service.restore_duplicate(
                    str(body.get("manifest_id") or ""),
                    str(body.get("entry_id") or "")))
            if route.path == "/api/v1/audit":
                return self._json(200, service.audit_library(
                    str(body.get("platform") or "")))
            if route.path == "/api/v1/queue/import-plan":
                return self._json(200, service.queue_import_plan(int(body.get("index", -1)), body))
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
            if route.path == "/api/v1/setup/complete":
                return self._json(200, service.finish_setup_guide(
                    str(body.get("mode") or "organize")))
            if route.path == "/api/v1/setup/dats":
                result = service.setup_dats(str(body.get("directory") or ""))
                return self._json(200 if result.get("ok") else 400, result)
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
            if route.path == "/api/v1/connection/test" and body.get("type"):
                # The editor's Test button posts the form it is showing, which
                # may not be saved yet. Answer for that connection -- not for
                # whatever happens to be stored -- or a brand-new webhook can
                # never be tested before it is saved.
                spec = NOTIFIERS.get(str(body.get("type")).lower())
                if spec is None:
                    return self._json(200, {"ok": False,
                                            "message": "unknown connection type"})
                cfg = dict(body)
                if cfg.get("id"):
                    old_cfg = service.store.get_item("connections",
                                                     str(cfg["id"])) or {}
                    for key in ("url", "token", "password", "key"):
                        if cfg.get(key, "") in ("********", ""):
                            cfg[key] = old_cfg.get(key, "")
                ok, detail = send_test(cfg)
                return self._json(200, {"ok": ok, "message": detail})
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
            if route.path == "/api/v1/hub/source/install":
                return self._install_hub_source(str(body.get("url") or ""))
            if route.path == "/api/v1/hub/source/check":
                got = check_source(
                    str(body.get("url") or ""),
                    service.store.settings.get("plugin_hosts") or None)
                return self._json(200 if got.ok else 400, {
                    "ok": got.ok, "url": got.url, "host": got.host,
                    "reason": got.reason,
                })
            if route.path == "/api/v1/hub/archive-org/auth":
                payload = body if isinstance(body, dict) else {}
                result = hub.archive_org_auth_configure(
                    email=payload.get("email", ""), password=payload.get("password", ""),
                    clear=payload.get("clear", False))
                return self._json(200 if result.get("ok") else 400, result)
            if route.path == "/api/v1/hub/plugin":
                slug = (body.get("slug") or "").strip()
                action = (body.get("action") or "").strip()
                if not slug or action not in ("install", "enable", "disable", "uninstall"):
                    return self._json(400, {"error": "slug and a valid action are required"})
                if action == "install" and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                    return self._install_hub_source(slug)
                fn = {"install": hub.install, "enable": hub.enable,
                      "disable": hub.disable, "uninstall": hub.uninstall}[action]
                result = fn(slug)
                return self._json(200 if result.get("ok") else 500, result)
            if route.path == "/api/v1/hub/igir":
                res = hub.run_igir(body if isinstance(body, dict) else {})
                return self._json(200 if res.get("ok") else 400, res)
            if route.path == "/api/v1/hub/exo2romm":
                res = hub.run_exo2romm(body if isinstance(body, dict) else {})
                return self._json(200 if res.get("ok") else 400, res)
            if route.path == "/api/v1/hub/fleet":
                res = hub.build_fleet_sync_plan(body if isinstance(body, dict) else {})
                return self._json(200 if res.get("ok") else 400, res)
            if route.path == "/api/request":
                game = (body.get("game") or "").strip()
                platform = (body.get("platform") or "").strip()
                if not game or not platform:
                    return self._json(400, {"error": "game and platform are required"})
                return self._json(200, service.request(game, platform))
            # Integration API: the request asset API external platforms poll.
            # POST: accept a request from an external game platform and start
            # the same search + grab pipeline the native button uses.
            # The response carries a stable request id the caller tracks.
            # GET: poll status -- requested, searching, download, imported,
            # available. Assets list the imported file paths for the player.
            if route.path == "/api/v1/integration/requests":
                game = (body.get("name") or "").strip()
                platform = (body.get("platform") or "").strip()
                if not game or not platform:
                    return self._json(400, {"error": "name and platform are required"})
                # The peer's own id, when it sends one, makes a retried POST
                # the same request rather than a second download.
                ext_id = (str(body.get("externalRequestId") or "").strip()
                          or uuid.uuid4().hex[:16])
                if len(ext_id) > 64:
                    # Truncating would fold two ids into one request.
                    return self._json(400, {"error": "externalRequestId is longer than 64 characters"})
                # The claim is atomic: two concurrent POSTs with one id must
                # not both reach a download client.
                if service.store.claim_seerr_request(SeerrRequest(
                        ext_id, platform, game=game, name=game, status="searching")):
                    result = service.request(game, platform,
                                             external_request_id=ext_id)
                    row = service.store.get_seerr_request(ext_id)
                    if not result.get("ok") and row.status == "searching":
                        # Some failures (an unknown platform) return before
                        # the pipeline touches the row; none may leave it
                        # "searching" forever.
                        service.store.update_seerr_request(
                            ext_id, status="failed",
                            error=str(result.get("error") or ""),
                            only_if_not_cancelled=True)
                return self._json(202, integration_view(
                    service.store.get_seerr_request(ext_id)))
            if route.path == "/api/v1/release/grab":
                release_id = (body.get("id") or "").strip()
                if not release_id:
                    return self._json(400, {"error": "id is required"})
                platform_name = str(body.get("platform") or "").strip()
                return self._json(
                    200, service.grab_candidate(release_id, platform_name))
            if route.path == "/api/import":
                return self._json(200, {"imported": service.import_finished()})
            if route.path in ("/api/v1/downloadclient", "/api/v1/indexer"):
                is_indexer = "indexer" in route.path
                key = "indexers" if is_indexer else "download_clients"
                with service._metadata_lock:
                    existing = service.store.get_item(key, body.get("id")) if body.get("id") else None
                    # Each family merges against its OWN type specs: the
                    # downloaders merge looks secrets up in CLIENT_TYPES, which
                    # has no entry for "prowlarr" -- it silently merged nothing
                    # and stored the form's `********` placeholder as the real
                    # API key (#57). Same split for redaction on the way out.
                    merged = (merge_indexer_secrets(dict(body), existing)
                              if is_indexer
                              else merge_secrets(dict(body), existing))
                    saved = service.store.put_item(key, merged)
                    service.reload_clients()
                return self._json(200, redact_indexer(saved) if is_indexer
                                  else redact(saved))
            if route.path == "/api/v1/library":
                with service._metadata_lock:
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
            if route.path in ("/api/v1/emulators/cores/install", "/api/v1/emulators/cores/configure"):
                try:
                    core_hub = CoreHub(service.store.path.parent)
                    result = (core_hub.configure() if route.path.endswith("/configure") else
                              core_hub.install(str(body.get("id") or ""), scanner=service.scan_installation))
                    return self._json(200, result)
                except (ValueError, OSError, requests.RequestException, scanners.ScanUnavailable) as err:
                    log.warning("Core installation failed: %s", type(err).__name__)
                    return self._json(400, {"error": str(err) if isinstance(err, (ValueError, scanners.ScanUnavailable))
                                           else "Could not install this core"})
            if route.path == "/api/v1/emulators/install":
                try:
                    emulator_hub = EmulatorHub(service.store.path.parent)
                    result = emulator_hub.install(str(body.get("id") or ""),
                                         str(body.get("asset") or ""),
                                         str(body.get("sha256") or ""),
                                         scanner=service.scan_installation)
                    return self._json(200, result)
                except (ValueError, OSError, requests.RequestException,
                        scanners.ScanUnavailable) as err:
                    log.warning("Emulator installation failed: %s", type(err).__name__)
                    return self._json(400, {"error": str(err) if isinstance(err, (ValueError, scanners.ScanUnavailable)) else "Emulator installation failed"})
            if route.path == "/api/v1/security/inspect":
                try:
                    return self._json(200, service.security_inspect(
                        str(body.get("path") or ""), str(body.get("engine") or "")))
                except (ValueError, OSError, scanners.ScanUnavailable) as err:
                    return self._json(400, {"error": str(err)})
            if route.path == "/api/v1/downloadclient/mdm/install":
                try:
                    return self._json(200, service.mdm_install(body.get("version", "")))
                except (mdm.MdmError, OSError, ValueError) as err:
                    return self._json(400, {"ok": False, "error": str(err)})
            if route.path == "/api/v1/indexer/test":
                # merge_indexer_secrets: a Test pressed on a saved indexer
                # whose form still shows `********` must test the STORED key,
                # not eight asterisks (#57: "Rejected the API key" after save).
                existing = service.store.get_item("indexers", body.get("id"))                     if body.get("id") else None
                return self._json(200, service.test_indexer(
                    merge_indexer_secrets(dict(body), existing)))
            if route.path in ("/api/v1/webhook", "/api/v1/webhook/ggrequestz"):
                result = service.handle_request_webhook(body)
                # GG Requestz treats every 2xx response as delivered and does
                # not inspect the JSON body. Returning 200 with {ok:false}
                # therefore made an unmapped platform disappear silently.
                # 202 is truthful for the asynchronous happy path; 422 makes
                # the sender log a rejected event and gives the operator a
                # reason in our response and logs.
                return self._json(202 if result.get("ok") else 422, result)
            if route.path == "/api/v1/command":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json(400, {"error": "name is required"})
                # The Decompress command takes options in the body: which
                # directory, whether to delete originals after verification,
                # and whether this is a dry run. Every other command keeps its
                # no-argument shape.
                if name == "Decompress":
                    return self._json(200, service.decompress_batch(
                        str(body.get("directory") or ""),
                        delete_originals=bool(body.get("delete_originals")),
                        dry_run=bool(body.get("dry_run"))))
                return self._json(200, service.run_command(name))
            return self._json(404, {"error": "not found"})

        def _delete(self):
            """Dispatch DELETE requests and serialize backend removal with restore."""
            route = urlparse(self.path)
            query_del = parse_qs(route.query)
            if route.path == "/api/v1/portal/proxy":
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(min(length, 25 * 1024 * 1024)) if length > 0 else b""
                return self._serve_portal_proxy(
                    target=(query_del.get("url") or [""])[0],
                    sub_path=(query_del.get("path") or [""])[0],
                    method="DELETE",
                    body=raw_body,
                )
            if is_portal_subpath(route.path):
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(min(length, 25 * 1024 * 1024)) if length > 0 else b""
                return self._serve_portal_proxy(
                    target="", sub_path=self.path, method="DELETE", body=raw_body)
            if route.path == "/api/v1/wanted/missing":
                # Dropping a request, which Wanted could not do: the list was
                # only ever emptied by an import, so a typo went on costing an
                # indexer search on every missing sweep and every RSS pass for
                # a game that does not exist.
                #
                # Game and platform may arrive as query parameters or as a
                # JSON body; a DELETE carrying one is unusual enough that
                # refusing it would just be a papercut.
                query = parse_qs(route.query, keep_blank_values=True)
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, TypeError):
                        body = {}
                if not isinstance(body, dict):
                    body = {}
                game = str(body.get("game")
                           or (query.get("game") or [""])[0]).strip()
                has_platform = "platform" in body or "platform" in query
                platform = str(
                    body.get("platform") if "platform" in body
                    else (query.get("platform") or [""])[0]
                ).strip()
                if not game or not has_platform:
                    return self._json(400, {
                        "error": "game and platform are both required"})
                removed = service.drop_request(game, platform)
                return self._json(200 if removed else 404, {"deleted": removed})
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
                with service._metadata_lock:
                    removed = service.store.delete_item("libraries", item_id)
                    service.reload_libraries()
                return self._json(200 if removed else 404, {"deleted": removed})
            for prefix, key in (("/api/v1/downloadclient/", "download_clients"),
                                ("/api/v1/indexer/", "indexers")):
                if route.path.startswith(prefix):
                    item_id = route.path[len(prefix):]
                    with service._metadata_lock:
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
                removed = service.delete_metadata_provider(item_id)
                return self._json(200 if removed else 404, {"deleted": removed})
            return self._json(404, {"error": "not found"})

        def _put(self):
            route = urlparse(self.path)
            query_put = parse_qs(route.query)
            length = int(self.headers.get("Content-Length") or 0)
            if route.path == "/api/v1/portal/proxy":
                raw_body = self.rfile.read(min(length, 25 * 1024 * 1024)) if length > 0 else b""
                return self._serve_portal_proxy(
                    target=(query_put.get("url") or [""])[0],
                    sub_path=(query_put.get("path") or [""])[0],
                    method="PUT",
                    body=raw_body,
                )
            if is_portal_subpath(route.path):
                raw_body = self.rfile.read(min(length, 25 * 1024 * 1024)) if length > 0 else b""
                return self._serve_portal_proxy(
                    target="", sub_path=self.path, method="PUT", body=raw_body)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})

            if route.path == "/api/v1/config":
                # A blank or masked secret means "unchanged", not "delete it":
                # the settings form never sees the real value (safe_settings
                # removes it), so echoing an empty field back must not wipe a
                # stored secret.
                secret = body.get("_oidc_client_secret")
                if "_oidc_client_secret" in body and secret in ("", "********"):
                    body.pop("_oidc_client_secret")
                # Serialize public settings writes with the updater launch.
                try:
                    with service._lock:
                        updated = service.store.update_settings(body)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                if any(str(k).startswith("oidc") or k == "_oidc_client_secret"
                       for k in body):
                    # An SSO change takes effect at the next login attempt,
                    # not the next restart.
                    service.reload_oidc()
                if "dat_path" in body:
                    # A stored path nothing re-reads is a setting that lies;
                    # reload_dats both stores and applies it.
                    service.reload_dats(str(body.get("dat_path") or ""))
                service.reload_policy()
                updated = service.safe_settings()
                # A library path is only really changed once the running
                # service files ROMs there, not once it is stored.
                path = updated.get("library_path")
                if path:
                    service.library = Path(path)
                return self._json(200, updated)
            return self._json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            # BaseHTTPRequestHandler logs the complete request target. The
            # GG Requestz credential has to live in that target because the
            # sender cannot attach an authentication header, so redact it
            # before it can reach either stdout or the in-app log ring.
            message = redact_query_credentials(fmt % args)
            log.info("%s %s", self.address_string(), message)

    return Handler


def serve(port: int = 6868, env: dict[str, str] | None = None):
    service = ROMarr(env)
    # Reconcile integration rows that were mid-dispatch when the server
    # died: if the external platform still wants them, reset to searching
    # and re-dispatch; if it doesn't, mark them cancelled.
    try:
        service.store.recover_seerr_dispatches()
    except Exception:
        log.exception("integration recovery skipped (non-fatal)")

    bind_env = env if env is not None else os.environ
    host = str(bind_env.get("ROMARR_HOST") or "0.0.0.0")
    ThreadingHTTPServer.request_queue_size = 256
    httpd = ThreadingHTTPServer((host, port), make_handler(service))

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

    log.info("ROMarr listening on %s://%s:%d, library=%s",
             scheme, host, port, service.library)
    httpd.serve_forever()
