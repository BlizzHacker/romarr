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

import pytest

from romarr import hub


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


def test_the_library_credentials_it_needs_do_get_through(environment):
    """The boundary has to still let the plugin do its job: import and enrich
    both need somewhere to import into."""
    env = hub._plugin_env()
    assert env.get("ROMM_URL") == "http://romm:8080"
    assert env.get("ROMM_USER") == "romarr"
    assert env.get("ROMM_PASSWORD") == "library-password"


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


def test_we_do_not_claim_a_sandbox_we_do_not_have():
    """ROM_HUB_ALLOW_UNSANDBOXED is set on purpose; the docs must not describe
    plugin execution as sandboxed while the code turns the sandbox off."""
    assert hub._plugin_env()["ROM_HUB_ALLOW_UNSANDBOXED"] == "1"
    source = (hub.__doc__ or "") + (hub._run_cli.__doc__ or "")
    assert "sandbox" not in source.lower() or "not sandboxed" in source.lower()
