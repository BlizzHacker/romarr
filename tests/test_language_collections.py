"""Tests for language-aware 1G1R collections (Issue #214)."""
from __future__ import annotations

from romarr.collections import (
    MISSING,
    PRESENT_VERIFIED,
    Policy,
    build_plan,
)
from romarr.dat import game_languages, parse_dat


_CMP_DAT = """\
clrmamepro (
\tname "Nintendo - Nintendo Entertainment System"
\tversion "2026.11.14"
)

game (
\tname "Alpha (Japan)"
\tlanguage "Japanese"
\trom ( name "Alpha (Japan).nes" size 100 crc aaaaaaaa )
)

game (
\tname "Alpha (USA)"
\tcloneof "Alpha (Japan)"
\tlanguage "English"
\trom ( name "Alpha (USA).nes" size 100 crc bbbbbbbb )
)

game (
\tname "Bravo (USA)"
\tlanguage "English"
\trom ( name "Bravo (USA).nes" size 100 crc cccccccc )
)

game (
\tname "Bravo (Europe)"
\tcloneof "Bravo (USA)"
\tlanguage "English / French"
\trom ( name "Bravo (Europe).nes" size 100 crc dddddddd )
)

game (
\tname "Charlie (Japan)"
\tlanguage "Japanese"
\trom ( name "Charlie (Japan).nes" size 100 crc eeeeeeee )
)

game (
\tname "Delta (Japan)"
\tlanguage "Japanese"
\trom ( name "Delta (Japan).nes" size 100 crc 11111111 )
)

game (
\tname "Delta (Japan) [T-Fr]"
\tcloneof "Delta (Japan)"
\trom ( name "Delta (Japan) [T-Fr].nes" size 100 crc 22222222 )
)

game (
\tname "Mystery Game (World)"
\trom ( name "Mystery Game (World).nes" size 100 crc 33333333 )
)
"""

_XML_DAT = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>Test XML</name>
    <version>2026.11.14</version>
  </header>
  <game name="Quest (Japan)" language="Japanese">
    <rom name="Quest (Japan).nes" size="100" crc="aaaaaaaa"/>
  </game>
  <game name="Quest (France)" cloneof="Quest (Japan)">
    <language>English / French</language>
    <rom name="Quest (France).nes" size="100" crc="bbbbbbbb"/>
  </game>
