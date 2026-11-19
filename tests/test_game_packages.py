"""PS4 base game, update and DLC: release classification, scoring and game folders."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from romarr.app import ROMarr, make_handler
from romarr.import_intent import preview_import
from romarr.library import ImportResult, import_rom
from romarr.platforms import resolve
from romarr.game_packages import (DEFAULT_PARTS, file_role, game_folder_name, identify, imported_role,
                                  normalise_parts,
                                 parse_header, plan_game_folder, release_parts, title_before_parts,
                                 title_id)
from romarr.selection import Release, best_release, judge, package_class
from romarr.store import QueueItem, Store

PS4 = resolve("ps4")
GB = 1024 ** 3
CONTENT_ID = "UP9000-CUSA00552_00-THELASTOFUS00000"
needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


def plan_ps4(packages, platform_dir, title):
    """plan_game_folder for PS4 packages."""
    return plan_game_folder(packages, platform_dir, title, "ps4", (".pkg",))


def header(content_type=0x1A, flags=0, content_id=CONTENT_ID, size=0x80):
    """A synthetic PS4 package header (big-endian, LibOrbisPkg layout)."""
    data = bytearray(size)
    data[0:4] = b"\x7fCNT"
    data[0x40:0x40 + len(content_id)] = content_id.encode()
    data[0x74:0x78] = content_type.to_bytes(4, "big")
    data[0x78:0x7C] = flags.to_bytes(4, "big")
    return bytes(data)


BASE_PKG = header()
PATCH_PKG = header(flags=0x40000000)
FIRST_PATCH_PKG = header(flags=0x00100000)
DELTA_PKG = header(content_type=0x1E)
DLC_PKG = header(content_type=0x1B, content_id="UP9000-CUSA00552_00-TLOUREMLEFTBEHND")
LICENSE_PKG = header(content_type=0x1C, content_id="UP9000-CUSA00552_00-TLOURSEASONPASS")


def make(root, relative, data=b"x"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# -- release titles ---------------------------------------------------------

@pytest.mark.parametrize("title, parts", [
    ("The Last of Us Remastered PS4", {"base"}),
    ("The.Last.of.Us.Remastered.PS4-DUPLEX", {"base"}),
    ("The.Last.of.Us.Remastered.Update.v1.11.PS4-DUPLEX", {"update"}),
    ("The Last of Us Remastered (Update Only) v1.11", {"update"}),
    ("The Last of Us Remastered Patch 1.11", {"update"}),
    ("The Last of Us Remastered + Update 1.11 + 3 DLC", {"base", "update", "dlc"}),
    ("The Last of Us Remastered [CUSA00552] incl. Update v1.11 and all DLC", {"base", "update", "dlc"}),
    ("Uncharted 4 v1.00 + v1.33 Update + all DLC", {"base", "update", "dlc"}),
    ("Fallout 4 Base + Update 1.38", {"base", "update"}),
    ("Bloodborne w/ Update", {"base", "update"}),
    ("Bloodborne & DLC", {"base", "dlc"}),
    ("Bloodborne.DLC.The.Old.Hunters.PS4-DUPLEX", {"dlc"}),
    ("Bloodborne Update 1.09 + DLC", {"update", "dlc"}),
    ("Fallout 4 Season Pass", {"dlc"}),
    ("Fallout 4 Add-On Pack", {"dlc"}),
    ("CUSA00552-patch", {"update"}),
    ("CUSA00552-ac", {"dlc"}),
    ("CUSA00552-app", {"base"}),
    ("UP9000-CUSA00552_00-THELASTOFUS00000-A0100-V0100", {"base"}),
    ("UP9000-CUSA00552_00-THELASTOFUS00000-A0111-V0100", {"update"}),
    # Words that only look like markers: an edition, a game called "Patched",
    # "AC" as Assassin's Creed, and "patched" as a word of its own.
    ("Assassins Creed Unity AC", {"base"}),
    ("Ghost of Tsushima Directors Cut PS4 FW 9.00 Backport", {"base"}),
    ("Game Name Patched", {"base"}),
])
def test_release_parts_reads_frozen_titles(title, parts):
    assert release_parts(title) == parts


def test_words_of_the_requested_title_are_not_markers():
    assert release_parts("Patch Quest v1.02") == {"update"}
    assert release_parts("Patch Quest v1.02", "Patch Quest") == {"base"}
    assert release_parts("Patch Quest Patch 1.03", "Patch Quest") == {"update"}


@pytest.mark.parametrize("title, core", [
    ("The Last of Us Remastered + Update 1.11 + 3 DLC", "The Last of Us Remastered"),
    ("Bloodborne [CUSA00900] incl. Update v1.09 and all DLC", "Bloodborne [CUSA00900]"),
    ("Uncharted 2 Update 1.09", "Uncharted 2"),
    ("Uncharted 4 v1.00 + v1.33 Update", "Uncharted 4 v1.00"),
    ("Bloodborne (DLC Only)", "Bloodborne"),
    ("Bloodborne PS4", "Bloodborne PS4"),
])
def test_title_before_parts(title, core):
    assert title_before_parts(title) == core


def test_pathological_titles_stay_linear():
    title = "1." * 400 + "x update"
    assert release_parts(title) == {"update"}


@pytest.mark.parametrize("value, parts", [
    (None, ("base",)), ([], ("base",)), ("dlc", ("dlc",)),
    (["DLC", "base", "update"], ("base", "update", "dlc")), (["update", "update"], ("update",)),
])
def test_normalise_parts(value, parts):
    assert normalise_parts(value) == parts


@pytest.mark.parametrize("value", [["patches"], [1], {"base": True}, 7])
def test_normalise_parts_refuses_anything_else(value):
    with pytest.raises(ValueError):
        normalise_parts(value)


# -- package headers and files ---------------------------------------------

@pytest.mark.parametrize("data, role", [
    (BASE_PKG, "base"), (PATCH_PKG, "update"), (FIRST_PATCH_PKG, "update"),
    (DELTA_PKG, "update"), (DLC_PKG, "dlc"), (LICENSE_PKG, "dlc"),
    (header(content_type=0x99), ""),
])
def test_header_roles(data, role):
    parsed = parse_header(data)
    assert parsed is not None and parsed.role == role


@pytest.mark.parametrize("data", [b"", b"\x7fCNT", b"\x00" * 0x80, header()[:0x7F]])
def test_not_a_package_header(data):
    assert parse_header(data) is None


def test_title_ids():
    assert parse_header(DLC_PKG).content_id == "UP9000-CUSA00552_00-TLOUREMLEFTBEHND"
    assert title_id(CONTENT_ID) == "CUSA00552"
    assert title_id("The Last of Us [cusa00552] v1.11.pkg") == "CUSA00552"
    assert title_id("Demon's Souls PPSA01341") == "PPSA01341"
    assert title_id("No id here CUSA1234.pkg") == ""


def test_file_role_header_beats_folder_and_name():
    assert file_role("Update/game.pkg", parse_header(BASE_PKG)) == "base"
    assert file_role("Update/game.pkg") == "update"
    assert file_role("Game + DLC/DLC/pack.pkg") == "dlc"
    assert file_role("Game Name DLC Pack.pkg") == "dlc"
    assert file_role("Game Name + Update.pkg") == "base"
    assert file_role("EP0002-CUSA04762_00-XXXX-A0105-V0100.pkg") == "update"
    assert file_role("Game Name.pkg") == "base"


def test_game_folder_names_are_safe():
    assert game_folder_name('Ratchet: Clank / "2016"') == "Ratchet Clank 2016"
    assert game_folder_name("CON") == ""
    assert game_folder_name(" ... ") == ""


def test_plan_files_base_update_and_dlc_in_one_folder(tmp_path):
    routes = plan_ps4([("a.pkg", "base", "CUSA00552"), ("b.pkg", "update", "CUSA00552"),
                               ("c.pkg", "dlc", "CUSA00552")], tmp_path, "The Last of Us")
    assert routes == {"a.pkg": "The Last of Us", "b.pkg": "The Last of Us/update",
                      "c.pkg": "The Last of Us/dlc"}


def test_plan_refuses_two_bases(tmp_path):
    assert plan_ps4([("a.pkg", "base", ""), ("b.pkg", "base", "")], tmp_path, "Two games") is None


def test_plan_refuses_an_update_for_another_game(tmp_path):
    assert plan_ps4([("a.pkg", "base", "CUSA00552"), ("b.pkg", "update", "CUSA99999")],
                            tmp_path, "The Last of Us") is None


def test_plan_reuses_the_game_folder_holding_the_same_title_id(tmp_path):
    make(tmp_path, "TLOU Remastered/UP9000-CUSA00552.pkg", BASE_PKG)
    make(tmp_path, "Other Game/other.pkg", header(content_id="UP9000-CUSA99999_00-OTHERGAME0000000"))
    routes = plan_ps4([("u.pkg", "update", "CUSA00552")], tmp_path, "The Last of Us Remastered")
    assert routes == {"u.pkg": "TLOU Remastered/update"}


def test_plan_does_not_guess_between_two_matching_folders(tmp_path):
    make(tmp_path, "TLOU/base.pkg", BASE_PKG)
    make(tmp_path, "TLOU copy/base.pkg", BASE_PKG)
    routes = plan_ps4([("u.pkg", "update", "CUSA00552")], tmp_path, "The Last of Us")
    assert routes == {"u.pkg": "The Last of Us/update"}


@needs_symlinks
def test_plan_never_matches_a_linked_game_folder(tmp_path):
    make(tmp_path / "elsewhere", "base.pkg", BASE_PKG)
    (tmp_path / "ps4").mkdir()
    (tmp_path / "ps4" / "TLOU").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    routes = plan_ps4([("u.pkg", "update", "CUSA00552")], tmp_path / "ps4", "The Last of Us")
    assert routes == {"u.pkg": "The Last of Us/update"}


# -- scoring ----------------------------------------------------------------

def rel(title, size=40 * GB, seeders=10):
    return Release(title, size, seeders, (1080,), "magnet:?xt=urn:btih:" + "a" * 40, "torrent")


GAME = "The Last of Us Remastered"
FROZEN = [
    rel("The Last of Us Remastered PS4"),
    rel("The.Last.of.Us.Remastered.Update.v1.11.PS4-DUPLEX", 2 * GB),
    rel("The Last of Us Remastered PS4 + Update 1.11 + 3 DLC"),
    rel("The.Last.of.Us.Remastered.DLC.Left.Behind.PS4-DUPLEX", GB),
]


def ranked(parts=None):
    scores = [(judge(r, GAME, PS4, parts=parts).points, r.title) for r in FROZEN]
    return [title for points, title in sorted(scores, key=lambda pair: -pair[0]) if points > 0]


def test_a_game_request_prefers_the_combined_pack_and_refuses_lone_extras():
    assert ranked() == ["The Last of Us Remastered PS4 + Update 1.11 + 3 DLC",
                        "The Last of Us Remastered PS4"]
    verdict = judge(FROZEN[1], GAME, PS4)
    assert verdict.points < 0 and "update, not the requested base game" in verdict.verdict


def test_a_request_for_everything_still_needs_the_game_first():
    assert ranked(["base", "update", "dlc"]) == ["The Last of Us Remastered PS4 + Update 1.11 + 3 DLC",
                                                 "The Last of Us Remastered PS4"]
    why = judge(FROZEN[0], GAME, PS4, parts=["base", "update", "dlc"]).why()
    assert any("base game (lacks update + DLC)" in line for line in why)
    verdict = judge(FROZEN[1], GAME, PS4, parts=["base", "update", "dlc"]).verdict
    assert verdict.endswith("update; the request needs the base game")


def test_a_request_for_the_update_prefers_the_update():
    assert ranked(["update"]) == ["The.Last.of.Us.Remastered.Update.v1.11.PS4-DUPLEX",
                                  "The Last of Us Remastered PS4 + Update 1.11 + 3 DLC"]
    assert best_release(FROZEN, GAME, PS4, parts=["update"]).title == FROZEN[1].title


def test_a_request_for_dlc_accepts_a_tiny_license_package():
    tiny = rel("The Last of Us Remastered DLC Season Pass PS4", 300 * 1024)
    assert judge(tiny, GAME, PS4, parts=["dlc"]).accepted
    assert not judge(tiny, GAME, PS4, parts=["base"]).accepted


def test_package_class_is_shown_where_games_have_updates_and_dlc():
    assert package_class(FROZEN[2], GAME, PS4) == "base game + update + DLC"
    assert package_class(FROZEN[2], GAME, resolve("ps3")) == "base game + update + DLC"
    assert package_class(FROZEN[2], GAME, resolve("psx")) == ""


def test_platforms_without_updates_keep_their_rules():
    psx = resolve("psx")
    why = judge(rel("Final Fantasy VII DLC PS1", GB), "Final Fantasy VII", psx).why()
    assert any("looks like a hack, beta or repack ('dlc')" in line for line in why)
    assert judge(rel("Final Fantasy VII Hotfix PS1", GB), "Final Fantasy VII", psx).verdict == "a PC release (hotfix)"
    assert judge(rel("The Last of Us Hotfix PS4"), "The Last of Us", PS4).accepted is False
    assert not any("looks like a hack" in line
                   for line in judge(FROZEN[2], GAME, PS4).why())


# Frozen release titles per platform: (platform, request, title, size, parts held).
PLATFORM_TITLES = [
    ("ps3", "Resistance 3", "Resistance.3.PS3-DUPLEX", 8 * GB, {"base"}),
    ("ps3", "Resistance 3", "Resistance.3.Update.v1.04.PS3-DUPLEX", 200 * 1024 ** 2, {"update"}),
    ("ps3", "Resistance 3", "Resistance 3 [BCUS98153] + Update 1.04 + DLC PS3", 8 * GB, {"base", "update", "dlc"}),
    ("psvita", "Persona 4 Golden", "Persona 4 Golden PSV + Update 1.01", 3 * GB, {"base", "update"}),
    ("psp", "Monster Hunter Freedom Unite", "Monster Hunter Freedom Unite DLC Pack PSP", 50 * 1024 ** 2, {"dlc"}),
    ("3ds", "Fire Emblem Fates", "Fire Emblem Fates 3DS + Update + All DLC", 3 * GB, {"base", "update", "dlc"}),
    ("wiiu", "Mario Kart 8", "Mario Kart 8 WiiU Update v4.1", 600 * 1024 ** 2, {"update"}),
    ("switch", "Bayonetta 2", "Bayonetta 2 NSW + Update v1.2.0 + DLC", 12 * GB, {"base", "update", "dlc"}),
    ("switch", "Bayonetta 2", "Bayonetta.2.Update.v1.2.0.NSW-VENOM", 500 * 1024 ** 2, {"update"}),
    ("xbox360", "Halo 3", "Halo 3 XBOX360 + Title Update + DLC", 7 * GB, {"base", "update", "dlc"}),
]


@pytest.mark.parametrize("slug, game, title, size, held", PLATFORM_TITLES)
def test_every_package_platform_reads_parts_and_ranks_packs(slug, game, title, size, held):
    platform = resolve(slug)
    release = rel(title, size)
    assert package_class(release, game, platform) == " + ".join(
        {"base": "base game", "update": "update", "dlc": "DLC"}[p] for p in ("base", "update", "dlc") if p in held)
    verdict = judge(release, game, platform)
    assert verdict.accepted is ("base" in held), verdict.why()
    if "update" in held and "base" not in held:
        assert judge(release, game, platform, parts=["update"]).accepted, judge(
            release, game, platform, parts=["update"]).why()


def test_pc_refuses_a_lone_update_and_prefers_the_full_repack():
    win = resolve("win")
    releases = [Release("Cyberpunk 2077 Update v2.1-GOG", 2 * GB, 30, (4050,), "magnet:?xt=urn:btih:" + "b" * 40, "torrent"),
                Release("Cyberpunk 2077 v2.1 + All DLCs [FitGirl Repack]", 60 * GB, 30, (4050,),
                        "magnet:?xt=urn:btih:" + "c" * 40, "torrent"),
                Release("Cyberpunk 2077 [FitGirl Repack]", 55 * GB, 30, (4050,), "magnet:?xt=urn:btih:" + "d" * 40, "torrent")]
    assert not judge(releases[0], "Cyberpunk 2077", win).accepted
    assert best_release(releases, "Cyberpunk 2077", win).title == releases[1].title
    assert best_release(releases, "Cyberpunk 2077", win, parts=["update"]).title == releases[0].title


@pytest.mark.parametrize("slug, name, family, role", [
    ("switch", "Bayonetta 2 [01007960049A0000][v0].nsp", "01007960049a0000", "base"),
    ("switch", "Bayonetta 2 [01007960049A0800][v196608].nsp", "01007960049a0000", "update"),
    ("switch-2", "Bayonetta 2 Costume [01007960049A1001][v0].nsp", "01007960049a0000", "dlc"),
    ("3ds", "Fire Emblem Fates (USA) [00040000001B4000].cia", "00040000001b4000", "base"),
    ("3ds", "Fire Emblem Fates Update [0004000E001B4000].cia", "00040000001b4000", "update"),
    ("3ds", "Fire Emblem Fates DLC [0004008C001B4000].cia", "00040000001b4000", "dlc"),
    ("wiiu", "Mario Kart 8 [0005000010145000].wua", "0005000010145000", "base"),
    ("wiiu", "Mario Kart 8 [0005000E10145000]", "0005000010145000", "update"),
    ("wiiu", "Mario Kart 8 DLC [0005000C10145000]", "0005000010145000", "dlc"),
    ("ps3", "UP0700-BLUS30443_00-DEMONSSOULS00000.pkg", "BLUS30443", ""),
    ("psvita", "Persona 4 Golden PCSE00120.vpk", "PCSE00120", ""),
    ("xbox360", "Halo 3 [0004000E001B4000].iso", "", ""),
    ("snes", "Chrono Trigger [01007960049A0800].sfc", "", ""),
])
def test_identify_reads_each_platforms_ids(slug, name, family, role):
    assert identify(name, slug) == (family, role)


def test_title_ids_say_what_a_file_is_on_switch_3ds_and_wiiu():
    assert file_role("Bayonetta 2 Update [01007960049A0800][v196608].nsp", platform_slug="switch") == "update"
    assert file_role("Game [0004008C001B4000].cia", platform_slug="3ds") == "dlc"
    assert file_role("Game DLC [0004000000055D00].cia", platform_slug="3ds") == "base"   # the ID wins


SWITCH_FILES = {"Bayonetta 2 [01007960049A0000][v0].nsp": b"base",
                "Bayonetta 2 Update [01007960049A0800][v196608].nsp": b"update",
                "Bayonetta 2 Costume [01007960049A1001][v0].nsp": b"dlc"}


@pytest.mark.parametrize("slug, files, folder", [
    ("switch", SWITCH_FILES, "Bayonetta 2"),
    ("3ds", {"Fire Emblem Fates [00040000001B4000].cia": b"base",
             "Fire Emblem Fates [0004000E001B4000].cia": b"update",
             "Fire Emblem Fates [0004008C001B4000].cia": b"dlc"}, "Fire Emblem Fates"),
    ("psvita", {"Persona 4 Golden.vpk": b"base", "Update/Persona 4 Golden Patch.vpk": b"update",
                "DLC/Persona 4 Golden Costumes.vpk": b"dlc"}, "Persona 4 Golden"),
])
def test_every_package_platform_imports_one_game_folder(tmp_path, slug, files, folder):
    download = download_with(tmp_path, files)
    library = tmp_path / "library"
    platform = resolve(slug)
    results = import_rom(download, platform, library, package_folders=True, game_title=folder)
    game = library / slug / folder
    roles = {Path(r.destination).parent.name: Path(r.destination).name for r in results if r.ok}
    assert all(r.ok for r in results), [r.reason for r in results]
    assert set(roles) == {folder, "update", "dlc"}
    assert all(Path(r.destination).is_relative_to(game) for r in results)


def test_a_later_switch_update_joins_its_game_by_title_id(tmp_path):
    library = tmp_path / "library"
    make(library / "switch", "Bayonetta 2/Bayonetta 2 [01007960049A0000][v0].nsp", b"base")
    download = make(tmp_path / "downloads", "Bayonetta 2 Update [01007960049A0800][v196608].nsp", b"update")
    result, = import_rom(download, resolve("switch"), library, package_folders=True, game_title="Bayonetta Two")
    assert result.ok and result.destination == (library / "switch" / "Bayonetta 2" / "update"
                                                / "Bayonetta 2 Update [01007960049A0800][v196608].nsp")


def test_retro_platforms_keep_their_flat_layout(tmp_path):
    download = make(tmp_path / "downloads", "Chrono Trigger (USA).sfc", b"rom" * 100)
    result, = import_rom(download, resolve("snes"), tmp_path / "library", package_folders=True,
                         game_title="Chrono Trigger")
    assert result.ok and result.destination == tmp_path / "library" / "snes" / "Chrono Trigger (USA).sfc"


def test_the_platform_list_marks_package_platforms(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    marked = {row["slug"] for row in service.platform_directory() if row["parts"]}
    assert marked == {"ps3", "ps4", "ps5", "psp", "psvita", "3ds", "wiiu",
                      "switch", "switch-2", "xbox360", "win"}


def test_package_words_never_turn_a_sequel_into_a_match():
    for title in ("Uncharted 2 PS4 + Update 1.09 + DLC", "Uncharted.2.Update.v1.09.PS4-DUPLEX"):
        assert judge(rel(title), "Uncharted", PS4, parts=["update"]).verdict == "title does not match 'Uncharted'"


def test_scene_names_match_on_ps4():
    assert judge(rel("The.Last.of.Us.Remastered.PS4-DUPLEX"), GAME, PS4).accepted
    assert judge(rel("The.Last.of.Us.Remastered.incl.Update.v1.11.PS4-DUPLEX"), GAME, PS4).accepted
    assert judge(rel("The.Last.of.Us.Remastered.PS4-DUPLEX [USA]"), GAME, PS4).accepted


def unmarked(title, categories=(1000,)):
    return Release(title, 0, 0, categories, "https://nzb.example/get/1", "usenet")


def test_package_points_never_make_an_unmarked_release_usable():
    verdict = judge(unmarked("The Last of Us Remastered + Update 1.11 + 3 DLC"), GAME, PS4)
    assert not verdict.accepted and verdict.verdict.startswith("no Sony PlayStation 4 evidence")


@pytest.mark.parametrize("release", [
    unmarked("The Last of Us Remastered [CUSA00552] + Update 1.11 + 3 DLC"),   # title ID
    unmarked("The Last of Us Remastered + Update 1.11 + 3 DLC", (1180,)),      # Console/PS4
    Release("The Last of Us Remastered + Update 1.11", 0, 0, (1000,), "hub://x", "hub"),
])
def test_ps4_evidence_besides_the_name(release):
    assert judge(release, GAME, PS4).accepted


# -- import layout ----------------------------------------------------------

def download_with(tmp_path, files):
    root = tmp_path / "downloads" / "The.Last.of.Us.Remastered.PS4-DUPLEX"
    for name, data in files.items():
        make(root, name, data)
    return root


FILES = {"UP9000-CUSA00552.pkg": BASE_PKG + b"base",
         "UP9000-CUSA00552-patch.pkg": PATCH_PKG + b"patch",
         "Left Behind.pkg": DLC_PKG + b"dlc"}


def destinations(results):
    assert all(r.ok for r in results), [r.reason for r in results]
    return sorted(str(r.destination) for r in results)


def test_import_files_base_update_and_dlc_in_one_game_folder(tmp_path):
    download = download_with(tmp_path, FILES)
    library = tmp_path / "library"
    results = import_rom(download, PS4, library, package_folders=True, game_title="The Last of Us Remastered")
    game = library / "ps4" / "The Last of Us Remastered"
    assert destinations(results) == sorted([str(game / "UP9000-CUSA00552.pkg"),
                                            str(game / "update" / "UP9000-CUSA00552-patch.pkg"),
                                            str(game / "dlc" / "Left Behind.pkg")])
    assert (game / "update" / "UP9000-CUSA00552-patch.pkg").read_bytes() == PATCH_PKG + b"patch"


def test_import_keeps_the_flat_layout_when_switched_off(tmp_path):
    download = download_with(tmp_path, FILES)
    library = tmp_path / "library"
    results = import_rom(download, PS4, library, package_folders=False, game_title="The Last of Us Remastered")
    assert destinations(results) == sorted(str(library / "ps4" / name) for name in FILES)


def test_import_names_the_folder_after_the_download_without_a_request(tmp_path):
    download = download_with(tmp_path, {"UP9000-CUSA00552.pkg": BASE_PKG})
    library = tmp_path / "library"
    result, = import_rom(download, PS4, library, package_folders=True)
    assert result.destination == library / "ps4" / download.name / "UP9000-CUSA00552.pkg"


def test_import_attaches_a_later_update_to_the_existing_game_folder(tmp_path):
    library = tmp_path / "library"
    make(library / "ps4", "TLOU/UP9000-CUSA00552.pkg", BASE_PKG)
    download = make(tmp_path / "downloads", "patch.pkg", PATCH_PKG)
    result, = import_rom(download, PS4, library, package_folders=True, game_title="The Last of Us Remastered")
    assert result.ok and result.destination == library / "ps4" / "TLOU" / "update" / "patch.pkg"


def test_import_keeps_two_games_flat(tmp_path):
    download = download_with(tmp_path, {"a.pkg": BASE_PKG,
                                        "b.pkg": header(content_id="UP9000-CUSA99999_00-OTHERGAME0000000")})
    library = tmp_path / "library"
    results = import_rom(download, PS4, library, package_folders=True, game_title="Bundle")
    assert destinations(results) == sorted(str(library / "ps4" / n) for n in ("a.pkg", "b.pkg"))


def test_reviewed_roles_decide_the_folders(tmp_path):
    download = download_with(tmp_path, {"one.pkg": b"no header", "two.pkg": b"no header either"})
    library = tmp_path / "library"
    results = import_rom(download, PS4, library, package_folders=True, game_title="Game",
                         package_roles={"one.pkg": "base", "two.pkg": "dlc"})
    game = library / "ps4" / "Game"
    assert destinations(results) == sorted([str(game / "one.pkg"), str(game / "dlc" / "two.pkg")])


def test_an_additional_game_from_review_is_never_filed_as_an_update(tmp_path):
    download = download_with(tmp_path, {"one.pkg": BASE_PKG, "Bonus.Update.pkg": b"no header"})
    library = tmp_path / "library"
    results = import_rom(download, PS4, library, package_folders=True, game_title="Game",
                         package_roles={"one.pkg": "base", "Bonus.Update.pkg": "additional-game"})
    assert destinations(results) == sorted(str(library / "ps4" / n) for n in ("one.pkg", "Bonus.Update.pkg"))


def test_a_reviewed_preview_shows_the_destinations_the_importer_uses(tmp_path):
    download = download_with(tmp_path, {"one.pkg": b"no header", "two.pkg": b"no header either"})
    library = tmp_path / "library"
    roles = {"one.pkg": "base", "two.pkg": "dlc"}
    preview = preview_import(download, PS4, library, package_folders=True, game_title="Game",
                             package_roles=roles)
    assert preview["package_routing"] is True
    shown = sorted(i["destination"] for i in preview["items"])
    results = import_rom(download, PS4, library, package_folders=True, game_title="Game", package_roles=roles)
    assert shown == destinations(results)


def test_conflicting_title_ids_go_to_review(tmp_path):
    download = download_with(tmp_path, {"base.pkg": BASE_PKG,
                                        "other.pkg": header(flags=0x40000000,
                                                            content_id="UP9000-CUSA99999_00-OTHERGAME0000000")})
    preview = preview_import(download, PS4, tmp_path / "library", package_folders=True, game_title="Game")
    assert preview["requires_review"] is True


@needs_symlinks
def test_import_never_writes_through_a_linked_game_folder(tmp_path):
    library = tmp_path / "library"
    (tmp_path / "elsewhere").mkdir()
    (library / "ps4").mkdir(parents=True)
    (library / "ps4" / "Game").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    download = download_with(tmp_path, {"base.pkg": BASE_PKG})
    result, = import_rom(download, PS4, library, package_folders=True, game_title="Game")
    assert result.ok and result.destination == library / "ps4" / "base.pkg"
    assert not any((tmp_path / "elsewhere").iterdir())


def test_preview_reads_roles_from_headers_and_needs_no_review(tmp_path):
    download = download_with(tmp_path, FILES)
    preview = preview_import(download, PS4, tmp_path / "library", package_folders=True,
                             game_title="The Last of Us Remastered")
    roles = {i["primary"]: (i["role"], i["confidence"]) for i in preview["items"]}
    assert roles == {"UP9000-CUSA00552.pkg": ("base", "header"),
                     "UP9000-CUSA00552-patch.pkg": ("update", "header"),
                     "Left Behind.pkg": ("dlc", "header")}
    assert preview["requires_review"] is False
    patch = next(i for i in preview["items"] if i["role"] == "update")
    assert Path(patch["destination"]).parts[-3:] == ("The Last of Us Remastered", "update", "UP9000-CUSA00552-patch.pkg")


def test_preview_without_headers_still_asks_for_review(tmp_path):
    download = download_with(tmp_path, {"Game.pkg": b"x", "Game Update.pkg": b"y"})
    preview = preview_import(download, PS4, tmp_path / "library", package_folders=True, game_title="Game")
    assert preview["requires_review"] is True
    assert {i["role"] for i in preview["items"]} == {"unknown"}


# -- requests ---------------------------------------------------------------

def test_wanted_parts_round_trip(tmp_path):
    store = Store(tmp_path / "s.json")
    store.want("Bloodborne", "ps4", parts=("base", "update", "dlc"))
    store.want("Bloodborne", "ps4")                       # a retry keeps them
    assert Store(tmp_path / "s.json").wanted[0].parts == ["base", "update", "dlc"]
    store.want("Bloodborne", "ps4", parts=DEFAULT_PARTS)  # the game alone is stored empty
    assert Store(tmp_path / "s.json").wanted[0].parts == []


def settle(tmp_path, store, item, files):
    """Run the import's Wanted settlement for library files with these headers."""
    outcomes = [ImportResult(True, make(tmp_path / "library", relative, data)) for relative, data in files]
    fake = SimpleNamespace(store=store)
    pending = ROMarr._pending_parts(fake, item, PS4)
    for w in list(store.wanted):            # what the title-based fulfilment loop does
        store.fulfil(w.game, w.platform)
    ROMarr._settle_parts(fake, item, PS4, outcomes, pending)
    return [(w.game, w.parts) for w in store.wanted]


