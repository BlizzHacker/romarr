"""Whether ROMarr actually lets ROM Hub confine a plugin.

ROM Hub runs each plugin as a subprocess with no library token, no filesystem
mount and no sockets of its own; its only route out is an RPC back to the host,
checked against the hosts that plugin declared. A seccomp filter enforces it.

ROMarr set `ROM_HUB_ALLOW_UNSANDBOXED=1` unconditionally, on the belief that
its container could not confine anything. That belief was wrong -- the filter
needs Linux and `pyseccomp`, both perfectly ordinary, and neither was a
container restriction. The Hub's own documentation calls that flag "no
confinement at all ... a development convenience, never a deployment setting",
and ROMarr was setting it in production.

So the flag is now asked for rather than assumed, and these pin that it is
never set while the sandbox is available.
"""

from __future__ import annotations

import pytest

from romarr import hub


def test_no_opt_out_when_the_sandbox_is_available(monkeypatch):
    """The regression that matters. Setting this turns the boundary off."""
    monkeypatch.setattr(hub, "sandbox_state",
                        lambda: (True, "seccomp filter available"))
    env = hub._plugin_env()
    assert "ROM_HUB_ALLOW_UNSANDBOXED" not in env, (
        "ROMarr disabled ROM Hub's confinement while it was available")


@pytest.mark.parametrize("flag", [None, "0", "true", "yes"])
def test_missing_sandbox_fails_closed_without_exact_opt_in(monkeypatch, flag):
    """An old Hub flag or a vaguely truthy value must not authorize fallback."""
    monkeypatch.setattr(hub, "_confinement_possible", lambda: True)
    monkeypatch.setattr(hub, "sandbox_state", lambda: (False, "pyseccomp is not installed"))
    monkeypatch.setenv("ROM_HUB_ALLOW_UNSANDBOXED", "1")
    if flag is None:
        monkeypatch.delenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", raising=False)
    else:
        monkeypatch.setenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", flag)
    with pytest.raises(RuntimeError, match="ROMARR_ALLOW_UNSANDBOXED_PLUGINS=1"):
        hub._plugin_env()
    with pytest.raises(RuntimeError, match="confinement is unavailable"):
        hub._unsandboxed()


def test_explicit_fallback_is_logged_and_passed_to_cli(monkeypatch, caplog):
    """The compatibility escape hatch must be deliberate and visible."""
    monkeypatch.setattr(hub, "_confinement_possible", lambda: True)
    monkeypatch.setattr(hub, "sandbox_state", lambda: (False, "pyseccomp is not installed"))
    monkeypatch.setenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", "1")
    with caplog.at_level("WARNING"):
        assert hub._plugin_env()["ROM_HUB_ALLOW_UNSANDBOXED"] == "1"
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "WITHOUT confinement" in said and "explicit operator choice" in said


def test_refused_cli_never_starts_a_subprocess(monkeypatch):
    """Return the existing CLI error shape before any worker can start."""
    import subprocess
    monkeypatch.delenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", raising=False)
    monkeypatch.setattr(hub, "_confinement_possible", lambda: True)
    monkeypatch.setattr(hub, "sandbox_state", lambda: (False, "no seccomp"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("refused CLI started"))
    result = hub.install("example")
    assert not result["ok"] and result["code"] == -1
    assert "ROMARR_ALLOW_UNSANDBOXED_PLUGINS" in result["err"]


def test_a_broken_probe_fails_closed_rather_than_claiming_safety(monkeypatch):
    """An exception must not read as "sandbox fine"."""
    def explode():
        raise RuntimeError("boom")
    monkeypatch.setattr(hub, "sandbox_state", explode)
    with pytest.raises(RuntimeError):
        hub._plugin_env()


def test_sandbox_state_reports_rather_than_raising(monkeypatch):
    """It is called on a page render; an unavailable Hub is a thing to show."""
    ok, why = hub.sandbox_state()
    assert isinstance(ok, bool) and isinstance(why, str) and why


def test_a_probe_error_says_what_runtime_piece_is_missing(monkeypatch):
    """A bare ``RuntimeError`` gave an Unraid operator nothing to fix."""
    import sys
    import types

    package = types.ModuleType("rom_hub")
    package.__path__ = []
    sandbox = types.ModuleType("rom_hub.sandbox")
    sandbox.probe = lambda: (_ for _ in ()).throw(
        RuntimeError("Unable to find libseccomp"))
    monkeypatch.setitem(sys.modules, "rom_hub", package)
    monkeypatch.setitem(sys.modules, "rom_hub.sandbox", sandbox)
    ok, why = hub.sandbox_state()
    assert ok is False
    assert "RuntimeError: Unable to find libseccomp" in why


def test_credentials_are_still_withheld_either_way(monkeypatch):
    """Confinement and the environment allowlist are separate boundaries, and
    turning one on must not quietly relax the other."""
    monkeypatch.setenv("ROMARR_API_KEY", "romarr-secret")
    monkeypatch.setenv("QBITTORRENT_PASS", "qbit-secret")
    monkeypatch.setenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", "1")
    for state in ((True, "ok"), (False, "pyseccomp is not installed")):
        monkeypatch.setattr(hub, "sandbox_state", lambda s=state: s)
        env = hub._plugin_env()
        assert "ROMARR_API_KEY" not in env
        assert "romarr-secret" not in set(env.values())
        assert "qbit-secret" not in set(env.values())


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_systems_without_seccomp_keep_plugins_with_a_warning(monkeypatch, caplog, platform):
    """Windows and macOS cannot confine plugins; refusing would only remove the feature."""
    monkeypatch.setattr(hub.sys, "platform", platform)
    monkeypatch.delenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", raising=False)
    monkeypatch.setattr(hub, "sandbox_state", lambda: (False, f"seccomp requires linux; this is {platform}"))
    with caplog.at_level("WARNING"):
        assert hub._plugin_env()["ROM_HUB_ALLOW_UNSANDBOXED"] == "1"
    assert "Linux-only" in " ".join(r.getMessage() for r in caplog.records)


def test_linux_without_seccomp_still_refuses(monkeypatch):
    monkeypatch.setattr(hub.sys, "platform", "linux")
    monkeypatch.delenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", raising=False)
    monkeypatch.setattr(hub, "sandbox_state", lambda: (False, "pyseccomp is not installed"))
    with pytest.raises(RuntimeError, match="ROMARR_ALLOW_UNSANDBOXED_PLUGINS=1"):
        hub._plugin_env()
