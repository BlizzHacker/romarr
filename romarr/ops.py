"""Operating ROMarr: metrics, rate limiting, backup and export.

Four things every mature *arr has and none of which is clever. What they have
in common is that each one is trivial to get subtly wrong in a way nobody
notices until the day it matters:

  * a metrics endpoint that is open when the rest of the app is not,
  * a rate limiter that counts the wrong thing and locks out the operator,
  * a backup that quietly contains every password in plain text,
  * an export that corrupts a title containing a comma.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# --- Prometheus -------------------------------------------------------------

def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_metrics(stats: dict) -> str:
    """Prometheus text exposition, built from a plain dict.

    Deliberately not a client library. The format is six lines of rules, and
    the dependency would be larger than the feature -- ROMarr is stdlib plus
    `requests` and that is worth keeping.
    """
    lines: list[str] = []

    def emit(name: str, kind: str, help_text: str, value, labels=None):
        full = f"romarr_{name}"
        if not any(line.startswith(f"# TYPE {full} ") for line in lines):
            lines.append(f"# HELP {full} {help_text}")
            lines.append(f"# TYPE {full} {kind}")
        if labels:
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
            lines.append(f"{full}{{{rendered}}} {value}")
        else:
            lines.append(f"{full} {value}")

    emit("up", "gauge", "Always 1; scrape failure is the signal for down.", 1)
    emit("platforms", "gauge", "Platforms ROMarr can request.",
         int(stats.get("platforms") or 0))
    emit("queue_size", "gauge", "Items currently queued.",
         int(stats.get("queued") or 0))
    emit("library_items", "gauge", "ROMs known in the library.",
         int(stats.get("library_items") or 0))
    emit("wanted_items", "gauge", "Games wanted but not yet found.",
         int(stats.get("wanted") or 0))
    emit("blocklist_size", "gauge", "Releases that will never be taken.",
         int(stats.get("blocklist") or 0))
    emit("uptime_seconds", "counter", "Seconds since start.",
         int(stats.get("uptime_seconds") or 0))

    for name, ok in (stats.get("dependencies") or {}).items():
        emit("dependency_up", "gauge",
             "1 when a configured dependency answered.",
             1 if ok else 0, {"name": name})

    # Import outcomes by DAT verdict. This is the series no other tool in this
    # category can publish, and the one worth alerting on: a rising
    # `verified="bad-dump"` means an indexer has started serving corrupt
    # dumps, which nothing else would surface until somebody pressed play.
    for verdict, count in (stats.get("imports") or {}).items():
        emit("imports_total", "counter",
             "Imports by DAT verification verdict.",
             int(count), {"verdict": verdict})

    return "\n".join(lines) + "\n"


# --- rate limiting ----------------------------------------------------------

#: Per-category limits, as (requests, seconds).
#:
#: Login is far tighter than the rest because it is the only endpoint where
#: guessing is the attack. Search is limited because each one fans out to
#: every configured indexer, so an unbounded caller does not hammer ROMarr --
#: it hammers somebody else's tracker, and gets the operator banned.
DEFAULT_LIMITS = {
    "login": (5, 60),
    "search": (30, 60),
    "download": (60, 60),
    "general": (300, 60),
}


class RateLimiter:
    """A fixed-window-per-key counter.

    Keyed on category *and* caller, never on category alone: a shared counter
    means one noisy script locks the operator out of their own UI, which is a
    denial of service the limiter introduced rather than prevented.
    """

    def __init__(self, limits: dict | None = None, clock=time.monotonic):
        self.limits = dict(limits or DEFAULT_LIMITS)
        self._clock = clock
        self._hits: dict[tuple[str, str], deque] = {}

    def check(self, category: str, caller: str = "") -> tuple[bool, int]:
        """(allowed, seconds until it would be allowed)."""
        limit, window = self.limits.get(category, self.limits["general"])
        key = (category, caller or "")
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= limit:
            return False, max(1, int(window - (now - hits[0])))
        hits.append(now)
        return True, 0

    @staticmethod
    def category_for(path: str) -> str:
        path = str(path or "")
        if "/login" in path:
            return "login"
        if "/search" in path or "/release" in path or "/candidates" in path:
            return "search"
        if "/grab" in path or "/download" in path or "/queue" in path:
            return "download"
        return "general"


# --- backup and restore -----------------------------------------------------

#: Settings a backup must never contain.
#:
#: A backup is copied to another machine, emailed to somebody helping with a
#: problem, and committed to a private repo "just in case". Every one of those
#: is a place a plaintext qBittorrent password should not be, and a backup
#: that silently carries credentials is worse than no backup because it is
#: handled as though it were harmless.
SECRET_KEYS = ("_api_key", "_password_hash", "_totp_secret", "_totp_backup")
SECRET_FIELDS = ("password", "api_key", "passkey", "token", "secret")


def _strip_secrets(value):
    if isinstance(value, dict):
        return {k: ("" if k in SECRET_FIELDS or k in SECRET_KEYS
                    else _strip_secrets(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def make_backup(settings: dict, *, include_secrets: bool = False) -> dict:
    """A restorable snapshot of the configuration.

    Credentials are stripped unless explicitly asked for, and the result says
    which it is -- so a restore can warn that the download clients will need
    their passwords again rather than silently producing an install that
    cannot log in to anything.
    """
    body = dict(settings)
    if not include_secrets:
        body = _strip_secrets(body)
    return {
        "kind": "romarr-backup",
        "version": 1,
        "created_at": int(time.time()),
        "contains_secrets": bool(include_secrets),
        "settings": body,
    }


def read_backup(payload) -> tuple[dict, str]:
    """(settings, warning). Raises ValueError on anything that is not a backup."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or payload.get("kind") != "romarr-backup":
        raise ValueError("not a ROMarr backup")
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("backup contains no settings")
    warning = ""
    if not payload.get("contains_secrets"):
        warning = ("This backup was taken without credentials. Download "
                   "clients, indexers and libraries will need their passwords "
                   "and API keys entered again.")
    return settings, warning


# --- export -----------------------------------------------------------------

def to_csv(rows: list[dict], columns: list[str] | None = None) -> str:
    """CSV via the stdlib writer, never string joining.

    Game titles contain commas, quotes and the occasional newline --
    "Ratchet & Clank: Up Your Arsenal", "Bust-A-Move '99" -- and hand-rolled
    CSV corrupts exactly those rows while looking correct for everything else.
    `\\r\\n` because that is what RFC 4180 says and what spreadsheets expect.
    """
    columns = columns or sorted({k for row in rows for k in row})
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore",
                            lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return out.getvalue()


def from_csv(text: str) -> list[dict]:
    return [dict(row) for row in csv.DictReader(io.StringIO(text or ""))]
