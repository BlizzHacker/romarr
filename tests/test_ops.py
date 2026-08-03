"""Metrics, rate limiting, backup and export.

None of it is clever. All of it is easy to get subtly wrong in a way nobody
notices until the day it matters, which is what these pin.
"""

from __future__ import annotations

import json

import pytest

from romarr.ops import (
    DEFAULT_LIMITS, RateLimiter, from_csv, make_backup, read_backup,
    render_metrics, to_csv)


# --- Prometheus ------------------------------------------------------------

def test_metrics_are_valid_exposition_format():
    text = render_metrics({"platforms": 58, "queued": 2, "uptime_seconds": 90})
    assert "# HELP romarr_platforms" in text
    assert "# TYPE romarr_platforms gauge" in text
    assert "romarr_platforms 58" in text
    assert text.endswith("\n")


def test_every_series_is_declared_exactly_once():
    """A repeated TYPE line makes Prometheus reject the whole scrape."""
    text = render_metrics({"dependencies": {"prowlarr": True, "romm": False}})
    types = [line for line in text.splitlines() if line.startswith("# TYPE")]
    assert len(types) == len(set(types))


def test_dependencies_are_labelled_series():
    text = render_metrics({"dependencies": {"prowlarr": True, "romm": False}})
    assert 'romarr_dependency_up{name="prowlarr"} 1' in text
    assert 'romarr_dependency_up{name="romm"} 0' in text


def test_import_verdicts_are_exported_because_nothing_else_can():
    """The series worth alerting on: a rising bad-dump count means an indexer
    has started serving corrupt dumps, which nothing else in this category
    would surface until somebody pressed play."""
    text = render_metrics({"imports": {"verified": 40, "bad-dump": 3,
                                       "unknown": 7}})
    assert 'romarr_imports_total{verdict="bad-dump"} 3' in text
    assert 'romarr_imports_total{verdict="verified"} 40' in text


def test_a_label_value_with_a_quote_is_escaped():
    """An unescaped quote produces a malformed line and Prometheus drops the
    entire scrape, not just that series."""
    text = render_metrics({"dependencies": {'we"ird': True}})
    assert 'name="we\\"ird"' in text


def test_metrics_render_from_an_empty_dict():
    assert "romarr_up 1" in render_metrics({})


# --- rate limiting ---------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_requests_under_the_limit_are_allowed():
    limiter = RateLimiter({"general": (3, 60)}, clock=Clock())
    assert all(limiter.check("general", "a")[0] for _ in range(3))


def test_the_limit_is_enforced():
    limiter = RateLimiter({"general": (3, 60)}, clock=Clock())
    for _ in range(3):
        limiter.check("general", "a")
    allowed, retry = limiter.check("general", "a")
    assert not allowed and retry > 0


def test_the_window_slides_forward():
    clock = Clock()
    limiter = RateLimiter({"general": (2, 60)}, clock=clock)
    limiter.check("general", "a")
    limiter.check("general", "a")
    assert not limiter.check("general", "a")[0]
    clock.now += 61
    assert limiter.check("general", "a")[0]


def test_one_caller_cannot_lock_out_another():
    """Keyed on caller as well as category. A shared counter means one noisy
    script locks the operator out of their own UI -- a denial of service the
    limiter introduced rather than prevented."""
    limiter = RateLimiter({"general": (2, 60)}, clock=Clock())
    limiter.check("general", "noisy")
    limiter.check("general", "noisy")
    assert not limiter.check("general", "noisy")[0]
    assert limiter.check("general", "operator")[0]


def test_categories_are_counted_separately():
    limiter = RateLimiter({"login": (1, 60), "general": (10, 60)},
                          clock=Clock())
    limiter.check("login", "a")
    assert not limiter.check("login", "a")[0]
    assert limiter.check("general", "a")[0]


def test_login_is_the_tightest_limit():
    """It is the only endpoint where guessing is the attack."""
    assert DEFAULT_LIMITS["login"][0] < DEFAULT_LIMITS["general"][0]
    assert DEFAULT_LIMITS["login"][0] <= 10


