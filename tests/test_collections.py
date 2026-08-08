"""Whole-set planning, 1G1R explanation, and resumable acquisition.

The property worth defending hardest: `explain` must never disagree with
`Dat.one_game_one_rom`. It exists to show that decision, and an explanation
that contradicts the choice is worse than none -- somebody reads it, believes
it, and sets their region order by it.
"""

from __future__ import annotations

import pytest

from romarr.collections import (BATCH_DONE, BATCH_PENDING, MISSING,
                                PRESENT_BAD, PRESENT_UNKNOWN, PRESENT_VERIFIED,
                                Batch, Policy, build_plan, explain, flags_in)
from romarr.dat import parse_dat

DAT = """<?xml version="1.0"?>
<datafile><header><name>Nintendo - NES</name><version>2026-01-01</version></header>
<game name="Contra (USA)"><rom name="a" size="1" crc="00000001"/></game>
<game name="Contra (Japan)" cloneof="Contra (USA)"><rom name="b" size="1" crc="00000002"/></game>
<game name="Contra (Europe)" cloneof="Contra (USA)"><rom name="c" size="1" crc="00000003"/></game>
<game name="Zelda (Japan)"><rom name="d" size="1" crc="00000004"/></game>
<game name="Metroid (USA)"><rom name="e" size="1" crc="00000005"/></game>
<game name="Metroid (USA) (Beta)" cloneof="Metroid (USA)"><rom name="f" size="1" crc="00000006"/></game>
<game name="Somebody's Hack (USA) (Hack)"><rom name="g" size="1" crc="00000007"/></game>
<game name="Beta Blocker (USA)"><rom name="h" size="1" crc="00000008"/></game>
</datafile>"""


@pytest.fixture
def dat():
    return parse_dat(DAT)


# --- category flags ---------------------------------------------------------

def test_flags_come_from_the_bracketed_groups_only():
    assert flags_in("Metroid (USA) (Beta)") == {"beta"}
    assert flags_in("Somebody's Hack (USA) (Hack)") == {"hack"}
    # The trap: a game genuinely called "Beta Blocker" is not a beta, and
    # matching the bare word anywhere would drop it from every set.
    assert flags_in("Beta Blocker (USA)") == set()


# --- the plan ---------------------------------------------------------------

def test_a_1g1r_plan_keeps_one_dump_per_game(dat):
    plan = build_plan(dat, {}, Policy())
    names = [t.name for t in plan.titles]
    assert "Contra (USA)" in names
    assert "Contra (Japan)" not in names and "Contra (Europe)" not in names
    # The hack and the beta are excluded by default policy; Beta Blocker is not.
    assert "Somebody's Hack (USA) (Hack)" not in names
    assert "Beta Blocker (USA)" in names


def test_a_game_only_available_outside_your_regions_is_kept_and_flagged(dat):
    """Dropping it would silently lose titles from a "complete" set."""
    plan = build_plan(dat, {}, Policy(regions=("usa",)))
    zelda = next(t for t in plan.titles if t.name.startswith("Zelda"))
    assert zelda.name == "Zelda (Japan)"
    assert zelda.outside_preference is True


def test_region_order_changes_which_dump_is_chosen(dat):
    europe_first = build_plan(dat, {}, Policy(regions=("europe", "usa")))
    names = [t.name for t in europe_first.titles]
    assert "Contra (Europe)" in names
    assert "Contra (USA)" not in names


def test_turning_1g1r_off_keeps_every_dump(dat):
    plan = build_plan(dat, {}, Policy(one_game_one_rom=False))
    names = [t.name for t in plan.titles]
    assert {"Contra (USA)", "Contra (Japan)", "Contra (Europe)"} <= set(names)


def test_the_counts_add_up(dat):
    present = {"Contra (USA)": PRESENT_VERIFIED,
               "Metroid (USA)": PRESENT_BAD,
               "Zelda (Japan)": PRESENT_UNKNOWN}
    plan = build_plan(dat, present, Policy())
    counts = plan.counts()
    assert counts[PRESENT_VERIFIED] == 1
    assert counts[PRESENT_BAD] == 1
    assert counts[PRESENT_UNKNOWN] == 1
    assert counts["expected"] == len(plan.titles)
    assert counts["have"] + counts[MISSING] == counts["expected"]