def test_an_import_leaves_the_missing_parts_on_wanted(tmp_path):
    store = Store(tmp_path / "s.json")
    item = QueueItem("Bloodborne", "ps4", "Bloodborne PS4", 5, "grabbed", parts=["base", "update", "dlc"])
    assert settle(tmp_path, store, item, [("ps4/Bloodborne/base.pkg", BASE_PKG)]) == [
        ("Bloodborne", ["update", "dlc"])]
    assert "still wanted: update + DLC" in store.events[-1].detail


def test_arrived_parts_come_from_the_files_not_the_title(tmp_path):
    store = Store(tmp_path / "s.json")
    item = QueueItem("Bloodborne", "ps4", "Bloodborne + Update 1.09 + DLC", 5, "grabbed",
                     parts=["base", "update", "dlc"])
    assert settle(tmp_path, store, item, [("ps4/Bloodborne/base.pkg", BASE_PKG)]) == [
        ("Bloodborne", ["update", "dlc"])]


def test_a_complete_import_leaves_nothing_on_wanted(tmp_path):
    store = Store(tmp_path / "s.json")
    item = QueueItem("Bloodborne", "ps4", "Bloodborne + Update 1.09 + DLC", 5, "grabbed",
                     parts=["base", "update", "dlc"])
    files = [("ps4/Bloodborne/base.pkg", BASE_PKG), ("ps4/Bloodborne/update/p.pkg", b"no header"),
             ("ps4/Bloodborne/dlc/d.pkg", b"no header")]
    assert settle(tmp_path, store, item, files) == []


