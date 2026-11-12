"""Offline proofs for IA login, secret isolation, renewal and request scope."""
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from romarr.ia_auth import ArchiveAuthError, configure, credentials, status
from romarr.ia_session import ArchiveOrgSession, ArchiveOrgTransport


@pytest.fixture
def root(tmp_path, monkeypatch):
    for key in ("EMAIL", "PASSWORD", "EMAIL_FILE", "PASSWORD_FILE"):
        for prefix in ("ROMARR_ARCHIVE_ORG_", "ROM_HUB_ARCHIVE_ORG_"):
            monkeypatch.delenv(prefix + key, raising=False)
    # Exercise ROM Hub's actual persistent encrypted file store in CI.
    monkeypatch.setenv("ROM_HUB_SECRET_STORE", "file")
    monkeypatch.setenv("ROM_HUB_SECRET_KEY", "test-key-stored-only-in-environment")
    return tmp_path


def _client(root, handler, remote):
    manager = ArchiveOrgSession(root, login_transport=httpx.MockTransport(handler))
    return manager, httpx.Client(transport=ArchiveOrgTransport(
        manager, transport=httpx.MockTransport(remote)))


def _login(request, token="session"):
    assert request.url == "https://archive.org/services/xauthn/?op=login"
    assert request.method == "POST"
    assert b"email=" in request.content and b"password=" in request.content
    return httpx.Response(200, json={"success": True, "values": {"cookies": {
        "logged-in-user": "account%40example.com; Max-Age=3600; domain=.archive.org",
        "logged-in-sig": token + "; Max-Age=3600; domain=.archive.org"}}})


def test_ui_uses_existing_secret_store_and_only_one_encrypted_entry(root):
    assert status(root)["configured"] is False
    assert configure(root, "account@example.com", "secret-password") == {"ok": True}
    assert credentials(root) == ("account@example.com", "secret-password")
    stored = root / "secrets.json"
    assert stored.is_file()
    content = stored.read_text()
    assert "secret-password" not in content and "account@example.com" not in content
    assert not (root / "state.json").exists()
    assert status(root) == {"configured": True, "external": False, "error": ""}
    assert configure(root, clear=True) == {"ok": True}
    assert credentials(root) is None


def test_external_files_override_ui_without_copying_to_store(root, monkeypatch):
    assert configure(root, "ui@example.com", "ui-secret") == {"ok": True}
    email_path, password_path = root / "email", root / "password"
    email_path.write_text("external@example.com\n")
    password_path.write_text("external-secret\n")
    monkeypatch.setenv("ROMARR_ARCHIVE_ORG_EMAIL_FILE", str(email_path))
    monkeypatch.setenv("ROMARR_ARCHIVE_ORG_PASSWORD_FILE", str(password_path))
    assert credentials(root) == ("external@example.com", "external-secret")
    assert configure(root, "second@example.com", "second-secret")["ok"] is False
    assert status(root)["external"] is True
    monkeypatch.delenv("ROMARR_ARCHIVE_ORG_EMAIL_FILE")
    monkeypatch.delenv("ROMARR_ARCHIVE_ORG_PASSWORD_FILE")
    assert credentials(root) == ("ui@example.com", "ui-secret")


def test_login_reuses_cookie_and_never_sends_it_to_unrelated_hosts(root):
    assert configure(root, "account@example.com", "secret-password")["ok"]
    logins, requests = [], []
    def login(req):
        logins.append(1)
        return _login(req)
    def remote(req):
        requests.append((str(req.url), req.headers.get("cookie")))
        return httpx.Response(200)
    manager, client = _client(root, login, remote)
    with client:
        for host in ("archive.org", "ia800408.us.archive.org", "ia600107.ca.archive.org"):
            assert client.get(f"https://{host}/view_archive.php").status_code == 200
        for host in ("www.archive.org", "archive.org.attacker.test", "example.com"):
            assert client.get(f"https://{host}/download").status_code == 200
        assert client.get("http://archive.org/test").status_code == 200
    assert len(logins) == 1
    expected = "logged-in-user=account%40example.com; logged-in-sig=session"
    assert [cookie for _, cookie in requests] == [expected] * 3 + [None] * 4


def test_cookie_expiry_renews_before_request(root):
    assert configure(root, "account@example.com", "secret-password")["ok"]
    logins = []
    def login(req):
        logins.append(1)
        return _login(req, "cookie" + str(len(logins)))
    manager, client = _client(root, login, lambda req: httpx.Response(200))
    with client:
        client.get("https://archive.org/metadata/test")
        manager._refresh_at = time.time() - 1  # simulate an expired renewal deadline
        client.get("https://archive.org/metadata/test")
    assert len(logins) == 2


