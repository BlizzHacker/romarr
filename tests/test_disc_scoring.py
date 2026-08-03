"""Scoring a disc release, where several cartridge-era rules invert.

The scorer was tuned against cartridge result sets and every rule in it was
right for those. Three of them are wrong for discs, and one of the three
would have rejected the entire correctly-dumped half of every disc search.
"""

from __future__ import annotations

import pytest

from romarr.platforms import GB, MB, by_slug
from romarr.selection import Release, best_release, explain, judge, score

PSX = by_slug("psx")
PS2 = by_slug("ps2")
WII = by_slug("wii")
SNES = by_slug("snes")
CONSOLE = (1000,)


def rel(title, size, seeders=30, categories=CONSOLE, protocol="torrent"):
    return Release(title=title, size=size, seeders=seeders,
                   categories=categories, download_url="magnet:?x",
                   protocol=protocol)


# --- the one that would have broken every disc search ----------------------

def test_a_redump_dump_is_a_good_dump_not_a_compilation():
    """Redump is the disc-preservation project, the way No-Intro is for
    cartridges. Its name on a disc release means the dump is right.

    It was in COMPILATION_MARKERS, which rejects outright -- so the single
    most reliable quality signal a disc release carries would have thrown it
    out, and the releases left standing would have been the unlabelled ones.
    """
    verdict = judge(rel("Silent Hill (USA) [Redump]", 500 * MB), "silent hill", PSX)
    assert verdict.accepted, verdict.why()


def test_redump_is_worth_points_on_a_disc():
    labelled = rel("Silent Hill (USA) [Redump]", 500 * MB)
    bare = rel("Silent Hill (USA)", 500 * MB)
    assert score(labelled, "silent hill", PSX) > score(bare, "silent hill", PSX)


def test_a_redump_collection_is_still_rejected():
    """The group's name is not the licence -- a set is still a set."""
    verdict = judge(rel("Redump - Sony PlayStation USA Collection", 400 * GB),
                    "silent hill", PSX)
    assert not verdict.accepted


def test_redump_is_still_a_compilation_marker_for_a_cartridge():
    """Nothing about the cartridge behaviour changes. A Redump-labelled SNES
    release is a set: Redump does not dump cartridges."""
    verdict = judge(rel("Chrono Trigger Redump", 3 * MB), "chrono trigger", SNES)
    assert not verdict.accepted


# --- multi-disc ------------------------------------------------------------

def test_disc_one_of_a_multi_disc_game_is_not_a_compilation():
    """How the live library stores every multi-disc PlayStation game:
    `(Disc 1).7z`, `(Disc 2).7z`. Rejecting those rejects the game."""
    for n in (1, 2, 3):
        verdict = judge(
            rel(f"Final Fantasy VII (USA) (Disc {n})", 600 * MB),
            "final fantasy vii", PSX)
        assert verdict.accepted, (n, verdict.why())


# --- size ------------------------------------------------------------------

def test_a_real_ps2_image_is_accepted():
    verdict = judge(rel("Shadow of the Colossus (USA)", 4 * GB),
                    "shadow of the colossus", PS2)
    assert verdict.accepted, verdict.why()


def test_a_pc_repack_is_still_too_big_for_a_ps2_request():
    verdict = judge(rel("Shadow of the Colossus", 60 * GB),
                    "shadow of the colossus", PS2)
    assert not verdict.accepted
    assert any("too big" in line for line in verdict.why())


def test_the_size_rejection_says_disc_image_not_cartridge():
    """An operator reading "too big for a Sony PlayStation cartridge" learns
    that the tool does not know what a PlayStation is."""
    lines = explain(rel("Some Game", 40 * GB), "some game", PSX)
    joined = " ".join(lines).lower()
    assert "disc" in joined
    assert "cartridge" not in joined


def test_a_cartridge_still_says_cartridge():
    lines = explain(rel("Some Game", 900 * MB), "some game", SNES)
    assert any("cartridge" in line.lower() for line in lines)


def test_something_far_too_small_to_be_a_disc_is_rejected():
    """A few hundred kilobytes offered for a PlayStation request is a patch,
    a cheat file or a lone .cue -- never the game."""
    verdict = judge(rel("Silent Hill (USA)", 300 * 1024), "silent hill", PSX)
    assert not verdict.accepted


def test_a_small_cartridge_is_not_caught_by_the_disc_floor():
    verdict = judge(rel("Super Metroid (USA) [!]", 3 * MB), "super metroid", SNES)
    assert verdict.accepted, verdict.why()


# --- foreign platform markers ---------------------------------------------

def test_a_wii_release_may_say_wad_without_being_refiled():
    """"wad" catches a Virtual Console repackage offered for a *cartridge*
    platform. On the Wii it is the catalogue."""
    verdict = judge(rel("Mega Man 9 (USA) (WiiWare) (WAD)", 40 * MB),
                    "mega man 9", WII)
    assert verdict.accepted, verdict.why()


def test_a_genesis_request_still_refuses_a_virtual_console_wad():
    genesis = by_slug("genesis-slash-megadrive")
    verdict = judge(rel("Phantasy.Star.IV.USA.SMD.Virtual.Console", 14 * MB),
                    "phantasy star iv", genesis)
    assert not verdict.accepted


def test_a_playstation_request_still_refuses_a_ps2_release():
    verdict = judge(rel("Final Fantasy X (USA) PS2", 4 * GB),
                    "final fantasy x", PSX)
    assert not verdict.accepted


def test_a_ps2_request_is_not_refused_for_saying_playstation():
    verdict = judge(rel("Final Fantasy X (USA) PlayStation 2", 4 * GB),
                    "final fantasy x", PS2)
    assert verdict.accepted, verdict.why()


# --- ranking ---------------------------------------------------------------

def test_the_smaller_of_two_equal_discs_is_not_automatically_preferred():
    """For cartridges, bigger means romset. For a disc it can simply mean an
    uncompressed rip of the same game, and a 100MB "PlayStation game" beside
    a 600MB one is far more likely to be the broken one.
    """
    small = rel("Silent Hill (USA)", 60 * MB, seeders=5)
    full = rel("Silent Hill (USA) [Redump]", 550 * MB, seeders=5)
    assert best_release([small, full], "silent hill", PSX) is full


def test_a_cartridge_still_prefers_the_smaller_file():
    dump = rel("Super Metroid (USA) [!]", 3 * MB, seeders=5)
    bigger = rel("Super Metroid (USA)", 20 * MB, seeders=5)
    assert best_release([bigger, dump], "super metroid", SNES) is dump


def test_nothing_qualifying_is_still_none():
    assert best_release([], "anything", PSX) is None
