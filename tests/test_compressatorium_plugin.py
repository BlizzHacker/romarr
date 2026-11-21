"""The Compressatorium plugin is preinstalled once and carries the connection.

Preinstalled, not forced: ROMarr installs it the first time ROM Hub is
available and never again, so an operator who removes it keeps it removed.
Its `url` and secret `api_key` are where ROMarr finds Compressatorium unless
the environment names one. These run against a real ROM Hub registry in a
temporary home, installing a throwaway git repository shaped like the plugin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

rom_hub = pytest.importorskip("rom_hub")
from rom_hub import catalog_sources as _catalog_sources, registry as _registry, secrets as _secrets  # noqa: E402,F401

from romarr import compressatorium_plugin as cp  # noqa: E402
from romarr import hub  # noqa: E402

MANIFEST = """[plugin]
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


def _git(*args, cwd):
    # git from the developer's PATH, run with an isolated HOME.
    subprocess.run([_GIT, *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd), "PATH": os.environ.get("PATH", "")})


@pytest.fixture
def bundled(tmp_path):
    """A bundled-plugins folder holding a git checkout of the plugin."""
    root = tmp_path / "bundled"
    plugin = root / cp.SLUG
    (plugin / "compressatorium").mkdir(parents=True)
    (plugin / "manifest.toml").write_text(MANIFEST, encoding="utf-8")
    (plugin / "compressatorium" / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "compressatorium" / "cores.py").write_text("", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=plugin)
    _git("add", "-A", cwd=plugin)
    _git("commit", "-q", "-m", "plugin", cwd=plugin)
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "rom-hub"
    path.mkdir()
    monkeypatch.setattr(hub, "available", lambda: True)
    monkeypatch.setenv("ROM_HUB_SECRET_STORE", "file")
    return path


def _installed(home):
    return [p.slug for p in _registry.Registry(home).installed()]


def test_first_start_installs_the_bundled_copy(home, bundled):
    outcome = cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})

    assert outcome == "installed"
    assert _installed(home) == [cp.SLUG]
    assert json.loads((home / cp.MARKER_NAME).read_text()) == {cp.SLUG: cp.PLUGIN_REF}


def test_a_disabled_plugin_stays_disabled(home, bundled):
    env = {"ROMARR_BUNDLED_PLUGINS": str(bundled)}
    cp.preinstall(home=home, env=env)
    _registry.Registry(home).set_enabled(cp.SLUG, False)

    assert cp.preinstall(home=home, env=env) == "offered"
    assert not _registry.Registry(home).get(cp.SLUG).enabled


def test_a_plugin_removed_by_hand_stays_removed(home, bundled):
    """ROM Hub has no uninstall; removing means its folder and state entry."""
    env = {"ROMARR_BUNDLED_PLUGINS": str(bundled)}
    cp.preinstall(home=home, env=env)
    import shutil
    shutil.rmtree(home / "plugins" / cp.SLUG)
    state = json.loads((home / "state.json").read_text())
    state.pop(cp.SLUG)
    (home / "state.json").write_text(json.dumps(state))

    assert cp.preinstall(home=home, env=env) == "offered"
    assert _installed(home) == []


def test_a_hand_installed_plugin_is_left_alone_and_recorded(home, bundled):
    _registry.Registry(home).install(str(bundled / cp.SLUG))

    assert cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": "/nonexistent"}) == "present"
    assert cp.SLUG in json.loads((home / cp.MARKER_NAME).read_text())


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

    assert cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": "/nonexistent"}) == "installed"
    assert calls == [(cp.PLUGIN_REPO, cp.PLUGIN_REF)]


def test_a_failed_install_is_retried_next_start(home, monkeypatch):
    class Offline:
        def __init__(self, root):
            pass

        def installed(self):
            return []

        def install(self, source, ref=None):
            raise RuntimeError("github.com unreachable")

    monkeypatch.setattr(_registry, "Registry", Offline)

    outcome = cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": "/nonexistent"})

    assert outcome.startswith("failed: github.com unreachable")
    assert not (home / cp.MARKER_NAME).exists()


def test_no_hub_means_nothing_happens(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "available", lambda: False)

    assert cp.preinstall(home=tmp_path) == "no-hub"
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------ connection --

def test_the_environment_wins(home, bundled):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})
    _registry.Registry(home).set_config(cp.SLUG, {"url": "http://plugin:8080"})

    url, key, source = cp.connection(
        {"COMPRESSATORIUM_URL": "http://env:8080", "COMPRESSATORIUM_API_KEY": "k"},
        home=home)

    assert (url, key, source) == ("http://env:8080", "k", "env")


def test_the_plugin_supplies_url_and_secret_key(home, bundled):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})
    _registry.Registry(home).set_config(cp.SLUG, {"url": "http://comp:8080"})
    _secrets.open_store(home).set(cp.SLUG, "api_key", "s3cret")

    assert cp.connection({}, home=home) == ("http://comp:8080", "s3cret", "plugin")

    status = cp.status({}, home=home)
    assert status["configured"] and status["api_key_set"] and status["plugin_enabled"]
    assert "s3cret" not in json.dumps(status)
    assert "pacnpal" in status["credit"]


