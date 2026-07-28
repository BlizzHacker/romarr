"""Persistence.

The *arr applications all keep the same things across a restart: what you
asked for, what happened to it, and how you configured the thing. Rommarr kept
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

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    at: str = field(default_factory=now_iso)


@dataclass
class WantedItem:
    """A request that has not been satisfied yet -- *arr's Wanted/Missing."""

    game: str
    platform: str
    added: str = field(default_factory=now_iso)
    attempts: int = 0
    last_error: str = ""


# Defaults are spelled out here rather than scattered through the UI so a fresh
# install and a configured one disagree about nothing.
DEFAULT_SETTINGS: dict[str, Any] = {
    # Media management
    "library_path": "/mnt/roms",
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
    # Behaviour
    "auto_import": True,
    "rescan_after_import": True,
}


class Store:
    """Everything Rommarr remembers."""

    # Past this the history file grows without bound and nobody reads the tail.
    MAX_EVENTS = 2000

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.events: list[Event] = []
        self.wanted: list[WantedItem] = []
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            # A corrupt file must not stop the service from starting; the
            # defaults are always a usable configuration.
            log.warning("could not read %s (%s); starting from defaults", self.path, err)
            return
        with self._lock:
            # Merged rather than replaced, so a setting added in a later
            # version has its default instead of being absent.
            self.settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
            self.events = [Event(**e) for e in raw.get("events", []) if isinstance(e, dict)]
            self.wanted = [WantedItem(**w) for w in raw.get("wanted", []) if isinstance(w, dict)]

    def save(self) -> None:
        with self._lock:
            payload = {
                "settings": self.settings,
                "events": [asdict(e) for e in self.events[-self.MAX_EVENTS:]],
                "wanted": [asdict(w) for w in self.wanted],
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write would otherwise leave truncated JSON and
        # the service would refuse to start.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".rommarr-", suffix=".tmp")
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

    # -- settings ----------------------------------------------------------

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


def to_jsonable(value: Any) -> Any:
    """dataclasses -> dicts, for the API layer."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value
