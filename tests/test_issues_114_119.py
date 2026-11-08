"""Tests for issues #114, #115, #116, #117, #118, and #119."""

from pathlib import Path
import pytest

from romarr.selection import (
    Release, judge, score, explain, detect_regions, is_foreign_language_tag,
    PC_RELEASE_MARKERS
)
from romarr.profiles import ReleaseProfile
from romarr.platforms import resolve, Platform, same_machine
from romarr.downloaders import match_queue_item
from romarr.store import QueueItem, Store
from romarr.library import _source_for, import_rom


# ==============================================================================
# Issue #114: MiNERVA selective download on qBittorrent 4.5.2
# ==============================================================================

def test_issue_114_add_parameters_and_path_matching():
    from romarr.clients import QBittorrent, QbitConfig
    from tests.test_minerva_selective_download import Session

    files = [
        {"index": 0, "name": "Minerva_Collection_123/TOSEC/Nintendo/other.zip", "priority": 1},
        {"index": 1, "name": "Minerva_Collection_123/TOSEC/Nintendo/Chrono Trigger.zip", "priority": 1},
    ]
    session = Session(files, state="metaDL")
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080", timeout=2), session=session)

    assert client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc123",
        "./TOSEC/Nintendo/Chrono Trigger.zip"
    )
    add_post = session.posts[0]
    assert add_post[0] == "torrents/add"
    assert add_post[1]["paused"] == "false"
    assert add_post[1]["stopCondition"] == "MetadataReceived"
    assert add_post[1]["contentLayout"] == "NoSubfolder"
    assert session.resumed is True


# ==============================================================================
# Issue #115: Collection (MiNERVA) grabs
# ==============================================================================

def test_issue_115_import_unwraps_directory_with_single_archive(tmp_path):
    download_dir = tmp_path / "minerva_download"
    download_dir.mkdir()
    # Hidden file like .parts
    (download_dir / ".parts").write_text("hidden")
    # Single selected archive
    zip_path = download_dir / "game.zip"
    import zipfile
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Super Mario Land (World).gb", b"dummy rom content")

    snes = resolve("gb")
    source = _source_for(download_dir, snes)
    assert "Super Mario Land (World).gb" in source.names()


def test_issue_115_tag_matches_before_shared_infohash():
    # Two grabs from the same collection torrent sharing the same infohash
    item1 = QueueItem("Game 1", "gb", "Release 1", 10, "imported",
                      release_id="btih:commonhash123", download_id="qbit-tag:tag-item-1")
    item2 = QueueItem("Game 2", "gb", "Release 2", 10, "grabbed",
                      release_id="btih:commonhash123", download_id="qbit-tag:tag-item-2")
    queue = [item1, item2]

    # When item2 finishes, qBittorrent reports tags including tag-item-2
    download_report = {
        "hash": "commonhash123",
        "name": "Collection Torrent",
        "tags": "romarr, tag-item-2",
    }
    matched = match_queue_item(queue, download_report)
    assert matched is item2


