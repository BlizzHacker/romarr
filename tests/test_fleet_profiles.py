"""Tests for multi-device fleet and standalone gaming launcher profiles."""

from romarr import hub


def test_fleet_profiles_contains_handhelds_and_retro_setups():
    res = hub.fleet_profiles()
    assert "profiles" in res
    profiles = {p["id"]: p for p in res["profiles"]}

    # Verify existing profiles remain intact
    assert "es-de" in profiles
    assert "retropie-arcadepunks" in profiles
    assert "argosy" in profiles
    assert "grout" in profiles
    assert "deckrommsync" in profiles

    # Verify newly added SteamOS, Retro Punks Arcade / Batocera, and Adreno profiles
    assert "steamos-deck" in profiles
    assert "rpi-batocera-arcadepunks" in profiles
    assert "adreno-android-handhelds" in profiles

    steamos = profiles["steamos-deck"]
    assert "SteamOS" in steamos["name"]
    assert steamos["gamelist_format"] == "esde"
    assert steamos["igir_token"] == "{es}"

    adreno = profiles["adreno-android-handhelds"]
    assert "Adreno" in adreno["name"] or "Snapdragon" in adreno["name"]
    assert adreno["igir_token"] == "{romm}"

    rpi_arcade = profiles["rpi-batocera-arcadepunks"]
    assert "Arcade Punks" in rpi_arcade["name"]
    assert rpi_arcade["igir_token"] == "{batocera}"


def test_build_fleet_sync_plan_for_steamos_and_adreno():
    plan_steamos = hub.build_fleet_sync_plan({
        "device_id": "steamos-deck",
        "source_path": "/home/deck/Emulation/roms",
    })
    assert plan_steamos["ok"] is True
    assert plan_steamos["device"]["id"] == "steamos-deck"
    assert "igir" in plan_steamos["igir_command"]

    plan_adreno = hub.build_fleet_sync_plan({
        "device_id": "adreno-android-handhelds",
        "prefer_regions": ["USA", "WORLD"],
    })
    assert plan_adreno["ok"] is True
    assert plan_adreno["device"]["id"] == "adreno-android-handhelds"
