"""Local launcher scanning — the credential-free half of store libraries.

This module exists because the README claimed EA, Battle.net and Epic
"have no API" and therefore could not be connected. Playnite and LaunchBox
have done it for years by reading what the launcher already wrote to disk,
which is what these tests pin down.
"""

import json
import sqlite3

from romarr.launchers import (BATTLENET_PRODUCTS, LocalGame, battlenet_games,
                              ea_games, epic_games, gog_galaxy_games,
                              scan_all, steam_games, steam_library_paths)


# -- Steam -------------------------------------------------------------------

def _acf(directory, appid, name):
    (directory / f"appmanifest_{appid}.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"%s"\n\t"name"\t\t"%s"\n}\n' % (appid, name),
        encoding="utf-8")


def test_steam_reads_installed_titles(tmp_path):
    _acf(tmp_path, "1", "Chrono Trigger")
    _acf(tmp_path, "2", "Half-Life 2")
    assert sorted(g.name for g in steam_games(tmp_path)) == \
        ["Chrono Trigger", "Half-Life 2"]
    assert all(g.launcher == "steam" for g in steam_games(tmp_path))


def test_steam_drops_valves_own_scaffolding(tmp_path):
    _acf(tmp_path, "1", "Chrono Trigger")
    _acf(tmp_path, "228980", "Steamworks Common Redistributables")
    _acf(tmp_path, "1493710", "Proton Experimental")
    assert [g.name for g in steam_games(tmp_path)] == ["Chrono Trigger"]


def test_steam_follows_its_own_library_index(tmp_path):
    """People put libraries on second drives with unguessable names, so the
    index is the only reliable source -- and an unplugged drive must simply
    contribute nothing."""
    main = tmp_path / "Steam" / "steamapps"
    other = tmp_path / "D_Drive" / "SteamLibrary" / "steamapps"
    main.mkdir(parents=True)
    other.mkdir(parents=True)
    (main / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        '\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
        '\t"2"\n\t{\n\t\t"path"\t\t"Z:\\\\GoneLibrary"\n\t}\n}\n'
        % (str(main.parent).replace("\\", "\\\\"),
           str(other.parent).replace("\\", "\\\\")),
        encoding="utf-8")
    paths = steam_library_paths(main)
    assert main in paths and other in paths
    assert not any("GoneLibrary" in str(p) for p in paths)


def test_steam_with_no_index_still_reads_the_local_library(tmp_path):
    _acf(tmp_path, "1", "Chrono Trigger")
    assert steam_library_paths(tmp_path) == [tmp_path]


# -- Epic --------------------------------------------------------------------

def test_epic_reads_manifests_including_the_bom(tmp_path):
    """Epic writes a BOM; a plain utf-8 read fails on the first character of
    every file, which is how a scanner silently finds nothing."""
    (tmp_path / "a.item").write_text(
        json.dumps({"DisplayName": "Sid Meier's Civilization VI",
                    "InstallLocation": "C:/Games/Civ6"}),
        encoding="utf-8-sig")
    games = epic_games(tmp_path)
    assert games == [LocalGame(name="Sid Meier's Civilization VI",
                               launcher="epic", path="C:/Games/Civ6")]


def test_epic_ignores_unreadable_manifests(tmp_path):
    (tmp_path / "broken.item").write_text("{not json", encoding="utf-8")
    (tmp_path / "good.item").write_text(json.dumps({"DisplayName": "Fortnite"}),
                                        encoding="utf-8-sig")
    assert [g.name for g in epic_games(tmp_path)] == ["Fortnite"]


# -- GOG Galaxy --------------------------------------------------------------

def test_gog_galaxy_reads_its_sqlite_database(tmp_path):
    db = tmp_path / "galaxy-2.0.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE GamePieceTypes (id INTEGER, type TEXT)")
    connection.execute("CREATE TABLE GamePieces "
                       "(gamePieceTypeId INTEGER, value TEXT)")
    connection.execute("INSERT INTO GamePieceTypes VALUES (1, 'originalTitle')")
    connection.execute("INSERT INTO GamePieces VALUES "
                       "(1, '{\"title\": \"Heroes of Might and Magic 3\"}')")
    connection.commit()
    connection.close()
    assert [g.name for g in gog_galaxy_games(db)] == \
        ["Heroes of Might and Magic 3"]


def test_gog_galaxy_absent_is_empty_not_an_error(tmp_path):
    assert gog_galaxy_games(tmp_path / "nope.db") == []


def test_gog_galaxy_survives_a_schema_it_does_not_know(tmp_path):
    db = tmp_path / "galaxy-2.0.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE Unrelated (x INTEGER)")
    connection.commit()
    connection.close()
    assert gog_galaxy_games(db) == []


# -- Battle.net --------------------------------------------------------------