</datafile>
"""


def test_parse_dat_retains_language_metadata_in_cmp_and_xml():
    cmp_dat = parse_dat(_CMP_DAT)
    assert cmp_dat.games["Alpha (Japan)"].languages == ("ja",)
    assert cmp_dat.games["Bravo (Europe)"].languages == ("en", "fr")
    assert cmp_dat.games["Mystery Game (World)"].languages == ()

    xml_dat = parse_dat(_XML_DAT)
    assert xml_dat.games["Quest (Japan)"].languages == ("ja",)
    assert xml_dat.games["Quest (France)"].languages == ("en", "fr")
    assert game_languages(xml_dat.games["Quest (France)"]) == ("en", "fr")


def test_language_priority_first_then_region_and_unknown_separate():
    dat = parse_dat(_CMP_DAT)
    policy = Policy(
        regions=("usa", "world", "europe", "japan"),
        preferred_languages=("fr", "en"),
        translation_policy="fill",
        translation_langs=("fr", "en"),
    )
    plan = build_plan(dat, {"Bravo (Europe)": PRESENT_VERIFIED}, policy)

    by_name = {t.name: t for t in plan.titles}
    # Alpha: Japanese-only parent skipped in favor of English USA clone.
    assert "Alpha (USA)" in by_name
    assert "Alpha (Japan)" not in by_name

    # Bravo: French+English Europe beats English-only USA because "fr" is #1.
    assert "Bravo (Europe)" in by_name
    assert by_name["Bravo (Europe)"].status == PRESENT_VERIFIED
    assert "Bravo (USA)" not in by_name

    # Charlie: Japanese-only with no translation is omitted, not counted missing.
    assert all("Charlie" not in t.name for t in plan.titles)

    # Delta: Japanese exclusive with [T-Fr] fan translation keeps only [T-Fr].
    assert "Delta (Japan) [T-Fr]" in by_name
    assert "Delta (Japan)" not in by_name
    assert by_name["Delta (Japan) [T-Fr]"].is_translation is True

    # Mystery Game: unknown language metadata is surfaced separately, not missing.
    assert all("Mystery Game" not in t.name for t in plan.titles)
    assert [t.name for t in plan.unknown_language_titles] == ["Mystery Game (World)"]

    counts = plan.counts()
    assert counts["expected"] == 3  # Alpha (USA), Bravo (Europe), Delta [T-Fr]
    assert counts["have"] == 1
    assert counts["missing"] == 2
    assert counts["language_omitted"] == 1
    assert counts["unknown_language"] == 1


def test_keep_both_does_not_require_untranslated_japanese_original():
    dat = parse_dat(_CMP_DAT)
    policy = Policy(
        regions=("usa", "world", "europe", "japan"),
        preferred_languages=("fr", "en"),
        translation_policy="keep_both",
        translation_langs=("fr", "en"),
    )
    plan = build_plan(dat, {}, policy)
    names = {t.name for t in plan.titles}
    assert "Delta (Japan) [T-Fr]" in names
    assert "Delta (Japan)" not in names


def test_disabled_preferred_languages_preserves_historical_behavior():
    dat = parse_dat(_CMP_DAT)
    policy = Policy(
        regions=("usa", "world", "europe", "japan"),
        preferred_languages=(),
        translation_policy="exclude",
    )
    plan = build_plan(dat, {}, policy)
    names = {t.name for t in plan.titles}
    assert "Charlie (Japan)" in names
    assert "Mystery Game (World)" in names
    assert plan.unknown_language_titles == []
    assert plan.language_omitted == 0


def test_one_game_one_rom_respects_optional_languages_order():
    dat = parse_dat(_CMP_DAT)
    chosen = dat.one_game_one_rom(
        ["usa", "world", "europe", "japan"],
        languages=["fr", "en"],
    )
    assert "Alpha (USA)" in chosen
    assert "Bravo (Europe)" in chosen


def test_ui_and_store_expose_preferred_languages():
    from romarr import store, ui

    assert "preferred_languages" in store.DEFAULT_SETTINGS
    assert store.DEFAULT_SETTINGS["preferred_languages"] == []
    html = ui.page()
    assert 'data-k="preferred_languages"' in html
    assert 'id="cpreflangs"' in html
    assert "unknown_language_titles" in html


def test_multilingual_filename_tags_unmarked_translations_and_strict_region():
    from romarr.dat import Dat, Game

    dat = Dat(
        name="No-Intro NES",
        games={
            "Castlevania (Europe) (En,Da)": Game(
                name="Castlevania (Europe) (En,Da)",
            ),
            "Super Mario Bros. (Europe) (Fr)": Game(
                name="Super Mario Bros. (Europe) (Fr)",
                languages=("fr",),
            ),
            "Super Mario Bros. (USA)": Game(
                name="Super Mario Bros. (USA)",
                cloneof="Super Mario Bros. (Europe) (Fr)",
                languages=("en",),
            ),
            "Mother (Japan) (Translation)": Game(
                name="Mother (Japan) (Translation)",
                languages=("fr",),
            ),
        },
    )
    # 1. (En,Da) should be recognized as English + Danish, not unknown!
    assert game_languages(dat.games["Castlevania (Europe) (En,Da)"]) == ("en", "da")

    # 2. Strict region ("usa",) with preferred_languages=("fr", "en") must fall
    #    back to English USA rather than picking French Europe and omitting!
    strict_plan = build_plan(
        dat,
        {},
        Policy(
            regions=("usa",),
            region_mode="strict",
            preferred_languages=("fr", "en"),
            translation_policy="fill",
            translation_langs=("en",),
        ),
    )
    names = {t.name for t in strict_plan.titles}
    assert "Super Mario Bros. (USA)" in names
    assert "Super Mario Bros. (Europe) (Fr)" not in names
    # 3. Unmarked (Translation) with languages=("fr",) must NOT match English-only
    #    translation_langs=("en",), and must be omitted as excluded.
    assert "Mother (Japan) (Translation)" not in names
    assert strict_plan.counts()["excluded"] == 1
    assert strict_plan.counts()["region_omitted"] == 1

