"""What a plugin subprocess is handed.

ROM Hub plugins are code, and running one means running it with ROMarr's
privileges -- there is no sandbox and SECURITY.md says so. That makes the
environment the one boundary actually being enforced, so it is worth a test.

It used to be `dict(os.environ, ...)`: every plugin inherited ROMARR_API_KEY,
ROMARR_PASSWORD, PROWLARR_API_KEY, QBITTORRENT_PASS and LIBRARY_PASSWORD. A
plugin needs somewhere to import into. It does not need the key to ROMarr
itself or the password to the torrent client.
"""

from __future__ import annotations

import pathlib

import pytest

from romarr import hub


@pytest.fixture(autouse=True)
def sandbox_available(monkeypatch):
    """These tests isolate credential grants from platform-specific sandbox support."""
    monkeypatch.setattr(hub, "sandbox_state", lambda: (True, "test sandbox available"))
    monkeypatch.delenv("ROMARR_PLUGIN_BACKEND_CREDENTIALS", raising=False)


@pytest.fixture
def environment(monkeypatch):
    """A ROMarr process holding every kind of secret it can hold."""
    secrets = {
        "ROMARR_API_KEY": "romarr-key-secret",
        "ROMARR_PASSWORD": "romarr-password-secret",
        "PROWLARR_API_KEY": "prowlarr-secret",
        "QBITTORRENT_PASS": "qbit-secret",
        "SABNZBD_API_KEY": "sab-secret",
        "NZBGET_PASS": "nzbget-secret",
        "LIBRARY_API_KEY": "library-key-secret",
        "AWS_SECRET_ACCESS_KEY": "unrelated-but-present",
        "GITHUB_TOKEN": "gh-secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LIBRARY_URL", "http://romm:8080")
    monkeypatch.setenv("LIBRARY_USERNAME", "romarr")
    monkeypatch.setenv("LIBRARY_PASSWORD", "library-password")
    return secrets


def test_no_romarr_or_client_secret_reaches_a_plugin(environment):
    env = hub._plugin_env()
    leaked = [name for name in environment if name in env]
    assert not leaked, f"plugin subprocess inherits {leaked}"


def test_no_secret_value_reaches_a_plugin_under_any_name(environment):
    """A rename is not a fix. Check the values, not just the keys."""
    env = hub._plugin_env()
    present = set(env.values())
    for name, value in environment.items():
        assert value not in present, f"{name}'s value reached the plugin"


def test_library_credentials_require_an_explicit_grant(environment, monkeypatch):
    """Ordinary search/fetch needs no backend password; legacy CLI import can opt in."""
    env = hub._plugin_env()
    assert env.get("ROMM_URL") == "http://romm:8080"
    assert "ROMM_USER" not in env and "ROMM_PASSWORD" not in env
    monkeypatch.setenv("ROMARR_PLUGIN_BACKEND_CREDENTIALS", "1")
    env = hub._plugin_env()
    assert env["ROMM_USER"] == "romarr"
    assert env["ROMM_PASSWORD"] == "library-password"


def test_embedded_backend_url_credentials_are_not_an_implicit_grant(monkeypatch):
    monkeypatch.setenv("LIBRARY_URL", "https://username:password@romm.example/prefix?token=secret#private")
    assert hub._plugin_env()["ROMM_URL"] == "https://romm.example/prefix"
    monkeypatch.setenv("ROMARR_PLUGIN_BACKEND_CREDENTIALS", "1")
    assert hub._plugin_env()["ROMM_URL"].startswith("https://username:password@")


def test_a_plugin_can_still_run_at_all(environment):
    env = hub._plugin_env()
    assert env.get("PATH") == "/usr/bin"
    assert env.get("ROM_HUB_HOME")


def test_the_allowlist_is_an_allowlist_not_a_denylist(monkeypatch):
    """A denylist stops covering whatever is added next, and whatever is added
    next is the credential nobody thought about."""
    monkeypatch.setenv("SOME_FUTURE_CREDENTIAL_NOBODY_ADDED_YET", "oops")
    env = hub._plugin_env()
    assert "SOME_FUTURE_CREDENTIAL_NOBODY_ADDED_YET" not in env


def test_proxy_settings_are_deliberately_passed(monkeypatch):
    """Self-hosters behind a proxy need plugins to reach the network at all."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    assert hub._plugin_env().get("HTTPS_PROXY") == "http://proxy:3128"


def test_what_the_docs_claim_matches_what_the_code_does():
    """This test used to assert the opposite, and was wrong.

    It pinned `ROM_HUB_ALLOW_UNSANDBOXED == "1"` as correct behaviour, on the
    belief that ROMarr's container could not confine a plugin. The Hub's filter
    needs Linux and `pyseccomp` and nothing else, so what the flag really did
    was switch off a boundary that worked. Understating protection is a
    cheaper mistake than overstating it, but it is still a wrong document -- it
    told operators not to expect something they were entitled to.
    """
    security = (pathlib.Path(__file__).resolve().parents[1]
                / "SECURITY.md").read_text(encoding="utf-8")
    confined = "ROM_HUB_ALLOW_UNSANDBOXED" not in hub._plugin_env()

    if confined:
        assert "There is no sandbox." not in security, (
            "SECURITY.md denies confinement that this install has")
    else:
        assert "no confinement" in security.lower(), (
            "confinement is off and SECURITY.md does not say so")


def test_invalid_backend_url_does_not_leak_or_break_plugin_environment(monkeypatch):
    monkeypatch.setenv("LIBRARY_URL", "http://user:password@[invalid")
    assert "ROMM_URL" not in hub._plugin_env()