@pytest.mark.parametrize("retain", [True, False])
@pytest.mark.parametrize("selective", [True, False])
def test_issue_115_history_and_collection_cleanup(tmp_path, retain, selective):
    from romarr.app import ROMarr

    # Create download folder with a valid ROM
    dl_dir = tmp_path / "download"
    dl_dir.mkdir()
    rom_file = dl_dir / "Tetris (World).gb"
    rom_file.write_bytes(b"\x00" * 32768)

    class FakeClient:
        configured = True
        name = "qBit"
        romarr_id = "qbit"
        removed = []

        def remove_download(self, download_id, *, delete_files=False):
            self.removed.append((download_id, delete_files))
            return True

        def torrent_files(self, torrent_hash):
            return [{"index": 0, "name": rom_file.name,
                     "size": rom_file.stat().st_size, "priority": 1, "progress": 1}]

        def completed(self):
            return [{
                "hash": "colhash",
                "name": "Minerva_Collection_Torrent",
                "tags": "qbit-tag:romarr-tetris",
                "content_path": str(rom_file),
                "save_path": str(dl_dir),
            }]

    # Create dummy library target
    lib_dir = tmp_path / "library"
    lib_dir.mkdir()

    svc = ROMarr(env={
        "ROMARR_DATA": str(tmp_path / "state.json"),
        "LIBRARY_PATH": str(lib_dir),
        "LIBRARY_KIND": "folder",
    })
    client = FakeClient()
    svc.clients = [client]

    svc.store.settings["retain_downloads_after_import"] = retain

    # Queue item with qbit-tag
    q_item = QueueItem("Tetris", "gb", "Tetris (World).gb", 10, "grabbed",
                       release_id="btih:colhash", download_id="qbit-tag:romarr-tetris",
                       download_client="qbit", file_selection_required=selective,
                       selected_file={"path": rom_file.name, "index": 0,
                                      "size": rom_file.stat().st_size, "hash": "colhash"} if selective else {})
    svc.store.enqueue(q_item)

    # Process import
    results = svc.import_finished()
    assert len(results) > 0
    assert results[0]["ok"] is True
    # Collection torrent was removed
    assert (("qbit-tag:romarr-tetris", True) in client.removed) is (not retain)
    # History Event recorded under game name rather than collection name
    imported_event = next(e for e in svc.store.events if e.kind == "imported")
    assert imported_event.game == "Tetris"


# ==============================================================================
# Issue #116: Scoring preferred_regions, strict mode, translations
# ==============================================================================

