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
    """Create a service with a disposable state file and a stopped scheduler."""
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json")})


class Release:
    """Simulate a successful GitHub latest-release JSON response."""

    status_code = 200
    headers = {}

    def raise_for_status(self):
        """Emulate the no-error path of requests.Response."""
        return None

    def json(self):
        """Provide a fixed, parseable release newer than the pinned test build."""
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
    assert applied == [
        {"expected_channel": "stable", "expected_release": "1.8.2"},
        {"expected_channel": "stable", "expected_release": "1.8.2"},
    ]


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
    monkeypatch.setattr(requests, "get", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("nightly checks must not query stable releases")))
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


def test_source_revision_suffix_is_not_a_stable_release(tmp_path, monkeypatch):
    """A local +gSHA build identity cannot authorize a stable-tag update."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1+gabc123")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    attempted = []
    monkeypatch.setattr(svc, "notify", lambda *a: attempted.append("notify"))
    monkeypatch.setattr(svc, "apply_update", lambda **kw: attempted.append("apply"))
    result = svc.check_update()
    assert result["indeterminate"]
    assert svc.update_status()["check_status"]["reason"] == "running_version_unknown"
    assert svc.update_status()["settings"]["auto_update_effective"] is False
    assert attempted == []


def test_known_version_development_provenance_is_not_stable_eligible(
        tmp_path, monkeypatch):
    """A source-ref development image cannot be equated with a release tag."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    monkeypatch.setattr("romarr.app.get_build_info",
                        lambda version: {"type": "development", "version_known": True})
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    launches = []
    monkeypatch.setattr(svc, "apply_update", lambda **kw: launches.append(kw))
    out = svc.check_update()
    assert out["indeterminate"]
    st = svc.update_status()
    assert st["check_status"]["reason"] == "development_build"
    assert st["settings"]["auto_update_effective"] is False
    assert launches == []


def test_disabled_daily_check_is_not_effective_auto_apply(tmp_path, monkeypatch):
    """An opted-in auto-update cannot run if daily checking is switched off."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    monkeypatch.setattr(selfupdate, "detect", lambda root=None:
                        selfupdate.Environment(kind="git", repo=str(tmp_path),
                                               branch="main", commit="abc"))
    svc.store.settings.update({"update_check": False, "auto_update": True})
    status = svc.update_status()
    assert status["settings"]["auto_update"] is True
    assert status["settings"]["update_check"] is False
    assert status["settings"]["auto_update_effective"] is False
    assert status["settings"]["auto_update_reason"] == "update_check_disabled"


def test_scheduled_check_waits_for_manual_check_and_applies_once(
        tmp_path, monkeypatch):
    """A scheduled daily sweep must not be discarded behind manual Check now."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    fetched, release, lock_attempted, finished = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event())
    requests_seen, applied, errors, answers = [], [], [], []

    real_lock = svc._update_check_lock

    class ObservedLock:
        """Watch the scheduled acquisition without changing mutex behavior."""

        def acquire(self, blocking=True):
            """Signal only when the scheduler tries a held mutex."""
            if blocking and real_lock.locked():
                lock_attempted.set()
            return real_lock.acquire(blocking=blocking)

        def release(self):
            """Release the actual service mutex."""
            return real_lock.release()

        def locked(self):
            """Report whether the actual service mutex is held."""
            return real_lock.locked()

    svc._update_check_lock = ObservedLock()

    def controlled_get(*args, **kwargs):
        """Hold only the first (manual) GitHub request during overlap."""
        requests_seen.append(True)
        if len(requests_seen) == 1:
            fetched.set()
            assert release.wait(5)
        return Release()

    def manual():
        """Run the first check without applying a release."""
        try:
            answers.append(svc.run_command("UpdateCheck"))
        except BaseException as err:
            errors.append(err)

    def scheduled():
        """Start the daily sweep while manual checking owns its lock."""
        try:
            answers.append(svc.check_update())
        except BaseException as err:
            errors.append(err)
        finally:
            finished.set()

    monkeypatch.setattr(requests, "get", controlled_get)
    monkeypatch.setattr(svc, "notify", lambda *a: [])
    monkeypatch.setattr(svc, "apply_update",
                        lambda **kwargs: applied.append(kwargs)
                        or {"ok": True, "message": "started"})
    first = threading.Thread(target=manual)
    second = threading.Thread(target=scheduled)
    first.start()
    try:
        assert fetched.wait(5)
        second.start()
        assert lock_attempted.wait(5), "scheduled check never reached the mutex"
        assert not finished.is_set(), "scheduled check bypassed the held mutex"
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert len(requests_seen) == 2
    assert all(answer["ok"] for answer in answers)
    assert applied == [{"expected_channel": "stable",
                        "expected_release": "1.8.2"}]


