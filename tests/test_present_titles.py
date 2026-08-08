"""What a set plan counts as "already on the shelf".

Collections asked the cached shelf. That shelf is a page kept for the Library
grid, so on the install this was run against -- 166,578 games -- a 1,032-title
GameCube plan was compared with the first few hundred rows and reported almost
everything as missing. The number was not slightly wrong, it was meaningless,
and nothing about it looked broken.

A plan concerns one platform, and a platform is one directory.
"""

from __future__ import annotations

from romarr.app import ROMarr
from romarr.collections import PRESENT_UNKNOWN, PRESENT_VERIFIED
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
    service, _ = service_with(tmp_path, shelf=["Some Game (USA)"])
    assert "Some Game (USA)" in service._present_titles("nosuchplatform")


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