def test_issue_116_preferred_regions_rank_scoring():
    profile = ReleaseProfile.from_settings({"preferred_regions": [" France ", "Europe", "World", "USA", "Japan"]})
    # 5 regions: rank 0 (france) -> 40, rank 1 (europe) -> 32, rank 3 (usa) -> 16
    j_fr = judge(Release("Game (France) (Fr).sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                 "game", resolve("snes"), profile=profile)
    j_us = judge(Release("Game (USA).sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                 "game", resolve("snes"), profile=profile)
    j_eu = judge(Release("Game (Europe) (En,Fr,De).sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                 "game", resolve("snes"), profile=profile)
    assert j_fr.points > j_eu.points > j_us.points
    assert (32, "region europe") in j_eu.reasons
    assert not any(label == "region france" for _, label in j_eu.reasons)
    assert any("region france" in r and "40" in r for r in j_fr.why())
    assert any("region usa" in r and "16" in r for r in j_us.why())


def test_issue_116_tag_filtering_disc_a_not_australia():
    detected = detect_regions("Chrono Trigger (Disc A).sfc")
    assert "australia" not in detected
    assert len(detected) == 0


def test_issue_116_strict_region_mode():
    profile = ReleaseProfile.from_settings({
        "preferred_regions": ["Japan"],
        "platform_profiles": {"snes": {"preferred_regions": ["USA"], "region_mode": "strict"}},
    }, resolve("snes"))
    j_us = judge(Release("Game (USA).sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                 "game", resolve("snes"), profile=profile)
    assert j_us.accepted and (40, "region usa") in j_us.reasons
    # Outside preferred regions -> rejected
    j_jp = judge(Release("Game (Japan).sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                 "game", resolve("snes"), profile=profile)
    assert j_jp.points < 0
    assert "region outside allowed list" in j_jp.why()[0]

    # No detected region -> kept
    j_none = judge(Release("Game Super.sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                   "game", resolve("snes"), profile=profile)
    assert j_none.points > 0


def test_issue_116_tosec_translation_and_exclude_policy():
    profile = ReleaseProfile(translation_policy="exclude")
    # TOSEC translation format [tr ru ...]
    j_tr = judge(Release("Game (1993)(Publisher)(US)[tr ru Magic Team].sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
                 "game", resolve("snes"), profile=profile)
    assert j_tr.points < 0
    assert "translation excluded by policy" in j_tr.why()[0]


def test_issue_116_usa_pt_penalty():
    profile = ReleaseProfile(translation_policy="prefer")
    # (USA) (Pt) gets translation penalty of -150
    j = judge(Release("Chrono Trigger (USA) (Pt) (BR Group).sfc", 1024 * 1024, 10, (1000,), "", "torrent"),
              "Chrono Trigger", resolve("snes"), profile=profile)
    assert any("not English, or a fan translation" in r and "-150" in r for r in j.why())


@pytest.mark.parametrize("tag", ["En", "NP", "Ja,En"])
def test_issue_138_japan_english_and_nintendo_power_are_not_penalized(tag):
    profile = ReleaseProfile.from_settings({"preferred_regions": ["USA", "Japan"]})
    title = f"Game (Japan) ({tag}).sfc"
    assert detect_regions(title) == {"japan"}
    result = judge(Release(title, 1024 * 1024, 10, (1000,), "", "torrent"),
                   "game", resolve("snes"), profile=profile)
    assert (20, "region japan") in result.reasons
    assert not any(label == "not English, or a fan translation" for _, label in result.reasons)


def test_issue_138_language_tags_do_not_grant_region_points_or_bypass_strict_mode():
    assert detect_regions("Game (En,Fr).sfc") == set()
    assert detect_regions("Game (FR).sfc") == {"france"}
    assert detect_regions("Game (Japan, USA) (En,Ja).sfc") == {"japan", "usa"}
    profile = ReleaseProfile.from_settings({"preferred_regions": ["France", "Europe"]})
    us = Release("Game (USA).sfc", 1024 * 1024, 10, (1000,), "", "torrent")
    assert not any(label.startswith("region ") for _, label in judge(us, "game", resolve("snes"), profile=profile).reasons)
    japanese = Release("Game (Japan) (En).sfc", 1024 * 1024, 10, (1000,), "", "torrent")
    strict = ReleaseProfile.from_settings({"preferred_regions": ["USA"], "region_mode": "strict"})
    assert "region outside allowed list (japan)" == judge(japanese, "game", resolve("snes"), profile=strict).verdict


# ==============================================================================
# Issue #117: Disc platform collection path & PC builds
# ==============================================================================

def test_issue_117_collection_path_rejects_other_platform():
    ps2 = resolve("ps2")
    rel = Release("Game.zip", 50 * 1024 * 1024, 10, (1000,), "", "torrent",
                  torrent_file_path="./TOSEC/Sega/Mega Drive & Genesis/Games/[BIN]/game.zip")
    j = judge(rel, "game", ps2)
    assert j.points < 0
    assert "collection path names another platform" in j.why()[0]


def test_issue_117_collection_path_awards_bonus_for_requested_platform():
    ps2 = resolve("ps2")
    rel = Release("Game (USA).zip", 50 * 1024 * 1024, 10, (1000,), "", "torrent",
                  torrent_file_path="./Redump/Sony - PlayStation 2/game.zip")
    j = judge(rel, "game", ps2)
    assert any("collection path names Sony PlayStation 2" in r and "60" in r for r in j.why())


def test_issue_117_disc_no_evidence_penalty():
    ps2 = resolve("ps2")
    # Disc release with no extension, no platform name, no collection path
    rel = Release("Game (USA)-group.rar", 50 * 1024 * 1024, 10, (1000,), "", "torrent")
    j = judge(rel, "game", ps2)
    assert any("no extension, platform name or path" in r and "-40" in r for r in j.why())


def test_issue_117_pc_categories_and_pc_markers():
    ps2 = resolve("ps2")
    # PC categories (4000-4999) rejected for console platform
    rel_pc_cat = Release("Game (USA).iso", 50 * 1024 * 1024, 10, (4050,), "", "torrent")
    assert judge(rel_pc_cat, "game", ps2).points < 0

    # PC markers x64 / win64 rejected
    rel_pc_marker = Release("Game.V1.0.WIN.X64-GROUP.iso", 50 * 1024 * 1024, 10, (1000,), "", "torrent")
    assert judge(rel_pc_marker, "game", ps2).points < 0


def test_issue_117_size_floor_8mib_scoring_and_import(tmp_path):
    ps2 = resolve("ps2")
    # Scoring size floor
    rel_small = Release("Game (USA).iso", 2 * 1024 * 1024, 10, (1000,), "", "torrent")
    j = judge(rel_small, "game", ps2)
    assert any("too small to be a Sony PlayStation 2 disc image" in r for r in j.why())

    # Import size floor
    dl_file = tmp_path / "undersized.iso"
    dl_file.write_bytes(b"\x00" * (1 * 1024 * 1024)) # 1 MiB
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    res = import_rom(dl_file, ps2, lib_root)
    assert res[0].ok is False
    assert "too small to be a Sony PlayStation 2 disc image" in res[0].reason


# ==============================================================================
# Issue #118: ROM Hub search drops irrelevant MiNERVA rows
# ==============================================================================

def test_issue_118_hub_search_filters_collection_paths(monkeypatch):
    import sys
    import types
    import romarr.hub as hub

    class DummyResult:
        def __init__(self, title, path, plugin="minerva-archive"):
            self._d = {"title": title, "extra": {"path": path}, "plugin": plugin}
        def model_dump(self):
            return self._d

    class DummyOutcome:
        results = [
            DummyResult("Game 1 (BIN)", "./TOSEC/Sega/Mega Drive & Genesis/Games/[BIN]/game1.zip"),
            DummyResult("Game 2 (PS2 ISO)", "./Redump/Sony - PlayStation 2/game2.zip"),
            DummyResult("Game 3 (TI-82)", "./TOSEC/Texas Instruments/TI-82/game3.zip"),
        ]

    class FakePlugin:
        enabled = True
        manifest = type("M", (), {"capabilities": ("search",)})()
        slug = "minerva-archive"

    class FakeRegistry:
        def __init__(self, *a, **kw): pass
        def installed(self):
            return [FakePlugin()]

    fake_root = types.ModuleType("rom_hub")
    fake_registry = types.ModuleType("rom_hub.registry")
    fake_registry.Registry = FakeRegistry

    class FakeFetcher:
        def __init__(self, *a, **kw): pass
        def close(self): pass

    fake_broker = types.ModuleType("rom_hub.broker")
    fake_fetcher_mod = types.ModuleType("rom_hub.broker.fetcher")
    fake_fetcher_mod.HttpxFetcher = FakeFetcher

    fake_disp = types.ModuleType("rom_hub.dispatcher")
    fake_disp.search_all = lambda *a, **kw: DummyOutcome()

    fake_root.registry = fake_registry
    fake_root.broker = fake_broker
    fake_broker.fetcher = fake_fetcher_mod
    fake_root.dispatcher = fake_disp

    monkeypatch.setitem(sys.modules, "rom_hub", fake_root)
    monkeypatch.setitem(sys.modules, "rom_hub.registry", fake_registry)
    monkeypatch.setitem(sys.modules, "rom_hub.broker", fake_broker)
    monkeypatch.setitem(sys.modules, "rom_hub.broker.fetcher", fake_fetcher_mod)
    monkeypatch.setitem(sys.modules, "rom_hub.dispatcher", fake_disp)

    rows, err = hub.search("game", platform="ps2", limit=25)
    assert err == ""
    # Only Game 2 (PS2) survives; Mega Drive and TI-82 are filtered out!
    assert len(rows) == 1
    assert rows[0]["title"] == "Game 2 (PS2 ISO)"


# ==============================================================================
# Issue #119: Dead manual grab is not auto-replaced
# ==============================================================================

def test_issue_119_dead_manual_grab_not_replaced_automatically(tmp_path):
    from romarr.app import ROMarr

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json")})
    # Manual grab for a series query
    item = QueueItem("Final Fantasy Series", "ps2", "Final Fantasy X.iso", 10, "failed",
                     detail="stalled: no download client response",
                     release_id="btih:deadrel123", release_fault=True, manual=True)
    svc.store.enqueue(item)

    dead_report = svc.retire_dead_downloads()
    # It was blocklisted
    assert dead_report["blocklisted"] == 1
    # But NOT regrabbed
    assert dead_report["regrabbed"] == 0
    # And the raw search text was NOT placed on Wanted
    assert not any(w.game == "Final Fantasy Series" for w in svc.store.wanted)
