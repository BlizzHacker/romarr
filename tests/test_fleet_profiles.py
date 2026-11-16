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


def test_fleet_profiles_split_files_vs_app_and_never_delete_by_default():
    res = hub.fleet_profiles()
    assert res["kinds"] == {
        "files": "Copy games onto the device's ROM folder",
        "app": "The device runs an app that connects to your library server",
    }
    by_id = {p["id"]: p for p in res["profiles"]}
    assert {p["kind"] for p in res["profiles"]} == {"files", "app"}
    for pid in ("es-de", "retropie-arcadepunks", "rpi-batocera-arcadepunks", "steamos-deck", "adreno-android-handhelds"):
        assert by_id[pid]["kind"] == "files"
        assert "{platform}" not in by_id[pid]["default_path"]
    for pid in ("argosy", "grout", "deckrommsync", "retroarch-webdav", "console-cfw-feeds"):
        assert by_id[pid]["kind"] == "app"
        assert by_id[pid]["default_path"] == ""

    # Default local plan is non-destructive and strips legacy {platform} suffix
    local_plan = hub.build_fleet_sync_plan({
        "device_id": "retropie-arcadepunks",
        "transport": "local",
        "target_path": "/home/pi/RetroPie/roms/{platform}",
    })
    assert local_plan["ok"] is True
    assert local_plan["kind"] == "files"
    assert local_plan["mirror"] is False
    assert local_plan["warnings"] == []
    assert local_plan["target_path"] == "/home/pi/RetroPie/roms"
    assert local_plan["output_path"] == "/home/pi/RetroPie/roms/{es}"
    assert "clean" not in local_plan["igir_command"].split()
    assert local_plan["preview_command"].startswith("igir test report ")
    assert "copy" not in local_plan["preview_command"].split()

    # Default SSH plan does not pass --delete-after
    ssh_plan = hub.build_fleet_sync_plan({
        "device_id": "rpi-batocera-arcadepunks",
        "transport": "ssh",
    })
    assert "--delete" not in ssh_plan["network_sync_command"]
    assert "root@batocera.local:/userdata/roms/" in ssh_plan["network_sync_command"]

    # Default rclone plan uses rclone copy, not rclone sync
    rclone_plan = hub.build_fleet_sync_plan({
        "device_id": "es-de",
        "transport": "rclone",
        "target_path": "remote:roms",
    })
    assert rclone_plan["network_sync_command"].startswith("rclone copy ")

    # Explicit mirror=True enables clean / --delete-after / sync and warns
    mirror_local = hub.build_fleet_sync_plan({
        "device_id": "es-de",
        "transport": "local",
        "mirror": True,
    })
    assert mirror_local["mirror"] is True
    assert "clean" in mirror_local["igir_command"].split()
    assert mirror_local["warnings"]

    mirror_ssh = hub.build_fleet_sync_plan({
        "device_id": "retropie-arcadepunks",
        "transport": "ssh",
        "mirror": True,
    })
    assert "--delete-after" in mirror_ssh["network_sync_command"]
    assert mirror_ssh["warnings"]


def test_app_profiles_return_setup_steps_not_filesystem_commands():
    plan = hub.build_fleet_sync_plan({
        "device_id": "argosy",
        "library_url": "https://romm.example.lan/",
    })
    assert plan["ok"] is True
    assert plan["kind"] == "app"
    assert plan["igir_command"] == ""
    assert plan["network_sync_command"] == ""
    assert any("https://romm.example.lan" in step for step in plan["setup"])

    bad = hub.build_fleet_sync_plan({"device_id": "nonexistent-device"})
    assert bad["ok"] is False


def test_library_shell_ui_has_no_hardcoded_host_hacks():
    from romarr.ui import page
    html = page()
    assert "romm.moveweight.com" not in html
    assert "192.168.0.94" not in html


def test_library_and_devices_page_is_named_plainly_and_agents_have_their_own_page():
    """AI agents are not a device or a library app: they get their own page,
    and the page that remains says what it is."""
    from romarr.ui import JS, page
    html = page()
    assert "Library &amp; Devices" in html or "Library & Devices" in html
    assert 'data-page="agents"' in html
    assert "RENDER.agents=" in JS
    portal = JS[JS.index("RENDER.portal="):JS.index("RENDER.agents=")]
    assert "data-ptab=\"mcp\"" not in portal
    assert "/api/v1/mcp" not in portal
    assert "go('agents')" in portal          # old saved tab still lands somewhere
    for jargon in ("Library Shell", "Device Fleet", "Load in Shell", "npx igir@latest"):
        assert jargon not in JS, jargon


def test_device_plan_page_offers_copy_and_shows_errors():
    from romarr.ui import JS
    portal = JS[JS.index("RENDER.portal="):JS.index("RENDER.agents=")]
    assert "data-copycmd" in portal
    assert "Could not build the plan" in portal


def test_home_relative_device_folders_still_expand_in_the_shell():
    """`~/ROMs` inside single quotes is a folder named "~", not home."""
    import subprocess
    local = hub.build_fleet_sync_plan({"device_id": "es-de", "source_path": "/roms"})
    output_arg = local["igir_command"].split("--output ")[1].split(" ")[0]
    assert output_arg == "~/'ROMs/{es}'"
    expanded = subprocess.run(["bash", "-c", "printf %s " + output_arg],
                              capture_output=True, text=True, check=True).stdout
    assert expanded.startswith("/") and expanded.endswith("/ROMs/{es}")

    remote = hub.build_fleet_sync_plan({"device_id": "steamos-deck", "transport": "ssh"})
    assert "deck@steamdeck.local:Emulation/roms/" in remote["network_sync_command"]
    assert "~" not in remote["network_sync_command"]
