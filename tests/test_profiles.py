"""Operator policy over the scorer: what to prefer, what to refuse, what to
never see again.

gamarr has release profiles and a blocklist. Both are flat: a word list, and a
set of release ids. ROMarr's scorer already explains itself release by release,
so both of these can be *visible* -- a preferred term shows up in `why()` with
the points it added, and a blocklist entry carries the reason it was blocked.

That is the difference worth having. "Why did it take that one" and "why will
it never take this one" are the two questions anybody actually asks.
"""

from __future__ import annotations

import pytest

from romarr.platforms import by_slug
from romarr.profiles import Blocklist, ReleaseProfile, release_id
from romarr.selection import Release, best_release, judge

SNES = by_slug("snes")
MB = 1024 * 1024


def rel(title, size=3 * MB, seeders=30, url="magnet:?xt=urn:btih:abc"):
    return Release(title=title, size=size, seeders=seeders,
                   categories=(1000,), download_url=url, protocol="torrent",
                   indexer="Test")


# --- release profiles ------------------------------------------------------

def test_a_preferred_term_adds_points_and_says_so():
    profile = ReleaseProfile(preferred=[("[!]", 40)])
    plain = judge(rel("Super Metroid (USA)"), "super metroid", SNES)
    good = judge(rel("Super Metroid (USA) [!]"), "super metroid", SNES,
                 profile=profile)
    assert good.points > plain.points
    assert any("[!]" in line for line in good.why())


def test_a_negative_preference_subtracts():
    profile = ReleaseProfile(preferred=[("beta", -50)])
    verdict = judge(rel("Super Metroid (USA) beta"), "super metroid", SNES,
                    profile=profile)
    assert any("-50" in line and "beta" in line for line in verdict.why())


def test_an_excluded_term_rejects_outright_and_names_itself():
    profile = ReleaseProfile(excluded=["proto"])
    verdict = judge(rel("Super Metroid (USA) proto"), "super metroid", SNES,
                    profile=profile)
    assert not verdict.accepted
    assert "proto" in verdict.verdict


def test_a_required_term_rejects_anything_without_it():
    profile = ReleaseProfile(required=["verified"])
    assert not judge(rel("Super Metroid (USA)"), "super metroid", SNES,
                     profile=profile).accepted
    assert judge(rel("Super Metroid (USA) verified"), "super metroid", SNES,
                 profile=profile).accepted


def test_terms_are_case_insensitive():
    profile = ReleaseProfile(excluded=["PROTO"])
    assert not judge(rel("game proto"), "game", SNES, profile=profile).accepted


def test_a_regex_term_is_supported_like_radarrs():
    """Radarr lets a term be a regex, and the cases that need one are real:
    matching `(Rev 1)` but not `(Rev 10)` cannot be done with a substring."""
    profile = ReleaseProfile(excluded=[r"/\(Rev \d\)/"])
    assert not judge(rel("Game (USA) (Rev 1)"), "game", SNES,
                     profile=profile).accepted
    assert judge(rel("Game (USA)"), "game", SNES, profile=profile).accepted


def test_a_broken_regex_is_ignored_rather_than_crashing_every_search():
    """An operator typing a bad pattern must not take the search down with
    it. The term is skipped and logged."""
    profile = ReleaseProfile(excluded=[r"/[unclosed/"])
    assert judge(rel("Game (USA)"), "game", SNES, profile=profile).accepted


def test_no_profile_leaves_scoring_exactly_as_it_was():
    release = rel("Super Metroid (USA) [!]")
    assert (judge(release, "super metroid", SNES).points
            == judge(release, "super metroid", SNES, profile=ReleaseProfile()).points)


def test_a_profile_changes_which_release_wins():
    """The point of the feature: an operator's preference has to actually move
    the pick, not merely the score.

    The term is a scene-group name the built-in scorer has no opinion about.
    Using `[!]` here would prove nothing -- it already earns the good-dump
    bonus, so it wins with or without a profile.
    """
    plain = rel("Chrono Trigger (USA)", seeders=50)
    tagged = rel("Chrono Trigger (USA) GOLDDISC", seeders=5)
    assert best_release([plain, tagged], "chrono trigger", SNES) is plain
    profile = ReleaseProfile(preferred=[("GOLDDISC", 200)])
    assert best_release([plain, tagged], "chrono trigger", SNES,
                        profile=profile) is tagged


# --- blocklist -------------------------------------------------------------

def test_a_release_has_a_stable_identity():
    a = rel("Game (USA)")
    assert release_id(a) == release_id(rel("Game (USA)"))
    assert release_id(a) != release_id(rel("Game (Europe)"))


def test_identity_uses_the_infohash_when_there_is_one():
    """Titles get rewritten by indexers; an infohash does not. Two results
    for the same torrent must block as one."""
    magnet = "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD&dn=x"
    same = "magnet:?xt=urn:btih:aabbccddeeff00112233445566778899aabbccdd"
    assert release_id(rel("One name", url=magnet)) == \
           release_id(rel("Totally different name", url=same))


