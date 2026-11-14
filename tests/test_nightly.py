"""Read-only official Nightly revision comparison tests, with no live GitHub calls."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from romarr import nightly, selfupdate
from romarr.app import ROMarr
from romarr.scheduler import Scheduler


RUNNING = "a" * 40
LATEST = "b" * 40
OFFICIAL = {"type": "development", "source_repository": "BlizzHacker/romarr",
            "source_ref": "refs/heads/main", "source_revision": RUNNING,
            "version_known": False}


class Reply:
    """Provide the small GitHub JSON contract expected by the comparator."""

    def __init__(self, body, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        """Fail like requests when an HTTP status is not successful."""
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        """Return the response body without making a network request."""
        return self.body


def _response_get(*replies):
    """Supply and count a bounded sequence of fake GitHub responses."""
    seen = []

    def get(url, *, timeout, headers):
        """Ensure every request is bounded, official and read-only."""
        assert url.startswith("https://api.github.com/repos/BlizzHacker/romarr/")
        assert timeout == 10
        assert headers["Accept"] == "application/vnd.github+json"
        seen.append(url)
        return replies[len(seen) - 1]

    return get, seen


def _comparison(status="ahead", ahead=1, behind=0, merge=RUNNING):
    """Build one ancestry response with an exact checked base commit."""
    return Reply({"status": status, "ahead_by": ahead, "behind_by": behind,
                  "base_commit": {"sha": RUNNING},
                  "merge_base_commit": {"sha": merge}})


def test_validated_official_image_without_git_checkout():
    """Generated official image provenance is sufficient for read-only checks."""
    assert nightly.running_main_revision(OFFICIAL) == RUNNING
    assert nightly.running_main_revision({**OFFICIAL, "source_ref": "refs/pull/12/head"}) == ""
    assert nightly.running_main_revision({**OFFICIAL, "source_repository": "fork/romarr"}) == ""
    assert nightly.running_main_revision({**OFFICIAL, "source_revision": "abc123"}) == ""


def test_missing_git_during_status_probe_cannot_verify_checkout(
        monkeypatch, tmp_path):
    """A disappearing Git executable must not make a failed probe look clean."""
    env = SimpleNamespace(kind="git", repo=str(tmp_path), branch="main",
                          commit=RUNNING[:8])
    answers = {"rev-parse --show-toplevel": str(tmp_path),
               "symbolic-ref --quiet --short HEAD": "main",
               "remote get-url origin": "https://github.com/BlizzHacker/romarr.git",
               "rev-parse --verify HEAD": RUNNING}
    actual_probe = nightly._local_git

    def probe(repo, *args):
        """Model Git disappearing after earlier identity probes succeed."""
        if args[0] == "status":
            monkeypatch.setattr(nightly.shutil, "which", lambda _: None)
            return actual_probe(repo, *args)
        return answers[" ".join(args)]

    monkeypatch.setattr(nightly, "_local_git", probe)
    assert nightly.git_checkout_revision(env) == ""


def test_clean_verified_git_checkout_uses_full_sha(monkeypatch, tmp_path):
    """Git/source installs can use official HEAD without generated metadata."""
    env = SimpleNamespace(kind="git", repo=str(tmp_path), branch="main",
                          commit=RUNNING[:8])
    answers = {"rev-parse --show-toplevel": str(tmp_path),
               "symbolic-ref --quiet --short HEAD": "main",
               "remote get-url origin": "https://github.com/BlizzHacker/romarr.git",
               "status --porcelain --untracked-files=all": "",
               "rev-parse --verify HEAD": RUNNING}
    monkeypatch.setattr(nightly, "_local_git",
                        lambda repo, *args: answers[" ".join(args)])
    startup = nightly.git_checkout_revision(env)
    assert startup == RUNNING
    assert nightly.running_main_revision({}, env, startup_git_revision=startup) == RUNNING
    answers["status --porcelain --untracked-files=all"] = "?? operator.txt"
    assert nightly.running_main_revision({}, env, startup_git_revision=startup) == ""
    answers["status --porcelain --untracked-files=all"] = None
    assert nightly.running_main_revision({}, env, startup_git_revision=startup) == ""
    answers["status --porcelain --untracked-files=all"] = ""
    answers["remote get-url origin"] = "https://github.com/untrusted/romarr"
    assert nightly.running_main_revision({}, env, startup_git_revision=startup) == ""
    answers["remote get-url origin"] = "https://github.com/BlizzHacker/romarr.git"
    answers["rev-parse --verify HEAD"] = LATEST
    env.commit = LATEST[:8]
    assert nightly.running_main_revision({}, env, startup_git_revision=startup) == ""


def test_same_commit_is_current_and_uses_one_request():
    """Equal full SHAs do not require a second ancestry API request."""
    get, calls = _response_get(Reply({"sha": RUNNING}))
    out = nightly.compare_main_revision(RUNNING, get, {"Accept": "application/vnd.github+json"})
    assert out == {"state": "current", "reason": "nightly_revision_equal",
                   "latest_revision": RUNNING}
    assert len(calls) == 1


def test_newer_main_is_available_only_with_proven_ancestry():
    """Require a newer descendant and zero commits behind."""
    get, calls = _response_get(Reply({"sha": LATEST}), _comparison())
    out = nightly.compare_main_revision(RUNNING, get, {"Accept": "application/vnd.github+json"})
    assert out["state"] == "available"
    assert out["latest_revision"] == LATEST
    assert calls[1].endswith(RUNNING + "..." + LATEST)


@pytest.mark.parametrize("answer", [
    _comparison(status="diverged", ahead=3, behind=2, merge="c" * 40),
    _comparison(status="behind", ahead=0, behind=1, merge=LATEST),
    _comparison(status="ahead", ahead=1, behind=0, merge="c" * 40),
    Reply({"status": "ahead", "ahead_by": True, "behind_by": 0,
           "base_commit": {"sha": RUNNING}, "merge_base_commit": {"sha": RUNNING}}),
    Reply({"status": "ahead", "ahead_by": 2, "behind_by": 0,
           "base_commit": {"sha": "x" * 40}, "merge_base_commit": {"sha": RUNNING}}),
])
def test_diverged_ahead_or_invalid_comparisons_never_offer_update(answer):
    """Mismatched or inconclusive Git histories must fail closed."""
    get, _ = _response_get(Reply({"sha": LATEST}), answer)
    out = nightly.compare_main_revision(RUNNING, get, {"Accept": "application/vnd.github+json"})
    assert out["state"] == "unknown"
    assert out.get("latest_revision", "") != RUNNING


@pytest.mark.parametrize("response,expected", [
    (Reply({}, 403), "request_denied"),
    (Reply({}, 403, {"X-RateLimit-Remaining": "0"}), "rate_limited"),
    (Reply({}, 429), "rate_limited"),
    (Reply({"sha": "short"}), "invalid_revision"),
    (Reply(["not", "object"]), "invalid_response"),
])
def test_http_and_untrusted_responses_report_no_update(response, expected):
    """Never represent API denial or malformed metadata as a newer commit."""
    get, _ = _response_get(response)
    out = nightly.compare_main_revision(RUNNING, get, {"Accept": "application/vnd.github+json"})
    assert out["reason"] == expected
    assert out["state"] in ("unknown", "error")
    assert not out.get("latest_revision")


def test_transport_error_is_not_an_update():
    """Surface network failure without accidentally offering an upgrade."""
    def fail(*args, **kwargs):
        """Model a bounded connection failure."""
        raise requests.ConnectionError("private test details")
    out = nightly.compare_main_revision(RUNNING, fail, {})
    assert out["state"] == "error"
    assert out["reason"] == "request_failed"
    assert "private test details" not in out["error"]


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Create a disposable service and disable the persistent task scheduler."""
    monkeypatch.setattr(Scheduler, "start", lambda self: None)
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json")})
    svc.store.settings.update({"update_channel": "nightly",
                               "auto_update": True, "update_check": True})
    monkeypatch.setattr("romarr.app.VERSION", "0.0.0+unknown")
    monkeypatch.setattr("romarr.app.get_build_info", lambda version: dict(OFFICIAL))
    monkeypatch.setattr(selfupdate, "detect", lambda root=None:
                        selfupdate.Environment(kind="docker", detail="image"))
    return svc


