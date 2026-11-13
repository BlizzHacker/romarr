"""TitleDB integration for Switch (issue #23) and the Switch platform row.

No-Intro and Redump publish no Switch DAT, so the hash-verification path
every other platform rides does not exist for this one. TitleDB -- blawar's
catalogue of every Switch title ID, as JSON on GitHub -- is what the
preservation scene uses instead. These tests cover the local half: ID
classification, family grouping, index lookup, filename validation. The
network half (`fetch_versions`, `load_names_into`) is exercised against the
real data files in deployment, never here -- a unit test that depends on
GitHub being reachable fails for reasons that are not the code's.
"""

from __future__ import annotations

import pytest

from romarr.platforms import resolve
from romarr.titled import (
    SwitchTitle, TitleDBIndex, base_tid_of, classify_tid,
    validate_switch_rom,
)


# Real IDs from the live catalogue, so the kind arithmetic is tested
# against the shapes Nintendo actually allocates rather than invented ones.
BOTW_BASE = "01007ef00011e000"      # Zelda: Breath of the Wild
MARIO_BASE = "0100000000010000"     # Super Mario Odyssey
SYSTEM_TID = "0100000000001000"     # qlaunch -- a system title below the boundary


def _index():
    index = TitleDBIndex()
    index.add(SwitchTitle(
        title_id=BOTW_BASE,
        name="The Legend of Zelda: Breath of the Wild",
        versions={"65536": "2017-03-03", "196608": "2018-02-01"}))
    index.add(SwitchTitle(
        title_id=MARIO_BASE, name="Super Mario Odyssey",
        versions={"65536": "2017-10-27"}))
    # A DLC and an update of BotW: same 13-hex prefix, different low bits.
    index.add(SwitchTitle(title_id=BOTW_BASE[:13] + "001",
                          versions={"65536": "2017-11-30"}))
    index.add(SwitchTitle(title_id=BOTW_BASE[:13] + "800",
                          versions={"196608": "2018-02-01"}))
    index.names_loaded = True
    return index


# ------------------------------------------------------------- platform --

def test_switch_is_a_platform():
    p = resolve("switch")
    assert p is not None and p.slug == "switch"
    assert p.name == "Nintendo Switch"


def test_switch_extensions_cover_the_dump_formats():
    p = resolve("nintendo switch")
    exts = set(p.extensions)
    # .nsp digital, .xci cartridge, .nsz/.xcz compressed -- every form a
    # Switch dump arrives in.
    assert {".nsp", ".xci", ".nsz", ".xcz"} <= exts


@pytest.mark.parametrize("slug", ["switch", "switch-2"])
def test_both_switch_generations_accept_a_nup(slug):
    """A NUP container is a Switch or Switch 2 dump, and it is listed last."""
    p = resolve(slug)
    assert p.extensions[-1] == ".nup"
    assert "nup" in p.native_markers


def test_switch_ceiling_admits_a_32gb_card():
    assert resolve("switch").max_size >= 32 * 1024 ** 3


def test_switch_name_does_not_resolve_to_nes():
    """'Nintendo Switch' contains the NES alias 'nintendo' -- the exact
    trap that filed 2,874 Switch rows as NES before the platform existed."""
    assert resolve("Nintendo Switch").slug == "switch"
    assert resolve("nintendo").slug == "nes"


# ------------------------------------------------------ ID classification --

def test_kind_comes_from_the_id_alone():
    """Base, DLC and update are arithmetic on the low 12 bits -- no
    database needed, which is the point."""
    assert classify_tid(BOTW_BASE) == "base"
    assert classify_tid(BOTW_BASE[:13] + "001") == "dlc"
    assert classify_tid(BOTW_BASE[:13] + "800") == "update"


def test_system_titles_are_not_games():
    assert classify_tid(SYSTEM_TID) == "system"
    assert classify_tid("0100000000000000") == "system"


def test_invalid_ids_are_said_to_be_invalid_not_guessed_at():
    assert classify_tid("") == "invalid"
    assert classify_tid("not-a-tid") == "invalid"
    assert classify_tid("01007EF00011E00") == "invalid"   # 15 digits
    assert classify_tid("01007EF00011E0000") == "invalid"  # 17 digits
    assert classify_tid("01007EF00011G000") == "invalid"   # not hex


def test_case_and_0x_prefix_are_tolerated():
    assert classify_tid("0x01007EF00011E000") == "base"
    assert classify_tid(BOTW_BASE.upper()) == "base"


