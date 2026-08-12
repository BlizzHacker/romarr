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
    assert resolve("Nintendo 64").slug == "n64"
    assert resolve("mega drive").slug == "genesis-slash-megadrive"


def test_the_japanese_machines_reach_their_own_folders():
    """"famicom" and "super famicom" were aliases of nes and snes, which was
    right while those were the only folders ROMarr could file into.

    RomM keeps them separate and the live library fills both -- 106 famicom,
    968 fds, 127 sfam -- so filing a Super Famicom request under `snes` puts
    it in a folder its requester did not ask for and leaves the one they did
    ask for empty.
    """
    assert resolve("super famicom").slug == "sfam"
    assert resolve("famicom").slug == "famicom"
    assert resolve("famicom disk system").slug == "fds"
    # The western twins are untouched.
    assert resolve("nes").slug == "nes"
    assert resolve("snes").slug == "snes"


def test_longer_alias_wins_over_shorter_substring():
    # "nintendo" is a substring of "super nintendo"; the specific one must win,
    # or every SNES request silently becomes an NES request.
    assert resolve("super nintendo entertainment system").slug == "snes"


def test_unknown_platform_is_none_not_a_guess():
    assert resolve("PlayStation 5") is None
    assert resolve("") is None


# --- RomM display names ---------------------------------------------------
#
# Every name below is one RomM actually serves, checked against the live
# library (romm.moveweight.com, RomM 5.1.0, 166,548 rows) on 2026-08-11. They
# are here because substring matching answered six of them with the wrong
# machine and two with nothing at all -- roughly 4,200 rows resolving to a
# console they are not.
#
# The wrong answers are the dangerous half. A row that resolves to nothing is
# visibly unhandled; a Switch row confidently answered "NES" gets an NES size
# ceiling, NES extensions and an NES import route, and every one of those is
# wrong in a way nothing downstream can detect.

def test_a_machine_we_do_not_model_resolves_to_nothing():
    """A shorter alias must not swallow the longer name that contains it.

    "Nintendo Switch" contains "nintendo" (an NES alias) and "PlayStation
    Vita" contains "playstation" (a PSX alias). Neither machine is modelled
    here, so the only correct answer is None -- 2,874 Switch rows and 34 Vita
    rows were answering as NES and PS1.
    """
    assert resolve("Nintendo Switch") is None
    assert resolve("PlayStation Vita") is None


def test_the_64dd_is_not_a_nintendo_64():
    """The disk drive is its own machine with its own dumps.

    "Nintendo 64DD" contains "nintendo 64" exactly, so partial matching
    answered n64 for all 15 of its rows. The trailing "dd" is not decoration.
    """
    assert resolve("Nintendo 64DD") is None


def test_the_ds_family_reaches_its_own_folders():
    """The DSi and the New 3DS resolve, and by name rather than by luck.

    Both happened to land right only because a longer label shared the start
    offset of "nintendo". RomM's slug forms remove the space, and with it the
    coincidence: "nintendo-dsi" and "new-nintendo-3ds" both answered NES.
    """
    for text in ("Nintendo DSi", "nintendo-dsi"):
        assert resolve(text).slug == "nds", text
    for text in ("New Nintendo 3DS", "new-nintendo-3ds"):
        assert resolve(text).slug == "3ds", text


def test_the_cd_addon_is_not_the_cartridge_machine():
    """"Turbografx-16/PC Engine CD" is the CD system, and only the CD system.

    The cartridge platform's name is a prefix of it, so position-ranked
    matching answered `turbografx16--1` for 1,262 CD rows -- a disc medium
    filed as a cartridge, with a 16MB ceiling and a `.pce` extension list that
    no CD image can satisfy.
    """
    assert resolve("Turbografx-16/PC Engine CD").slug == \
        "turbografx-16-slash-pc-engine-cd"
    # The cartridge machine still resolves under its own name.
    assert resolve("TurboGrafx-16").slug == "turbografx16--1"