def test_battlenet_recovers_product_codes(tmp_path):
    """product.db is protobuf; the product codes inside it are plain ASCII,
    and the catalogue is small enough to name."""
    db = tmp_path / "product.db"
    db.write_bytes(b"\x0a\x03d2r\x12\x08\x03wow\x00\xffosi\x00")
    names = {g.name for g in battlenet_games(db)}
    assert "Diablo II: Resurrected" in names
    assert "World of Warcraft" in names
    assert "Diablo Immortal" in names


def test_battlenet_missing_file_is_empty(tmp_path):
    assert battlenet_games(tmp_path / "nope.db") == []


def test_the_blizzard_table_covers_the_current_catalogue():
    for code in ("d2r", "fenris", "wow", "s2", "prometheus", "hero"):
        assert code in BATTLENET_PRODUCTS


# -- EA ----------------------------------------------------------------------

def test_ea_reads_the_installerdata_it_writes(tmp_path):
    """The file that proves 'EA has no API' was the wrong question: nothing
    needs to be asked of EA at all."""
    game = tmp_path / "Dead Space" / "__Installer"
    game.mkdir(parents=True)
    (game / "installerdata.xml").write_text(
        '<?xml version="1.0"?><DiPManifest><gameTitle locale="en_US">'
        'Dead Space</gameTitle></DiPManifest>', encoding="utf-8")
    assert [g.name for g in ea_games([tmp_path])] == ["Dead Space"]


def test_ea_falls_back_to_the_directory_name(tmp_path):
    game = tmp_path / "Mass Effect" / "__Installer"
    game.mkdir(parents=True)
    (game / "installerdata.xml").write_text("<DiPManifest/>", encoding="utf-8")
    assert [g.name for g in ea_games([tmp_path])] == ["Mass Effect"]


def test_ea_empty_folder_is_empty_not_an_error(tmp_path):
    assert ea_games([tmp_path, tmp_path / "does-not-exist"]) == []


# -- everything at once ------------------------------------------------------

def test_scan_all_degrades_per_launcher(tmp_path):
    """A machine with no Epic contributes no Epic games rather than failing
    the whole scan."""
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    _acf(steamapps, "1", "Chrono Trigger")
    games = scan_all({
        "steam": [steamapps],
        "epic": [tmp_path / "no-epic-here"],
        "gog": [tmp_path / "no-gog.db"],
        "battlenet": [tmp_path / "no-bnet.db"],
        "ea": [tmp_path / "no-ea"],
    })
    assert [g.name for g in games] == ["Chrono Trigger"]


def test_scan_all_deduplicates_across_libraries(tmp_path):
    """The same game in two Steam libraries is one game."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _acf(one, "1", "Chrono Trigger")
    _acf(two, "1", "Chrono Trigger")
    games = scan_all({"steam": [one, two], "epic": [], "gog": [],
                      "battlenet": [], "ea": []})
    assert len(games) == 1


# -- Amazon Games -------------------------------------------------------------

def test_amazon_reads_the_launchers_sqlite(tmp_path):
    import sqlite3
    from romarr.launchers import amazon_games
    db = tmp_path / "GameInstallInfo.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE DbSet (Id TEXT, ProductTitle TEXT, "
                 "InstallDirectory TEXT)")
    conn.execute("INSERT INTO DbSet VALUES ('1', 'Fallout 76', 'C:/x')")
    conn.execute("INSERT INTO DbSet VALUES ('2', 'Blade Runner', 'C:/y')")
    conn.commit()
    conn.close()
    assert sorted(g.name for g in amazon_games(db)) == \
        ["Blade Runner", "Fallout 76"]
    assert all(g.launcher == "amazon" for g in amazon_games(db))


def test_amazon_survives_a_schema_it_does_not_know(tmp_path):
    import sqlite3
    from romarr.launchers import amazon_games
    db = tmp_path / "GameInstallInfo.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE Other (x INTEGER)")
    conn.commit()
    conn.close()
    assert amazon_games(db) == []
    assert amazon_games(tmp_path / "absent.sqlite") == []


# -- Ubisoft ------------------------------------------------------------------

def test_ubisoft_names_games_from_their_install_dirs():
    from romarr.launchers import ubisoft_games
    games = ubisoft_games([r"C:\Games\Assassin's Creed Origins",
                           "D:/Ubisoft/Rayman Legends/"])
    assert [g.name for g in games] == ["Assassin's Creed Origins",
                                      "Rayman Legends"]
    assert all(g.launcher == "ubisoft" for g in games)


def test_scan_all_never_reads_the_registry_behind_a_tests_back(tmp_path):
    """An explicit location dict without a ubisoft key must not fall
    through to the real machine's registry."""
    games = scan_all({"steam": [], "epic": [], "gog": [],
                      "battlenet": [], "ea": [], "amazon": []})
    assert games == []
