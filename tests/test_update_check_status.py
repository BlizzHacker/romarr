"""Update-check status and manual safety regressions at the service boundary."""

import threading

import requests

from romarr import selfupdate
from romarr.app import ROMarr


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
    svc = _svc(tmp_path)
    svc.store.settings["_latest_version"] = "9.9.9"
    svc.store.settings["_update_available"] = True
    status = svc.update_status()
    assert status["check_status"]["state"] == "never_checked"
    assert status["update_available"] is False
    assert svc.stats()["update_available"] is False


def test_manual_command_cannot_notify_or_auto_apply(tmp_path, monkeypatch):
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
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    notified, applied = [], []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(svc, "apply_update", lambda: applied.append(True)
                        or {"ok": True, "message": "started"})
    assert svc.check_update()["ok"]
    assert svc.check_update()["ok"]
    assert len(notified) == 1
    assert len(applied) == 2  # Existing once-per-daily-sweep apply semantics.


def test_failed_check_clears_availability_without_erasing_last_success(
        tmp_path, monkeypatch):
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
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "0.0.0+unknown")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Release())
    out = svc.run_command("UpdateCheck")
    assert out["indeterminate"]
    assert svc.update_status()["check_status"]["state"] == "unknown"
    assert svc.update_status()["update_available"] is False


def test_nightly_channel_requires_separate_revision_check(tmp_path, monkeypatch):
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
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    started, release = threading.Event(), threading.Event()
    count = []
    def blocking_get(*args, **kwargs):
        count.append(1)
        started.set()
        assert release.wait(5)
        return Release()
    monkeypatch.setattr(requests, "get", blocking_get)
    errors = []
    def run_first():
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
    svc = _svc(tmp_path)
    monkeypatch.setattr("romarr.app.VERSION", "1.8.1")
    svc.store.settings["auto_update"] = True
    notified, applied = [], []
    monkeypatch.setattr(svc, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(svc, "apply_update", lambda: applied.append(True))

    def switch_channel(*args, **kwargs):
        svc.store.settings["update_channel"] = "nightly"
        return Release()

    monkeypatch.setattr(requests, "get", switch_channel)
    out = svc.check_update()
    assert out["indeterminate"]
    assert not notified and not applied
    status = svc.update_status()
    assert status["check_status"]["reason"] == "build_or_channel_changed"
    assert status["update_available"] is False