def test_a_slash_joined_display_name_resolves_to_the_machine_it_names():
    """RomM joins the names one machine went by with a slash.

    "Commodore C64/128/MAX" is a C64, spelled the way RomM spells it. Matching
    the whole string against "commodore 64" found nothing and 688 rows
    resolved to None.
    """
    assert resolve("Commodore C64/128/MAX").slug == "c64"


def test_the_famicom_answers_to_its_english_name():
    """"Family Computer" is what the Famicom was called on its own box.

    It shares no substring with "famicom", so 106 rows resolved to nothing.
    """
    assert resolve("Family Computer").slug == "famicom"


def test_a_platform_name_buried_inside_a_word_is_not_a_match():
    """Substring matching had no word boundaries, and that is where it broke.

    "pc" sits inside "Amstrad PCW" and "ds" inside "Edsac", so those answered
    Windows and Nintendo DS. The Amstrad alone was 753 rows -- the second
    largest wrong answer in the library after the Switch.

    `selection._mentions` has rejected these two substrings by name since the
    scorer was written ("ds" is inside "worlds", "pc" is inside "pcb"). The
    resolver never got the same guard, and unlike the scorer it decides which
    directory a file is written to.
    """
    for text in ("Amstrad PCW", "Edsac  1", "TADS", "SDS Sigma 7"):
        assert resolve(text) is None, text


def test_a_whole_word_match_with_a_qualifier_left_over_is_not_a_match():
    """Every one of these begins with a name we model and ends somewhere else.

    The old rule only rejected a leftover that began with a digit, so
    "PlayStation 5" was caught and "PlayStation VR" was not. A leftover word
    is the same statement as a leftover number: it names a different machine.
    """
    for text in ("PC Booter", "PC-6001", "NEC PC-6000 Series", "Windows Phone",
                 "Windows Mobile", "Atari VCS", "Coleco Adam", "Plex Arcade",
                 "Telstar Arcade", "ZX Spectrum Next", "Intellivision Amico",
                 "Nintendo Playstation", "PlayStation VR", "PlayStation Now"):
        assert resolve(text) is None, text


def test_the_compound_display_names_keep_reaching_their_machines():
    """The other half of the bill: exact matching must not lose what worked.

    Substring matching answered these correctly by accident of prefix, and
    they are 5,000-odd rows between them. Exact matching only reaches them
    because each is now declared, which is the trade this change makes --
    coverage comes from the table, never from a guess.
    """
    assert resolve("Sega Mega Drive/Genesis").slug == "genesis-slash-megadrive"
    assert resolve("Sega Master System/Mark III").slug == "sms"
    assert resolve("Sega Game Gear").slug == "gamegear"
    assert resolve("Family Computer Disk System").slug == "fds"
    assert resolve("Super Nintendo Entertainment System").slug == "snes"


def test_punctuation_and_case_do_not_change_the_answer():
    """RomM, a request form and a slug spell the same machine three ways."""
    for text in ("Sega Mega Drive/Genesis", "SEGA MEGA DRIVE / GENESIS",
                 "sega mega drive genesis"):
        assert resolve(text).slug == "genesis-slash-megadrive", text


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
# The search deliberately casts a wide net (see ROMarr._search_releases), so
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


# --- language, translation and edit markers ---------------------------------
#
# A release title says what was DONE to a ROM at least as often as it says
# which ROM it is, and none of that was read. Every title below is a real
# Prowlarr result for one of these games, not an invented example.

def test_a_fan_translation_loses_to_a_plain_dump():
    """The failure this section exists for. A request for Chrono Trigger was
    fulfilled with a Russian fan translation: it was the best-seeded result and
    nothing in its title said "hack" or "translation" in so many words, so the
    scorer had no reason to prefer the 1-seeder English dump next to it."""
    snes = by_slug("snes")
    russian = rel("[SNES] Chrono Trigger [RUS] [jRPG] [by Chief-NET] [1995]",
                  size=4 * 1024 * 1024, seeders=13)
    plain = rel("Chrono Trigger - Super Nintendo",
                size=2 * 1024 * 1024, seeders=1)

    assert score(russian, "chrono trigger", snes) < score(plain, "chrono trigger", snes)
    assert best_release([russian, plain], "chrono trigger", snes) is plain


