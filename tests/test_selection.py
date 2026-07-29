import pytest

from romarr.indexers import _download_link
from romarr.platforms import resolve, platform_for_file, by_slug, all_extensions
from romarr.selection import (
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
    from romarr.platforms import PLATFORMS
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


# --- platform evidence -------------------------------------------------------
#
# The search deliberately casts a wide net (see Romarr._search_releases), so
# scoring has to be able to tell the same game apart across consoles.

def test_a_rom_extension_beats_a_bare_title():
    # ".smc" is the strongest signal a release is a SNES cartridge dump --
    # stronger than any words in the title.
    snes = resolve("snes")
    with_ext = Release(title="Super Metroid (JU) [!].smc", size=3 << 20, seeders=3,
                       categories=(1000,), download_url="magnet:?xt=urn:btih:a",
                       protocol="torrent", indexer="x")
    bare = Release(title="Super Metroid", size=3 << 20, seeders=3,
                   categories=(1000,), download_url="magnet:?xt=urn:btih:b",
                   protocol="torrent", indexer="x")
    assert score(with_ext, "Super Metroid", snes) > score(bare, "Super Metroid", snes)


def test_naming_the_requested_platform_helps_but_a_foreign_one_still_disqualifies():
    snes = resolve("snes")
    named = Release(title="Super Metroid - Super Nintendo", size=1 << 20, seeders=1,
                    categories=(1010,), download_url="magnet:?xt=urn:btih:c",
                    protocol="torrent", indexer="x")
    bare = Release(title="Super Metroid", size=1 << 20, seeders=1,
                   categories=(1010,), download_url="magnet:?xt=urn:btih:d",
                   protocol="torrent", indexer="x")
    assert score(named, "Super Metroid", snes) > score(bare, "Super Metroid", snes)

    # The wider net must not start accepting other consoles' releases.
    foreign = Release(title="Super Metroid [Nintendo Switch port]", size=11 << 20,
                      seeders=7, categories=(1010,), download_url="magnet:?xt=urn:btih:e",
                      protocol="torrent", indexer="x")
    assert score(foreign, "Super Metroid", snes) < 0


# --- prowlarr download links -------------------------------------------------
#
# Prowlarr's field names mislead, and getting this wrong is silent: the release
# is selected and then discarded for "no usable download link".

def test_a_real_magnet_in_guid_is_used_when_magneturl_is_a_prowlarr_link():
    # Exactly what The Pirate Bay returns through Prowlarr: magnetUrl is a
    # proxy URL carrying the api key, and the actual magnet is in guid.
    link = _download_link({
        "magnetUrl": "http://192.168.0.115:9696/2/download?apikey=SECRET&link=abc",
        "downloadUrl": None,
        "guid": "magnet:?xt=urn:btih:633E19066F941216B22D456D57F59694E3A0C425&dn=Super+Metroid",
        "infoHash": "633E19066F941216B22D456D57F59694E3A0C425",
    })
    assert link.startswith("magnet:?xt=urn:btih:633E1906")
    assert "apikey" not in link and "SECRET" not in link


def test_a_magnet_is_rebuilt_from_infohash_when_no_literal_one_exists():
    link = _download_link({
        "magnetUrl": "http://prowlarr/1/download?apikey=SECRET",
        "guid": "http://prowlarr/1/details",
        "infoHash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        "title": "Some Game (USA)",
    })
    assert link.startswith("magnet:?xt=urn:btih:ABCDEF0123456789")
    assert "SECRET" not in link
    # Without an announce target a bare hash may never find peers.
    assert "&tr=" in link


def test_an_http_link_is_the_last_resort_and_nothing_is_invented():
    assert _download_link({"downloadUrl": "https://indexer.example/x.torrent"}) \
        == "https://indexer.example/x.torrent"
    assert _download_link({"magnetUrl": None, "guid": None}) == ""


def test_a_real_download_url_beats_a_rebuilt_magnet():
    # A rebuilt magnet is a hash plus four guessed trackers. When the indexer
    # offers an actual .torrent there is no reason to prefer the guess.
    link = _download_link({
        "downloadUrl": "http://prowlarr/1/download?apikey=SECRET",
        "infoHash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
    })
    assert link == "http://prowlarr/1/download?apikey=SECRET"


# --- private trackers --------------------------------------------------------
#
# Both of these were silent: the release was selected correctly and then either
# discarded or handed to the download client as a magnet that could never start.

def test_a_private_result_never_gets_a_public_magnet():
    """A private torrent disables DHT and PEX and announces only to its own
    tracker with the account's passkey. A magnet rebuilt with public announce
    URLs cannot find a peer, so preferring one over Prowlarr's download URL
    produced a torrent that sat at zero peers forever."""
    row = {
        "downloadUrl": "http://prowlarr/1/download?apikey=SECRET",
        "infoHash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        "title": "Chrono Trigger (USA)",
    }
    assert _download_link(row, private=True) == "http://prowlarr/1/download?apikey=SECRET"
    # And with nothing to proxy through, it reports honestly rather than
    # inventing a magnet that cannot work.
    assert _download_link({"infoHash": "AB" * 20}, private=True) == ""
    # The public path is unchanged.
    assert _download_link({"infoHash": "AB" * 20}).startswith("magnet:?xt=urn:btih:")


def test_a_private_tracker_keeps_a_release_with_no_seeders():
    """Zero seeders means dead on a public tracker and 'nobody is awake right
    now' on a private one. Rejecting it outright made the entire catalogue of a
    rare-retro tracker unreachable."""
    dead_public = rel("Panzer Dragoon Saga (USA)", seeders=0)
    quiet_private = Release(
        title="Panzer Dragoon Saga (USA)", size=512 * 1024, seeders=0,
        categories=(1090,), download_url="http://prowlarr/1/download?apikey=x",
        protocol="torrent", indexer="RetroWithin", private=True)

    assert score(dead_public, "panzer dragoon saga") < 0
    assert score(quiet_private, "panzer dragoon saga") > 0


def test_seeder_count_no_longer_outranks_being_the_right_release():
    """The ranking bug that made private trackers pointless. At
    `min(seeders, 50) * 4` availability was worth up to 200 points -- more than
    every quality signal combined -- so a heavily-seeded public romset beat an
    exact, correctly-labelled cartridge dump from a private tracker."""
    snes = by_slug("snes")
    public_romset = Release(
        title="Super Metroid", size=400 * 1024 * 1024, seeders=500,
        categories=(1090,), download_url="magnet:?xt=urn:btih:a",
        protocol="torrent", indexer="Public", private=False)
    private_dump = Release(
        title="Super Metroid (USA).smc", size=3 * 1024 * 1024, seeders=1,
        categories=(1090,), download_url="http://prowlarr/1/download?apikey=x",
        protocol="torrent", indexer="bitGAMER", private=True)

    assert score(private_dump, "super metroid", snes) > \
        score(public_romset, "super metroid", snes)
    assert best_release([public_romset, private_dump], "super metroid", snes) \
        is private_dump
