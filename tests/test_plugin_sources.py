"""Plugin sources: ROMarr's own forge is allowed, and extra forges are a real setting."""
import pytest

from romarr.catalogue import DEFAULT_ALLOWED_HOSTS, check_source, merged_hosts, parse_hosts
from romarr.store import Store


def test_romarrs_own_forge_is_allowed_out_of_the_box():
    assert DEFAULT_ALLOWED_HOSTS[-2:] == ("git.moveweight.com", "git.romarr.app")
    assert check_source("https://git.romarr.app/BlizzHacker/rom-hub-theromdepot").ok
    assert check_source("https://git.moveweight.com/BlizzHacker/rom-hub-theromdepot").ok


def test_extra_forges_add_to_the_defaults_and_never_replace_them():
    assert check_source("https://forge.example.org/me/plugin", "forge.example.org").ok
    assert check_source("https://github.com/me/plugin", "forge.example.org").ok
    assert merged_hosts("forge.example.org, git.example.com")[-2:] == ["forge.example.org", "git.example.com"]


def test_an_unknown_forge_says_exactly_where_to_add_it():
    refused = check_source("https://forge.example.org/me/plugin")
    assert not refused.ok
    assert "Settings -> General -> Extra plugin sources" in refused.reason


@pytest.mark.parametrize("bad", ["https://forge.example.org", "forge.example.org/path", "forge:8443", "not a host"])
def test_only_plain_hostnames_are_accepted(bad):
    with pytest.raises(ValueError):
        parse_hosts(bad)


def test_the_setting_is_saved_normalised(tmp_path):
    store = Store(tmp_path / "romarr.json")
    store.update_settings({"plugin_hosts": " Forge.Example.org\ngit.example.com, forge.example.org "})
    assert store.settings["plugin_hosts"] == "forge.example.org, git.example.com"
    with pytest.raises(ValueError):
        store.update_settings({"plugin_hosts": "https://nope.example"})
    assert store.settings["plugin_hosts"] == "forge.example.org, git.example.com"
