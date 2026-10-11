"""Strict region mode and configurable fan-translation languages (issue #37).

Two invariants worth defending:

1. Defaults never change. An existing operator's plan with no new settings
   must be byte-for-byte what it was: prefer-mode fallback, T-En assumed for
   unmarked translations.
2. Strict means strict. When the operator selects "only selected regions",
   nothing outside the allowlist enters the plan -- not a winner, not a
   keep-both original, not a translation -- and every omission is counted so
   the total still explains itself.
"""

from romarr.collections import (Policy, build_plan, normalise_lang,
                                translation_langs_in)
from romarr.dat import Dat, Game, Rom


def _dat_with(*names) -> Dat:
    games = {}
    for i, name in enumerate(names):
        clone = "" if i == 0 else names[0]
        games[name] = Game(name=name, cloneof=clone,
                           roms=(Rom(name=name + ".smc", crc=f"{i:08x}"),))
    return Dat(name="Test", version="1", games=games)


# -- language parsing ---------------------------------------------------------

def test_translation_langs_are_parsed_from_scene_markers():
    assert translation_langs_in("Spiel (Japan) [T-De]") == {"de"}
    assert translation_langs_in("Some RPG (Japan) [T+Eng]") == {"en"}
    assert translation_langs_in("Game (Japan) [T-En by Foo v1.0]") == {"en"}
    assert translation_langs_in("Super Metroid (USA)") == set()


def test_language_aliases_normalise_to_two_letter_codes():
    assert normalise_lang("Eng") == "en"
    assert normalise_lang("DEU") == "de"
    assert normalise_lang("german") == "de"
    # An unknown code passes through so an identical selection still matches.
    assert normalise_lang("zz") == "zz"


# -- configurable languages ---------------------------------------------------

def test_default_language_selection_stays_english_only():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-De]")
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe"),
                                      translation_policy="fill"))
    # A German translation is not what the default policy reads; the JP dump
    # is kept exactly as before languages were configurable.
    assert [t.name for t in plan.titles] == ["Digimon (Japan)"]


def test_a_selected_language_is_used_by_fill():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-De]")
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe"),
                                      translation_policy="fill",
                                      translation_langs=("de",)))
    assert [t.name for t in plan.titles] == ["Digimon (Japan) [T-De]"]
    assert plan.titles[0].is_translation
    assert "German translation" in plan.titles[0].chosen_because


def test_several_languages_can_be_selected_and_best_region_still_wins():
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En]",
                    "Digimon (Germany) [T-De]")
    plan = build_plan(dat, {}, Policy(regions=("germany", "europe"),
                                      translation_policy="fill",
                                      translation_langs=("de", "en")))
    # Both are candidates; the one whose base region ranks higher wins.
    assert [t.name for t in plan.titles] == ["Digimon (Germany) [T-De]"]


def test_prefer_takes_only_a_selected_language():
    dat = _dat_with("Chrono Trigger (USA)", "Chrono Trigger (Japan) [T-En]",
                    "Chrono Trigger (Japan) [T-De]")
    plan = build_plan(dat, {}, Policy(regions=("usa",),
                                      translation_policy="prefer",
                                      translation_langs=("de",)))
    assert [t.name for t in plan.titles] == ["Chrono Trigger (Japan) [T-De]"]


# -- strict region mode -------------------------------------------------------

def test_prefer_mode_keeps_a_regionless_title_by_default():
    dat = _dat_with("Zelda (Japan)")
    plan = build_plan(dat, {}, Policy(regions=("germany", "europe")))
    assert [t.name for t in plan.titles] == ["Zelda (Japan)"]
    assert plan.titles[0].outside_preference
    assert plan.counts()["region_omitted"] == 0


def test_strict_mode_omits_a_regionless_title_and_counts_it():
    dat = _dat_with("Zelda (Japan)")
    plan = build_plan(dat, {}, Policy(regions=("germany", "europe"),
                                      region_mode="strict"))
    assert plan.titles == []
    assert plan.counts()["region_omitted"] == 1