def test_base_tid_of_groups_a_family():
    assert base_tid_of(BOTW_BASE[:13] + "001") == BOTW_BASE
    assert base_tid_of(BOTW_BASE[:13] + "800") == BOTW_BASE
    assert base_tid_of("garbage") == ""


# --------------------------------------------------------------- index --

def test_latest_version_is_the_highest_not_the_newest_listed():
    index = _index()
    title = index.lookup_by_id(BOTW_BASE)
    assert title.latest_version == 196608


def test_lookup_is_case_insensitive_and_tolerates_0x():
    index = _index()
    assert index.lookup_by_id(BOTW_BASE.upper()).name.startswith("The Legend")
    assert index.lookup_by_id("0x" + BOTW_BASE) is not None


def test_lookup_misses_cleanly():
    assert _index().lookup_by_id("ffffffffffffffff") is None


def test_family_of_returns_base_dlc_and_update():
    family = _index().family_of(BOTW_BASE[:13] + "001")
    assert [t.title_id for t in family["base"]] == [BOTW_BASE]
    assert len(family["dlc"]) == 1
    assert len(family["update"]) == 1


def test_search_by_name_is_partial_and_case_insensitive():
    results = _index().search_by_name("mario")
    assert len(results) == 1 and results[0].name == "Super Mario Odyssey"


def test_search_without_names_returns_empty_not_an_error():
    index = TitleDBIndex()
    index.add(SwitchTitle(title_id=BOTW_BASE))  # versions.json shape: no name
    assert index.search_by_name("zelda") == []


# ---------------------------------------------------- filename validation --

def test_validate_extracts_a_title_id_from_a_real_filename_shape():
    result = validate_switch_rom(
        f"The Legend of Zelda - Breath of the Wild [{BOTW_BASE.upper()}].nsp",
        index=_index())
    assert result["valid"] and result["status"] == "verified"
    assert result["kind"] == "base"
    assert result["latest_version"] == 196608


def test_validate_accepts_an_explicit_title_id():
    result = validate_switch_rom("whatever.nsp", title_id=MARIO_BASE.upper(),
                                 index=_index())
    assert result["valid"] and result["title"].name == "Super Mario Odyssey"


def test_a_dlc_id_verifies_as_dlc():
    result = validate_switch_rom(
        f"DLC [{BOTW_BASE[:13].upper()}001].nsp", index=_index())
    assert result["valid"] and result["kind"] == "dlc"


def test_validate_falls_back_to_name_search_when_names_loaded():
    result = validate_switch_rom("Super Mario Odyssey.nsp", index=_index())
    assert result["valid"] and result["status"] == "matched_by_name"


def test_unknown_title_id_is_not_a_failure_just_not_in_the_database():
    """A new release ahead of the catalogue refresh is not a bad dump."""
    result = validate_switch_rom("Brand New Game [0100abcdef012000].nsp",
                                 index=_index())
    assert not result["valid"]
    assert result["status"] == "not_in_database"
    assert result["kind"] == "base"


def test_without_an_index_the_id_still_classifies():
    result = validate_switch_rom(f"Game [{BOTW_BASE.upper()}].nsp")
    assert result["status"] == "classified_only"
    assert result["kind"] == "base"


def test_no_identifier_anywhere_reports_honestly():
    result = validate_switch_rom("mystery.nsp", index=_index())
    assert not result["valid"]
    assert result["status"] == "no_identifier"


def test_a_long_digit_run_is_not_mistaken_for_a_title_id():
    """19-digit serials exist in filenames; a title ID is exactly 16 hex
    with no adjacent hex digit on either side."""
    result = validate_switch_rom("Game 1234567890123456789.nsp",
                                 index=_index())
    assert result["status"] != "verified"
    # The 16-digit window inside the run must NOT be extracted as an ID.
    result2 = validate_switch_rom("1234567890123456789012.nsp", index=_index())
    assert result2["status"] != "verified"


def test_a_nup_is_a_library_file_for_rename_and_collections():
    """Library rename and collection planning treat a .nup like an .nsp."""
    from romarr.collection_torrent import ROM_SUFFIXES as COLLECTION_SUFFIXES
    from romarr.library_rename import DISC_SUFFIXES

    assert ".nup" in DISC_SUFFIXES
    assert ".nup" in COLLECTION_SUFFIXES
