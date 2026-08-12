"""The platforms added so two measured backlogs could actually be loaded.

Nothing was blocking these games except a missing row in `platforms.py`. Two
separate pipelines had already done their work and stopped at the same wall:

  * Archive.org -- 64,953 items across eleven collections that
    `index-unmappable.json` recorded as "no romm platform", including 42,254
    Apple II items of which 22,473 are already written to `idx-appleii.jsonl`.
  * Vimm's Lair -- 7,353 of 22,803 catalogued titles skipped with the reason
    "no ROMarr platform slug", 3,406 of them Xbox 360 digital releases.

Every slug below is RomM's own `fs_slug`, read off the live library rather
than composed, because the slug IS the folder a download lands in. That makes
these strings the load-bearing part of the change: rename one and the files go
somewhere RomM will not look. Every display name is likewise RomM's own, and
`resolve` matches exactly, so a name that is not declared is a platform that
cannot be reached from the catalogue at all.
"""

from __future__ import annotations

import pytest

from romarr import platforms
from romarr.platforms import COMPUTER, DISC, by_slug, resolve
from romarr.selection import Release, score


# fs_slug -> the display name RomM publishes for that folder, taken from
# /api/platforms on the live library (393 platform rows, 84 with games).
ROMM_FOLDERS = {
    "apple2": "Apple II",
    "atari8bit": "Atari 8-bit",
    "atari-st": "Atari ST/STE",
    "mac": "Mac",
    "cpet": "Commodore PET",
    "sinclair-zx81": "Sinclair Zx81",
    "trs-80-mc-10": "TRS-80 MC-10",
    "palm-os": "Palm OS",
    "sg1000": "SG-1000",
    "odyssey-2-slash-videopac-g7000": "Odyssey 2 Slash Videopac G7000",
    "xbox": "Xbox",
    "xbox360": "Xbox 360",
    "ps3": "PlayStation 3",
    "wiiu": "Wii U",
}


@pytest.mark.parametrize("slug", sorted(ROMM_FOLDERS))
def test_the_folder_romm_uses_is_the_slug_declared(slug):
    """A slug that is not RomM's fs_slug is a directory RomM never scans."""
    assert by_slug(slug) is not None, f"{slug} is not in the platform table"


@pytest.mark.parametrize("slug,display", sorted(ROMM_FOLDERS.items()))
def test_romms_own_display_name_resolves_to_that_folder(slug, display):
    """`resolve` is exact now, so an undeclared display name is a dead end.

    RomM publishes the long, sometimes ugly form -- "Odyssey 2 Slash Videopac
    G7000" is its slug title-cased -- and that is the string the catalogue
    hands over. Answering it is the whole job.
    """
    got = resolve(display)
    assert got is not None, f"{display!r} resolves to nothing"
    assert got.slug == slug, f"{display!r} resolves to {got.slug}, not {slug}"


# Archive.org collection -> the slug its items belong in, with the item count
# `index-unmappable.json` measured on 2026-08-12.
ARCHIVE_COLLECTIONS = {
    "softwarelibrary_apple": ("apple2", 42254),
    "softwarelibrary_atari": ("atari8bit", 15557),
    "softwarelibrary_zx_81": ("sinclair-zx81", 1096),
    "softwarelibrary_palm": ("palm-os", 961),
    "softwarelibrary_mc10": ("trs-80-mc-10", 947),
    "softwarelibrary_atari_st_games": ("atari-st", 884),
    "softwarelibrary_mac": ("mac", 479),
    "softwarelibrary_pet": ("cpet", 379),
    "library_magnavox_odyssey2": ("odyssey-2-slash-videopac-g7000", 132),
    "sg_1000_library": ("sg1000", 109),
}


@pytest.mark.parametrize("collection,expected",
                         sorted((c, s) for c, (s, _) in
                                ARCHIVE_COLLECTIONS.items()))
def test_every_blocked_archive_collection_now_has_a_home(collection, expected):
    assert by_slug(expected) is not None, f"{collection} still has no slug"


def test_the_apple_ii_index_already_on_disk_resolves():
    """`idx-appleii.jsonl` was built before the platform existed.

    22,473 lines are waiting under the name `appleii`, which is RomM's
    *metadata* slug and the name of three different folders there. The folder
    holding the library's 666 existing Apple II rows is `apple2`, so that is
    the slug, and `appleii` has to answer to it or the index loads nowhere.
    """
    assert resolve("appleii").slug == "apple2"
    assert resolve("apple ii").slug == "apple2"


