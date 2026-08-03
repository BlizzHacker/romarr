"""Exporting to LaunchBox, Playnite and ES-DE.

The plugins in `contrib/` are .NET and PowerShell and cannot be compiled or
run here. This module is what they consume, it is pure functions over library
rows, and it is therefore the part that can actually be proven -- which is why
the integration is built on it rather than on the plugins.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from romarr.frontends import (
    FORMATS, esde_folder, launchbox_platform, to_gamelist, to_launchbox,
    to_playnite)
from romarr.platforms import PLATFORMS

ROWS = [
    {"id": "1", "title": "Super Metroid", "platform": "snes",
     "path": "/roms/snes/Super Metroid (USA).sfc",
     "filename": "Super Metroid (USA).sfc", "verified": "verified"},
    {"id": "2", "title": "Silent Hill", "platform": "psx",
     "path": "/roms/psx/Silent Hill (USA).chd",
     "filename": "Silent Hill (USA).chd"},
]


# --- platform naming, which is the whole difficulty ------------------------

def test_launchbox_gets_the_names_it_recognises():
    """LaunchBox matches metadata by platform NAME. A name it does not know
    imports with no box art, no description and no association with the rest
    of that system -- it looks like the export half-worked, which is harder to
    diagnose than a clean failure."""
    assert launchbox_platform("snes") == "Super Nintendo Entertainment System"
    assert launchbox_platform("psx") == "Sony Playstation"
    assert launchbox_platform("genesis-slash-megadrive") == "Sega Genesis"


def test_an_unmapped_platform_falls_back_to_its_slug():
    """Better a tile named `weird-slug` than no tile."""
    assert launchbox_platform("weird-slug") == "weird-slug"


def test_esde_gets_its_own_folder_names():
    assert esde_folder("genesis-slash-megadrive") == "megadrive"
    assert esde_folder("turbografx16--1") == "tg16"
    assert esde_folder("snes") == "snes"


def test_most_platforms_have_a_launchbox_name():
    """A slug with no mapping still exports, but silently loses its metadata,
    so the coverage is worth pinning rather than discovering later."""
    named = sum(1 for p in PLATFORMS if p.slug in
                __import__("romarr.frontends", fromlist=["x"]).LAUNCHBOX_PLATFORMS)
    assert named >= len(PLATFORMS) - 5, f"only {named}/{len(PLATFORMS)} mapped"


# --- LaunchBox -------------------------------------------------------------

def test_launchbox_export_is_valid_xml_with_the_expected_shape():
    root = ET.fromstring(to_launchbox(ROWS))
    assert root.tag == "LaunchBox"
    games = root.findall("Game")
    assert len(games) == 2
    assert games[0].findtext("Title") == "Super Metroid"
    assert games[0].findtext("ApplicationPath").endswith("Super Metroid (USA).sfc")
    assert games[0].findtext("Platform") == "Super Nintendo Entertainment System"


def test_a_row_with_no_path_is_skipped_not_written_broken():
    """ApplicationPath is what LaunchBox launches. A dead tile is worse than a
    missing one -- it looks like a working library until somebody clicks it."""
    root = ET.fromstring(to_launchbox([{"title": "No file", "platform": "snes"}]))
    assert root.findall("Game") == []


def test_the_dat_verdict_survives_into_notes():
    """LaunchBox has no field for it and it is the most useful thing in the
    row, so it goes somewhere a person will read rather than being dropped."""
    root = ET.fromstring(to_launchbox(ROWS))
    assert "verified" in root.findall("Game")[0].findtext("Notes")


def test_a_title_with_xml_special_characters_survives():
    """`Ratchet & Clank` and `<untitled>` break hand-written XML and are
    exactly the titles a real library contains."""
    rows = [{"title": "Ratchet & Clank: <Up> \"Arsenal\"", "platform": "ps2",
             "path": "/roms/ps2/r.iso"}]
    root = ET.fromstring(to_launchbox(rows))
    assert root.findall("Game")[0].findtext("Title") == rows[0]["title"]


# --- ES-DE -----------------------------------------------------------------

def test_gamelist_is_valid_and_uses_relative_paths():
    """EmulationStation expects `./name`, and a relative path is what lets a
    gamelist survive the ROM directory being mounted somewhere else -- the
    normal case when the files live on a NAS and the frontend is a handheld."""
    root = ET.fromstring(to_gamelist(ROWS))
    assert root.tag == "gameList"
    paths = [g.findtext("path") for g in root.findall("game")]
    assert paths == ["./Super Metroid (USA).sfc", "./Silent Hill (USA).chd"]


def test_gamelist_falls_back_to_the_filename_for_a_name():
    root = ET.fromstring(to_gamelist([{"filename": "Game.sfc"}]))
    assert root.findall("game")[0].findtext("name") == "Game.sfc"


def test_gamelist_skips_a_row_with_nothing_to_point_at():
    assert ET.fromstring(to_gamelist([{"title": "Nothing"}])).findall("game") == []


# --- Playnite --------------------------------------------------------------

def test_playnite_export_is_flat_json_a_script_can_read():
    """The consumer is PowerShell. Anything needing a schema to interpret is a
    script that breaks the first time a field is absent."""
    payload = json.loads(to_playnite(ROWS))
    assert payload["source"] == "ROMarr"
    assert len(payload["games"]) == 2
    game = payload["games"][0]
    assert set(game) == {"name", "platform", "romPath", "id", "verified"}
    assert game["platform"] == "Super Nintendo Entertainment System"


def test_playnite_export_omits_rows_with_no_file():
    payload = json.loads(to_playnite([{"title": "No file", "platform": "snes"}]))
    assert payload["games"] == []


def test_every_playnite_field_is_a_string_even_when_absent():
    """A missing key and a null both break `$game.verified` in PowerShell in
    ways that are hard to read in a log."""
    payload = json.loads(to_playnite(ROWS))
    for game in payload["games"]:
        assert all(isinstance(v, str) for v in game.values())


# --- the registry ----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(FORMATS))
def test_every_format_renders_and_declares_itself(name):
    spec = FORMATS[name]
    assert spec["label"] and spec["filename"] and spec["content_type"]
    out = spec["render"](ROWS)
    assert isinstance(out, str) and out.strip()


@pytest.mark.parametrize("name", sorted(FORMATS))
def test_every_format_survives_an_empty_library(name):
    """Day one, and the moment a filter matches nothing. Neither should
    produce a file the launcher refuses to open."""
    out = FORMATS[name]["render"]([])
    assert out.strip()
    if "xml" in FORMATS[name]["content_type"]:
        ET.fromstring(out)
    else:
        json.loads(out)
