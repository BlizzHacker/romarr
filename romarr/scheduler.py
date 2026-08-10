"""The clock. Everything ROMarr does on its own schedule goes through here.

Until now ROMarr only acted when asked: a search happened when somebody typed
one, and "Import completed downloads automatically" was a checkbox that
nothing read. Every other *arr -- and Questarr, which is where the pressure
to fix this came from -- runs its acquisition on a clock: completed downloads
are imported within a minute, the Wanted list is re-searched on an interval,
and the indexers' RSS feeds are watched between searches so a release that
appears an hour after you asked is grabbed an hour after it appears, not a
fortnight later when the next manual search happens to run.

One thread, not one per job. The jobs are all short and none may overlap
itself, so a single loop that asks "who is due" every tick is simpler to
reason about and impossible to leak. A job that raises is recorded and
rescheduled, never dropped: the scheduler surviving its jobs is the whole
point of having one.

Intervals are read through a callable at every tick rather than captured at
registration, so changing a setting applies at the next tick without a
restart. An interval of zero means "off", which is how a checkbox disables a
job without the scheduler needing to know checkboxes exist.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    """One recurring task and what happened last time it ran."""

    name: str            # the command name, e.g. "RssSync"
    label: str           # what the Tasks page shows
    interval: object     # () -> seconds between runs; 0 or less disables
    run: object          # () -> str, a one-line summary for the Tasks page
    last_run: str = ""
    last_result: str = ""
    last_error: str = ""
    #: monotonic time of the last start, so due-ness never trusts wall-clock
    #: changes (a container whose clock steps at boot would otherwise fire
    #: everything at once, or nothing for hours).
    _last_started: float = field(default=0.0, repr=False)


class Scheduler:
    """Runs registered jobs when they are due, one at a time."""

    #: How often the loop wakes to check for due jobs. Fine-grained enough
    #: that a one-minute job runs roughly every minute; coarse enough to cost
    #: nothing.
    TICK = 15.0

    def __init__(self):
        self._jobs: list[Job] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def add(self, name: str, label: str, interval, run) -> Job:
        job = Job(name=name, label=label, interval=interval, run=run)
        self._jobs.append(job)
        return job

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="romarr-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- the loop ------------------------------------------------------------

    def _loop(self) -> None:
        # First pass waits one full tick rather than firing everything at
        # boot: the service is still wiring clients and libraries, and a
        # search storm during startup is how a slow Prowlarr makes ROMarr
        # look hung.
        while not self._stop.wait(self.TICK):
            for job in self._jobs:
                if self._due(job):
                    self._run(job)

    def _due(self, job: Job) -> bool:
        try:
            seconds = float(job.interval() or 0)
        except Exception:
            return False
        if seconds <= 0:
            return False
        return (time.monotonic() - job._last_started) >= seconds

    def _run(self, job: Job) -> None:
        job._last_started = time.monotonic()
        job.last_run = _now_iso()
        try:
            job.last_result = str(job.run() or "done")
            job.last_error = ""
        except Exception as exc:
            # The job failed; the scheduler must not. Recorded where the
            # Tasks page can show it, because a job that fails silently on a
            # timer is a feature that "sometimes doesn't work".
            job.last_error = f"{exc.__class__.__name__}: {exc}"
            job.last_result = ""
            log.warning("scheduled %s failed: %s", job.name, job.last_error)

    # -- surface -------------------------------------------------------------

    def run_now(self, name: str) -> dict | None:
        """Run one job immediately, for the Tasks page's Run button."""
        for job in self._jobs:
            if job.name == name:
                with self._lock:
                    self._run(job)
                return self.describe(job)
        return None

    def describe(self, job: Job) -> dict:
        try:
            seconds = float(job.interval() or 0)
        except Exception:
            seconds = 0
        return {
            "name": job.name,
            "label": job.label,
            "interval_seconds": int(seconds),
            "enabled": seconds > 0,
            "last_run": job.last_run,
            "last_result": job.last_result,
            "last_error": job.last_error,
        }

    def status(self) -> list[dict]:
        return [self.describe(j) for j in self._jobs]


# -- backoff -----------------------------------------------------------------

#: The ladder an unfound Wanted item climbs. A title searched five minutes
#: ago is not searched again on the next sweep; one that has failed for
#: months is tried weekly, not hourly. Radarr calls the same idea
#: "re-search backoff"; the numbers here are hours.
BACKOFF_HOURS = (0, 4, 8, 24, 48, 96, 168)


def next_search_due(attempts: int, last_searched: str) -> bool:
    """Whether a Wanted item has waited out its backoff.

    `attempts` is how many automatic searches have failed; `last_searched`
    is when the most recent one ran (ISO, UTC). An item never searched is
    always due.
    """
    if not last_searched:
        return True
    hours = BACKOFF_HOURS[min(attempts, len(BACKOFF_HOURS) - 1)]
    try:
        last = datetime.fromisoformat(last_searched)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    waited = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return waited >= hours
