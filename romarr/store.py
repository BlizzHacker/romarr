"""Persistence.

The *arr applications all keep the same things across a restart: what you
asked for, what happened to it, and how you configured the thing. ROMarr kept
all of it in memory, so a restart lost your history and your settings -- which
is the difference between a tool and a demo.

This is a JSON file with a lock rather than a database, and that is a
deliberate ceiling rather than an oversight: the entire working set is a few
thousand events, and an *arr that needs a database daemon to file ROMs is
harder to install than the thing it automates. If this ever outgrows a file,
the shape here (load / mutate / save under one lock) is the same shape SQLite
would want.

Writes are atomic -- written to a sibling temp file and renamed -- because the
alternative is that a crash mid-write leaves a truncated JSON file and the
service will not start again.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields as dataclass_fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StateUnreadable(RuntimeError):
    """The state file is there and we are not allowed to read it.

    Its own class rather than a bare RuntimeError so the entry point can tell
    this apart from a crash and print the fix instead of a traceback.
    """


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(raw: Any, cls: type) -> list:
    """Rebuild stored dicts into dataclasses, ignoring fields we do not know.

    `cls(**row)` raises TypeError on a key this version has never heard of,
    and that is a state file which cannot be loaded -- an install that will
    not boot because it was once run by a newer ROMarr. Dropping the unknown
    key loses the field and keeps the service.
    """
    known = {f.name for f in dataclass_fields(cls)}
    return [cls(**{k: v for k, v in row.items() if k in known})
            for row in (raw or []) if isinstance(row, dict)]


@dataclass
class Event:
    """One thing that happened, in the sense *arr means by History."""

    kind: str          # grabbed | imported | failed | ignored
    game: str
    platform: str
    release: str = ""
    detail: str = ""
    seeders: int = 0
    size: int = 0
    indexer: str = ""
    # Which library server received it. Empty for events that predate multiple
    # libraries, and for events that never reached one.
    library: str = ""
    at: str = field(default_factory=now_iso)


@dataclass
class WantedItem:
    """A request that has not been satisfied yet -- *arr's Wanted/Missing."""

    game: str
    platform: str
    added: str = field(default_factory=now_iso)
    attempts: int = 0
    last_error: str = ""
    # When the scheduler last searched for this automatically. Empty means
    # never, which makes a fresh request immediately eligible; together with
    # `attempts` it drives the re-search backoff, so a title that has failed
    # for months is retried weekly rather than hourly.
    searched_at: str = ""


@dataclass
class QueueItem:
    """One download ROMarr is waiting on -- what *arr calls Activity.

    This used to live only in the service object, so a restart emptied
    Activity while the download client carried happily on seeding. Losing the
    rows lost the only record of which *game* a finished torrent belongs to:
    the import sweep matches a completed download to its queue row by release
    title, so after a restart it could no longer tell that
    `Super.Metroid.USA.zip` was the SNES request from an hour ago, and skipped
    the file it had just finished downloading.
    """

    game: str
    platform: str
    release: str
    seeders: int
    state: str                # queued | grabbed | imported | failed
    detail: str = ""
    at: str = field(default_factory=now_iso)
    # Identity of the release this row took, as profiles.release_id computes
    # it -- the infohash when the link carried one. Stored so a dead download
    # can be blocklisted by the same identity a later search will compute,
    # without persisting the download URL, which carries the indexer API key.
    release_id: str = ""
    indexer: str = ""
    size: int = 0
    # Whether a failure is the *release's* fault. "qBittorrent rejected it"
    # and "the archive held no ROMs" are; "no download client configured" is
    # not, and blocklisting a release because the operator has not set up a
    # client yet would punish the release for somebody else's mistake.
    release_fault: bool = False
    # Set once this row's release has been blocklisted, so the sweep that
    # retires dead downloads never does it twice.
    blocklisted: bool = False
    # SeerrNG's request identity, when ROMarr was dispatched by SeerrNG.
    # Empty for internally-originated requests (UI, /api/request, frontends).
    external_request_id: str = ""
    # Absolute, import-verified destinations carried across a restart.
    # A request can be satisfied from any one row, so the list is not
    # necessarily 1:1 with game files; an import-failed row leaves it empty.
    imported_paths: list[str] = field(default_factory=list)


