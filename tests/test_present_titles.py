"""What a set plan counts as "already on the shelf".

Collections asked the cached shelf. That shelf is a page kept for the Library
grid, so on the install this was run against -- 166,578 games -- a 1,032-title
GameCube plan was compared with the first few hundred rows and reported almost
everything as missing. The number was not slightly wrong, it was meaningless,
and nothing about it looked broken.

A plan concerns one platform, and a platform is one directory.
"""

from __future__ import annotations

import zipfile
import zlib

from romarr.app import ROMarr
from romarr.collections import PRESENT_UNKNOWN, PRESENT_VERIFIED
from romarr.dat import Dat, Game as DatGame, Rom
from romarr.libraries import Game


def service_with(tmp_path, shelf=None):
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    service.game_libraries = [({"name": "L", "type": "folder",
                                "path": str(library)}, object())]
    if shelf is not None:
        service._library_cache = ([Game(id=str(i), name=n)
                                   for i, n in enumerate(shelf)], 0.0, "")
    return service, library


def test_the_platform_directory_is_read_rather_than_the_shelf(tmp_path):
    service, library = service_with(tmp_path, shelf=["Only Cached Game"])
    platform = library / "ngc"
    platform.mkdir()
    for name in ("Luigi's Mansion (USA).iso", "1080 Avalanche (USA).iso"):
        (platform / name).write_text("x")

    present = service._present_titles("ngc")

    assert set(present) == {"Luigi's Mansion (USA)", "1080 Avalanche (USA)"}
    assert "Only Cached Game" not in present, (
        "the paged shelf was used instead of the directory")


def test_a_disc_set_stored_as_a_directory_counts_as_present(tmp_path):
    """Multi-track rips land as a folder named for the game."""
    service, library = service_with(tmp_path)
    platform = library / "psx"
    platform.mkdir()
    (platform / "Final Fantasy VII (USA)").mkdir()
    (platform / "Final Fantasy VII (USA)" / "disc1.bin").write_text("x")

    assert "Final Fantasy VII (USA)" in service._present_titles("psx")


def test_it_falls_back_to_the_shelf_when_there_is_no_directory(tmp_path):
    """A server-backed library ROMarr cannot list on disk still gets an
    answer, just a partial one."""
    service, library = service_with(tmp_path)
    service._library_cache = ([
        *(Game(id=str(i), name=f"NES Friend {i} (USA)", platform="nes")
          for i in range(125)),
        Game(id="snes", name="Some Game (USA)", platform="Super Nintendo"),
        Game(id="cloud", name="Another Game (USA)", platform="snes", origin="cloud"),
    ], 0.0, "")
    scan = {}
    assert service._present_titles("snes", scan=scan) == {
        "Some Game (USA)": PRESENT_UNKNOWN,
    }
    assert scan == {"folders": [{"path": str(library / "snes"), "files": None}],
                    "used_cache": True, "cached_rows": 1}


def test_an_import_verdict_upgrades_a_plain_presence(tmp_path):
    from romarr.store import Event

    service, library = service_with(tmp_path)
    platform = library / "nes"
    platform.mkdir()
    (platform / "Contra (USA).nes").write_text("x")
    service.store.record(Event(kind="imported", game="Contra (USA)",
                               platform="nes", detail="verified: /x/y"))

    present = service._present_titles("nes")
    assert present["Contra (USA)"] == PRESENT_VERIFIED


def test_a_file_with_no_recorded_verdict_is_present_not_verified(tmp_path):
    """Being on disk says it is there. It does not say it is right."""
    service, library = service_with(tmp_path)
    platform = library / "nes"
    platform.mkdir()
    (platform / "Contra (USA).nes").write_text("x")
    assert service._present_titles("nes")["Contra (USA)"] == PRESENT_UNKNOWN


def test_an_empty_platform_directory_is_not_an_error(tmp_path):
    service, library = service_with(tmp_path)
    (library / "snes").mkdir()
    assert service._present_titles("snes") == {}