def test_miscconsoles_is_still_refused():
    """2,155 items, and no slug can be right for them.

    Archive.org's own `emulator` field on a 300-item sample names gbcolor,
    nes, gba, gameboy and snes -- five machines ROMarr already supports. The
    blocker is not a missing platform, it is that the indexer maps one
    collection to one slug, so a mixed collection has to be split by that
    field before any of it can be filed.
    """
    assert resolve("miscconsoles") is None
    assert resolve("mixed") is None


# Vimm's Lair system label -> slug. The six that were skipped, plus two that
# already worked, so a regression on either side shows up here.
VIMM_LABELS = {
    "Xbox": "xbox",
    "Xbox 360": "xbox360",
    "Xbox 360 (Digital)": "xbox360",
    "PlayStation 3": "ps3",
    "Wii U": "wiiu",
    "WiiWare": "wii",
    "Wii": "wii",
    "PlayStation 2": "ps2",
}


@pytest.mark.parametrize("label,slug", sorted(VIMM_LABELS.items()))
def test_every_vimm_system_label_resolves(label, slug):
    got = resolve(label)
    assert got is not None, f"Vimm label {label!r} resolves to nothing"
    assert got.slug == slug, f"{label!r} resolves to {got.slug}, not {slug}"


def test_the_vimm_plugin_slugs_are_the_ones_that_exist():
    """`.rom-hub/plugins/vimm-net` already mapped two labels to slugs.

    It named `xbox` and `ps3` before either was a platform here. Picking
    different strings would have left that plugin resolving to nothing while
    looking correct, so they are pinned rather than merely happening to agree.
    """
    assert resolve("Xbox").slug == "xbox"
    assert resolve("PlayStation 3").slug == "ps3"


def test_wiiware_is_the_wii_because_it_is_the_same_machine():
    """363 `.wad` files that Dolphin loads exactly as it loads a Wii disc.

    RomM has no WiiWare folder, and inventing one would put them where no
    scanner is pointed. The Wii already declares `.wad` for this reason.
    """
    wii = resolve("WiiWare")
    assert wii.slug == "wii"
    assert ".wad" in wii.extensions


def test_xbox_360_digital_lands_on_the_console_that_runs_it():
    """3,406 XBLA and Games-on-Demand titles, and one Xbox 360 folder."""
    assert resolve("Xbox 360 (Digital)").slug == "xbox360"
    assert resolve("xbla").slug == "xbox360"


def test_the_two_xbox_generations_stay_apart():
    """"xbox" is a substring of "Xbox 360" -- the psx/ps2 trap, one family over."""
    assert resolve("Xbox").slug == "xbox"
    assert resolve("Xbox 360").slug == "xbox360"
    assert resolve("microsoft xbox").slug == "xbox"
    assert resolve("microsoft xbox 360").slug == "xbox360"


def test_an_xbox_360_disc_is_rejected_for_an_original_xbox_request():
    """The half of the split that scoring has to enforce, not just naming.

    An original Xbox cannot read an XGD3 disc, and the sizes overlap, so the
    only thing standing between the two is the title naming the machine.
    """
    xbox = by_slug("xbox")
    release = Release(title="Halo 3 (USA) Xbox 360", size=7 << 30, seeders=50,
                      categories=(1000,), download_url="magnet:?xt=urn:btih:a",
                      protocol="torrent", indexer="x")
    assert score(release, "halo 3", xbox) <= 0


def test_an_xbox_title_is_not_rejected_for_its_own_platform():
    xbox = by_slug("xbox")
    release = Release(title="Halo 2 (USA) Xbox", size=6 << 30, seeders=50,
                      categories=(1000,), download_url="magnet:?xt=urn:btih:b",
                      protocol="torrent", indexer="x")
    assert score(release, "halo 2", xbox) > 0


# The extension that carried 100% of a live sample of each collection. First
# in the tuple is not cosmetic: `build_platform_index` picks one file per item
# by walking this order and skips an item with no match, so a wrong leader
# silently drops the collection's whole payload.
SAMPLED_LEAD_EXTENSION = {
    "apple2": ".dsk",
    "atari8bit": ".atr",
    "sinclair-zx81": ".p",
    "palm-os": ".prc",
    "trs-80-mc-10": ".c10",
    "atari-st": ".st",
    "mac": ".img",
    "cpet": ".d64",
    "odyssey-2-slash-videopac-g7000": ".bin",
}