def test_a_disabled_or_unset_plugin_configures_nothing(home, bundled):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})

    assert cp.connection({}, home=home) == ("", "", "")

    reg = _registry.Registry(home)
    reg.set_config(cp.SLUG, {"url": "http://comp:8080"})
    reg.set_enabled(cp.SLUG, False)

    assert cp.connection({}, home=home) == ("", "", "")


# ------------------------------------------------------------- configure --

def test_configure_saves_url_and_keeps_the_key_out_of_state(home, bundled):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})

    assert cp.configure(url="http://comp:8080/", api_key="s3cret", home=home) == {"ok": True}

    assert cp.connection({}, home=home) == ("http://comp:8080", "s3cret", "plugin")
    assert "s3cret" not in (home / "state.json").read_text()


def test_configure_replaces_the_key_without_touching_the_url(home, bundled):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})
    cp.configure(url="http://comp:8080", api_key="old", home=home)

    cp.configure(api_key="new", home=home)
    assert cp.connection({}, home=home) == ("http://comp:8080", "new", "plugin")

    cp.configure(clear_api_key=True, home=home)
    assert cp.connection({}, home=home) == ("http://comp:8080", "", "plugin")


@pytest.mark.parametrize("url, why", [
    ("ftp://comp", "http:// or https://"),
    ("comp:8080", "http:// or https://"),
    ("http://user:pw@comp:8080", "API key in its own field"),
    ("http://comp:8080/?x=1", "no query"),
])
def test_configure_refuses_a_url_it_cannot_use(home, bundled, url, why):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})

    result = cp.configure(url=url, home=home)

    assert not result["ok"] and why in result["error"]
    assert cp.connection({}, home=home) == ("", "", "")


def test_configure_needs_the_plugin(home):
    assert cp.configure(url="http://comp:8080", home=home) == {
        "ok": False, "error": "Install the Compressatorium plugin first"}


def test_an_installed_plugin_missing_from_the_catalogue_still_gets_a_card(home, bundled, monkeypatch):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})
    monkeypatch.setattr(hub, "HOME", home)

    class EmptyCatalogue:
        entries = []

    monkeypatch.setattr(_catalog_sources, "load_all", lambda root: EmptyCatalogue())

    items = hub.plugins()["items"]

    assert [(i["slug"], i["installed"], i["enabled"]) for i in items] == [
        (cp.SLUG, True, True)]
    assert items[0]["capabilities"] == ["cores"]


# --- review fixes (#232) ---------------------------------------------------

def test_the_bundled_checkout_is_installed_from_a_private_copy(home, bundled, monkeypatch):
    # The image builds the checkout as root and ROMarr runs as PUID; git
    # refuses to clone a repository another user owns, so ROMarr installs
    # from a copy it owns instead.
    seen = []
    real = _registry.Registry.install

    def spy(self, source, *a, **k):
        seen.append(source)
        return real(self, source, *a, **k)

    monkeypatch.setattr(_registry.Registry, "install", spy)
    assert cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)}) == "installed"
    assert len(seen) == 1
    assert seen[0] == str(home / cp.SOURCES_DIR / cp.SLUG)
    # ROM Hub records this as the plugin's source; updates clone from it later.
    assert (home / cp.SOURCES_DIR / cp.SLUG / "manifest.toml").is_file()
    assert not list((home / cp.SOURCES_DIR).glob(".copy-*"))


class _FailingStore:
    def __init__(self, real, fail):
        self.real, self.fail = real, fail

    def get(self, *a):
        return self.real.get(*a)

    def set(self, *a):
        if "set" in self.fail:
            raise OSError("keyring locked")
        return self.real.set(*a)

    def delete(self, *a):
        return self.real.delete(*a)


def test_a_failed_key_save_keeps_the_previous_address(home, bundled, monkeypatch):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})
    cp.configure(url="http://a:8080", api_key="key-a", home=home)
    real_open = _secrets.open_store
    monkeypatch.setattr(_secrets, "open_store", lambda h: _FailingStore(real_open(h), {"set"}))

    result = cp.configure(url="http://b:8080", api_key="key-b", home=home)

    assert not result["ok"]
    monkeypatch.setattr(_secrets, "open_store", real_open)
    assert cp.connection({}, home=home) == ("http://a:8080", "key-a", "plugin")


def test_a_failed_address_save_restores_the_previous_key(home, bundled, monkeypatch):
    cp.preinstall(home=home, env={"ROMARR_BUNDLED_PLUGINS": str(bundled)})
    cp.configure(url="http://a:8080", api_key="key-a", home=home)

    def broken(self, slug, config):
        raise OSError("state.json is read-only")

    monkeypatch.setattr(_registry.Registry, "set_config", broken)
    result = cp.configure(url="http://b:8080", api_key="key-b", home=home)

    assert not result["ok"]
    monkeypatch.undo()
    monkeypatch.setattr(hub, "available", lambda: True)
    monkeypatch.setenv("ROM_HUB_SECRET_STORE", "file")
    assert cp.connection({}, home=home) == ("http://a:8080", "key-a", "plugin")
