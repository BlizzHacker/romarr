"""Update-check status and manual safety regressions at the service boundary."""

import threading

import pytest
import requests

from romarr import selfupdate
from romarr.app import ROMarr
from romarr.scheduler import Scheduler


@pytest.fixture(autouse=True)
def _disable_scheduler(monkeypatch):
    """Keep each test service from spawning a long-lived daily scheduler."""
    monkeypatch.setattr(Scheduler, "start", lambda self: None)


def _svc(tmp_path):
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json")})


class Release:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"tag_name": "v1.8.2", "html_url": "https://example.test/release"}


def test_initial_status_is_never_checked_even_with_legacy_cached_flag(tmp_path):
    """Ignore legacy cached availability until a validated comparison exists."""
    svc = _svc(tmp_path)
    svc.store.settings["_latest_version"] = "9.9.9"
    svc.store.settings["_update_available"] = True
    status = svc.update_status()
    assert status["check_status"]["state"] == "never_checked"
    assert status["update_available"] is False
    assert svc.stats()["update_available"] is False


def test_manual_command_cannot_notify_or_auto_apply(tmp_path, monkeypatch):
    """Keep an operator-triggered check from notifying or launching an update."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    notified, applied = [], []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(svc, "apply_update", lambda: applied.append(True))
    out = svc.run_command("UpdateCheck")
    assert out["ok"] and out["state"] == "available"
    assert notified == [] and applied == []
    status = svc.update_status()
    assert status["check_status"]["state"] == "available"
    assert status["check_status"]["last_success_at"]
    assert status["check_status"]["last_attempt_at"]


def test_scheduled_check_preserves_opted_in_apply_and_notify_once(tmp_path, monkeypatch):
    """Preserve scheduled application and one notification per release."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    notified, applied = [], []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(svc, "apply_update", lambda **kwargs: applied.append(kwargs)
                        or {"ok": True, "message": "started"})
    assert svc.check_update()["ok"]
    assert svc.check_update()["ok"]
    assert len(notified) == 1
    assert len(applied) == 2  # Existing once-per-daily-sweep apply semantics.


def test_failed_check_clears_availability_without_erasing_last_success(
        tmp_path, monkeypatch):
    """Keep the previous successful comparison while recording network failure."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    monkeypatch.setattr(svc, "notify", lambda *a: [])
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    assert svc.run_command("UpdateCheck")["ok"]
    before = svc.update_status()["check_status"]["last_success_at"]
    assert svc.update_status()["update_available"]
    monkeypatch.setattr(requests, "get", lambda *a, **kw: (_ for _ in ()).throw(
        requests.ConnectionError("offline")))
    out = svc.run_command("UpdateCheck")
    status = svc.update_status()
    assert out["ok"] is False
    assert status["check_status"]["state"] == "error"
    assert status["check_status"]["reason"] == "request_failed"
    assert status["check_status"]["last_success_at"] == before
    assert status["check_status"]["last_error"]
    assert status["latest_version"] == "1.8.2"
    assert status["update_available"] is False
    assert svc.stats()["update_available"] is False


def test_unknown_build_has_unknown_state_not_current(tmp_path, monkeypatch):
    """Expose unversioned builds as unknown rather than current."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "0.0.0+unknown")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    out = svc.run_command("UpdateCheck")
    assert out["indeterminate"]
    assert svc.update_status()["check_status"]["state"] == "unknown"
    assert svc.update_status()["update_available"] is False


def test_nightly_channel_requires_separate_revision_check(tmp_path, monkeypatch):
    """Avoid comparing a nightly checkout with a stable release tag."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings.update({"update_channel": "nightly", "auto_update": True})
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    monkeypatch.setattr(svc, "apply_update", lambda: (_ for _ in ()).throw(
        AssertionError("nightly check must not auto-apply a stable tag")))
    out = svc.check_update()
    assert out["indeterminate"]
    st = svc.update_status()
    assert st["check_status"]["reason"] == "nightly_revision_not_checked"
    assert st["settings"]["auto_update"] is True
    assert st["settings"]["auto_update_effective"] is False


def test_docker_configured_auto_apply_is_not_effective(tmp_path, monkeypatch):
    """Expose a saved preference without claiming Docker can self-update."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(selfupdate, "detect", lambda root=None:
                        selfupdate.Environment(kind="docker", detail="image"))
    status = svc.update_status()
    assert status["settings"]["auto_update"] is True
    assert status["settings"]["auto_update_effective"] is False
    assert status["settings"]["auto_update_reason"] == "install_cannot_self_update"


