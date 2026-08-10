import pytest

from romarr.store import Store


def store(tmp_path):
    return Store(tmp_path / "s.json")


def test_status_rating_and_notes_round_trip(tmp_path):
    s = store(tmp_path)
    s.set_game_meta("snes", "Chrono Trigger", status="playing", rating=9,
                    notes="third playthrough")
    meta = s.game_meta("snes", "Chrono Trigger")
    assert meta["status"] == "playing"
    assert meta["rating"] == 9
    assert meta["notes"] == "third playthrough"


def test_meta_survives_a_restart(tmp_path):
    store(tmp_path).set_game_meta("snes", "Earthbound", status="completed")
    again = store(tmp_path)
    assert again.game_meta("snes", "Earthbound")["status"] == "completed"


def test_lookup_is_case_insensitive_like_wanted(tmp_path):
    s = store(tmp_path)
    s.set_game_meta("snes", "EarthBound", rating=10)
    assert s.game_meta("snes", "earthbound")["rating"] == 10


def test_partial_update_leaves_other_fields_alone(tmp_path):
    s = store(tmp_path)
    s.set_game_meta("snes", "Terranigma", status="playing", notes="save at boss")
    s.set_game_meta("snes", "Terranigma", rating=8)
    meta = s.game_meta("snes", "Terranigma")
    assert meta["status"] == "playing"
    assert meta["notes"] == "save at boss"
    assert meta["rating"] == 8


def test_clearing_every_field_removes_the_record(tmp_path):
    s = store(tmp_path)
    s.set_game_meta("snes", "ActRaiser", status="shelved", rating=6, notes="x")
    s.set_game_meta("snes", "ActRaiser", status="", rating=0, notes="")
    assert s.game_meta("snes", "ActRaiser") == {}
    assert s.all_game_meta() == []


def test_an_unknown_status_is_refused_with_the_valid_ones_named(tmp_path):
    with pytest.raises(ValueError) as err:
        store(tmp_path).set_game_meta("snes", "X", status="backlogged")
    assert "playing" in str(err.value)


def test_a_rating_off_the_scale_is_refused(tmp_path):
    with pytest.raises(ValueError):
        store(tmp_path).set_game_meta("snes", "X", rating=11)


def test_wanted_and_owned_are_not_storable_statuses(tmp_path):
    """Both exist in the UI, derived: wanted IS the wanted list and owned is
    the library. Storing either would create a second copy that drifts."""
    for derived in ("wanted", "owned"):
        with pytest.raises(ValueError):
            store(tmp_path).set_game_meta("snes", "X", status=derived)


def test_mark_searched_stamps_only_the_matching_item(tmp_path):
    s = store(tmp_path)
    s.want("Chrono Trigger", "snes")
    s.want("Earthbound", "snes")
    s.mark_searched("Chrono Trigger", "snes")
    rows = {w["game"]: w for w in s.missing()}
    assert rows["Chrono Trigger"]["searched_at"]
    assert rows["Earthbound"]["searched_at"] == ""