def test_every_non_english_language_marker_is_penalised():
    snes = by_slug("snes")
    clean = score(rel("Chrono Trigger (USA)"), "chrono trigger", snes)
    for marker in ("[RUS]", "[GER]", "[FRA]", "[ESP]", "[ITA]", "[KOR]",
                   "[POL]", "PT-BR"):
        tagged = rel(f"Chrono Trigger (USA) {marker}")
        assert score(tagged, "chrono trigger", snes) < clean, marker


def test_a_language_marker_only_counts_as_a_whole_word():
    """"ger" is inside "Trigger" -- the game that motivated all of this. "ita"
    is inside "digital", "spa" inside "space", "fin" inside "final". A
    substring check penalises a large part of every result set."""
    snes = by_slug("snes")
    for title, wanted in (("Chrono Trigger (USA)", "chrono trigger"),
                          ("Digital Pinball (USA)", "digital pinball"),
                          ("Space Invaders (USA)", "space invaders"),
                          ("Final Fantasy III (USA)", "final fantasy iii")):
        assert score(rel(title), wanted, snes) > 0, title


def test_a_multi_language_marker_is_penalised():
    # "[MULTI5]", "[MULTi8-ENG]", "MULTi10-PLAZA": always a localised PC or
    # emulator release in the results, never a cartridge dump.
    snes = by_slug("snes")
    clean = rel("Final Fantasy III (USA)")
    multi = rel("Final Fantasy III (USA) [MULTI5][RELOADED]")
    assert score(multi, "final fantasy iii", snes) < score(clean, "final fantasy iii", snes)


def test_goodtools_translation_tags_are_penalised():
    # (T-Eng) and (T+Rus) are the GoodTools marks for "this is a translation
    # patch applied to the ROM". Either way it is not the published game.
    snes = by_slug("snes")
    clean = score(rel("Final Fantasy V (Japan)"), "final fantasy v", snes)
    for tag in ("(T-Eng)", "(T+Eng)", "(T+Rus)", "[T+Ger1.0]"):
        translated = rel(f"Final Fantasy V (Japan) {tag}")
        assert score(translated, "final fantasy v", snes) < clean, tag


def test_a_release_credited_to_a_group_is_penalised():
    """"by <group>" on a cartridge ROM means somebody made this rather than
    dumped it -- "[by Chief-NET]", "by progameroms", "by SMW Central"."""
    snes = by_slug("snes")
    clean = rel("Super Mario World (USA)")
    credited = rel("Super Mario World (USA) [by SMW Central]")
    assert score(credited, "super mario world", snes) < score(clean, "super mario world", snes)


def test_hack_beta_and_proto_tags_are_penalised():
    snes = by_slug("snes")
    clean = score(rel("Super Mario World (USA)"), "super mario world", snes)
    for tag in ("[Hack]", "[Beta]", "[Proto]"):
        assert score(rel(f"Super Mario World (USA) {tag}"),
                     "super mario world", snes) < clean, tag


# --- region codes and dump quality ------------------------------------------

def test_compact_region_codes_score_positively():
    """"(U)", "(E)", "(UE)" and "(JU)" are the GoodTools region codes and the
    most common labelling in a real result set. Only the spelled-out forms were
    read, so a multi-region dump scored nothing for its region at all."""
    snes = by_slug("snes")
    bare = score(rel("Super Metroid"), "super metroid", snes)
    for code in ("(U)", "(E)", "(UE)", "(JU)", "(W)"):
        assert score(rel(f"Super Metroid {code}"), "super metroid", snes) > bare, code


def test_a_verified_good_dump_marker_scores_positively():
    # "[!]" is GoodTools for "this dump was verified against a known-good
    # checksum" -- the strongest quality signal a ROM title carries.
    snes = by_slug("snes")
    verified = rel("Super Metroid (JU) [!].smc", size=3 * 1024 * 1024, seeders=3)
    plain = rel("Super Metroid (JU).smc", size=3 * 1024 * 1024, seeders=3)
    assert score(verified, "super metroid", snes) > score(plain, "super metroid", snes)


