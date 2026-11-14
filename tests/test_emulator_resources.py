"""Resource coverage is separate from installable emulator software."""
from urllib.parse import urlsplit

from romarr.emulator_catalog import resource_catalog


def test_resource_inventory_keeps_every_supplied_reference():
    """Named sources remain discoverable even when their URL is uncertain."""
    data = resource_catalog(limit=100)
    assert data["total"] == data["available"] == 58
    names = {row["name"].casefold() for row in data["items"]}
    assert {"internet archive", "emulation general wiki", "classicreload"} <= names
    assert any(row["name"].casefold() == "minerva archive" for row in data["items"])
    assert {row["category"] for row in data["items"]} == {"browser", "source", "reference"}
    assert any(not row["url"] for row in data["items"])


def test_resource_links_and_search_have_explicit_boundaries():
    """Unverified destinations cannot become guessed or executable links."""
    for row in resource_catalog(limit=100)["items"]:
        assert not row["url"] or urlsplit(row["url"]).scheme in ("http", "https")
        assert row["description"]
    assert any(row["name"] == "Internet Archive" for row in resource_catalog(query="Internet Archive")["items"])
    assert resource_catalog(offset=50, limit=100)["total"] == 58
