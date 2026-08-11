"""Translation-aware 1G1R (Q2) and RomM folder structures (Q3)."""

import zipfile
from pathlib import Path

from romarr.collections import (Policy, Title, build_plan, flags_in,
                                is_translation)
from romarr.dat import Dat, Game, Rom
from romarr.library import import_rom, platform_dir
from romarr.platforms import by_slug

SNES = by_slug("snes")


# -- detection ---------------------------------------------------------------

def test_translation_markers_are_recognised():
    assert is_translation("Digimon Adventure (Japan) [T-En by Foo v1.0]")
    assert is_translation("Some RPG (Japan) [T+Eng]")
    assert is_translation("Game (Japan) (Translation)")
    assert not is_translation("Super Metroid (USA)")
    assert not is_translation("Beta Blocker (USA)")  # not a beta, not a trans


def test_translation_is_its_own_flag_not_hack():
    """A trainer is a hack; a fan translation is what makes a JP-only game
    playable. Lumping them together was the bug."""
    assert flags_in("Game (Japan) [T-En]") == {"translation"}
    assert flags_in("Game (USA) (Trainer)") == {"hack"}


# -- 1G1R fill behaviour -----------------------------------------------------

def _dat_with(*names) -> Dat:
    # All variants clone the first, so they form one 1G1R group.
    games = {}
    for i, name in enumerate(names):
        clone = "" if i == 0 else names[0]
        games[name] = Game(name=name, cloneof=clone,
                           roms=(Rom(name=name + ".smc", crc=f"{i:08x}"),))
    return Dat(name="Test", version="1", games=games)


def test_default_policy_leaves_a_jp_only_game_japanese():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En by X]")
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe")))
    names = [t.name for t in plan.titles]
    assert names == ["Digimon (Japan)"], "exclude policy keeps the JP dump only"
    assert all(not t.is_translation for t in plan.titles)


def test_fill_uses_a_translation_only_when_no_preferred_region_exists():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En by X]")
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe"),
                                      translation_policy="fill"))
    assert [t.name for t in plan.titles] == ["Digimon (Japan) [T-En by X]"]
    assert plan.titles[0].is_translation
    assert "no dump exists in your preferred regions" in plan.titles[0].chosen_because


def test_fill_leaves_a_us_game_alone_even_with_a_translation_present():
    dat = _dat_with("Chrono Trigger (USA)", "Chrono Trigger (Japan) [T-En]")
    plan = build_plan(dat, {}, Policy(regions=("usa",),
                                      translation_policy="fill"))
    assert [t.name for t in plan.titles] == ["Chrono Trigger (USA)"]


def test_prefer_takes_the_translation_over_a_us_dump():
    dat = _dat_with("Chrono Trigger (USA)", "Chrono Trigger (Japan) [T-En]")
    plan = build_plan(dat, {}, Policy(regions=("usa",),
                                      translation_policy="prefer"))
    assert [t.name for t in plan.titles] == ["Chrono Trigger (Japan) [T-En]"]
    assert plan.titles[0].is_translation


def test_keep_both_yields_the_original_and_the_translation():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En by X]")
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe"),
                                      translation_policy="keep_both"))
    names = {t.name for t in plan.titles}
    assert names == {"Digimon (Japan)", "Digimon (Japan) [T-En by X]"}
    trans = [t for t in plan.titles if t.is_translation]
    assert len(trans) == 1 and trans[0].name.endswith("[T-En by X]")


def test_present_translation_is_not_reported_missing():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En]")
    present = {"Digimon (Japan) [T-En]": "verified"}
    plan = build_plan(dat, present, Policy(regions=("usa",),
                                           translation_policy="fill"))
    assert plan.titles[0].status == "verified"


# -- folder structure --------------------------------------------------------

def test_platform_dir_flat_is_structure_a():
    root = Path("/roms")
    assert platform_dir(root, SNES) == root / "snes"


def test_platform_dir_nested_is_structure_b():
    root = Path("/roms")
    assert platform_dir(root, SNES, layout="nested") == root / "snes" / "roms"
    # RomM's own aliases resolve the same way.
    assert platform_dir(root, SNES, layout="romm_b") == root / "snes" / "roms"


def test_platform_dir_translation_subfolder():
    root = Path("/roms")
    assert platform_dir(root, SNES, translation=True) \
        == root / "snes" / "Translations"
    assert platform_dir(root, SNES, layout="nested", translation=True) \
        == root / "snes" / "roms" / "Translations"


def _zip(path, name):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, b"\x00" * 2048)
    return path


def test_import_files_into_structure_b(tmp_path):
    library = tmp_path / "library"
    archive = _zip(tmp_path / "g.zip", "Super Mario World (USA).smc")
    [result] = import_rom(archive, SNES, library, layout="nested")
    assert result.ok
    assert result.destination == \
        library / "snes" / "roms" / "Super Mario World (USA).smc"


def test_import_a_translation_into_its_subfolder(tmp_path):
    library = tmp_path / "library"
    archive = _zip(tmp_path / "g.zip", "Digimon (Japan) [T-En].smc")
    [result] = import_rom(archive, SNES, library, layout="nested",
                          translation=True)
    assert result.ok
    assert result.destination.parent == library / "snes" / "roms" / "Translations"


# -- surfaces exposed --------------------------------------------------------

def test_the_settings_carry_layout_and_translation_policy():
    from romarr.store import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["library_layout"] == "flat"
    assert DEFAULT_SETTINGS["translation_policy"] == "exclude"


def test_the_ui_exposes_both_controls():
    from romarr.ui import page
    html = page()
    assert "translation_policy" in html
    assert "library_layout" in html
    assert "Structure B" in html


def test_collection_plan_over_http_honours_translation_policy(tmp_path, monkeypatch):
    """The plan endpoint failed over HTTP once for a None default; a new
    policy field is exactly where that regresses, so this drives it through
    the service the way the route does."""
    from romarr.app import ROMarr
    from romarr.dat import Dat, DatIndex, Game, Rom
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    dat = Dat(name="T", version="1", games={
        "Digimon (Japan)": Game(name="Digimon (Japan)",
                                roms=(Rom(name="a.smc", crc="1"),)),
        "Digimon (Japan) [T-En]": Game(name="Digimon (Japan) [T-En]",
                                       cloneof="Digimon (Japan)",
                                       roms=(Rom(name="b.smc", crc="2"),)),
    })
    idx = DatIndex()
    idx.dats = [dat]
    svc.dats = idx
    out = svc.collection_plan("T", "snes", regions=["usa"],
                              translation_policy="fill")
    assert out["policy"]["translation_policy"] == "fill"
    names = [t["name"] for t in out["titles"]]
    assert names == ["Digimon (Japan) [T-En]"]
    assert out["titles"][0]["translation"] is True
