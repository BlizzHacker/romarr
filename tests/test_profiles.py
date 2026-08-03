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
