"""The generated API key has to survive a restart.

Found on a live install: `romarr.json` was a week older than the running
process, because nothing saved the store after the key was generated. The key
therefore existed only in memory and was a different one after every restart --
so every script authenticating against it broke on restart, and the value shown
under Settings -> General was one nobody could rely on.

Nothing failed loudly. The service started, the UI worked, and only somebody
holding a key from an earlier boot would notice.
"""

from __future__ import annotations

import json

from romarr.app import ROMarr


def test_a_generated_key_is_written_to_disk_immediately(tmp_path):
    store = tmp_path / "s.json"
    service = ROMarr({"ROMARR_DATA": str(store)})
    assert store.exists(), "nothing was saved after generating a key"
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk["settings"]["_api_key"] == service.store.settings["_api_key"]


def test_the_same_key_comes_back_after_a_restart(tmp_path):
    store = str(tmp_path / "s.json")
    first = ROMarr({"ROMARR_DATA": store}).store.settings["_api_key"]
    second = ROMarr({"ROMARR_DATA": store}).store.settings["_api_key"]
    assert first == second, "the API key rotated across a restart"
    assert first  # and it is not simply empty both times


def test_a_key_that_survives_actually_authenticates_after_a_restart(tmp_path):
    """The property that matters to a script, rather than to the store."""
    store = str(tmp_path / "s.json")
    key = ROMarr({"ROMARR_DATA": store}).store.settings["_api_key"]
    revived = ROMarr({"ROMARR_DATA": store})
    assert revived.auth.check_key(key) is True


def test_an_operator_supplied_key_is_not_written_back(tmp_path):
    """It belongs to the environment. Persisting it would mean removing the
    variable silently kept the old value, which is not what unsetting means."""
    store = tmp_path / "s.json"
    ROMarr({"ROMARR_DATA": str(store), "ROMARR_API_KEY": "from-the-template"})
    if store.exists():
        on_disk = json.loads(store.read_text(encoding="utf-8"))
        assert on_disk["settings"].get("_api_key") != "from-the-template"


def test_an_existing_stored_key_is_not_replaced(tmp_path):
    store = tmp_path / "s.json"
    first = ROMarr({"ROMARR_DATA": str(store)})
    kept = first.store.settings["_api_key"]
    first.store.settings["library_path"] = "/somewhere"
    first.store.save()

    again = ROMarr({"ROMARR_DATA": str(store)})
    assert again.store.settings["_api_key"] == kept
    assert again.store.settings["library_path"] == "/somewhere"
