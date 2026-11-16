"""A slow RomM count must not turn a complete library read into a failed one.

Issue #216: on a 28k-game RomM with a cold MariaDB cache, the filtered
`GET /api/roms?limit=1&missing=false` count takes 6-10 s. `Romm.counts()` ran
it on the 5 s health budget, ROMarr hung up at 5.005 s (RomM's nginx logs a
499), and the refresh loop treated that one timeout as a failed library read:
"Library read incomplete: 28,316 games read" after every game had been read.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from romarr.app import ROMarr
from romarr.clients import Romm, RommConfig
from romarr.libraries import Game


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = json.dumps(body)
        self.ok = True

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class TimingSession:
    """A RomM 5.x that records the timeout each count query was given."""

    TOTALS = {None: 28316, "false": 28316, "true": 0}

    def __init__(self):
        self.timeouts = []

    def get(self, url, **kw):
        params = kw.get("params") or {}
        self.timeouts.append(kw.get("timeout"))
        return FakeResponse({"total": self.TOTALS[params.get("missing")], "items": []})

    def post(self, url, **kw):
        return FakeResponse({"access_token": "t"})


def test_counts_get_a_budget_a_slow_filtered_count_fits_in():
    """The filtered count is measured at 6-10 s cold; 5 s cut it off."""
    session = TimingSession()
    romm = Romm(RommConfig(base_url="http://romm", username="u", password="p"),
                session=session)

    assert romm.counts() == {"total": 28316, "on_disk": 28316, "catalogued": 0}
    count_timeouts = [t for t in session.timeouts if t is not None]
    assert len(count_timeouts) >= 3, "every count query carries an explicit timeout"
    assert all(t == Romm.COUNT_TIMEOUT for t in count_timeouts)
    assert Romm.COUNT_TIMEOUT > Romm.HEALTH_TIMEOUT
    assert Romm.COUNT_TIMEOUT >= 30, "a cold-cache count took 9.6 s in the report"


class EndRefresh(Exception):
    """Stop the refresh loop at its normal wait boundary."""


def refresh_once(backend, library_max=100):
    """Run the real background loop once, without threads or configuration."""
    service = ROMarr.__new__(ROMarr)
    service.game_libraries = [({"name": "RomM"}, backend)]
    service._library_cache = (None, 0, "")
    service._library_scan = {}
    service.LIBRARY_PAGE = 2
    service.LIBRARY_MAX = library_max
    published = []

    def publish(shelf, error, *, partial):
        published.append((list(shelf), error, partial))
        service._library_cache = (list(shelf), 1, error)

    def wait(seconds):
        raise EndRefresh()

    service._publish_library = publish
    service._wait = wait
    with pytest.raises(EndRefresh):
        service._refresh_counts()
    return service, published


def paged_backend(games, counts):
    """A RomM-shaped backend: server-side counts plus offset/limit pages."""
    def page(limit, offset, timeout):
        return games[offset:offset + limit]

    return SimpleNamespace(name="RomM", counts=counts, games=page,
                           BACKGROUND_TIMEOUT=300)


def test_a_count_timeout_does_not_mark_a_complete_read_incomplete():
    """Every page arrived, so the shelf is whole; only the split is unknown."""
    games = [Game(id=str(n), name=f"Game {n}", platform="snes") for n in range(5)]

    def slow_counts():
        raise requests.ReadTimeout("count took longer than the budget")

    service, published = refresh_once(paged_backend(games, slow_counts))

    rows, error, partial = published[-1]
    assert len(rows) == 5
    assert not partial, "a finished paged read is not a partial library"
    scan, message = service._library_scan_status()
    assert scan["stage"] == "complete"
    assert "incomplete" not in message.lower()
    assert "RomM" not in service._library_reasons
    assert service._count_split["total"] == 5, "falls back to the games read"
    assert service._count_split["on_disk"] is None, "the split stays unknown"


def test_a_failed_page_read_is_still_reported_as_incomplete():
    """The fix must not hide a real read failure behind the count fallback."""
    def broken_pages(limit, offset, timeout):
        if offset:
            raise requests.ReadTimeout("page read failed")
        return [Game(id="1", name="A", platform="snes"),
                Game(id="2", name="B", platform="snes")]

    def slow_counts():
        raise requests.ReadTimeout("count took longer than the budget")

    backend = SimpleNamespace(name="RomM", counts=slow_counts, games=broken_pages,
                              BACKGROUND_TIMEOUT=300)
    service, published = refresh_once(backend)

    assert published[-1][2], "a read that stopped early stays partial"
    scan, _ = service._library_scan_status()
    assert scan["stage"] == "error"


def test_a_count_timeout_on_a_capped_read_reports_the_total_as_unknown():
    """Rows read up to LIBRARY_MAX are a lower bound, not the library's size."""
    games = [Game(id=str(n), name=f"Game {n}", platform="snes") for n in range(5)]

    def slow_counts():
        raise requests.ReadTimeout("count took longer than the budget")

    service, published = refresh_once(paged_backend(games, slow_counts), library_max=4)

    assert len(published[-1][0]) == 4
    assert published[-1][2], "a capped read stays marked partial"
    assert service._count_cache[0] is None, "no capped number posing as the total"
    assert service._count_split["total"] is None
    scan, _ = service._library_scan_status()
    assert scan["limit_reached"]


def test_a_count_timeout_below_the_cap_keeps_the_exact_total():
    """A read that finished under LIBRARY_MAX counted every game."""
    games = [Game(id=str(n), name=f"Game {n}", platform="snes") for n in range(5)]

    def slow_counts():
        raise requests.ReadTimeout("count took longer than the budget")

    service, _ = refresh_once(paged_backend(games, slow_counts), library_max=100)

    assert service._count_cache[0] == 5
    assert service._count_split["total"] == 5