def test_a_blocked_release_is_rejected_with_the_recorded_reason():
    """gamarr's blocklist tells you a release is blocked. This tells you why,
    which is the only form of the answer anybody can act on."""
    blocked = rel("Game (USA)")
    blocklist = Blocklist()
    blocklist.add(blocked, reason="download failed twice: stalled at 0%")

    verdict = judge(blocked, "game", SNES, blocklist=blocklist)

    assert not verdict.accepted
    assert "stalled at 0%" in verdict.verdict


def test_an_unblocked_release_is_untouched():
    blocklist = Blocklist()
    blocklist.add(rel("Other (USA)"), reason="x")
    assert judge(rel("Game (USA)"), "game", SNES, blocklist=blocklist).accepted


def test_blocking_survives_a_round_trip_through_storage():
    blocklist = Blocklist()
    blocklist.add(rel("Game (USA)"), reason="corrupt")
    restored = Blocklist.from_items(blocklist.as_items())
    assert not judge(rel("Game (USA)"), "game", SNES,
                     blocklist=restored).accepted


def test_a_block_records_when_and_what_so_it_can_be_reviewed():
    blocklist = Blocklist()
    blocklist.add(rel("Game (USA)"), reason="bad dump")
    entry = blocklist.as_items()[0]
    assert entry["title"] == "Game (USA)"
    assert entry["reason"] == "bad dump"
    assert entry["blocked_at"] > 0
    assert entry["indexer"] == "Test"


def test_a_block_can_be_lifted():
    blocklist = Blocklist()
    blocklist.add(rel("Game (USA)"), reason="x")
    assert blocklist.remove(release_id(rel("Game (USA)")))
    assert judge(rel("Game (USA)"), "game", SNES, blocklist=blocklist).accepted


def test_lifting_something_that_was_never_blocked_is_not_an_error():
    assert not Blocklist().remove("nope")


def test_best_release_skips_blocked_entries():
    good = rel("Game (USA)", seeders=5)
    bad = rel("Game (USA) [!]", seeders=50)
    blocklist = Blocklist()
    blocklist.add(bad, reason="failed")
    assert best_release([good, bad], "game", SNES, blocklist=blocklist) is good


def test_blocking_everything_yields_no_pick_rather_than_a_bad_one():
    releases = [rel("Game (USA)"), rel("Game (Europe)")]
    blocklist = Blocklist()
    for release in releases:
        blocklist.add(release, reason="x")
    assert best_release(releases, "game", SNES, blocklist=blocklist) is None


# --- both together ---------------------------------------------------------

def test_a_blocklist_beats_a_preference():
    """An operator who preferred a term and then blocked a specific release
    meant the block. Scoring it highly and then taking it would be the worst
    possible reading."""
    release = rel("Game (USA) [!]")
    blocklist = Blocklist()
    blocklist.add(release, reason="corrupt")
    verdict = judge(release, "game", SNES,
                    profile=ReleaseProfile(preferred=[("[!]", 500)]),
                    blocklist=blocklist)
    assert not verdict.accepted


# --- issue #113: minimum size, per-platform profiles, custom scoring --------

PSX = by_slug("psx")
SWITCH = by_slug("switch")
NES = by_slug("nes")


def test_minimum_size_refuses_small_releases_with_clear_reason():
    profile = ReleaseProfile(min_size_mb=10)
    small = rel("Super Metroid (USA)", size=2 * MB)
    verdict_small = judge(small, "super metroid", SNES, profile=profile)
    assert not verdict_small.accepted
    assert "smaller than release profile minimum size" in verdict_small.verdict
    assert "10" in verdict_small.verdict

    ok_size = rel("Super Metroid (USA)", size=15 * MB)
    verdict_ok = judge(ok_size, "super metroid", SNES, profile=profile)
    assert verdict_ok.accepted

    # Unknown size (0) should not be refused by minimum size
    zero_size = rel("Super Metroid (USA)", size=0)
    verdict_zero = judge(zero_size, "super metroid", SNES, profile=profile)
    assert verdict_zero.accepted


def test_maximum_size_refuses_oversized_releases():
    profile = ReleaseProfile(max_size_mb=50)
    huge = rel("Super Metroid (USA)", size=80 * MB)
    verdict = judge(huge, "super metroid", SNES, profile=profile)
    assert not verdict.accepted
    assert "larger than release profile maximum size" in verdict.verdict

    ok_size = rel("Super Metroid (USA)", size=30 * MB)
    assert judge(ok_size, "super metroid", SNES, profile=profile).accepted


