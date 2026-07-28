import pytest

from rommarr.platforms import resolve, platform_for_file, by_slug, all_extensions
from rommarr.selection import (
    Release, best_release, is_game_release, pick_rom_file, score, title_matches,
)


def rel(title, *, size=512 * 1024, seeders=20, cats=(1030,),
        protocol="torrent", url="magnet:?xt=urn:btih:abc"):
    return Release(title=title, size=size, seeders=seeders, categories=cats,
                   download_url=url, protocol=protocol)


# --- platform resolution --------------------------------------------------

def test_resolves_common_names_and_aliases():
    assert resolve("SNES").slug == "snes"
    assert resolve("Super Nintendo").slug == "snes"
    assert resolve("super famicom").slug == "snes"
    assert resolve("Nintendo 64").slug == "n64"
    assert resolve("mega drive").slug == "genesis-slash-megadrive"


def test_longer_alias_wins_over_shorter_substring():
    # "nintendo" is a substring of "super nintendo"; the specific one must win,
    # or every SNES request silently becomes an NES request.
    assert resolve("super nintendo entertainment system").slug == "snes"


def test_unknown_platform_is_none_not_a_guess():
    assert resolve("PlayStation 5") is None
    assert resolve("") is None


def test_extension_maps_back_to_a_platform():
    assert platform_for_file("Zelda.smc").slug == "snes"
    assert platform_for_file("Mario.GBA").slug == "gba"
    assert platform_for_file("notes.txt") is None


def test_every_platform_declares_at_least_one_extension():
    from rommarr.platforms import PLATFORMS
    for p in PLATFORMS:
        assert p.extensions, f"{p.slug} has no extensions"
    assert ".smc" in all_extensions()


# --- release filtering ----------------------------------------------------

def test_non_game_categories_are_rejected():
    movie = rel("Super Mario Bros 1993 1080p", cats=(2040,))
    assert not is_game_release(movie)
    assert score(movie, "super mario") < 0


def test_title_must_actually_match_the_request():
    assert title_matches("Super Mario World (USA)", "super mario world")
    assert not title_matches("Sonic the Hedgehog (USA)", "super mario world")
    # Short words are ignored, so this still matches.
    assert title_matches("The Legend of Zelda (USA)", "legend of zelda")


def test_dead_torrent_is_never_chosen():
    assert score(rel("Super Mario World (USA)", seeders=0), "super mario world") < 0


def test_usenet_is_not_penalised_for_having_no_seeders():
    nzb = rel("Super Mario World (USA)", seeders=0, protocol="usenet", url="http://x/n.nzb")
    assert score(nzb, "super mario world") > 0


def test_usa_region_beats_japan():
    usa = rel("Super Mario World (USA)")
    jpn = rel("Super Mario World (Japan)")
    assert score(usa, "super mario world") > score(jpn, "super mario world")


def test_prototypes_and_hacks_are_deprioritised():
    clean = rel("Super Mario World (USA)")
    hack = rel("Super Mario World (USA) [Hack]")
    assert score(clean, "super mario world") > score(hack, "super mario world")


def test_oversized_release_for_a_cartridge_platform_is_penalised():
    snes = by_slug("snes")
    small = rel("Super Mario World (USA)", size=512 * 1024)
    huge = rel("Super Mario World (USA)", size=8 * 1024 * 1024 * 1024)
    assert score(small, "super mario world", snes) > score(huge, "super mario world", snes)


def test_best_release_returns_none_when_nothing_qualifies():
    assert best_release([], "anything") is None
    assert best_release([rel("Unrelated Thing")], "super mario world") is None


def test_best_release_prefers_smaller_file_on_a_score_tie():
    a = rel("Super Mario World (USA)", size=4 * 1024 * 1024)
    b = rel("Super Mario World (USA)", size=512 * 1024)
    assert best_release([a, b], "super mario world") is b


# --- picking the ROM out of a finished download ---------------------------

def test_picks_the_rom_and_ignores_the_extras():
    snes = by_slug("snes")
    files = ["readme.nfo", "cover.jpg", "Super Mario World (USA).smc", "file_id.diz"]
    assert pick_rom_file(files, snes) == "Super Mario World (USA).smc"


def test_prefers_usa_dump_in_a_multi_region_archive():
    snes = by_slug("snes")
    files = ["Mario (Japan).smc", "Mario (USA).smc", "Mario (Europe).smc"]
    assert pick_rom_file(files, snes) == "Mario (USA).smc"


def test_returns_none_when_the_download_holds_no_rom():
    snes = by_slug("snes")
    assert pick_rom_file(["readme.nfo", "cover.jpg"], snes) is None
    assert pick_rom_file([], snes) is None


def test_extension_order_is_respected():
    # .smc is declared before .sfc, so it wins when both are present.
    snes = by_slug("snes")
    assert pick_rom_file(["Game.sfc", "Game.smc"], snes) == "Game.smc"


def test_a_switch_release_is_never_chosen_for_a_snes_request():
    """The real failure: searching for a SNES game returned
    '[Nintendo Switch] Super Mario World (NSP)'. The title matched and the size
    was plausible, so nothing else rejected it."""
    snes = by_slug("snes")
    switch = rel("[Nintendo Switch] Super Mario World (smw) [NSP][ENG]",
                 size=9 * 1024 * 1024, seeders=62)
    cart = rel("Super Mario World (USA).smc", size=512 * 1024, seeders=30)

    assert score(switch, "super mario world", snes) < 0
    assert best_release([switch, cart], "super mario world", snes) is cart


def test_platform_markers_match_whole_words_only():
    # "ds" is inside "worlds"; "pc" is inside "pcb". A naive substring check
    # disqualifies most of a result set.
    snes = by_slug("snes")
    fine = rel("Super Mario World (USA)", size=512 * 1024, seeders=40)
    assert score(fine, "super mario world", snes) > 0


def test_a_platforms_own_name_does_not_disqualify_it():
    gba = by_slug("gba")
    ok = rel("Pokemon Emerald (USA) Game Boy Advance", size=16 * 1024 * 1024, seeders=40)
    assert score(ok, "pokemon emerald", gba) > 0