def test_manual_nightly_status_uses_verified_revision_and_never_applies(
        service, monkeypatch):
    """Report a new Nightly in the existing API without launching updates."""
    get, calls = _response_get(Reply({"sha": LATEST}), _comparison())
    monkeypatch.setattr(requests, "get", get)
    invoked = []
    monkeypatch.setattr(service, "apply_update", lambda **k: invoked.append(k))
    monkeypatch.setattr(service, "notify", lambda *a: invoked.append(a))
    result = service.run_command("UpdateCheck")
    status = service.update_status()
    assert result["ok"] and result["state"] == "available"
    assert status["check_status"]["state"] == "available"
    assert status["check_status"]["latest_revision"] == LATEST
    assert status["check_status"]["running_revision"] == RUNNING
    assert status["update_available"] is True
    assert status["settings"]["auto_update_effective"] is False
    assert len(calls) == 2
    assert not invoked


def test_scheduled_nightly_check_never_applies_or_notifies(service, monkeypatch):
    """An enabled daily check cannot schedule a Nightly installation."""
    get, _ = _response_get(Reply({"sha": RUNNING}))
    monkeypatch.setattr(requests, "get", get)
    invoked = []
    monkeypatch.setattr(service, "apply_update", lambda **k: invoked.append(k))
    monkeypatch.setattr(service, "notify", lambda *a: invoked.append(a))
    result = service.check_update(manual=False)
    assert result["state"] == "current"
    assert not invoked


