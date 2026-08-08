"""Loading DATs must not walk the whole library.

`reload_dats` did `root.rglob("*.dat")`. Pointed at a DAT directory that is
fine; pointed at a ROM library it is the entire library. On a live install --
58 platforms on a network mount, Switch dumps and all -- the walk had not
finished after ten minutes, so setting DAT_PATH to the obvious place, the
directory that holds the ROMs *and* their datfiles, made ROMarr look like it
had hung on startup.

Nothing errored. It just never came back.
"""

from __future__ import annotations

import romarr.app as app
from romarr.app import _find_dats


def test_it_finds_dats_beside_their_platform(tmp_path):
    """How people actually store them: the datfile next to the ROMs."""
    (tmp_path / "ngc").mkdir()
    (tmp_path / "nes").mkdir()
    (tmp_path / "ngc" / "Nintendo - GameCube - Datfile.dat").write_text("x")
    (tmp_path / "nes" / "Nintendo - NES - Datfile.dat").write_text("x")
    (tmp_path / "top-level.dat").write_text("x")

    found, stopped = _find_dats(tmp_path)

    assert {p.name for p in found} == {
        "Nintendo - GameCube - Datfile.dat",
        "Nintendo - NES - Datfile.dat",
        "top-level.dat"}
    assert stopped == ""


def test_it_does_not_descend_forever(tmp_path, monkeypatch):
    deep = tmp_path
    for level in range(6):
        deep = deep / f"level{level}"
        deep.mkdir()
        (deep / f"at{level}.dat").write_text("x")

    monkeypatch.setattr(app, "DAT_SCAN_DEPTH", 3)
    found, _ = _find_dats(tmp_path)
    names = {p.name for p in found}
    assert "at0.dat" in names and "at1.dat" in names
    assert "at5.dat" not in names, "the walk went deeper than it was allowed to"


def test_a_huge_tree_stops_and_says_so(tmp_path, monkeypatch):
    """The live case. Stopping quietly would leave somebody wondering why
    only some of their DATs loaded."""
    big = tmp_path / "roms"
    big.mkdir()
    for n in range(60):
        (big / f"rom{n}.zip").write_text("x")
    (big / "real.dat").write_text("x")

    monkeypatch.setattr(app, "DAT_SCAN_MAX_ENTRIES", 20)
    found, stopped = _find_dats(tmp_path)
    assert stopped, "no warning was produced after truncating the scan"
    assert "DAT_PATH" in stopped


def test_directories_full_of_media_are_skipped(tmp_path):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "not-a-dat.dat").write_text("x")
    (tmp_path / "saves").mkdir()
    (tmp_path / "saves" / "also-not.dat").write_text("x")
    (tmp_path / "real.dat").write_text("x")

    found, _ = _find_dats(tmp_path)
    assert {p.name for p in found} == {"real.dat"}


def test_xml_dats_are_found_too(tmp_path):
    (tmp_path / "redump.xml").write_text("x")
    assert {p.name for p in _find_dats(tmp_path)[0]} == {"redump.xml"}


def test_a_library_of_roms_with_one_datfile_finds_it_quickly(tmp_path):
    """The shape that broke: thousands of ROM files, one datfile among them."""
    platform = tmp_path / "snes"
    platform.mkdir()
    for n in range(500):
        (platform / f"Game {n} (USA).sfc").write_text("x")
    (platform / "Nintendo - SNES - Datfile.dat").write_text("x")

    found, stopped = _find_dats(tmp_path)
    assert [p.name for p in found] == ["Nintendo - SNES - Datfile.dat"]
    assert stopped == ""


def test_reload_reports_what_it_examined(tmp_path):
    (tmp_path / "a.dat").write_text("<datafile></datafile>")
    service = app.ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    got = service.reload_dats(str(tmp_path))
    assert got["examined"] >= 1
    assert got["path"] == str(tmp_path)