@dataclass
class SeerrRequest:
    """A durable, resumable integration request.

    SeerrNG (or any peer speaking the integration contract) submits a
    request by its own ``externalRequestId``. ROMarr persists the row,
    drives the normal search/dispatch pipeline, and reports status back
    through ``GET /api/v1/integration/requests/{id}``. The row outlives
    restarts, so a server that dies mid-handoff can recover and tell the
    peer "your download was already queued, here is the status" instead of
    silently dropping it.

    ``status`` is one of:
      ``searching``    -- accepted, dispatch pipeline running.
      ``dispatching``  -- queue row created; cancel is racy (see
                           ``mark_seerr_dispatching``).
      ``downloading``  -- the download client reports the release active.
      ``available``    -- imported and verified under the library root.
      ``failed``       -- the dispatch pipeline reported an error.
      ``cancelled``    -- the peer cancelled it; not retryable.

    ``error`` carries the last human-readable failure, if any. ``retry``
    is offered only from a ``failed`` state, with a single confirmation
    gate (see ``confirmNoExistingDownload``) when the error is the
    handoff-restart case. ``cancel`` is always offered while the row is
    still actionable (searching / dispatching / downloading).
    """

    external_request_id: str
    platform: str
    game: str = ""
    name: str = ""
    catalog_provider: str = ""
    catalog_id: int = 0
    platform_id: int = 0
    status: str = "accepted"
    error: str = ""
    assets: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


# Defaults are spelled out here rather than scattered through the UI so a fresh
# install and a configured one disagree about nothing.
DEFAULT_SETTINGS: dict[str, Any] = {
    # How a friend's server reaches this one, e.g. https://romarr.example.com.
    # Peering is the only feature that needs ROMarr to know its own address:
    # an invitation carries it, and without one the friend who redeems the
    # invitation has nowhere to call back to.
    "public_url": "",
    # Read ROM hashes from the library server so netplay can match on bytes.
    # Daily and at startup; off only if you never want the traffic.
    "hash_index": True,
    # Media management
    #
    # Empty means "nobody has chosen one", which is what a fresh install is.
    # It used to default to /mnt/roms, and because the service treats a stored
    # path as an operator's decision that outranks the environment, that
    # default silently outranked LIBRARY_PATH and ROMM_LIBRARY on every install
    # -- making both documented variables do nothing at all. It looked correct
    # only because the default matched the path the docs used as an example.
    "library_path": "",
    # Where No-Intro/Redump DATs live, as ROMarr sees it. Applied through
    # reload_dats when saved from the UI, so the setting acts rather than
    # just sits.
    "dat_path": "",
    # Directory shape ROMs are filed in, matching the library server's own
    # setting. "flat" is <root>/<platform>/<rom> (RomM "Structure A"); "nested"
    # is <root>/<platform>/roms/<rom> (RomM "Structure B"). Overridable per
    # library on the Libraries page; this is the default for the primary one.
    "library_layout": "flat",
    # What to do with English fan translations in a 1G1R set:
    # exclude | fill | prefer | keep_both. See collections.TRANSLATION_POLICIES.
    # keep_both files the translation in the platform's Translations subfolder.
    "translation_policy": "exclude",
    "rename_on_import": True,
    "overwrite_existing": False,
    # Profile: for ROMs the meaningful axis is region and revision, not
    # bitrate -- this is the games equivalent of a quality profile.
    "preferred_regions": ["USA", "World", "Europe", "Japan"],
    "allow_beta": False,
    "allow_rom_hacks": False,
    # Acquisition
    "min_seeders": 1,
    "max_size_mb": 8192,
    "protocol": "torrent",       # torrent | usenet
    # Remote path mappings, the same concept Radarr and Sonarr expose.
    #
    # A download client reports the path it sees. When the client and this
    # service are different containers or hosts, that path means nothing here:
    # qBittorrent says /mnt/usb1/Downloads/game.zip and this process has that
    # volume mounted somewhere else, or not at all. Without a translation the
    # import fails with "download path does not exist" while the file is
    # sitting right there.
    #
    # Each entry is {"remote": "<what the client says>", "local": "<what we see>"}.
    "remote_path_mappings": [],
    # Import lists: titles fed into Wanted on the List Sync schedule. Each
    # entry keeps a ledger of what it already added, so a list re-syncing
    # never resurrects a title that was acquired and fulfilled.
    "import_lists": [],
    "list_sync_interval_hours": 6,
    # Download clients and indexers, each a list of stored configurations.
    #
    # These used to come from environment variables only, which meant the
    # Settings pages could show them and not change them -- you had to edit a
    # file and restart to add a client. On first run the environment is read
    # once to seed these, so an existing install keeps working, and after that
    # the store is authoritative.
    "download_clients": [],
    "indexers": [],
    # Behaviour
    "auto_import": True,
    "rescan_after_import": True,
    # Failed download handling, the same idea Radarr and Sonarr have: a
    # release that could not be downloaded is not one to choose again. When
    # on, a download that the client refused, that finished with no ROMs in
    # it, or that stalled is added to the blocklist and the next best release
    # is grabbed in its place. Without this the next missing/RSS sweep re-runs
    # the same scorer over the same results and picks the same dead file.
    "blocklist_failed_downloads": True,
    # How long a grabbed download may sit without finishing before it counts
    # as stalled, in minutes. 0 disables stall detection entirely, leaving
    # only outright failures to be retired. Generous by default: a large disc
    # image on a thin swarm is slow, not dead.
    "stalled_timeout_minutes": 180,
    # The clock. Zero disables a job; the scheduler reads these live, so a
    # change applies at the next tick without a restart.
    #
    # Import polls often because it is cheap and the person is usually
    # waiting; the missing search is hours apart because hammering indexers
    # for titles that were not there this morning is how trackers hand out
    # bans; RSS fills the gap between those sweeps by watching what is new
    # instead of asking again for everything.
    "auto_import_interval_minutes": 1,
    "search_missing_interval_hours": 12,
    "rss_sync_interval_minutes": 60,
    # Whether to ask github.com once a day if a newer ROMarr exists. Checking
    # is the whole feature -- nothing is ever downloaded or applied.
    "update_check": True,
}