@pytest.mark.parametrize("slug,ext", sorted(SAMPLED_LEAD_EXTENSION.items()))
def test_the_sampled_payload_extension_leads(slug, ext):
    assert by_slug(slug).extensions[0] == ext


def test_sg_1000_declares_both_forms_its_two_sources_use():
    """Archive.org ships `.bin`; the live folder holds `.sg` and `.sc`.

    Declaring only one of them indexes half the catalogue and imports none of
    the other half.
    """
    exts = by_slug("sg1000").extensions
    for ext in (".sg", ".sc", ".bin"):
        assert ext in exts, ext


def test_sg_1000_is_not_the_master_system():
    """Different video hardware; an sms core will not run these cartridges."""
    assert resolve("SG-1000").slug == "sg1000"
    assert resolve("sega sg 1000").slug == "sg1000"
    assert resolve("Sega Master System/Mark III").slug == "sms"


def test_the_new_home_computers_are_computers_and_the_consoles_are_not():
    for slug in ("apple2", "atari8bit", "atari-st", "mac", "cpet",
                 "sinclair-zx81", "trs-80-mc-10", "palm-os"):
        assert by_slug(slug).media == COMPUTER, slug
    assert by_slug("sg1000").media == "cartridge"
    assert by_slug("odyssey-2-slash-videopac-g7000").media == "cartridge"
    for slug in ("xbox", "xbox360", "ps3", "wiiu"):
        assert by_slug(slug).media == DISC, slug


def test_no_ambiguous_short_word_was_claimed_as_an_alias():
    """The refusals, pinned so a later "helpful" alias cannot undo them.

    Each of these names a machine that is either a different platform in RomM
    or an ordinary English word. `resolve` returning None sends the caller to
    "unknown platform: ..."; resolving it wrongly files a download into
    another machine's folder and says nothing.
    """
    for text in (
        "pet",            # ordinary word; the platform is "commodore pet"
        "trs 80",         # RomM has three separate TRS-80 folders
        "videopac",       # the G7400 is a different machine with its own folder
        # RomM publishes this as the display name of `videopac-g7400`, which
        # holds 110 games. Claiming it here answered all 110 about the wrong
        # machine until the sweep over every live display name found it.
        "videopac g7000",
        "apple iigs",     # 16-bit machine, its own folder, not apple2
        "atari",          # 2600/5200/7800/ST/8-bit all answer to this
    ):
        assert resolve(text) is None, f"{text!r} should not resolve"


def test_every_new_platform_declares_an_extension_nothing_else_leads_with():
    """A platform whose whole extension list is borrowed cannot be told apart.

    Not a hard rule for the table as a whole -- `.bin` and `.zip` are shared
    everywhere -- but each machine added here brought at least one format of
    its own, and losing that is how a platform quietly becomes unreachable by
    file inspection.
    """
    own = {
        "apple2": ".dsk", "atari8bit": ".atr", "atari-st": ".st",
        "mac": ".dc42", "cpet": ".d64", "sinclair-zx81": ".p",
        "trs-80-mc-10": ".c10", "palm-os": ".prc", "sg1000": ".sg",
        "wiiu": ".wux",
    }
    for slug, ext in own.items():
        assert ext in by_slug(slug).extensions, slug


def test_the_backlog_each_platform_unblocks_is_recorded():
    """The counts are the reason the platforms exist; keep them next to them."""
    # 64,953 measured across eleven collections, less the 2,155 in
    # miscconsoles that stay refused.
    total = sum(n for _, n in ARCHIVE_COLLECTIONS.values())
    assert total == 62798
    for _, (slug, _) in ARCHIVE_COLLECTIONS.items():
        assert by_slug(slug) is not None


def test_adding_platforms_did_not_move_an_existing_one():
    """The table is append-only in effect: nothing already routed changed."""
    assert resolve("snes").slug == "snes"
    assert resolve("Sega Mega Drive/Genesis").slug == "genesis-slash-megadrive"
    assert resolve("Commodore C64/128/MAX").slug == "c64"
    assert resolve("Nintendo Switch") is None
    assert resolve("Amstrad PCW") is None
    assert platforms.by_slug("wii").max_size == 12 * 1024 ** 3
