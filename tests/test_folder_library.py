"""The filesystem backend: every frontend that reads a folder, at once.

RomM, Gaseous and Retrom are servers with APIs. Almost nothing else in the
emulation world is -- Batocera, RetroPie, Recalbox, EmulationStation and ES-DE,
EmuDeck, Pegasus, Lakka, muOS, ArkOS, LaunchBox, Playnite and Steam ROM Manager
all just read ROMs out of a directory laid out by platform.
"""

from romarr.app import ROMarr
from romarr.libraries import (
    FolderConfig, FolderLibrary, LIBRARY_KINDS, LIBRARY_TYPES,
    build_library, build_library_from_config,
)


def library(tmp_path, *files):
    for name in files:
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x00" * 2048)
    return FolderLibrary(FolderConfig(root=str(tmp_path)))


def test_a_missing_directory_is_unreachable_not_a_crash(tmp_path):
    lib = FolderLibrary(FolderConfig(root=str(tmp_path / "nope")))
    assert lib.configured is True
    assert lib.reachable() is False
    assert lib.count() == 0
    assert lib.games() == []


def test_an_unconfigured_folder_is_not_reachable():
    assert FolderLibrary(FolderConfig(root="")).configured is False
    assert FolderLibrary(FolderConfig(root="")).reachable() is False


def test_it_counts_roms_and_ignores_everything_else(tmp_path):
    lib = library(tmp_path,
                  "snes/Super Metroid.smc",
                  "snes/Chrono Trigger.sfc",
                  "genesis/Sonic.md",
                  "snes/readme.txt",           # not a ROM
                  "snes/boxart.png",           # not a ROM
                  "covers/art.jpg")
    assert lib.reachable() is True
    assert lib.count() == 3


def test_a_zipped_rom_counts_because_that_is_how_they_ship(tmp_path):
    lib = library(tmp_path, "nes/Contra.zip", "nes/Metroid.7z")
    assert lib.count() == 2


def test_the_platform_is_the_containing_directory(tmp_path):
    """Which is the layout every one of these frontends imposes -- and the same
    layout the importer writes, so a ROM ROMarr files lands where it is found."""
    lib = library(tmp_path, "snes/Super Metroid.smc", "genesis/Sonic.md")
    by_name = {g.name: g for g in lib.games()}
    assert by_name["Super Metroid"].platform == "snes"
    assert by_name["Sonic"].platform == "genesis"


def test_games_paginate(tmp_path):
    lib = library(tmp_path, *[f"snes/Game {n:02d}.smc" for n in range(10)])
    first = lib.games(limit=4)
    second = lib.games(limit=4, offset=4)
    assert len(first) == 4 and len(second) == 4
    assert {g.name for g in first}.isdisjoint({g.name for g in second})


def test_rescan_is_a_no_op_that_reports_success(tmp_path):
    """Nothing needs telling: the file is on disk and the frontend finds it on
    its next scan. False would mark every successful import as half-broken."""
    assert library(tmp_path).rescan("snes") is True


def test_the_scan_is_bounded_so_a_huge_library_cannot_stall_the_refresh(tmp_path):
    lib = library(tmp_path, *[f"snes/G{n}.smc" for n in range(20)])
    lib.MAX_SCAN = 5
    assert lib.count() == 5


def test_it_is_a_registered_kind_with_no_url_field():
    """There is no server. Offering an address field invites somebody to fill it
    in and then wonder why nothing connects."""
    assert "folder" in LIBRARY_KINDS
    fields = {f["name"] for f in LIBRARY_TYPES["folder"]["fields"]}
    assert "url" not in fields
    assert {"path", "name", "enable", "is_default", "platforms"} <= fields


def test_it_builds_from_the_environment_and_from_stored_config(tmp_path):
    from_env = build_library("folder", {"LIBRARY_PATH": str(tmp_path)})
    assert isinstance(from_env, FolderLibrary)
    assert from_env.reachable() is True

    from_cfg = build_library_from_config({"type": "folder", "path": str(tmp_path)})
    assert isinstance(from_cfg, FolderLibrary)
    assert from_cfg.reachable() is True


def test_the_older_romm_library_variable_still_selects_the_path(tmp_path):
    lib = build_library("folder", {"ROMM_LIBRARY": str(tmp_path)})
    assert lib.reachable() is True


def test_the_service_drives_it_end_to_end(tmp_path):
    roms = tmp_path / "roms"
    (roms / "snes").mkdir(parents=True)
    (roms / "snes" / "Super Metroid.smc").write_bytes(b"\x00" * 4096)

    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "LIBRARY_KIND": "folder",
                  "LIBRARY_PATH": str(roms)})

    assert [c["type"] for c, _ in svc.game_libraries] == ["folder"]
    row = svc.libraries_status()[0]
    assert row["ok"] is True
    assert row["path_exists"] is True
    assert svc.game_libraries[0][1].count() == 1


def test_a_folder_library_needs_no_credentials_to_be_seeded(tmp_path):
    """The whole appeal: no server, no account, no API key -- just the path."""
    roms = tmp_path / "roms"
    roms.mkdir()
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "LIBRARY_KIND": "folder",
                  "LIBRARY_PATH": str(roms)})
    stored = svc.store.list_items("libraries")
    assert len(stored) == 1
    assert stored[0]["type"] == "folder"
    assert stored[0]["path"] == str(roms)