class Store:
    """Everything ROMarr remembers."""

    # Past this the history file grows without bound and nobody reads the tail.
    MAX_EVENTS = 2000

    # The queue is live state, not a log: rows leave it when they import or
    # are cleared. The cap is a backstop against a runaway sweep filling the
    # state file, not an expected working size.
    MAX_QUEUE = 500

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        # Deep, not shallow. DEFAULT_SETTINGS holds mutable lists
        # (download_clients, indexers, preferred_regions); a shallow copy hands
        # every Store the *same* list objects, so appending a client in one
        # place silently appends it everywhere -- including into the module
        # default, which then leaks into every Store created afterwards.
        self.settings: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self.events: list[Event] = []
        self.wanted: list[WantedItem] = []
        self.queue: list[QueueItem] = []
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as err:
            # Unreadable and unparseable used to share this handler, and they
            # are not the same failure. A file we cannot read is one that is
            # intact and belongs to somebody else -- typically a root-owned
            # romarr.json under a container running as PUID. Starting from
            # defaults recovers nothing there; it destroys. The next save
            # replaces the API key, the password hash and the whole history,
            # and the install comes back unclaimed, so whoever reaches the port
            # next gets to set the password. Refusing to start costs an outage
            # and loses nothing.
            raise StateUnreadable(
                f"{self.path} exists but cannot be read ({err}). This file "
                "holds the API key, the password hash and the request "
                "history; starting from defaults would overwrite it and leave "
                "the install unclaimed. Fix the ownership or permissions -- in "
                "Docker, PUID/PGID must own /config and everything inside it "
                "-- and start ROMarr again."
            ) from err
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as err:
            # A corrupt file must not stop the service from starting; the
            # defaults are always a usable configuration. Unlike the case
            # above there is nothing here to preserve -- the bytes no longer
            # parse, so no version of this file can be handed back.
            log.warning("could not read %s (%s); starting from defaults", self.path, err)
            return
        with self._lock:
            # Merged rather than replaced, so a setting added in a later
            # version has its default instead of being absent.
            self.settings = {**copy.deepcopy(DEFAULT_SETTINGS), **(raw.get("settings") or {})}
            self.events = _rows(raw.get("events"), Event)
            self.wanted = _rows(raw.get("wanted"), WantedItem)
            # Absent in files written before the queue was persisted, which is
            # simply an empty queue -- the same thing those installs had after
            # every restart anyway.
            self.queue = _rows(raw.get("queue"), QueueItem)

    def save(self) -> None:
        with self._lock:
            payload = {
                "settings": self.settings,
                "events": [asdict(e) for e in self.events[-self.MAX_EVENTS:]],
                "wanted": [asdict(w) for w in self.wanted],
                "queue": [asdict(q) for q in self.queue[-self.MAX_QUEUE:]],
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write would otherwise leave truncated JSON and
        # the service would refuse to start.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".romarr-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("could not write %s: %s", self.path, err)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- history -----------------------------------------------------------

    def record(self, event: Event) -> Event:
        with self._lock:
            self.events.append(event)
            if len(self.events) > self.MAX_EVENTS:
                del self.events[: len(self.events) - self.MAX_EVENTS]
        self.save()
        return event

    def history(self, limit: int = 100, kind: str = "") -> list[dict]:
        with self._lock:
            items = [e for e in self.events if not kind or e.kind == kind]
            return [asdict(e) for e in reversed(items[-limit:])]

    # -- wanted ------------------------------------------------------------

    def want(self, game: str, platform: str) -> WantedItem:
        """Add to Wanted, or return the existing entry.

        Requesting the same game twice is a retry, not a second entry.
        """
        with self._lock:
            for item in self.wanted:
                if item.game.lower() == game.lower() and item.platform == platform:
                    return item
            item = WantedItem(game=game, platform=platform)
            self.wanted.append(item)
        self.save()
        return item

    def fulfil(self, game: str, platform: str) -> bool:
        """Drop something from Wanted once it has actually arrived."""
        with self._lock:
            before = len(self.wanted)
            self.wanted = [
                w for w in self.wanted
                if not (w.game.lower() == game.lower() and w.platform == platform)
            ]
            changed = len(self.wanted) != before
        if changed:
            self.save()
        return changed

    def unwant(self, game: str, platform: str) -> bool:
        """Drop a request nobody wants any more.

        The same removal `fulfil` performs, deliberately kept apart from it:
        fulfil means "this arrived", and the two are not the same event to
        anybody reading History later. Without this the only way off the
        Wanted list was an import, so a misspelled request cost an indexer
        search on every missing sweep and every RSS pass, forever, for a game
        that does not exist.
        """
        return self.fulfil(game, platform)

    def missing(self) -> list[dict]:
        with self._lock:
            return [asdict(w) for w in self.wanted]

    def note_failure(self, game: str, platform: str, reason: str) -> None:
        with self._lock:
            for item in self.wanted:
                if item.game.lower() == game.lower() and item.platform == platform:
                    item.attempts += 1
                    item.last_error = reason
                    break
        self.save()

    def mark_searched(self, game: str, platform: str) -> None:
        """Stamp when an automatic search ran, whatever it found.

        Separate from note_failure on purpose: the backoff clock starts when
        a search RUNS. Stamping only failures would make an item whose search
        crashed mid-way immediately eligible again, which is a retry storm
        with extra steps.
        """
        with self._lock:
            for item in self.wanted:
                if item.game.lower() == game.lower() and item.platform == platform:
                    item.searched_at = now_iso()
                    break
        self.save()

    # -- queue (Activity) ---------------------------------------------------

    def enqueue(self, item: QueueItem) -> QueueItem:
        """Add one row to the queue and write it down."""
        with self._lock:
            self.queue.append(item)
            if len(self.queue) > self.MAX_QUEUE:
                del self.queue[: len(self.queue) - self.MAX_QUEUE]
        self.save()
        return item

    def set_queue(self, items) -> None:
        """Replace the whole queue -- what remove, retry and clear do."""
        with self._lock:
            self.queue = list(items)
        self.save()

    def queue_rows(self) -> list[dict]:
        with self._lock:
            return [asdict(q) for q in self.queue]

    # -- per-game shelf state ------------------------------------------------
    #
    # What Questarr tracks per game -- playing / completed / shelved, a
    # rating, a note -- and the two states it also has that ROMarr derives
    # instead of storing: "wanted" IS the wanted list, and "owned" is the
    # library. Storing either would create a second copy that drifts.

    #: The statuses a person can set. Empty string clears.
    GAME_STATUSES = ("playing", "completed", "shelved")

    @staticmethod
    def _meta_key(platform: str, game: str) -> str:
        return f"{platform}/{game.strip().lower()}"

    def set_game_meta(self, platform: str, game: str, *, status=None,
                      rating=None, notes=None) -> dict:
        """Update the fields that were sent and leave the rest alone.

        `None` means "not in this request"; empty string (or 0) means
        "clear it". A record with nothing left in it is removed entirely so
        the file does not fill with empty husks of games somebody once rated.
        """
        key = self._meta_key(platform, game)
        with self._lock:
            table = self.settings.setdefault("game_meta", {})
            row = dict(table.get(key) or {})
            if status is not None:
                status = str(status).strip().lower()
                if status and status not in self.GAME_STATUSES:
                    raise ValueError(
                        f"unknown status {status!r}; one of "
                        f"{', '.join(self.GAME_STATUSES)} or empty to clear")
                row["status"] = status
            if rating is not None:
                rating = int(rating)
                if not 0 <= rating <= 10:
                    raise ValueError("rating is 0-10, where 0 clears it")
                row["rating"] = rating
            if notes is not None:
                row["notes"] = str(notes)
            fields = {k: v for k, v in row.items()
                      if k in ("status", "rating", "notes")
                      and v not in ("", 0, None)}
            if fields:
                row = {**fields, "game": game, "platform": platform}
                table[key] = row
            else:
                # Nothing left worth keeping: remove the record entirely so
                # the file does not fill with empty husks of games somebody
                # once rated.
                row = {}
                table.pop(key, None)
        self.save()
        return dict(row)

    def game_meta(self, platform: str, game: str) -> dict:
        with self._lock:
            return dict((self.settings.get("game_meta") or {})
                        .get(self._meta_key(platform, game)) or {})

    def all_game_meta(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in (self.settings.get("game_meta") or {}).values()]

    # -- settings ----------------------------------------------------------

    # -- collections (download clients, indexers) --------------------------

    def _collection(self, key: str) -> list[dict]:
        return self.settings.setdefault(key, [])

    def list_items(self, key: str) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._collection(key)]

    def get_item(self, key: str, item_id: str) -> dict | None:
        with self._lock:
            for item in self._collection(key):
                if str(item.get("id")) == str(item_id):
                    return dict(item)
        return None

    def put_item(self, key: str, item: dict) -> dict:
        """Add or replace one entry, returning it with its id."""
        import uuid
        with self._lock:
            items = self._collection(key)
            if item.get("id"):
                for i, existing in enumerate(items):
                    if str(existing.get("id")) == str(item["id"]):
                        items[i] = item
                        break
                else:
                    items.append(item)
            else:
                item["id"] = uuid.uuid4().hex[:12]
                items.append(item)
            out = dict(item)
        self.save()
        return out

    def delete_item(self, key: str, item_id: str) -> bool:
        with self._lock:
            items = self._collection(key)
            before = len(items)
            self.settings[key] = [i for i in items if str(i.get("id")) != str(item_id)]
            changed = len(self.settings[key]) != before
        if changed:
            self.save()
        return changed

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial settings update.

        Unknown keys are dropped rather than stored: a typo in a PUT should not
        silently become permanent state that nothing ever reads.
        """
        with self._lock:
            for key, value in patch.items():
                if key in DEFAULT_SETTINGS:
                    self.settings[key] = value
            out = dict(self.settings)
        self.save()
        return out

    # -- Integration request identity ------------------------------------
    #
    # The integration surface (``/api/v1/integration/requests/*``) is the
    # contract external request systems — SeerrNG today — speak. Each
    # request has a durable row in ``romarr.json`` keyed by the peer's
    # ``externalRequestId`` so a restart does not lose the binding, and the
    # store methods below are the single place that mutates it. The HTTP
    # layer reads and reports; it never writes status.

    MAX_INTEGRATION_REQUESTS = 500
    _INTEGRATION_TERMINAL = {"available", "failed", "cancelled"}

    def _row_for_integration(self, external_request_id: str) -> "SeerrRequest | None":
        with self._lock:
            for item in self.settings.get("integration_requests", []):
                if item.get("id") == external_request_id:
                    return item
        return None

    def get_seerr_request(self, external_request_id: str) -> "SeerrRequest | None":
        """Return the current ``SeerrRequest`` for ``external_request_id``.

        Rows are stored as dicts (they live inside a settings list so they
        round-trip through JSON); reconstitute on read so the HTTP layer
        sees a real dataclass with attribute access.
        """
        row = self._row_for_integration(external_request_id)
        if row is None:
            return None
        return SeerrRequest(
            external_request_id=row["id"],
            game=row.get("game", ""),
            name=row.get("name", row.get("game", "")),
            platform=row.get("platform", ""),
            catalog_provider=row.get("catalog_provider", ""),
            catalog_id=int(row.get("catalog_id", 0) or 0),
            platform_id=int(row.get("platform_id", 0) or 0),
            status=row.get("status", "accepted"),
            error=row.get("error", ""),
            assets=list(row.get("assets") or []),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )

    def put_seerr_request(self, row: "SeerrRequest") -> "SeerrRequest":
        """Insert or replace a row by ``external_request_id``.

        Bounded by ``MAX_INTEGRATION_REQUESTS``; when the ceiling is hit,
        the oldest terminal row (available / failed / cancelled) is the one
        that gives, in FIFO order, so a long-running peer's in-flight
        requests survive the eviction pass. The row is persisted before the
        lock is released.
        """
        from datetime import datetime, timezone
        with self._lock:
            items = self.settings.setdefault("integration_requests", [])
            target = next(
                (item["id"] for item in items if item.get("id") == row.external_request_id),
                None,
            )
            stored = {
                "id": row.external_request_id,
                "game": row.game,
                "name": row.name or row.game,
                "platform": row.platform,
                "catalog_provider": row.catalog_provider,
                "catalog_id": int(row.catalog_id or 0),
                "platform_id": int(row.platform_id or 0),
                "status": row.status,
                "error": row.error,
                "assets": list(row.assets),
                "created_at": row.created_at or now_iso(),
                "updated_at": row.updated_at or now_iso(),
            }
            if target is None:
                items.append(stored)
                while (len(items) > self.MAX_INTEGRATION_REQUESTS
                       and any(i["status"] in self._INTEGRATION_TERMINAL
                               for i in items)):
                    for item in items:
                        if (item["status"] in self._INTEGRATION_TERMINAL
                                and len(items) > self.MAX_INTEGRATION_REQUESTS):
                            items.remove(item)
                            break
            else:
                for i, item in enumerate(items):
                    if item.get("id") == target:
                        items[i] = stored
                        break
        self.save()
        return row

    def update_seerr_request(self, external_request_id: str, *, status: str,
                              error: str = "",
                              assets: list[str] | None = None,
                              name: str = "",
                              game: str = "",
                              platform: str = "",
                              only_if_not_cancelled: bool = False) -> "SeerrRequest | None":
        """Update the row's status/error in place; persist before releasing lock.

        ``only_if_not_cancelled`` lets a dispatch pipeline that is racing a
        peer's cancel lose gracefully: the pipeline writes its progress, the
        cancel wins, and a subsequent status write that *would* flip a
        cancelled row back to ``searching`` is a no-op.
        """
        with self._lock:
            items = self.settings.setdefault("integration_requests", [])
            for item in items:
                if item.get("id") != external_request_id:
                    continue
                if only_if_not_cancelled and item.get("status") == "cancelled":
                    return self.get_seerr_request(external_request_id)
                item["status"] = status
                item["error"] = error
                if assets is not None:
                    item["assets"] = assets
                if name:
                    item["name"] = name
                    item["game"] = item.get("game") or name
                if game:
                    item["game"] = game
                if platform:
                    item["platform"] = platform
                item["updated_at"] = now_iso()
                break
        self.save()
        return self.get_seerr_request(external_request_id)

    def mark_seerr_dispatching(self, external_request_id: str) -> bool:
        """Claim the handoff slot so the peer's cancel cannot race the add.

        Returns ``True`` iff the pipeline is allowed to proceed. The row is
        flipped to ``dispatching`` under the lock; a peer that cancels
        *during* the pipeline's queue add will see the row in
        ``dispatching`` state and receive an ``"active"`` result from
        ``cancel_seerr_request`` rather than a success, so the peer knows
        to retry once the release settles.
        """
        with self._lock:
            items = self.settings.setdefault("integration_requests", [])
            for item in items:
                if item.get("id") != external_request_id:
                    continue
                if item.get("status") == "cancelled":
                    return False
                item["status"] = "dispatching"
                item["error"] = ""
                item["updated_at"] = now_iso()
                break
            else:
                return False
        self.save()
        return True

    def cancel_seerr_request(
        self, external_request_id: str, confirm_no_existing_download: bool = False
    ) -> str:
        """Cancel before dispatch, or require queue confirmation after recovery.

        Returns one of:

        * ``"cancelled"``  -- row was cancelled (now or previously); no download.
        * ``"missing"``    -- no row for this id; the peer should re-submit.
        * ``"available"``  -- the row's import is already verified in the library;
                             a cancel from the peer is too late, nothing to do.
        * ``"active"``     -- the row's release is still being searched /
                             imported; cancel is not offered while it is live.
        * ``"confirmation"`` -- the row is paused at a handoff-restart error
                             (the server died between queue add and client add)
                             and the peer has not yet confirmed there is no
                             pending download in the client. The HTTP layer
                             must re-call with ``confirm_no_existing_download``
                             once the operator has checked.
        """
        with self._lock:
            items = self.settings.setdefault("integration_requests", [])
            for item in items:
                if item.get("id") != external_request_id:
                    continue
                status = item.get("status", "")
                error = item.get("error", "")
                if status == "cancelled":
                    return "cancelled"
                if status == "available":
                    return "available"
                if (error.startswith("The server restarted during download handoff.")
                        and not confirm_no_existing_download):
                    return "confirmation"
                queue = [q for q in self.queue if q.external_request_id == external_request_id]
                if any(q.state == "imported" for q in queue):
                    return "available"
                if status in ("dispatching", "downloading") or any(
                        q.state in ("queued", "grabbed") for q in queue):
                    return "active"
                item["status"] = "cancelled"
                item["error"] = ""
                item["updated_at"] = now_iso()
                break
            else:
                return "missing"
        self.save()
        return "cancelled"

    def recover_seerr_dispatches(self) -> None:
        """Reconcile rows that were ``dispatching`` when the server died.

        The dispatch pipeline writes the queue row and flips status; if the
        process is killed in that window the row is stuck at ``dispatching``.
        On startup, look up each such row's queue state and move it to the
        truth that already happened:

        * latest ``grabbed``     -> ``downloading`` (the client had it).
        * latest ``imported``    -> ``available`` (the import verified).
        * latest ``failed``      -> ``failed`` with the import detail.
        * no row / no signal     -> ``failed`` with the handoff-restart
                                     error so the peer can confirm and retry.

        Called once at startup, before the request handler begins serving.
        """
        changed = False
        with self._lock:
            items = self.settings.get("integration_requests", [])
            for item in items:
                if item.get("status") != "dispatching":
                    continue
                rows = [q for q in self.queue
                        if q.external_request_id == item.get("id")]
                latest = rows[-1] if rows else None
                if latest and latest.state == "grabbed":
                    item["status"], item["error"] = "downloading", ""
                elif latest and latest.state == "imported":
                    item["status"], item["error"] = "available", ""
                elif latest and latest.state in ("failed", "import-failed"):
                    item["status"] = "failed"
                    item["error"] = latest.detail or "ROMarr could not complete the request."
                else:
                    item["status"] = "failed"
                    item["error"] = (
                        "The server restarted during download handoff. "
                        "Check the download client's queue and history before retrying.")
                item["updated_at"] = now_iso()
                changed = True
        if changed:
            self.save()

    def latest_seerr_request(self) -> "SeerrRequest | None":
        """Return the most recently updated integration row, or ``None``.

        Used by the ``/api/v1/integration/requests/current`` endpoint so the
        external platform can poll a single, most-fresh status in one call
        without having to enumerate the full queue.
        """
        rows = self.settings.get("integration_requests", [])
        if not isinstance(rows, list) or not rows:
            return None
        latest: dict = rows[0]
        for row in rows[1:]:
            if row.get("updated_at", "") > latest.get("updated_at", ""):
                latest = row
        return SeerrRequest(
            external_request_id=str(latest.get("id", "")),
            name=str(latest.get("name", "")),
            platform=str(latest.get("platform", "")),
            status=str(latest.get("status", "requested")),
            assets=list(latest.get("assets") or []),
            error=str(latest.get("error", "")),
            created_at=str(latest.get("created_at", "")),
            updated_at=str(latest.get("updated_at", "")),
        )


def to_jsonable(value: Any) -> Any:
    """dataclasses -> dicts, for the API layer."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value