def test_a_translation_never_outranks_a_verified_dump_on_seeders_alone():
    """Both halves of the Chrono Trigger failure in one assertion: the
    translation had 13 seeders and the good dump had 3."""
    snes = by_slug("snes")
    translation = rel("[SNES] Super Metroid [RUS] [by Chief-NET]",
                      size=3 * 1024 * 1024, seeders=40)
    verified = rel("Super Metroid (JU) [!].smc", size=3 * 1024 * 1024, seeders=3)
    assert best_release([translation, verified], "super metroid", snes) is verified


# --- how big a cartridge can be ----------------------------------------------
#
# One 512MB ceiling covered every platform, which is far larger than any
# cartridge ever made. A 452MB PC build of Final Fantasy III passed it, ranked
# top on seeders, and was picked for a SNES request -- the same class of
# failure as the translation above, reached through size instead of language.

def test_a_pc_sized_release_is_rejected_for_a_cartridge_platform():
    snes = by_slug("snes")
    pc_build = rel("FINAL FANTASY III v1 1 0", size=452 * 1024 * 1024, seeders=18)
    assert score(pc_build, "final fantasy iii", snes) < 0


def test_the_size_ceiling_is_per_platform_not_one_number():
    """A 100MB download is absurd for a SNES cartridge and unremarkable for a
    Game Boy Advance one. A single ceiling cannot say both."""
    hundred_mb = 100 * 1024 * 1024
    assert score(rel("Pokemon Emerald (U)", size=hundred_mb), "pokemon emerald",
                 by_slug("gba")) > 0
    assert score(rel("Chrono Trigger (U)", size=hundred_mb), "chrono trigger",
                 by_slug("snes")) < 0


def test_a_full_size_cartridge_dump_is_never_penalised_for_being_large():
    # The biggest carts that actually shipped: 6MB on SNES (Tales of Phantasia),
    # 32MB on GBA, 64MB on N64. None of these is suspicious.
    for slug, size in (("snes", 6 * 1024 * 1024),
                       ("gba", 32 * 1024 * 1024),
                       ("n64", 64 * 1024 * 1024),
                       ("genesis-slash-megadrive", 8 * 1024 * 1024)):
        biggest = rel("Some Game (USA)", size=size)
        assert score(biggest, "some game", by_slug(slug)) > 0, slug


def test_a_retranslation_is_penalised_like_a_translation():
    """"Chrono Trigger (Retranslated)" is a real RetroWithin title. The junk
    marker was the exact word "translation", so every other form of the word --
    translated, retranslated, retranslation -- went unnoticed."""
    snes = by_slug("snes")
    clean = rel("Chrono Trigger (USA)")
    for word in ("(Retranslated)", "(Translated)", "(Retranslation)"):
        assert score(rel(f"Chrono Trigger (USA) {word}"), "chrono trigger", snes) \
            < score(clean, "chrono trigger", snes), word


def test_every_platform_declares_a_ceiling_matched_to_its_medium():
    """A ceiling has to be loose enough to admit the game and tight enough to
    keep a PC repack out, and where that line sits depends on the medium.

    This was one number for every platform, because every platform was a
    cartridge. Splitting it by medium is what lets a 4GB PS2 image through
    while a 452MB PC build of Final Fantasy III still cannot pass for a SNES
    cartridge -- the failure the per-platform ceiling was introduced for.
    """
    from romarr.platforms import CARTRIDGE, COMPUTER, DISC, GB, PLATFORMS
    limits = {
        # Nothing cartridge-era needs a third of a gigabyte -- except the two
        # late handhelds, whose cards genuinely are that big.
        CARTRIDGE: 300 * 1024 * 1024,
        DISC: 16 * GB,
        COMPUTER: 4 * GB,
        # Modern PC: a AAA install routinely passes 100GB, and the ceiling
        # exists to reject romsets-pretending-to-be-cartridges, a failure
        # mode digital does not have.
        "digital": 300 * GB,
    }
    big_cards = {"nds", "3ds"}
    for p in PLATFORMS:
        assert p.max_size > 0, p.slug
        if p.slug in big_cards:
            assert p.max_size <= 8 * GB, p.slug
            continue
        assert p.max_size < limits[p.media], p.slug


