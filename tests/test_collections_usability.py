"""Collections must be usable without reading the source.

A set queued with no platform requested every title for nowhere and still
read 100% done; a queued set never advanced unless someone pressed Run next
for every slice. These pin the fixes.
"""

from __future__ import annotations

from romarr.app import ROMarr
from romarr.collections import Batch, platform_for_dat
from romarr.dat import Dat, Game, Rom


def test_a_dat_name_suggests_its_platform():
    assert platform_for_dat("Nintendo - Super Nintendo Entertainment System (20240101-000000)") == "snes"
    assert platform_for_dat("Nintendo - Game Boy Advance") == "gba"
    assert platform_for_dat("Sega - Master System - Mark III") == "sms"
    assert platform_for_dat("Sony - PlayStation") == "psx"


def test_the_vendor_alone_never_picks_a_platform():
    # "Nintendo" is an NES alias; an unknown Nintendo system must not become the NES.
    assert platform_for_dat("Nintendo - Something Nobody Ships") == ""
    assert platform_for_dat("") == ""


def test_the_operators_catalog_mapping_wins():
    assert platform_for_dat("My Odd DAT", {"my odd dat": "snes"}) == "snes"


def _service(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    service.scheduler.stop()
    library = tmp_path / "library"
    library.mkdir()
    service.game_libraries = [({"name": "L", "type": "folder", "path": str(library)}, object())]
    games = {n: Game(name=n, roms=(Rom(name=n + ".sfc", crc=f"{i:08x}"),))
             for i, n in enumerate(["Alpha (USA)", "Beta Quest (USA)"])}
    service.dats.dats.append(Dat(name="Nintendo - Super Nintendo Entertainment System",
                                 version="1", games=games))
    return service


def test_starting_a_set_without_a_platform_uses_the_dats_own(tmp_path):
    service = _service(tmp_path)
    result = service.collection_start("Nintendo - Super Nintendo Entertainment System", "")
    assert result.get("ok"), result
    assert result["platform"] == "snes"
    assert result["queued"] == 2


def test_starting_a_set_for_an_unknown_system_is_refused(tmp_path):
    service = _service(tmp_path)
    service.dats.dats[0].name = "Homebrew - Unknown Thing"
    result = service.collection_start("Homebrew - Unknown Thing", "")
    assert "pick the platform" in result["error"]


def test_a_failed_request_counts_as_failed_not_done(tmp_path):
    service = _service(tmp_path)
    service.request = lambda name, platform: {"ok": False, "error": "no usable release among 0 result(s)"}
    batch = Batch(id="b1", platform="snes", dat="x", queue=["Alpha (USA)"])
    service.store.put_item("batches", batch.to_dict())
    progress = service.collection_step("b1")
    assert progress["done"] == 0 and progress["failed"] == 1
    status = service.collection_status()
    row = status["batches"][0]
    assert row["failures"] == [{"title": "Alpha (USA)",
                                "reason": "no usable release among 0 result(s)"}]
    assert status["dat_platforms"]["Nintendo - Super Nintendo Entertainment System"] == "snes"
    assert status["interval_minutes"] == 10


def test_the_scheduled_tick_advances_queued_sets_and_skips_paused_ones(tmp_path):
    service = _service(tmp_path)
    asked = []
    service.request = lambda name, platform: asked.append(name) or {"ok": True}
    service.store.put_item("batches", Batch(id="go", platform="snes", queue=["A", "B"],
                                            per_pass=1).to_dict())
    service.store.put_item("batches", Batch(id="wait", platform="snes", status="paused",
                                            queue=["C"]).to_dict())
    assert service._collection_batches_active()
    result = service.collection_tick()
    assert asked == ["A"]
    assert result["stepped"] == 1 and result["requested"] == 1
    service.collection_tick()
    assert asked == ["A", "B"]
    assert not service._collection_batches_active()
    assert service.collection_tick()["stepped"] == 0