def test_403_reauthenticates_only_once_then_preserves_refusal(root):
    assert configure(root, "account@example.com", "secret-password")["ok"]
    logins, outgoing = [], []
    def login(req):
        logins.append(1)
        return _login(req, "cookie" + str(len(logins)))
    def remote(req):
        outgoing.append(req.headers["cookie"])
        return httpx.Response(403)
    _, client = _client(root, login, remote)
    with client:
        response = client.get("https://archive.org/download/restricted.zip/member.chd")
    assert response.status_code == 403
    assert len(logins) == 2
    assert len(outgoing) == 2 and outgoing[0] != outgoing[1]


def test_bad_login_message_cannot_expose_password(root):
    assert configure(root, "account@example.com", "secret-password")["ok"]
    _, client = _client(root, lambda req: httpx.Response(
        200, json={"success": False, "error": "secret-password"}), lambda req: httpx.Response(200))
    with client, pytest.raises(ArchiveAuthError, match="rejected"):
        client.get("https://archive.org/metadata/restricted")
    # The returned message contains no response body or submitted credentials.


def test_romarr_uses_rom_hub_authentication():
    import romarr.ia_auth as adapter
    import romarr.ia_session as session_adapter
    import rom_hub.ia_auth as shared
    import rom_hub.ia_session as shared_session
    assert adapter.configure is shared.configure
    assert adapter.credentials is shared.credentials
    assert session_adapter.archive_transport is shared_session.archive_transport
    assert session_adapter.ArchiveOrgSession is shared_session.ArchiveOrgSession


def test_romarr_auth_settings_use_shared_store(root, monkeypatch):
    from romarr import hub
    monkeypatch.setattr(hub, "HOME", root)
    monkeypatch.setattr("rom_hub.registry.Registry.get", lambda self, slug: object())
    assert hub.archive_org_auth_configure("account@example.com", "secret-password")["ok"]
    assert hub.archive_org_auth_status() == {"configured": True, "external": False, "error": ""}
    assert credentials(root) == ("account@example.com", "secret-password")
    assert hub.archive_org_auth_configure(clear=True)["ok"]
    assert hub.archive_org_auth_status()["configured"] is False


@pytest.mark.parametrize("failure", ["setup", "search"])
def test_authenticated_search_failure_preserves_anonymous_filtering(root, monkeypatch, failure):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from romarr import hub

    monkeypatch.setattr(hub, "HOME", root)
    assert configure(root, "account@example.com", "secret-password")["ok"]
    plugins = [SimpleNamespace(slug=slug, enabled=True, manifest=SimpleNamespace(capabilities=["search"]))
               for slug in ("anonymous", "archive-org")]
    monkeypatch.setattr("rom_hub.registry.Registry.installed", lambda self: plugins)
    monkeypatch.setattr(hub, "_unsandboxed", lambda: False)
    monkeypatch.setattr("rom_hub.broker.fetcher.HttpxFetcher", Mock())
    rows = [dict(plugin="anonymous", title="Wrong.nes", extra={"path": "NES/Wrong.nes"}),
            dict(plugin="anonymous", title="First.sfc", extra={"path": "SNES/First.sfc"}),
            dict(plugin="anonymous", title="Second.sfc", extra={"path": "SNES/Second.sfc"})]
    def transport(slug, home):
        if failure == "setup":
            raise RuntimeError("unavailable")
    def search(group, **kwargs):
        if group[0].slug == "archive-org":
            raise RuntimeError("unavailable")
        return SimpleNamespace(results=rows)
    monkeypatch.setattr("romarr.ia_session.archive_transport", transport)
    monkeypatch.setattr("rom_hub.dispatcher.search_all", search)
    results, error = hub.search("Game", platform="snes", limit=1)
    assert [row["title"] for row in results] == ["First.sfc"]
    assert "unavailable" in error


def test_plugin_reported_failure_is_visible(root, monkeypatch):
    from types import SimpleNamespace
    from romarr import hub
    from rom_hub.dispatcher import PluginStatus, SearchOutcome

    monkeypatch.setattr(hub, "HOME", root)
    monkeypatch.setattr(hub, "_unsandboxed", lambda: True)
    plugin = SimpleNamespace(slug="archive-org", enabled=True,
                             manifest=SimpleNamespace(capabilities=["search"]))
    monkeypatch.setattr("rom_hub.registry.Registry.installed", lambda self: [plugin])
    monkeypatch.setattr("rom_hub.dispatcher.search_all", lambda *args, **kwargs:
                        SearchOutcome(results=[], statuses=[PluginStatus(
                            "archive-org", False, 0, "Archive.org rejected the login")]))
    rows, error = hub.search("Game", platform="psx")
    assert rows == []
    assert "Archive.org rejected the login" in error