def test_parts_requested_during_the_download_are_kept(tmp_path):
    store = Store(tmp_path / "s.json")
    store.want("Bloodborne", "ps4", parts=("base", "dlc"))       # asked while downloading
    item = QueueItem("Bloodborne", "ps4", "Bloodborne PS4", 5, "grabbed", parts=["base", "update"])
    assert settle(tmp_path, store, item, [("ps4/Bloodborne/base.pkg", BASE_PKG)]) == [
        ("Bloodborne", ["update", "dlc"])]


def test_an_update_request_is_cleared_even_when_titles_differ(tmp_path):
    store = Store(tmp_path / "s.json")
    store.want("Bloodborne", "ps4", parts=("update",))
    item = QueueItem("Bloodborne", "ps4", "Bloodborne.Update.v1.09.PS4-DUPLEX", 5, "grabbed", parts=["update"])
    fake = SimpleNamespace(store=store)
    pending = ROMarr._pending_parts(fake, item, PS4)
    # No title match: the fulfilment loop leaves the entry alone.
    patch = make(tmp_path / "library", "ps4/Bloodborne/update/CUSA03173-patch.pkg", PATCH_PKG)
    ROMarr._settle_parts(fake, item, PS4, [ImportResult(True, patch)], pending)
    assert store.wanted == []


