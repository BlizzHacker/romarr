"""The per-plugin dashboard: detail, settings and try-it, against fakes of ROM Hub."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from romarr import hub, plugin_dashboard as pd

README = """# Demo plugin

<table><tr><td>

**Part of [Cartridge](https://example.org)** brand block

</td></tr></table>

Finds demos.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `api_key` | `secret` | - | **required**: your demozoo key |
| `max_requests` | `int` | `12` | requests one search may make |
"""


class FakeRegistry:
    def __init__(self, plugin):
        self.plugin = plugin
        self.saved = None

    def get(self, slug):
        if slug != self.plugin.slug:
            raise KeyError(slug)
        return self.plugin

    def set_config(self, slug, config):
        self.saved = dict(config)
        self.plugin.config = dict(config)


class FakeStore(dict):
    def get(self, slug, key):  # noqa: D401 - dict.get shape is (key, default)
        return dict.get(self, (slug, key))

    def set(self, slug, key, value):
        self[(slug, key)] = value

    def delete(self, slug, key):
        return self.pop((slug, key), None) is not None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    manifest = SimpleNamespace(
        name="Demozoo", version="0.2.1",
        capabilities={"search": "x", "metadata": "y"},
        network=["demozoo.org", "api.github.com"],
        config_schema={"api_key": {"type": "secret"},
                       "max_requests": {"type": "int", "default": 12},
                       "set_name": {"type": "bool", "default": True},
                       "systems": {"type": "list[str]", "default": []},
                       "collection": {"type": "str", "default": "Demozoo"}})
    plugin = SimpleNamespace(slug="demozoo", manifest=manifest, path=tmp_path,
                             enabled=True, config={"max_requests": 20})
    registry, store = FakeRegistry(plugin), FakeStore()
    monkeypatch.setattr(pd, "_registry", lambda: registry)
    monkeypatch.setattr(pd, "_secret_store", lambda: store)
    monkeypatch.setattr(hub, "plugins", lambda: {"items": [
        {"slug": "demozoo", "description": "Demoscene productions.", "key_required": True,
         "repository": "https://github.com/BlizzHacker/rom-hub-demozoo", "platforms": ["dos", "amiga"]}]})
    hub.configure_github_token("")
    return SimpleNamespace(registry=registry, store=store, plugin=plugin)


def test_detail_reads_the_manifest_readme_and_catalogue(env):
    d = pd.detail("demozoo")
    assert d["ok"] and d["name"] == "Demozoo" and d["version"] == "0.2.1"
    fields = {f["key"]: f for f in d["fields"]}
    assert fields["max_requests"]["value"] == 20
    assert fields["collection"]["value"] == "Demozoo"
    assert fields["api_key"] == {"key": "api_key", "type": "secret", "default": None,
                                 "help": "required: your demozoo key", "set": False}
    assert "brand block" not in d["readme"] and "Finds demos." in d["readme"]
    assert [g["capability"] for g in d["guide"]] == ["metadata", "search"]


def test_detail_says_what_is_missing(env):
    needs = " ".join(pd.detail("demozoo")["needs"])
    assert "Needs your API key" in needs and "60 requests an hour" in needs
    env.store.set("demozoo", "api_key", "k")
    hub.configure_github_token("ghp_test")
    try:
        assert pd.detail("demozoo")["needs"] == []
    finally:
        hub.configure_github_token("")


def test_a_secret_value_never_reaches_the_page(env):
    env.store.set("demozoo", "api_key", "s3cret")
    d = pd.detail("demozoo")
    assert "s3cret" not in repr(d)
    assert {f["key"]: f for f in d["fields"]}["api_key"]["set"] is True


def test_save_coerces_each_type_and_keeps_secrets_out_of_config(env):
    r = pd.save("demozoo", {"max_requests": "30", "set_name": "off", "systems": "dos, amiga\nc64"},
                {"api_key": "abc"})
    assert r == {"ok": True}
    assert env.registry.saved == {"max_requests": 30, "set_name": False,
                                  "systems": ["dos", "amiga", "c64"]}
    assert env.store.get("demozoo", "api_key") == "abc"


def test_a_bad_value_writes_nothing(env):
    r = pd.save("demozoo", {"max_requests": "lots"}, {"api_key": "abc"})
    assert not r["ok"] and "whole number" in r["error"]
    assert env.registry.saved is None and env.store.get("demozoo", "api_key") is None


@pytest.mark.parametrize("values,secrets,clear,message", [
    ({"nope": 1}, {}, [], "no setting called nope"),
    ({"api_key": "x"}, {}, [], "is a secret"),
    ({}, {"collection": "x"}, [], "no secret called collection"),
    ({}, {}, ["max_requests"], "no secret called max_requests"),
])
def test_save_refuses_what_the_manifest_does_not_declare(env, values, secrets, clear, message):
    r = pd.save("demozoo", values, secrets, clear)
    assert not r["ok"] and message in r["error"]


def test_clearing_a_secret_removes_it(env):
    env.store.set("demozoo", "api_key", "old")
    assert pd.save("demozoo", clear=["api_key"]) == {"ok": True}
    assert env.store.get("demozoo", "api_key") is None


def test_try_search_asks_only_this_plugin(env, monkeypatch):
    seen = {}

    def fake_search(query, platform, limit, only=None):
        seen.update(query=query, platform=platform, only=only)
        return [{"title": "Second Reality", "platform": "dos", "size_bytes": 10}], ""

    monkeypatch.setattr(hub, "search", fake_search)
    r = pd.run("demozoo", "search", query=" Second Reality ", platform="dos")
    assert r["ok"] and r["rows"][0]["title"] == "Second Reality"
    assert seen == {"query": "Second Reality", "platform": "dos", "only": "demozoo"}


def test_try_enrich_needs_a_numeric_id_and_runs_the_hub(env, monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "_run_cli", lambda *a, **k: calls.append(a) or {"ok": True, "out": "rom 7: updated name", "err": ""})
    assert not pd.run("demozoo", "enrich", rom_id="7; rm -rf /")["ok"]
    r = pd.run("demozoo", "enrich", rom_id="7")
    assert r == {"ok": True, "lines": ["rom 7: updated name"]}
    assert calls == [("enrich", "demozoo", "7")]
    pd.run("demozoo", "enrich", rom_id="7", source_id="Super Metroid (Japan, USA) (En,Ja)")
    assert calls[-1] == ("enrich", "demozoo", "7", "--source-id", "Super Metroid (Japan, USA) (En,Ja)")
    assert not pd.run("demozoo", "enrich", rom_id="7", source_id="a\nb")["ok"]


def test_try_refuses_a_capability_the_plugin_lacks(env):
    assert "does not list cores" in pd.run("demozoo", "cores")["error"]
    assert "unknown action" in pd.run("demozoo", "explode")["error"]


def test_github_token_reaches_the_hub_cli_and_can_be_cleared():
    hub.configure_github_token("ghp_abc")
    try:
        assert hub.github_token_set()
        assert hub._host_env()["ROM_HUB_GITHUB_TOKEN"] == "ghp_abc"
    finally:
        hub.configure_github_token("")
    assert not hub.github_token_set()
    assert "ROM_HUB_GITHUB_TOKEN" not in hub._host_env()


def test_the_settings_api_keeps_the_github_token_write_only(tmp_path):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    svc.store.update_settings({"_github_token": "ghp_secret123"})
    safe = svc.safe_settings()
    assert "_github_token" not in safe and "ghp_secret123" not in str(safe)
    assert safe["github_token_set"] is True
    hub.configure_github_token("")