def test_disabling_scheduled_check_mid_request_suppresses_notifications_and_apply(
        tmp_path, monkeypatch):
    """A preference change during GitHub fetch must stop scheduled side effects."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings.update({"update_check": True, "auto_update": True})
    notified, applied = [], []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(svc, "apply_update", lambda **kw: applied.append(kw))

    def disable_during_fetch(*args, **kwargs):
        """Simulate an operator disabling the daily check before the reply."""
        with svc._lock:
            svc.store.settings["update_check"] = False
        return Release()

    monkeypatch.setattr(requests, "get", disable_during_fetch)
    result = svc.check_update()
    assert result["ok"] and result["state"] == "available"
    assert not notified and not applied
    assert svc.store.settings.get("_update_notified") != "1.8.2"
    assert svc.update_status()["settings"]["auto_update_effective"] is False


def test_disabling_scheduled_check_at_notification_blocks_git_launch(
        tmp_path, monkeypatch):
    """The guarded launcher must reject a disabled schedule after notification."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings.update({"update_check": True, "auto_update": True})
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    env = selfupdate.Environment(kind="git", repo=str(tmp_path), branch="main",
                                 python="python3", commit="abc", service="romarr")
    monkeypatch.setattr(selfupdate, "detect", lambda root=None: env)
    launches = []
    monkeypatch.setattr(selfupdate, "launch",
                        lambda *args: launches.append(args) or {"ok": True})

    def disable_after_notice(_message):
        """Disable the scheduler while the notification callback is running."""
        with svc._lock:
            svc.store.settings["update_check"] = False
        return []

    monkeypatch.setattr(svc, "notify", disable_after_notice)
    result = svc.check_update()
    assert result["ok"]
    assert launches == []


def test_explicit_scheduled_apply_checks_enabled_flag_under_launch_lock(
        tmp_path, monkeypatch):
    """A previously validated release cannot apply after daily checks are off."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings.update({
        "auto_update": True, "update_check": False,
        "update_channel": "stable", "_latest_version": "1.8.2"})
    env = selfupdate.Environment(kind="git", repo=str(tmp_path), branch="main",
                                 python="python3", commit="abc", service="romarr")
    monkeypatch.setattr(selfupdate, "detect", lambda root=None: env)
    launched = []
    monkeypatch.setattr(selfupdate, "launch",
                        lambda *args: launched.append(args) or {"ok": True})
    result = svc.apply_update(expected_channel="stable",
                              expected_release="1.8.2")
    assert result["ok"] is False
    assert "scheduled update preference" in result["error"]
    assert launched == []

def test_changing_channel_after_record_does_not_reserve_notice(
        tmp_path, monkeypatch):
    """Never mark a stable notification sent after switching to nightly."""
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings.update({"update_channel": "stable",
                               "update_check": True, "auto_update": False})
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    notified = []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))

    original_record = svc._record_update_check

    def change_after_record(*args, **kwargs):
        """Change the channel after the status is recorded, before notice."""
        original_record(*args, **kwargs)
        if args[0] == "available":
            with svc._lock:
                svc.store.settings["update_channel"] = "nightly"

    monkeypatch.setattr(svc, "_record_update_check", change_after_record)
    result = svc.check_update()
    assert result["ok"]
    assert not notified
    assert svc.store.settings.get("_update_notified") != "1.8.2"
    assert svc.update_status()["check_status"]["state"] == "unknown"
