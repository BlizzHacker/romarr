"""The Mister Download Manager plugin is preinstalled once and drives the MDM clients.

Preinstalled, not forced: ROMarr installs it the first time ROM Hub is
available and never again. Once its `mode` is set, ROMarr's `mdm_http` and
`mdm_torrent` clients take their mode, remote URL, API token, category and
connections from it, and saving one of those clients in ROMarr writes back to
it. These run against a real ROM Hub registry in a temporary home, installing
a throwaway git repository shaped like the plugin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

rom_hub = pytest.importorskip("rom_hub")
from rom_hub import registry as _registry, secrets as _secrets  # noqa: E402

from romarr import compressatorium_plugin as cp  # noqa: E402
from romarr import hub  # noqa: E402
from romarr import mdm_plugin as mp  # noqa: E402
from romarr import preinstalled_plugins as pp  # noqa: E402
from romarr.app import ROMarr, make_handler  # noqa: E402
from romarr.downloaders import MdmDirectClient, MdmTorrentClient  # noqa: E402

MANIFEST = """[plugin]
slug        = "mdm"
name        = "Mister Download Manager (direct downloads)"
version     = "0.1.0"
rpp_version = "1"
license     = "MIT"

[capabilities]
cores = "mdm.cores:Cores"

[permissions]
network = ["api.github.com", "github.com", "release-assets.githubusercontent.com"]
romm_api = []

[config]
mode        = { type = "str",    default = "" }
remote_url  = { type = "str",    default = "" }
api_token   = { type = "secret" }
connections = { type = "int",    default = 8 }
category    = { type = "str",    default = "" }
"""

COMPRESSATORIUM_MANIFEST = """[plugin]
slug        = "compressatorium"
name        = "Compressatorium (batch decompression)"
version     = "0.1.0"
rpp_version = "1"
license     = "MIT"

[capabilities]
cores = "compressatorium.cores:Cores"

[permissions]
network = ["api.github.com", "raw.githubusercontent.com"]
romm_api = []