@pytest.mark.parametrize("path,category", [
    ("/api/v1/login", "login"),
    ("/api/v1/search?q=x", "search"),
    ("/api/v1/release", "search"),
    ("/api/v1/queue", "download"),
    ("/api/v1/game", "general"),
])
def test_paths_are_categorised(path, category):
    assert RateLimiter.category_for(path) == category


def test_an_unknown_category_falls_back_to_general_not_to_unlimited():
    limiter = RateLimiter({"general": (1, 60)}, clock=Clock())
    limiter.check("nonsense", "a")
    assert not limiter.check("nonsense", "a")[0]


# --- backup ----------------------------------------------------------------

SETTINGS = {
    "library_path": "/roms",
    "min_seeders": 2,
    "_api_key": "SUPERSECRET",
    "_totp_secret": "BASE32SECRET",
    "download_clients": [
        {"name": "qbit", "type": "qbittorrent", "password": "hunter2"},
    ],
    "indexers": [{"name": "ix", "api_key": "IXKEY"}],
}


def test_a_backup_carries_no_credentials_by_default():
    """A backup gets copied to another machine, emailed to somebody helping,
    and committed to a private repo "just in case". Every one of those is a
    place a plaintext qBittorrent password should not be."""
    body = json.dumps(make_backup(SETTINGS))
    for secret in ("SUPERSECRET", "hunter2", "IXKEY", "BASE32SECRET"):
        assert secret not in body, secret


def test_a_backup_keeps_everything_that_is_not_a_credential():
    backup = make_backup(SETTINGS)
    assert backup["settings"]["library_path"] == "/roms"
    assert backup["settings"]["min_seeders"] == 2
    assert backup["settings"]["download_clients"][0]["name"] == "qbit"


def test_secrets_are_included_only_when_asked_for():
    body = json.dumps(make_backup(SETTINGS, include_secrets=True))
    assert "hunter2" in body


def test_a_backup_says_which_kind_it_is():
    assert make_backup(SETTINGS)["contains_secrets"] is False
    assert make_backup(SETTINGS, include_secrets=True)["contains_secrets"] is True


def test_restoring_warns_that_credentials_are_missing():
    """Otherwise a restore silently produces an install that cannot log in to
    anything, and the operator debugs their download client instead."""
    settings, warning = read_backup(make_backup(SETTINGS))
    assert settings["library_path"] == "/roms"
    assert "password" in warning.lower()


def test_restoring_a_full_backup_warns_about_nothing():
    _, warning = read_backup(make_backup(SETTINGS, include_secrets=True))
    assert warning == ""


def test_a_backup_round_trips_through_json():
    settings, _ = read_backup(json.dumps(make_backup(SETTINGS)))
    assert settings["min_seeders"] == 2


@pytest.mark.parametrize("payload", [
    "{}", '{"kind": "something-else"}', '{"kind": "romarr-backup"}', "[]",
])
def test_restoring_something_that_is_not_a_backup_is_refused(payload):
    with pytest.raises(ValueError):
        read_backup(payload)


# --- export ----------------------------------------------------------------

def test_csv_survives_a_title_with_a_comma():
    """Game titles contain commas, quotes and apostrophes. Hand-rolled CSV
    corrupts exactly those rows and looks correct for everything else."""
    rows = [{"title": "Ratchet & Clank: Up Your Arsenal, Special", "platform": "ps2"}]
    text = to_csv(rows)
    assert from_csv(text)[0]["title"] == rows[0]["title"]


def test_csv_survives_quotes_and_newlines():
    rows = [{"title": 'He said "hi"\nagain', "platform": "snes"}]
    assert from_csv(to_csv(rows))[0]["title"] == rows[0]["title"]


def test_csv_uses_rfc_4180_line_endings():
    assert to_csv([{"a": "1"}]).endswith("\r\n")


def test_csv_columns_are_stable_across_rows_with_different_keys():
    text = to_csv([{"a": 1}, {"b": 2}])
    assert text.splitlines()[0] == "a,b"


def test_csv_of_nothing_is_still_valid():
    assert to_csv([], ["title"]).strip() == "title"
