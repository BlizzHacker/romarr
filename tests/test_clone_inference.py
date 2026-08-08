"""1G1R on a DAT that declares no clones.

Found running against a real library. The No-Intro GameCube datfile there has
1,907 entries and not one `cloneof` attribute -- Redump-style DATs generally
carry none. `parent_of` therefore returned each game's own name, every dump was
its own group, and `one_game_one_rom` collapsed nothing.

It did not fail. It reported 1,834 "titles" and listed
"007 - Agent Under Fire (Europe)", "(USA)" and "(USA) (Rev 1)" as three
separate games. Every test passed, because every fixture declared its clones.
"""

from __future__ import annotations

import pytest

from romarr.dat import base_title, parse_dat


def dat_of(*names, clones=None):
    clones = clones or {}
    games = "".join(
        '<game name="%s"%s><rom name="r%d" size="1" crc="%08x"/></game>'
        % (n, (' cloneof="%s"' % clones[n]) if n in clones else "", i, i)
        for i, n in enumerate(names))
    return parse_dat('<?xml version="1.0"?><datafile><header><name>T</name>'
                     '</header>%s</datafile>' % games)


# --- the normalising rule ---------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Contra (USA)", "Contra"),
    ("Contra (USA) (Rev 1)", "Contra"),
    ("Contra (Japan, Europe)", "Contra"),
    ("007 - Agent Under Fire (USA) (Rev 1)", "007 - Agent Under Fire"),
    ("Luigi's Mansion (USA)", "Luigi's Mansion"),
    ("Some Game [b]", "Some Game"),
])
def test_versions_of_one_game_reduce_to_the_same_title(name, expected):
    assert base_title(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("Resident Evil 4 (USA) (Disc 1)", "Resident Evil 4 (Disc 1)"),
    ("Resident Evil 4 (USA) (Disc 2)", "Resident Evil 4 (Disc 2)"),
    ("Final Fantasy VII (USA) (Disc 3) (Rev 1)", "Final Fantasy VII (Disc 3)"),
])
def test_discs_stay_apart(name, expected):
    """Both discs are things you need. Collapsing them would report a complete
    set as missing half of itself."""
    assert base_title(name) == expected


def test_two_discs_of_one_game_are_two_entries():
    dat = dat_of("Resident Evil 4 (USA) (Disc 1)",
                 "Resident Evil 4 (USA) (Disc 2)")
    chosen = dat.one_game_one_rom(["usa"])
    assert len(chosen) == 2


# --- inference only when the DAT has nothing to say -------------------------

def test_a_dat_without_clones_still_collapses_regions():
    """The live case."""
    dat = dat_of("007 - Agent Under Fire (USA)",
                 "007 - Agent Under Fire (USA) (Rev 1)",
                 "007 - Agent Under Fire (Europe)",
                 "Luigi's Mansion (USA)")
    assert dat.declares_clones is False
    chosen = dat.one_game_one_rom(["usa", "europe"])
    assert len(chosen) == 2, sorted(chosen)
    assert "Luigi's Mansion (USA)" in chosen
    assert any(n.startswith("007") and "(USA)" in n for n in chosen)
    assert not any("(Europe)" in n for n in chosen)


def test_a_declared_relationship_is_believed_over_the_name():
    """Its author knows things a filename does not say. "Totally Different"
    shares no title with Sonic and still joins the group, because the DAT
    said so."""
    dat = dat_of("Sonic (USA)", "Sonic (Japan)", "Totally Different (USA)",
                 clones={"Sonic (Japan)": "Sonic (USA)",
                         "Totally Different (USA)": "Sonic (USA)"})
    assert dat.declares_clones is True
    assert len(dat.one_game_one_rom(["usa"])) == 1


def test_partial_clone_data_still_infers_the_rest():
    """The rule that had to change once it met real data.

    Gating inference on "this DAT declares nothing at all" looked like the
    conservative choice. The real GameCube datfile carries exactly one cloneof
    across 1,907 entries -- enough to look like clone data and switch inference
    off entirely, so 1G1R returned 1,906 titles and collapsed nothing. Use what
    is declared, infer what is not.
    """
    dat = dat_of("A (USA)", "A (Japan)", "B (USA)", "B (Japan)",
                 clones={"A (Japan)": "A (USA)"})
    chosen = dat.one_game_one_rom(["usa"])
    assert "A (USA)" in chosen and "A (Japan)" not in chosen
    assert "B (USA)" in chosen and "B (Japan)" not in chosen
    assert len(chosen) == 2


def test_region_preference_still_decides_the_winner_without_clone_data():
    dat = dat_of("Contra (Japan)", "Contra (Europe)", "Contra (USA)")
    assert list(dat.one_game_one_rom(["europe", "usa"])) == ["Contra (Europe)"]
    assert list(dat.one_game_one_rom(["usa", "europe"])) == ["Contra (USA)"]


def test_a_game_with_one_dump_is_unaffected():
    dat = dat_of("Only One (Japan)")
    assert list(dat.one_game_one_rom(["usa"])) == ["Only One (Japan)"]


def test_titles_that_merely_look_similar_are_not_merged():
    dat = dat_of("Madden NFL 2002 (USA)", "Madden NFL 2003 (USA)",
                 "Sonic Adventure 2 (USA)", "Sonic Adventure 2 Battle (USA)")
    assert len(dat.one_game_one_rom(["usa"])) == 4