def test_in_flight_manual_check_returns_busy_without_second_request(
        tmp_path, monkeypatch):
    """Reject overlapping manual checks without extra network requests."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    started, release = threading.Event(), threading.Event()
    count = []
    def blocking_get(*args, **kwargs):
        """Hold the first fetch until another request attempts the check."""
        count.append(1)
        started.set()
        assert release.wait(5)
        return Release()
    monkeypatch.setattr(requests, "get", blocking_get)
    errors = []
    def run_first():
        """Collect any failure from the in-flight worker thread."""
        try:
            svc.run_command("UpdateCheck")
        except BaseException as err:
            errors.append(err)
    t = threading.Thread(target=run_first)
    t.start()
    try:
        assert started.wait(5)
        second = svc.run_command("UpdateCheck")
        assert second["busy"] is True
        assert svc.update_status()["check_status"]["in_progress"] is True
    finally:
        release.set()
        t.join(5)
    assert not t.is_alive()
    assert not errors
    assert count == [1]


def test_saved_verdict_is_invalid_after_channel_or_build_change(
        tmp_path, monkeypatch):
    """Invalidate previous verdicts after changing the comparator identity."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    assert svc.run_command("UpdateCheck")["ok"]
    svc.store.settings["update_channel"] = "nightly"
    assert svc.update_status()["check_status"]["state"] == "unknown"
    assert svc.update_status()["update_available"] is False
    svc.store.settings["update_channel"] = "stable"
    monkeypatch.setattr("romarr.app.VERSION", "1.8.3")
    assert svc.update_status()["check_status"]["reason"] == "build_or_channel_changed"
    assert svc.update_status()["update_available"] is False


def test_invalid_release_payload_does_not_overwrite_known_latest(
        tmp_path, monkeypatch):
    """Reject malformed GitHub metadata without destroying cached release data."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    assert svc.run_command("UpdateCheck")["ok"]
    class Broken(Release):
        def json(self):
            return {"tag_name": "not-a-release"}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Broken())
    out = svc.run_command("UpdateCheck")
    assert out["reason"] == "invalid_release"
    assert svc.update_status()["latest_version"] == "1.8.2"
    assert svc.update_status()["update_available"] is False


def test_channel_change_during_network_request_never_applies_old_result(
        tmp_path, monkeypatch):
    """Discard an in-flight check when its configured channel changes."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    notified, applied = [], []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(svc, "apply_update", lambda: applied.append(True))

    def switch_channel(*args, **kwargs):
        """Change the channel before returning a successful release response."""
        svc.store.settings["update_channel"] = "nightly"
        return Release()

    monkeypatch.setattr(requests, "get", switch_channel)
    out = svc.check_update()
    assert out["indeterminate"]
    assert not notified and not applied
    status = svc.update_status()
    assert status["check_status"]["reason"] == "build_or_channel_changed"
    assert status["update_available"] is False


def test_notify_channel_switch_blocks_unchecked_nightly_launch(
        tmp_path, monkeypatch):
    """Notification-time channel changes cannot redirect a stable update."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    env = selfupdate.Environment(kind="git", repo=str(tmp_path), branch="main",
                                 python="python3", commit="abc", service="romarr")
    monkeypatch.setattr(selfupdate, "detect", lambda root=None: env)
    launched = []
    monkeypatch.setattr(selfupdate, "launch",
                        lambda *a: launched.append(a) or {"ok": True})

    def change_channel(_message):
        """Simulate a preference change after the release was checked."""
        svc.store.settings["update_channel"] = "nightly"
        return []

    monkeypatch.setattr(svc, "notify", change_channel)
    outcome = svc.check_update()
    assert outcome["ok"] is True
    assert "auto-update did not run" in outcome["message"]
    assert launched == []


def test_unknown_unparseable_running_version_never_offers_update(
        tmp_path, monkeypatch):
    """Malformed local version data cannot compare as an old release."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "development")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    triggered = []
    monkeypatch.setattr(svc, "notify", lambda *a: triggered.append(True))
    monkeypatch.setattr(svc, "apply_update", lambda: triggered.append(True))
    result = svc.check_update()
    assert result["indeterminate"]
    status = svc.update_status()
    assert status["check_status"]["state"] == "unknown"
    assert status["settings"]["auto_update_effective"] is False
    assert not triggered