def test_minimum_seeders_enforced():
    profile = ReleaseProfile(min_seeders=5)
    few = rel("Game (USA)", seeders=2)
    verdict_few = judge(few, "game", SNES, profile=profile)
    assert not verdict_few.accepted
    assert "less than 5 seeders" in verdict_few.verdict

    many = rel("Game (USA)", seeders=10)
    assert judge(many, "game", SNES, profile=profile).accepted


def test_custom_region_scores():
    profile = ReleaseProfile(region_scores={"japan": 100, "usa": 20})
    j_jp = judge(rel("Game (Japan)"), "game", SNES, profile=profile)
    j_us = judge(rel("Game (USA)"), "game", SNES, profile=profile)
    assert j_jp.points > j_us.points
    assert any("region japan" in line and "+100" in line for line in j_jp.why())
    assert any("region usa" in line and "+20" in line for line in j_us.why())


def test_negative_region_score_penalizes():
    profile = ReleaseProfile(region_scores={"japan": -50, "usa": 40})
    j_jp = judge(rel("Game (Japan)"), "game", SNES, profile=profile)
    assert any("region japan" in line and "-50" in line for line in j_jp.why())


def test_custom_size_preference_tiebreaker():
    # PSX normally prefers larger disc images
    profile = ReleaseProfile(size_preference="smaller")
    small = rel("Game (USA)", 100 * MB, seeders=10)
    big = rel("Game (USA)", 500 * MB, seeders=10)
    assert best_release([small, big], "game", PSX, profile=profile) is small

    # SNES normally prefers smaller cartridge dumps
    profile_large = ReleaseProfile(size_preference="larger")
    small_snes = rel("Game (USA)", 2 * MB, seeders=10)
    big_snes = rel("Game (USA)", 4 * MB, seeders=10)
    assert best_release([small_snes, big_snes], "game", SNES, profile=profile_large) is big_snes


def test_size_scoring_bonus_points():
    profile = ReleaseProfile(size_preference="smaller", size_weight=30)
    j = judge(rel("Game (USA)", 1 * MB), "game", SNES, profile=profile)
    assert any("size preference" in line for line in j.why())


def test_per_platform_profile_from_settings():
    settings = {
        "min_size_mb": 1,
        "max_size_mb": 100,
        "preferred_regions": ["USA", "Europe"],
        "preferred_terms": [("[!]", 40)],
        "platform_profiles": {
            "nes": {
                "min_size_mb": 0.02,
                "max_size_mb": 8,
                "preferred_terms": [("PRG0", 15)],
            },
            "switch": {
                "min_size_mb": 500,
                "max_size_mb": 32768,
                "size_preference": "smaller",
                "region_scores": {"japan": 80, "usa": 40},
            }
        }
    }
    nes_prof = ReleaseProfile.from_settings(settings, "nes")
    assert nes_prof.min_size_mb == 0.02
    assert nes_prof.max_size_mb == 8
    assert nes_prof.preferred_regions == ["usa", "europe"]
    assert any(t[0] == "[!]" for t in nes_prof.preferred)
    assert any(t[0] == "PRG0" for t in nes_prof.preferred)

    switch_prof = ReleaseProfile.from_settings(settings, "switch")
    assert switch_prof.min_size_mb == 500
    assert switch_prof.max_size_mb == 32768
    assert switch_prof.size_preference == "smaller"
    assert switch_prof.region_scores == {"japan": 80, "usa": 40}

    # Unconfigured platform falls back to global
    gb_prof = ReleaseProfile.from_settings(settings, "gb")
    assert gb_prof.min_size_mb == 1
    assert gb_prof.max_size_mb == 100


def test_end_to_end_switch_tiny_file_refusal_vs_nes():
    settings = {
        "platform_profiles": {
            "nes": {"min_size_mb": 0.01, "max_size_mb": 8},
            "switch": {"min_size_mb": 500, "max_size_mb": 32768},
        }
    }
    nes_prof = ReleaseProfile.from_settings(settings, NES)
    switch_prof = ReleaseProfile.from_settings(settings, SWITCH)

    tiny = rel("Mario", 5 * MB)
    assert not judge(tiny, "mario", SWITCH, profile=switch_prof).accepted
    assert judge(tiny, "mario", NES, profile=nes_prof).accepted


def test_service_profile_for_resolves_and_inherits(tmp_path):
    from romarr.app import ROMarr
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                          "ROMARR_AUTH": "disabled"})
    service.store.update_settings({
        "min_size_mb": 2,
        "platform_profiles": {
            "nes": {"min_size_mb": 0.05, "max_size_mb": 4},
            "switch": {"min_size_mb": 800},
        }
    })
    service.reload_policy()

    nes_p = service.profile_for("nes")
    assert nes_p.min_size_mb == 0.05
    assert nes_p.max_size_mb == 4

    switch_p = service.profile_for(SWITCH)
    assert switch_p.min_size_mb == 800

    global_p = service.profile_for(None)
    assert global_p.min_size_mb == 2


