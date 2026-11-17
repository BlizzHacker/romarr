"""Monitoring (Sonarr/Radarr style) and the add-game lookup."""

from __future__ import annotations

from romarr.store import Store, WantedItem


def _store(tmp_path):
    return Store(str(tmp_path / "romarr.json"))


def test_wanted_entries_are_monitored_by_default(tmp_path):
    store = _store(tmp_path)
    item = store.want("Chrono Trigger", "snes")
    assert item.monitored is True


def test_unmonitored_entry_stays_listed_and_can_be_toggled(tmp_path):
    store = _store(tmp_path)
    store.want("Chrono Trigger", "snes", monitored=False)
    assert store.missing()[0]["monitored"] is False
    assert store.set_monitored("chrono trigger", "snes", True) is True
    assert store.missing()[0]["monitored"] is True
    assert store.set_monitored("Nope", "snes", True) is False


def test_old_state_without_the_field_loads_as_monitored(tmp_path):
    import json
    path = tmp_path / "romarr.json"
    path.write_text(json.dumps({"wanted": [{"game": "Zelda", "platform": "nes"}]}))
    store = Store(str(path))
    assert store.wanted[0].monitored is True
    assert isinstance(store.wanted[0], WantedItem)


def test_igdb_search_never_sends_sort():
    """IGDB answers HTTP 406 to `search` combined with `sort`."""
    from romarr import metadata
    seen = {}

    def fake_query(cfg, endpoint, body, strict=False):
        seen["body"] = body
        return []

    original = metadata.igdb_query
    metadata.igdb_query = fake_query
    try:
        metadata.igdb_catalog_search({"client_id": "x", "token": "y"}, "zelda", 5, 0)
    finally:
        metadata.igdb_query = original
    assert "search \"zelda\"" in seen["body"] and "sort " not in seen["body"]
