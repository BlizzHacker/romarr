"""Saved plugin secrets reach plugins that ROMarr starts in-process."""
from types import SimpleNamespace

from romarr import hub


def test_secrets_are_resolved_with_the_hub_clis_own_helper(monkeypatch):
    import rom_hub.cli as cli
    seen = {}

    def fake_prepare(plugin, root):
        seen["args"] = (plugin.slug, root)
        return {"api_key": "k"}

    monkeypatch.setattr(cli, "prepare_secrets", fake_prepare)
    plugin = SimpleNamespace(slug="demozoo")
    assert hub._plugin_secrets(plugin) == {"api_key": "k"}
    assert seen["args"] == ("demozoo", hub.HOME)


def test_an_unreadable_store_means_no_secrets_not_a_failure(monkeypatch):
    import rom_hub.cli as cli

    def broken(plugin, root):
        raise RuntimeError("keyring locked")

    monkeypatch.setattr(cli, "prepare_secrets", broken)
    assert hub._plugin_secrets(SimpleNamespace(slug="curseforge")) == {}


def test_every_in_process_plugin_start_passes_secrets():
    """search_all and both PluginProcess starts in hub.py hand secrets over."""
    import inspect
    source = inspect.getsource(hub)
    assert "secrets_for=_plugin_secrets" in source
    assert source.count("secrets=_plugin_secrets(plugin)") == 2
    assert source.count("PluginProcess(") == source.count("secrets=_plugin_secrets(plugin)")
