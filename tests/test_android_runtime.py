"""Packaged Android must not offer desktop process installers or self-updates."""
import pytest
from romarr import browser, hub, mdm, selfupdate


def test_android_bundle_cannot_self_modify_even_beside_git(tmp_path, monkeypatch):
    """App distributor owns bundled code, regardless of nearby Git metadata."""
    monkeypatch.setenv("ROMARR_ANDROID", "1")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(selfupdate, "_container_marker", lambda: True)
    result = selfupdate.detect(tmp_path)
    assert not result.can_apply and result.kind == "none"
    assert "APK" in result.detail


def test_android_hub_refuses_before_starting_a_process(monkeypatch):
    """Even an accidentally installed host cannot spawn desktop Python plugins."""
    monkeypatch.setenv("ROMARR_ANDROID", "1")
    def forbidden(*args, **kwargs):
        """Fail if the process boundary is crossed."""
        raise AssertionError("must not spawn")
    import subprocess
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert not hub.available()
    assert not hub._run_cli("install", "example")["ok"]


def test_android_mdm_does_not_offer_desktop_binary(monkeypatch):
    """Embedded Android can report Linux without being a Linux desktop."""
    monkeypatch.setenv("ROMARR_ANDROID", "1")
    monkeypatch.setattr(mdm.sys, "platform", "linux")
    result = mdm.asset_spec()
    assert not result["installable"] and not result["asset"]


def test_android_browser_reports_actual_missing_capability(monkeypatch):
    """Driver installation instructions for desktops do not apply to Android."""
    monkeypatch.setenv("ROMARR_ANDROID", "1")
    with pytest.raises(browser.Unavailable, match="Android"):
        browser._playwright()