[config]
url     = { type = "str",    default = "" }
api_key = { type = "secret" }
"""

_GIT = shutil.which("git") or "git"
REMOTE = "https://mdm.lan:8463"


def _git(*args, cwd):
    # git from the developer's PATH, run with an isolated HOME.
    subprocess.run([_GIT, *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd), "PATH": os.environ.get("PATH", "")})


def _checkout(root, slug, package, manifest):
    plugin = root / slug
    (plugin / package).mkdir(parents=True)
    (plugin / "manifest.toml").write_text(manifest, encoding="utf-8")
    (plugin / package / "__init__.py").write_text("", encoding="utf-8")
    (plugin / package / "cores.py").write_text("", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=plugin)
    _git("add", "-A", cwd=plugin)
    _git("commit", "-q", "-m", "plugin", cwd=plugin)


@pytest.fixture
def bundled(tmp_path):
    """A bundled-plugins folder holding git checkouts of both plugins."""
    root = tmp_path / "bundled"
    _checkout(root, mp.SLUG, "mdm", MANIFEST)
    _checkout(root, cp.SLUG, "compressatorium", COMPRESSATORIUM_MANIFEST)
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "rom-hub"
    path.mkdir()
    monkeypatch.setattr(hub, "available", lambda: True)
    monkeypatch.setattr(hub, "HOME", path)
    monkeypatch.setenv("ROM_HUB_SECRET_STORE", "file")
    return path


@pytest.fixture
def installed(home, bundled):
    assert mp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)}) == "installed"
    return home


def _remote(home, token="t0ken"):
    assert mp.configure(mode="remote", remote_url=REMOTE, api_token=token,
                        connections=16, category="compressed", home=home) == {"ok": True}


# ------------------------------------------------------------ preinstall --

def test_first_start_installs_the_bundled_copy(installed):
    assert [p.slug for p in _registry.Registry(installed).installed()] == [mp.SLUG]
    assert json.loads((installed / pp.MARKER_NAME).read_text()) == {mp.SLUG: mp.PLUGIN_REF}
    plugin = _registry.Registry(installed).get(mp.SLUG)
    assert plugin.enabled and plugin.manifest.name == mp.LABEL


def test_both_preinstalled_plugins_share_one_marker(home, bundled):
    env = {"ROMARR_BUNDLED_PLUGINS": str(bundled)}
    threads = [threading.Thread(target=fn, kwargs={"home": home, "env": env})
               for fn in (cp.preinstall, mp.preinstall)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert json.loads((home / pp.MARKER_NAME).read_text()) == {
        cp.SLUG: cp.PLUGIN_REF, mp.SLUG: mp.PLUGIN_REF}
    assert sorted(p.slug for p in _registry.Registry(home).installed()) == [cp.SLUG, mp.SLUG]


def test_a_disabled_plugin_stays_disabled_and_configures_nothing(installed, bundled):
    _remote(installed)
    _registry.Registry(installed).set_enabled(mp.SLUG, False)

    assert mp.preinstall(home=installed, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)}) == "offered"
    assert mp.settings(home=installed) is None
    assert mp.apply({"type": "mdm_http", "mode": "cli"}) == {"type": "mdm_http", "mode": "cli"}


def test_without_a_bundled_copy_it_installs_the_pinned_release(home, monkeypatch):
    calls = []

    class FakeRegistry:
        def __init__(self, root):
            pass

        def installed(self):
            return []

        def install(self, source, ref=None):
            calls.append((source, ref))

    monkeypatch.setattr(_registry, "Registry", FakeRegistry)

    assert mp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": "/nonexistent"}) == "installed"
    assert calls == [("https://github.com/BlizzHacker/rom-hub-mdm", "v0.1.0")]


def test_no_hub_means_nothing_happens(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "available", lambda: False)

    assert mp.preinstall(home=tmp_path) == "no-hub"
    assert mp.settings(home=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------- settings --

def test_an_unset_mode_leaves_the_clients_alone(installed):
    assert mp.settings(home=installed) is None
    row = {"type": "mdm_torrent", "mode": "remote", "remote_url": "https://other:1"}
    assert mp.apply(row) is row
    assert mp.status(home=installed)["configured"] is False


def test_set_plugin_settings_are_laid_over_both_clients(installed):
    _remote(installed)
    http_row = {"type": "mdm_http", "mode": "cli", "remote_url": "", "api_token": "",
                "connections": 4, "category": "", "save_path": "/d"}
    torrent_row = {"type": "mdm_torrent", "mode": "cli", "category": "romarr",
                   "save_path": "/t"}

    http = mp.apply(http_row)
    torrent = mp.apply(torrent_row)

    assert http == {**http_row, "mode": "remote", "remote_url": REMOTE,
                    "api_token": "t0ken", "connections": 16, "category": "compressed"}
    assert torrent == {**torrent_row, "mode": "remote", "remote_url": REMOTE,
                       "api_token": "t0ken", "category": "compressed"}
    assert "connections" not in torrent
    assert http_row["mode"] == "cli"  # the stored row is not changed
    assert mp.apply({"type": "qbittorrent", "category": "x"}) == {"type": "qbittorrent",
                                                                 "category": "x"}


def test_the_token_stays_out_of_state_and_status(installed):
    _remote(installed, token="s3cret")

    assert "s3cret" not in (installed / "state.json").read_text()
    status = mp.status(home=installed)
    assert "s3cret" not in json.dumps(status)
    assert status["configured"] and status["api_token_set"] and status["plugin_enabled"]
    assert status["label"] == "Mister Download Manager (direct downloads)"
    assert "anefzaoui" in status["credit"] and "GPL-3.0-or-later" in status["credit"]


def test_configure_replaces_and_clears_the_token_without_touching_the_rest(installed):
    _remote(installed, token="old")

    assert mp.configure(api_token="new", home=installed) == {"ok": True}
    assert mp.settings(home=installed).api_token == "new"
    assert mp.settings(home=installed).remote_url == REMOTE

    assert mp.configure(clear_api_token=True, home=installed) == {"ok": True}
    assert mp.settings(home=installed).api_token == ""


@pytest.mark.parametrize("changes, why", [
    ({"mode": "daemon"}, "cli, remote, or blank"),
    ({"mode": "remote"}, "Remote mode needs remote_url"),
    ({"mode": "remote", "remote_url": "http://mdm.lan:8463"}, "https://"),
    ({"mode": "remote", "remote_url": "ftp://mdm.lan"}, "https://"),
    ({"mode": "remote", "remote_url": "https://u:p@mdm.lan"}, "own field"),
    ({"mode": "remote", "remote_url": "https://mdm.lan/?x=1"}, "no query"),
    ({"mode": "cli", "connections": 0}, "1 to 128"),
    ({"mode": "cli", "connections": 129}, "1 to 128"),
    ({"mode": "cli", "connections": True}, "1 to 128"),
    ({"mode": "cli", "connections": "many"}, "1 to 128"),
    ({"mode": "cli", "category": "romarr"}, "category must be blank"),
])
def test_configure_refuses_what_mdm_cannot_use(installed, changes, why):
    result = mp.configure(home=installed, **changes)

    assert not result["ok"] and why in result["error"]
    assert mp.settings(home=installed) is None


def test_plain_http_is_allowed_to_this_machine(installed):
    assert mp.configure(mode="remote", remote_url="http://127.0.0.1:8463/",
                        home=installed) == {"ok": True}
    assert mp.settings(home=installed).remote_url == "http://127.0.0.1:8463"


def test_configure_needs_the_plugin(home):
    assert mp.configure(mode="cli", home=home) == {
        "ok": False, "error": "Install the Mister Download Manager (direct downloads) plugin first"}


class _FailingStore:
    def __init__(self, real):
        self.real = real

    def get(self, *a):
        return self.real.get(*a)

    def set(self, *a):
        raise OSError("keyring locked")

    def delete(self, *a):
        return self.real.delete(*a)


def test_a_failed_token_save_keeps_the_previous_address(installed, monkeypatch):
    _remote(installed, token="key-a")
    real_open = _secrets.open_store
    monkeypatch.setattr(_secrets, "open_store", lambda h: _FailingStore(real_open(h)))

    result = mp.configure(remote_url="https://b.lan:8463", api_token="key-b", home=installed)

    assert not result["ok"]
    monkeypatch.setattr(_secrets, "open_store", real_open)
    current = mp.settings(home=installed)
    assert (current.remote_url, current.api_token) == (REMOTE, "key-a")


def test_a_failed_settings_save_restores_the_previous_token(installed, monkeypatch):
    _remote(installed, token="key-a")

    def broken(self, slug, config):
        raise OSError("state.json is read-only")

    monkeypatch.setattr(_registry.Registry, "set_config", broken)
    result = mp.configure(remote_url="https://b.lan:8463", api_token="key-b", home=installed)

    assert not result["ok"]
    monkeypatch.undo()
    monkeypatch.setattr(hub, "available", lambda: True)
    monkeypatch.setenv("ROM_HUB_SECRET_STORE", "file")
    current = mp.settings(home=installed)
    assert (current.remote_url, current.api_token) == (REMOTE, "key-a")


# ------------------------------------------------------------ round trip --

def test_a_client_save_does_nothing_while_the_plugin_is_unset(installed):
    result = mp.sync_from_client({"type": "mdm_http", "mode": "remote",
                                  "remote_url": REMOTE, "api_token": "x"}, home=installed)

    assert result == {"ok": True, "synced": False}
    assert mp.settings(home=installed) is None


def test_a_client_save_writes_back_to_a_set_plugin(installed):
    _remote(installed, token="keep-me")

    result = mp.sync_from_client({
        "type": "mdm_http", "mode": "remote", "remote_url": "https://new.lan:8463",
        "api_token": mp.SECRET_PLACEHOLDER, "connections": "32", "category": "programs",
    }, home=installed)

    assert result == {"ok": True, "synced": True}
    current = mp.settings(home=installed)
    assert current == mp.Settings(mode="remote", remote_url="https://new.lan:8463",
                                  api_token="keep-me", connections=32, category="programs")


def test_a_torrent_save_keeps_connections_and_drops_its_label(installed):
    _remote(installed)

    mp.sync_from_client({"type": "mdm_torrent", "mode": "remote", "remote_url": REMOTE,
                         "api_token": "fresh", "category": "romarr"}, home=installed)

    current = mp.settings(home=installed)
    assert (current.connections, current.category, current.api_token) == (16, "", "fresh")


def test_an_emptied_token_field_clears_the_token(installed):
    _remote(installed)

    mp.sync_from_client({"type": "mdm_torrent", "mode": "cli", "api_token": ""},
                        home=installed)

    assert mp.settings(home=installed).api_token == ""
    assert mp.settings(home=installed).mode == "cli"


def test_a_refused_client_save_changes_nothing(installed):
    _remote(installed)

    result = mp.sync_from_client({"type": "mdm_http", "mode": "remote",
                                  "remote_url": "http://mdm.lan", "connections": 8},
                                 home=installed)

    assert not result["ok"] and "https://" in result["error"]
    assert mp.settings(home=installed).remote_url == REMOTE


def test_test_uses_the_plugin_token_for_a_placeholder(installed):
    _remote(installed, token="plugin-token")
    submitted = {"type": "mdm_http", "api_token": mp.SECRET_PLACEHOLDER}

    assert mp.for_test(submitted, {**submitted, "api_token": "row-token"})["api_token"] == \
        "plugin-token"
    assert mp.for_test({"type": "mdm_http", "api_token": "typed"},
                       {"type": "mdm_http", "api_token": "typed"})["api_token"] == "typed"
    # An emptied field tests as no token, even though merge_secrets put the
    # row's old token back into the merged row.
    assert mp.for_test({"type": "mdm_http", "api_token": ""},
                       {"type": "mdm_http", "api_token": "row-token"})["api_token"] == ""


# ------------------------------------------------------------------ HTTP --

@pytest.fixture
def server(tmp_path, installed):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "ROMARR_API_KEY": "testkey"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None):
    req = urllib.request.Request(
        base + path, method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json", "X-Api-Key": "testkey"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_hub_card_saves_settings_and_the_clients_follow(server):
    base, service = server
    code, _ = call(base, "/api/v1/downloadclient", {
        "type": "mdm_http", "name": "Direct", "enable": True, "mode": "cli",
        "save_path": "/downloads/sites", "connections": 4})
    assert code == 200

    code, result = call(base, "/api/v1/hub/mdm", {
        "mode": "remote", "remote_url": REMOTE, "api_token": "hub-token",
        "connections": 24, "category": "other"})

    assert (code, result) == (200, {"ok": True})
    [client] = [c for c in service.clients if isinstance(c, MdmDirectClient)]
    assert client._mode == "remote" and client._remote.base_url == REMOTE
    assert client._remote.api_token == "hub-token" and client._category == "other"
    code, status = call(base, "/api/v1/hub/status")
    assert status["mdm"]["configured"] and "hub-token" not in json.dumps(status)
    code, capability = call(base, "/api/v1/downloadclient/mdm")
    assert capability["plugin"]["mode"] == "remote"


def test_the_hub_card_refuses_bad_types_and_values(server):
    base, _ = server
    assert call(base, "/api/v1/hub/mdm", {"mode": 1})[0] == 400
    assert call(base, "/api/v1/hub/mdm", {"connections": True})[0] == 400
    code, result = call(base, "/api/v1/hub/mdm", {"mode": "remote"})
    assert code == 400 and "remote_url" in result["error"]


def test_a_client_edit_round_trips_to_the_plugin(server):
    base, service = server
    _remote(hub.HOME, token="first")

    settings = service.safe_settings()
    code, saved = call(base, "/api/v1/downloadclient", {
        "type": "mdm_torrent", "name": "Torrents", "enable": True, "mode": "remote",
        "remote_url": "https://edited.lan:8463", "api_token": mp.SECRET_PLACEHOLDER,
        "category": "videos", "save_path": "/downloads/torrents"})

    assert code == 200 and saved["api_token"] in ("", mp.SECRET_PLACEHOLDER)
    current = mp.settings()
    assert (current.remote_url, current.category, current.api_token) == (
        "https://edited.lan:8463", "videos", "first")
    [client] = [c for c in service.clients if isinstance(c, MdmTorrentClient)]
    assert client._remote.base_url == "https://edited.lan:8463"
    # What the form shows is what is in force, token masked.
    [row] = service.safe_settings()["download_clients"]
    assert row["remote_url"] == "https://edited.lan:8463"
    assert row["api_token"] == mp.SECRET_PLACEHOLDER
    assert "first" not in json.dumps(settings) + json.dumps(service.safe_settings())


def test_a_refused_client_edit_saves_neither_side(server):
    base, service = server
    _remote(hub.HOME)

    code, result = call(base, "/api/v1/downloadclient", {
        "type": "mdm_http", "name": "Direct", "enable": True, "mode": "remote",
        "remote_url": "http://mdm.lan:8463", "connections": 8})

    assert code == 400 and "https://" in result["error"]
    assert service.store.list_items("download_clients") == []
    assert mp.settings().remote_url == REMOTE


def test_other_clients_are_saved_as_before(server):
    base, service = server
    _remote(hub.HOME)

    code, _ = call(base, "/api/v1/downloadclient", {
        "type": "qbittorrent", "name": "qbit", "enable": True, "host": "qbit",
        "port": 8080, "category": "romarr"})

    assert code == 200
    assert mp.settings().category == "compressed"


def test_the_hub_and_clients_pages_surface_the_plugin():
    from romarr import ui

    assert "pl.slug==='mdm'" in ui.JS and "'/api/v1/hub/mdm'" in ui.JS
    assert "mdm.plugin.label" in ui.JS
    # Compressatorium's card still posts where it did.
    assert "'/api/v1/hub/compressatorium'" in ui.JS


def test_the_docker_image_bundles_the_pinned_plugin():
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text("utf-8")
    assert f"ARG MDM_PLUGIN_REF={mp.PLUGIN_REF}" in dockerfile
    assert f"{mp.PLUGIN_REPO} \\\n        /opt/romarr/bundled-plugins/{mp.SLUG};" in dockerfile


def test_the_status_route_never_returns_the_token(server):
    base, _ = server
    _remote(hub.HOME, token="get-secret")

    code, status = call(base, "/api/v1/hub/mdm")

    assert code == 200 and status["configured"] and status["api_token_set"]
    assert "get-secret" not in json.dumps(status)