def test_changed_channel_invalidates_in_flight_nightly(service, monkeypatch):
    """Do not persist availability if settings change during the HTTP check."""
    def change(url, **kwargs):
        """Switch channel after the verified GitHub request begins."""
        service.store.settings["update_channel"] = "stable"
        return Reply({"sha": RUNNING})
    monkeypatch.setattr(requests, "get", change)
    outcome = service.run_command("UpdateCheck")
    assert outcome["state"] == "unknown"
    assert service.update_status()["update_available"] is False


def test_changed_running_build_invalidates_persisted_nightly(service, monkeypatch):
    """A previous successful verdict must not survive a new running SHA."""
    get, _ = _response_get(Reply({"sha": RUNNING}))
    monkeypatch.setattr(requests, "get", get)
    assert service.run_command("UpdateCheck")["state"] == "current"
    monkeypatch.setattr("romarr.app.get_build_info",
                        lambda version: {**OFFICIAL, "source_revision": LATEST})
    status = service.update_status()
    assert status["check_status"]["state"] == "unknown"
    assert status["check_status"]["reason"] == "nightly_build_changed"


def test_service_captures_git_revision_at_construction(tmp_path, monkeypatch):
    """A Git checkout's running SHA is captured once, before later Git movement."""
    env = selfupdate.Environment(kind="git", repo=str(tmp_path), branch="main",
                                 commit=RUNNING[:8])
    monkeypatch.setattr(selfupdate, "detect", lambda root=None: env)
    observed = []
    def capture(value):
        """Record the startup capture, which must not update after service init."""
        observed.append(value)
        return RUNNING
    monkeypatch.setattr(nightly, "git_checkout_revision", capture)
    monkeypatch.setattr(Scheduler, "start", lambda self: None)
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "service.json")})
    assert svc._nightly_git_start_revision == RUNNING
    assert observed == [env]