# --- repackaged games are not cartridge dumps -------------------------------

def test_a_wii_virtual_console_wad_is_not_a_genesis_cartridge():
    """Reported live: a Genesis request grabbed
    "Phantasy.Star.IV.USA.SMD.Virtual.Console" -- a Wii Virtual Console WAD. It
    cannot be played as a Genesis ROM, and it could not even have been imported,
    because no Genesis extension appears among its files (a .wad and a pile of
    .par2 volumes).

    Two faults compounded. Nothing named Virtual Console, so the foreign-platform
    check passed: _mentions flattens the dots, and the title says "Virtual
    Console" rather than "Wii". Then the ROM-extension bonus was a plain
    substring test, and the dot-separated scene name contains ".smd" -- so the
    release earned the strongest platform signal the scorer has, for being a Wii
    package. ".md" matched as well, nested inside ".smd".
    """
    genesis = by_slug("genesis-slash-megadrive")
    wad = rel("Phantasy.Star.IV.USA.SMD.Virtual.Console", size=14_660_000)
    assert score(wad, "phantasy star iv", genesis) < 0


def test_an_extension_only_counts_where_a_filename_would_end():
    """The bonus exists because ".smc" identifies a cartridge better than any
    words in a title. That is only true of a real extension: mid-name, between
    dots, it is a platform token in a scene release and evidence of nothing.
    """
    snes = by_slug("snes")
    ends = rel("Super Metroid (JU) [!].smc")
    delimited = rel("Super Metroid.smc (USA)")
    mid_token = rel("Super.Metroid.smc.Virtual.Console.Collection")

    assert score(ends, "super metroid", snes) > 0
    assert score(delimited, "super metroid", snes) > 0
    assert score(mid_token, "super metroid", snes) < score(ends, "super metroid", snes)


def test_a_shorter_extension_does_not_match_inside_a_longer_one():
    """Genesis declares both .md and .smd, and ".md" is a substring of ".smd" --
    so any .smd release scored the bonus twice over, and any dotted name with
    "SMD" in it scored it at all."""
    genesis = by_slug("genesis-slash-megadrive")
    real = rel("Phantasy Star IV (USA).smd")
    assert score(real, "phantasy star iv", genesis) > 0


def test_a_compilation_does_not_win_a_single_game_request():
    """Both titles are real, from one live Genesis search for Phantasy Star IV.
    The compilation was ranked first and the cartridge second:

        44   17.5MB  SEGA Genesis Classics Phantasy Star IV
        32    2.3MB  Phantasy Star IV - Mega Drive - Genesis

    "SEGA Genesis Classics" is the Steam package. It says "Genesis", so it
    collected the platform bonus, and 17.5MB sits inside the headroom the size
    ceiling deliberately leaves for zipped dumps with box art. Nothing rejected
    it, and a PC installer holds no .smd for the importer to find -- so the grab
    succeeds and the import cannot.

    A compilation is the same class of thing as a romset: not a cartridge dump,
    and not importable as one.
    """
    genesis = by_slug("genesis-slash-megadrive")
    compilation = rel("SEGA Genesis Classics Phantasy Star IV", size=17_500_000)
    cartridge = rel("Phantasy Star IV - Mega Drive - Genesis", size=2_300_000)

    assert score(compilation, "phantasy star iv", genesis) < 0
    assert score(cartridge, "phantasy star iv", genesis) > 0


def test_a_romset_or_anthology_is_refused_too():
    snes = by_slug("snes")
    for title in ("Super Nintendo Complete Romset (No-Intro)",
                  "SNES Game Collection 2024",
                  "Super Mario Anthology"):
        assert score(rel(title, size=8_000_000), "super mario world", snes) < 0, title
