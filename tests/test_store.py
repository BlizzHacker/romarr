import json

from rommarr.store import DEFAULT_SETTINGS, Event, Store


def test_history_and_settings_survive_a_restart(tmp_path):
    """The whole point of the store: a restart used to lose everything."""
    path = tmp_path / "rommarr.json"
    s = Store(path)
    s.record(Event(kind="grabbed", game="Super Mario World", platform="snes",
                   release="Super Mario World (USA)", seeders=120))
    s.update_settings({"min_seeders": 5, "library_path": "/srv/roms"})

    reopened = Store(path)
    assert reopened.settings["min_seeders"] == 5
    assert reopened.settings["library_path"] == "/srv/roms"
    assert len(reopened.events) == 1
    assert reopened.events[0].game == "Super Mario World"


def test_a_setting_added_later_gets_its_default(tmp_path):
    """An older file must not leave a new setting missing entirely."""
    path = tmp_path / "rommarr.json"
    path.write_text(json.dumps({"settings": {"min_seeders": 9}}), encoding="utf-8")
    s = Store(path)
    assert s.settings["min_seeders"] == 9
    for key, value in DEFAULT_SETTINGS.items():
        if key != "min_seeders":
            assert s.settings[key] == value


def test_unknown_settings_are_dropped_rather_than_stored(tmp_path):
    """A typo in a PUT should not become permanent state nothing reads."""
    s = Store(tmp_path / "r.json")
    s.update_settings({"min_seeders": 3, "totally_made_up": "yes"})
    assert s.settings["min_seeders"] == 3
    assert "totally_made_up" not in s.settings


def test_a_corrupt_file_does_not_stop_the_service_starting(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = Store(path)
    assert s.settings == DEFAULT_SETTINGS
    assert s.events == []


def test_requesting_the_same_game_twice_is_a_retry_not_a_duplicate(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Contra", "nes")
    s.want("contra", "nes")          # different case, same game
    s.want("Contra", "snes")         # different platform, genuinely different
    assert len(s.wanted) == 2


def test_an_arrival_clears_the_wanted_entry(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Zelda", "snes")
    assert s.fulfil("zelda", "snes") is True
    assert s.missing() == []
    # Fulfilling something that was never wanted is a no-op, not an error.
    assert s.fulfil("Zelda", "snes") is False


def test_failures_are_counted_against_the_wanted_entry(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Earthbound", "snes")
    s.note_failure("Earthbound", "snes", "no usable release")
    s.note_failure("Earthbound", "snes", "no usable release")
    item = s.missing()[0]
    assert item["attempts"] == 2
    assert item["last_error"] == "no usable release"


def test_history_is_newest_first_and_filterable(tmp_path):
    s = Store(tmp_path / "r.json")
    s.record(Event(kind="grabbed", game="A", platform="nes"))
    s.record(Event(kind="failed", game="B", platform="nes"))
    s.record(Event(kind="imported", game="C", platform="nes"))

    assert [e["game"] for e in s.history()] == ["C", "B", "A"]
    assert [e["game"] for e in s.history(kind="failed")] == ["B"]
    assert len(s.history(limit=2)) == 2


def test_history_is_capped_so_the_file_cannot_grow_without_bound(tmp_path):
    path = tmp_path / "r.json"
    s = Store(path)
    s.MAX_EVENTS = 10
    for i in range(25):
        s.record(Event(kind="grabbed", game=f"Game {i}", platform="nes"))
    assert len(s.events) == 10
    # And the cap has to survive the round trip, not just live in memory.
    assert len(json.loads(path.read_text(encoding="utf-8"))["events"]) == 10


def test_a_write_leaves_no_temp_files_behind(tmp_path):
    s = Store(tmp_path / "r.json")
    s.record(Event(kind="grabbed", game="A", platform="nes"))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".rommarr-")]
    assert leftovers == []