def test_a_nested_library_is_read_from_its_roms_folder(tmp_path):
    """Structure B files ROMs in <root>/<platform>/roms. The planner scanned
    <root>/<platform>, found only the `roms` directory, and reported every
    title as missing (issue #34)."""
    service, library = service_with(tmp_path)
    service.game_libraries = [({"name": "L", "type": "folder",
                                "path": str(library), "layout": "nested"},
                               object())]
    nested = library / "ps2" / "roms"
    nested.mkdir(parents=True)
    (nested / "Dark Cloud (Europe) (En,Fr,De,Es,It).iso").write_text("x")

    present = service._present_titles("ps2")

    assert set(present) == {"Dark Cloud (Europe) (En,Fr,De,Es,It)"}


def test_a_flat_library_is_unchanged_by_the_layout_fix(tmp_path):
    service, library = service_with(tmp_path)
    (library / "ps2").mkdir()
    (library / "ps2" / "Manhunt (Europe).iso").write_text("x")

    assert set(service._present_titles("ps2")) == {"Manhunt (Europe)"}


def test_a_game_filed_in_a_subfolder_counts_as_present(tmp_path):
    """Recursion, the question that was asked and the bug behind it.

    The planner used iterdir(): a game filed one directory deeper than the
    platform root surfaced as its PARENT DIRECTORY's name, and the title
    itself read as missing -- so a collection batch queued a fresh download
    of a game already on disk.
    """
    service, library = service_with(tmp_path)
    platform = library / "snes"
    (platform / "Nintendo").mkdir(parents=True)
    (platform / "Nintendo" / "Chrono Trigger (USA).sfc").write_text("x")

    present = service._present_titles("snes")

    assert "Chrono Trigger (USA)" in present
    assert "Nintendo" in present, (
        "the containing folder also counts: a library organized by "
        "publisher/genre must not lose its disc-set-style folders")


def test_translations_subfolder_contents_are_seen(tmp_path):
    """keep-both policy files T-En patches under Translations/; the planner
    must see the titles there or it re-downloads every patched game."""
    service, library = service_with(tmp_path)
    trans = library / "snes" / "Translations"
    trans.mkdir(parents=True)
    (trans / "Final Fantasy VI (USA) (T-En).sfc").write_text("x")

    assert "Final Fantasy VI (USA) (T-En)" in service._present_titles("snes")


def test_a_deep_disc_set_answers_to_its_directory_name(tmp_path):
    service, library = service_with(tmp_path)
    disc = library / "psx" / "Square" / "Final Fantasy VII (USA)"
    disc.mkdir(parents=True)
    (disc / "FF7 (USA) (Disc 1).bin").write_text("x")
    (disc / "FF7 (USA) (Disc 2).bin").write_text("x")

    present = service._present_titles("psx")

    assert "Final Fantasy VII (USA)" in present
    assert "FF7 (USA) (Disc 1)" in present