def test_strict_mode_keeps_every_title_inside_the_allowlist():
    dat = _dat_with("Contra (USA)", "Contra (Japan)", "Contra (Europe)")
    plan = build_plan(dat, {}, Policy(regions=("germany", "europe"),
                                      region_mode="strict"))
    assert [t.name for t in plan.titles] == ["Contra (Europe)"]
    assert plan.counts()["region_omitted"] == 0


def test_strict_mode_rejects_a_translation_outside_the_allowlist():
    """fill would take the JP translation; strict must not, because the
    translation itself is a Japan dump."""
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En]")
    plan = build_plan(dat, {}, Policy(regions=("germany", "europe"),
                                      translation_policy="fill",
                                      region_mode="strict"))
    assert plan.titles == []
    assert plan.counts()["region_omitted"] == 1


def test_strict_mode_accepts_a_translation_inside_the_allowlist():
    dat = _dat_with("Digimon (Germany) [T-De]")
    plan = build_plan(dat, {}, Policy(regions=("germany", "europe"),
                                      translation_policy="fill",
                                      translation_langs=("de",),
                                      region_mode="strict"))
    assert [t.name for t in plan.titles] == ["Digimon (Germany) [T-De]"]
    assert plan.counts()["region_omitted"] == 0


def test_strict_keep_both_omits_a_group_with_no_dump_inside_the_allowlist():
    """keep_both fills a region gap with a translation -- but in strict mode
    a region gap IS the omission, so the whole group drops and is counted
    once (not twice, once per row)."""
    dat = _dat_with("Digimon (Japan)", "Digimon (Japan) [T-En]")
    plan = build_plan(dat, {}, Policy(regions=("europe",),
                                      translation_policy="keep_both",
                                      region_mode="strict"))
    assert plan.titles == []
    assert plan.counts()["region_omitted"] == 1


def test_strict_mode_applies_with_1g1r_off():
    dat = _dat_with("Contra (USA)", "Contra (Japan)")
    plan = build_plan(dat, {}, Policy(regions=("usa",), one_game_one_rom=False,
                                      region_mode="strict"))
    assert [t.name for t in plan.titles] == ["Contra (USA)"]
    assert plan.counts()["region_omitted"] == 1


# -- service wiring -----------------------------------------------------------

def test_collection_plan_service_honours_strict_and_languages(tmp_path):
    from romarr.app import ROMarr
    from romarr.dat import DatIndex
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    dat = Dat(name="T", version="1", games={
        "Digimon (Japan)": Game(name="Digimon (Japan)",
                                roms=(Rom(name="a.smc", crc="1"),)),
        "Digimon (Japan) [T-De]": Game(name="Digimon (Japan) [T-De]",
                                       cloneof="Digimon (Japan)",
                                       roms=(Rom(name="b.smc", crc="2"),)),
    })
    idx = DatIndex()
    idx.dats = [dat]
    svc.dats = idx

    out = svc.collection_plan("T", "snes", regions=["usa"],
                              translation_policy="fill",
                              translation_langs=["de"])
    assert out["policy"]["translation_langs"] == ["de"]
    assert out["policy"]["region_mode"] == "prefer"
    assert [t["name"] for t in out["titles"]] == ["Digimon (Japan) [T-De]"]

    out = svc.collection_plan("T", "snes", regions=["usa"],
                              translation_policy="fill",
                              translation_langs="de", region_mode="strict")
    # A string is accepted and split, the way the HTTP route passes it.
    assert out["policy"]["region_mode"] == "strict"
    assert out["titles"] == []
    assert out["counts"]["region_omitted"] == 1


def test_settings_default_to_prefer_and_english():
    from romarr.store import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["region_mode"] == "prefer"
    assert DEFAULT_SETTINGS["translation_langs"] == ["en"]


def test_ui_exposes_region_mode_and_languages():
    from romarr.ui import page
    html = page()
    assert "region_mode" in html
    assert "translation_langs" in html