def test_missing_lists_only_what_is_absent(dat):
    plan = build_plan(dat, {"Contra (USA)": PRESENT_VERIFIED}, Policy())
    missing = [t.name for t in plan.missing()]
    assert "Contra (USA)" not in missing
    assert "Metroid (USA)" in missing


def test_an_excluded_choice_falls_back_to_a_releasable_sibling():
    """Excluding prototypes must not delete the game when a real dump exists."""
    dat = parse_dat("""<?xml version="1.0"?>
<datafile><header><name>T</name></header>
<game name="Thing (Japan) (Proto)"><rom name="a" size="1" crc="0a"/></game>
<game name="Thing (Japan)" cloneof="Thing (Japan) (Proto)"><rom name="b" size="1" crc="0b"/></game>
</datafile>""")
    plan = build_plan(dat, {}, Policy(regions=("japan",), exclude=frozenset({"proto"})))
    assert [t.name for t in plan.titles] == ["Thing (Japan)"]


# --- the explanation must match the decision --------------------------------

def test_the_explanation_names_the_region_that_won(dat):
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe", "japan")))
    contra = next(t for t in plan.titles if t.name == "Contra (USA)")
    assert "USA" in contra.chosen_because
    assert "parent" in contra.chosen_because


def test_every_discarded_clone_is_listed_with_a_reason(dat):
    plan = build_plan(dat, {}, Policy(regions=("usa", "europe", "japan")))
    contra = next(t for t in plan.titles if t.name == "Contra (USA)")
    discarded = dict(contra.discarded)
    assert set(discarded) == {"Contra (Japan)", "Contra (Europe)"}
    assert all(reason for reason in discarded.values())
    assert "EUROPE" in discarded["Contra (Europe)"]


@pytest.mark.parametrize("regions", [
    ("usa", "europe", "japan"),
    ("europe", "usa", "japan"),
    ("japan", "usa"),
    ("usa",),
    (),
])
def test_explain_never_contradicts_one_game_one_rom(dat, regions):
    """The property this module lives or dies on.

    `one_game_one_rom` decides; `explain` describes. If the description ever
    names a different winner, somebody reads it and sets their region order by
    a lie.
    """
    engine = dat.one_game_one_rom(list(regions))
    plan = build_plan(dat, {}, Policy(regions=regions, exclude=frozenset()))
    for title in plan.titles:
        assert title.name in engine, (
            f"plan chose {title.name!r}, which the engine did not")
        # And nothing it listed as discarded is the engine's winner.
        for lost, _ in title.discarded:
            assert lost not in engine or lost == title.name


# --- resumable acquisition --------------------------------------------------

def test_a_batch_works_through_its_queue_in_slices():
    batch = Batch(id="b1", queue=[f"Game {n}" for n in range(12)], per_pass=5)
    assert len(batch.take()) == 5
    assert len(batch.queue) == 7
    assert len(batch.take()) == 5
    assert len(batch.take()) == 2
    assert batch.queue == []


def test_progress_reflects_successes_and_failures():
    batch = Batch(id="b1", queue=["a", "b", "c", "d"])
    for name in batch.take(4):
        batch.record(name, ok=(name != "c"), reason="no release found")
    progress = batch.progress()
    assert progress["done"] == 3
    assert progress["failed"] == 1
    assert progress["remaining"] == 0
    assert progress["percent"] == 100.0
    assert batch.status == BATCH_DONE


def test_a_batch_survives_a_restart():
    """It is state, not a loop. A 3,000-title set outlives one process."""
    batch = Batch(id="b1", platform="nes", queue=["a", "b", "c"], per_pass=2)
    batch.record(batch.take(1)[0], ok=True)
    revived = Batch.from_dict(batch.to_dict())
    assert revived.done == ["a"]
    assert revived.queue == ["b", "c"]
    assert revived.per_pass == 2
    assert revived.platform == "nes"


def test_failures_can_be_retried_without_redoing_the_successes():
    batch = Batch(id="b1", queue=["a", "b", "c"])
    for name in batch.take(3):
        batch.record(name, ok=(name == "a"), reason="nothing found")
    assert batch.retry_failed() == 2
    assert sorted(batch.queue) == ["b", "c"]
    assert batch.done == ["a"]
    assert batch.status == BATCH_PENDING


def test_an_empty_batch_does_not_divide_by_zero():
    assert Batch(id="b1").progress()["percent"] == 0.0
