"""Tests for host-side FlareSolverr configuration and expanded ROM Hub UI."""
from __future__ import annotations

import pytest

from romarr import hub, ui
from romarr.store import Store


def test_store_validates_and_normalizes_flaresolverr_url(tmp_path):
    store = Store(tmp_path / "romarr.json")
    updated = store.update_settings({"flaresolverr_url": "  http://localhost:8191/  "})
    assert updated["flaresolverr_url"] == "http://localhost:8191"

    with pytest.raises(ValueError):
        store.update_settings({"flaresolverr_url": "ftp://localhost:8191"})
    with pytest.raises(ValueError):
        store.update_settings({"flaresolverr_url": "not-a-url"})
    assert store.settings["flaresolverr_url"] == "http://localhost:8191"

    cleared = store.update_settings({"flaresolverr_url": ""})
    assert cleared["flaresolverr_url"] == ""


def test_hub_flaresolverr_configuration_and_ui_discovery():
    hub.configure_flaresolverr("http://localhost:8191/")
    try:
        assert hub.flaresolverr_url() == "http://localhost:8191"
    finally:
        hub.configure_flaresolverr("")

    html = ui.page()
    assert 'data-k="flaresolverr_url"' in html
    assert 'data-quick="' in html
    for kw in ("'guides','Guides & FAQs'", "'tutorials','Video Tutorials'",
               "'mods','Mods & Addons'", "'bots','Bots & Tools'",
               "'github','Open-Source Repos'"):
        assert kw in html
    for label in ("GameFAQs", "StrategyWiki", "CurseForge", "ModDB", "Nexus Mods",
                  "Modrinth", "Thunderstore", "GameBanana", "GitHub"):
        assert label in html