def test_a_game_folder_named_like_a_marker_is_still_the_base(tmp_path):
    path = make(tmp_path, "ps4/Patch Quest/Patch Quest.pkg", b"no header")
    assert imported_role(path, "Patch Quest") == "base"
    assert imported_role(make(tmp_path, "ps4/Patch Quest/dlc/x.pkg", b"no header")) == "dlc"


@pytest.fixture
def server(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body):
    req = urllib.request.Request(base + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"X-Api-Key": "k", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_wanted_add_stores_the_requested_parts(server):
    base, service = server
    code, out = call(base, "/api/v1/wanted/add",
                     {"game": "Bloodborne", "platform": "ps4", "monitored": False, "parts": ["update", "dlc"]})
    assert code == 200 and out["ok"]
    assert service.store.wanted[0].parts == ["update", "dlc"]


def test_parts_are_validated(server):
    base, _ = server
    assert call(base, "/api/request", {"game": "Bloodborne", "platform": "ps4", "parts": "dlc"})[0] == 400
    code, out = call(base, "/api/request", {"game": "Bloodborne", "platform": "ps4", "parts": ["patches"]})
    assert code == 200 and out["ok"] is False and "unknown part" in out["error"]


def test_a_request_scores_and_grabs_with_its_parts(server, monkeypatch):
    _, service = server
    monkeypatch.setattr(service, "_search_releases", lambda game, platform: list(FROZEN))
    grabbed = {}

    def fake_grab(pick, game, platform_slug, *, external_request_id="", parts=None, **kw):
        grabbed.update(title=pick.title, parts=parts)
        return {"ok": True, "release": pick.title, "seeders": pick.seeders}

    monkeypatch.setattr(service, "grab", fake_grab)
    assert service.request(GAME, "ps4", parts=["update"])["ok"]
    assert grabbed == {"title": FROZEN[1].title, "parts": ("update",)}


def test_interactive_search_shows_the_package(server, monkeypatch):
    _, service = server
    monkeypatch.setattr(service, "_search_releases", lambda game, platform: list(FROZEN))
    items = {i["title"]: i for i in service.candidates(GAME, "ps4")["items"]}
    assert items[FROZEN[2].title]["package"] == "base game + update + DLC"
    assert items[FROZEN[1].title]["package"] == "update"
    assert items[FROZEN[1].title]["accepted"] is False


class FakeClient:
    """A torrent client that accepts every release."""

    name = "fake"
    protocol = "torrent"
    configured = True

    def __init__(self):
        self.added = []

    def add(self, url, **kw):
        self.added.append(url)
        return True


def test_the_queue_row_remembers_the_requested_parts(tmp_path, monkeypatch):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    monkeypatch.setattr(service, "_search_releases", lambda game, platform: list(FROZEN))
    service.clients = [FakeClient()]
    result = service.request(GAME, "ps4", parts=["base", "update", "dlc"])
    assert result["ok"] and result["release"] == FROZEN[2].title
    assert service.store.queue[-1].parts == ["base", "update", "dlc"]
    other = service.request("Super Metroid", "ps4")
    assert other["ok"] is False and service.store.queue[-1].parts == []


@pytest.mark.parametrize("kind, expected", [("romm", True), ("retrobat", False), ("folder", False),
                                            ("gaseous", False), ("", False)])
def test_game_folders_are_for_romm_libraries(tmp_path, kind, expected):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    assert service._package_folders({"type": kind}) is expected
    service.store.settings["game_package_folders"] = False
    assert service._package_folders({"type": kind}) is False


def test_a_lone_package_names_its_folder_without_the_extension(tmp_path):
    download = make(tmp_path / "downloads", "Bayonetta 2 [01007960049A0000][v0].nsp", b"base")
    result, = import_rom(download, resolve("switch"), tmp_path / "library", package_folders=True)
    assert result.ok and result.destination.parent.name == "Bayonetta 2 [01007960049A0000][v0]"


# -- CodeRabbit round 2 ------------------------------------------------------

def test_a_game_request_that_only_got_an_update_keeps_wanting_the_game(tmp_path):
    store = Store(tmp_path / "s.json")
    store.want("Bloodborne", "ps4")
    item = QueueItem("Bloodborne", "ps4", "Bloodborne PS4", 5, "grabbed")   # parts [] = the game
    assert settle(tmp_path, store, item, [("ps4/Bloodborne/update/p.pkg", PATCH_PKG)]) == [("Bloodborne", [])]


def test_a_manual_grab_without_parts_asks_for_nothing_more(tmp_path):
    store = Store(tmp_path / "s.json")
    item = QueueItem("Bloodborne", "ps4", "Bloodborne Update", 5, "grabbed", manual=True)
    assert settle(tmp_path, store, item, [("ps4/Bloodborne/update/p.pkg", PATCH_PKG)]) == []


def test_restored_parts_keep_the_entry_unmonitored(tmp_path):
    store = Store(tmp_path / "s.json")
    store.want("Bloodborne", "ps4", monitored=False, parts=("base", "dlc"))
    item = QueueItem("Bloodborne", "ps4", "Bloodborne PS4", 5, "grabbed", parts=["base", "dlc"])
    assert settle(tmp_path, store, item, [("ps4/Bloodborne/base.pkg", BASE_PKG)]) == [("Bloodborne", ["dlc"])]
    assert store.wanted[0].monitored is False


def test_preview_routes_only_the_reviewed_selection(tmp_path):
    other = header(content_id="UP9000-CUSA99999_00-OTHERGAME0000000")
    download = download_with(tmp_path, {"a.pkg": BASE_PKG, "a-patch.pkg": PATCH_PKG, "b.pkg": other})
    library = tmp_path / "library"
    whole = preview_import(download, PS4, library, package_folders=True, game_title="Game")
    assert {Path(i["destination"]).parent.name for i in whole["items"]} == {"ps4"}   # two games: flat
    chosen = ["a.pkg", "a-patch.pkg"]
    reviewed = preview_import(download, PS4, library, package_folders=True, game_title="Game",
                              package_roles={"a.pkg": "base", "a-patch.pkg": "update"},
                              selected_primaries=chosen)
    shown = sorted(i["destination"] for i in reviewed["items"] if i["primary"] in chosen)
    results = import_rom(download, PS4, library, package_folders=True, game_title="Game",
                         package_roles={"a.pkg": "base", "a-patch.pkg": "update"},
                         selected_primaries=chosen)
    assert shown == destinations(results)


def test_a_reviewed_import_keeps_the_confirmed_filename(tmp_path, monkeypatch):
    import romarr.library as library_module
    monkeypatch.setattr(library_module, "_import_name", lambda primary, verdict, rename: "Canonical.pkg")
    download = download_with(tmp_path, {"source.pkg": BASE_PKG})
    library = tmp_path / "library"
    result, = import_rom(download, PS4, library, package_folders=True, game_title="Game",
                         package_roles={"source.pkg": "base"}, rename=True)
    assert result.ok and result.destination == library / "ps4" / "Game" / "source.pkg"
    unreviewed, = import_rom(download_with(tmp_path / "again", {"other.pkg": BASE_PKG}), PS4, library,
                             package_folders=True, game_title="Other", rename=True)
    assert unreviewed.ok and unreviewed.destination.name == "Canonical.pkg"


def test_an_occupied_title_folder_holding_another_game_is_not_reused(tmp_path):
    make(tmp_path, "Game/base.pkg", header(content_id="UP9000-CUSA00552_00-THELASTOFUS00000"))
    assert plan_ps4([("u.pkg", "update", "CUSA99999")], tmp_path, "Game") is None
    assert plan_ps4([("u.pkg", "update", "CUSA00552")], tmp_path, "Game") == {"u.pkg": "Game/update"}


def test_a_second_game_joined_by_plus_is_a_compilation():
    verdict = judge(rel("Bloodborne + Update 1.09 + Dark Souls III PS4"), "Bloodborne", PS4)
    assert verdict.verdict == "title does not match 'Bloodborne'"
    assert judge(rel("Bloodborne + Update 1.09 + DLC PS4"), "Bloodborne", PS4).accepted


def test_a_title_id_says_what_a_release_is():
    threeds = resolve("3ds")
    update = rel("Fire Emblem Fates [0004000E001B4000] 3DS", GB)
    assert package_class(update, "Fire Emblem Fates", threeds) == "update"
    assert not judge(update, "Fire Emblem Fates", threeds).accepted
    assert judge(update, "Fire Emblem Fates", threeds, parts=["update"]).accepted


@pytest.mark.parametrize("slug, accepted", [("ps4", True), ("ps3", False), ("ps5", False)])
def test_a_playstation_serial_is_evidence_for_its_own_console_only(slug, accepted):
    release = Release("The Last of Us Remastered [CUSA00552]", 0, 0, (1000,), "https://nzb.example/get/1", "usenet")
    verdict = judge(release, GAME, resolve(slug))
    assert verdict.accepted is accepted, verdict.why()