def test_collection_preview_and_queue_recognize_renamed_zips(tmp_path):
    """The SNES preview identifies dumps whose archive names omit DAT tags."""
    service, library = service_with(tmp_path)
    folder = library / "snes"
    folder.mkdir()
    records = {
        "Zombies Ate My Neighbors (USA)": b"zombies usa",
        "Zombies Ate My Neighbors (Europe)": b"zombies europe",
        "Zool (USA)": b"zool usa",
        "Zool (USA) (Sample)": b"zool sample",
        "Zoop (USA)": b"zoop usa",
        "Zoop (Europe)": b"zoop europe",
    }
    dat = Dat("SNES", games={
        name: DatGame(name, (Rom(name + ".sfc", len(data),
                                 f"{zlib.crc32(data):08x}"),))
        for name, data in records.items()
    })
    service.dats.add(dat)
    for archive_name, dat_name in {
        "zombies ate my neighbors.zip": "Zombies Ate My Neighbors (USA)",
        "zool (sample cart).zip": "Zool (USA) (Sample)",
        "zool.zip": "Zool (USA)",
        "zoop.zip": "Zoop (Europe)",
    }.items():
        with zipfile.ZipFile(folder / archive_name, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("rom.sfc", records[dat_name])
            if archive_name == "zool.zip":
                archive.comment = b"ZIPs with archive comments also match."

    plan = service.collection_plan("SNES", "snes", regions=["usa", "europe"])
    assert plan["library_scan"] == {
        "folders": [{"path": str(folder), "files": 4}],
        "used_cache": False,
        "cached_rows": 0,
    }
    assert {t["name"]: t["status"] for t in plan["titles"]} == {
        "Zombies Ate My Neighbors (USA)": PRESENT_UNKNOWN,
        "Zool (USA)": PRESENT_UNKNOWN,
        "Zoop (USA)": "missing",
    }
    batch = service.collection_start("SNES", "snes", regions=["usa", "europe"])
    assert batch["queued"] == 1
    assert service._batches()[batch["id"]].queue == ["Zoop (USA)"]


def test_a_dat_filename_matches_case_insensitively_with_variant_tags(tmp_path):
    """Exact region and revision still matter when matching by name."""
    service, library = service_with(tmp_path)
    folder = library / "snes"
    folder.mkdir()
    (folder / "chrono trigger (usa) (rev 1).7z").touch()
    names = ("Chrono Trigger (USA) (Rev 1)", "Chrono Trigger (USA)")
    dat = Dat("SNES", games={name: DatGame(name) for name in names})

    assert service._present_titles("snes", dat) == {names[0]: PRESENT_UNKNOWN}


def test_zip_headers_do_not_guess_from_ambiguous_or_partial_dumps(tmp_path):
    """A CRC shared by variants, a single track and a multi-ROM ZIP are ambiguous."""
    service, library = service_with(tmp_path)
    folder = library / "snes"
    folder.mkdir()
    games = {}

    def rom(data):
        return Rom("rom.sfc", len(data), f"{zlib.crc32(data):08x}")

    for name in ("Shared (USA)", "Shared (Japan)"):
        games[name] = DatGame(name, (rom(b"same dump"),))
    games["Tracks (USA)"] = DatGame("Tracks (USA)", (rom(b"track 1"), rom(b"track 2")))
    games["Cartridge (USA)"] = DatGame("Cartridge (USA)", (rom(b"cartridge"),))
    for name, data in (("shared", b"same dump"), ("tracks", b"track 1"),
                       ("two roms", b"cartridge")):
        with zipfile.ZipFile(folder / (name + ".zip"), "w") as archive:
            archive.writestr("rom.sfc", data)
            if name == "two roms":
                archive.writestr("extra.sfc", b"other rom")
    (folder / "cartridge.zip").write_bytes(b"not a ZIP")

    assert service._present_titles("snes", Dat("SNES", games=games)) == {}



def test_a_zip_using_an_unsupported_version_does_not_abort_the_preview(tmp_path):
    """ZIP metadata that this Python cannot parse is skipped."""
    service, library = service_with(tmp_path)
    folder = library / "snes"
    folder.mkdir()
    name, data = "Game (USA)", b"cart data"
    dat = Dat("SNES", games={name: DatGame(
        name, (Rom("game.sfc", len(data), f"{zlib.crc32(data):08x}"),))})
    service.dats.add(dat)
    with zipfile.ZipFile(folder / "bad.zip", "w") as archive:
        info = zipfile.ZipInfo("game.sfc")
        info.extract_version = zipfile.MAX_EXTRACT_VERSION + 1
        archive.writestr(info, data)

    plan = service.collection_plan("SNES", "snes")
    assert plan["counts"]["have"] == 0
    assert plan["counts"]["missing"] == 1
