"""The Collections source picker offers game torrents, not films or TV."""
from __future__ import annotations

import pytest

from romarr.app import ROMarr
from romarr.clients import QBittorrent, QbitConfig
from romarr.source_filter import looks_like_game_torrent, torrent_labels


@pytest.mark.parametrize("name, labels", [
    ("The.Show.S02E05.1080p.WEB-DL.DDP5.1.H.264-GRP", ["tv-sonarr"]),
    ("The.Show.S03.COMPLETE.720p.HDTV.x264", []),
    ("Some.Movie.2019.2160p.UHD.BluRay.REMUX.HDR10.HEVC.TrueHD.Atmos", []),
    ("Some Movie (2021) [1080p] [WEBRip] [5.1]", ["radarr"]),
    ("Genesis.2018.1080p.BluRay.x264", []),
    ("Saturn Returns Season 1", []),
    ("Artist - Album (2020) [FLAC]", ["music"]),
    ("Holiday Photos", []),
    ("ubuntu-24.04-desktop-amd64.iso", []),
    ("Nintendo - Super Nintendo Entertainment System", ["movies"]),
])
def test_films_tv_music_and_unknowns_are_not_game_sources(name, labels):
    assert not looks_like_game_torrent(name, labels)


@pytest.mark.parametrize("name, labels", [
    ("Nintendo - Super Nintendo Entertainment System (20240101-000000)", []),
    ("No-Intro Romset 2024", []),
    ("Redump Sony PlayStation 2 (USA)", []),
    ("TOSEC Commodore Amiga Games", []),
    ("MAME 0.262 ROMs (merged)", []),
    ("Sega Saturn CHD collection", []),
    ("gba_1g1r_set", []),
    ("Unnamed pack", ["games"]),
    ("Unnamed pack", ["Retro-ROMs"]),
])
def test_game_sets_are_offered(name, labels):
    assert looks_like_game_torrent(name, labels)


def test_romarr_category_counts_even_with_a_video_tag_in_the_name():
    assert looks_like_game_torrent("Odd.Name.1080p", ["romarr-sets"],
                                   own_categories=["romarr-sets"])


def test_generic_words_alone_are_not_a_platform():
    for name in ("Switch", "Windows Wallpapers", "Arcade Fire Live",
                 "PC Magazine 2020", "Spectrum Documentary"):
        assert not looks_like_game_torrent(name), name


def test_labels_come_from_category_tags_and_transmission_labels():
    assert torrent_labels({"category": "tv", "tags": "a, b",
                           "labels": ["games"]}) == ["tv", "a", "b", "games"]
    assert torrent_labels({}) == []


class _ListingQbit(QBittorrent):
    def __init__(self, rows):
        super().__init__(QbitConfig(base_url="http://qbit"))
        self.romarr_id = "q1"
        self.romarr_name = "qBittorrent"
        self.rows = rows

    def torrents(self):
        return self.rows


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMARR_DATA", str(tmp_path / "state.json"))
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                       "LIBRARY_PATH": str(tmp_path / "library")})


def _row(hash_char, name, category=""):
    return {"hash": hash_char * 40, "name": name, "size": 1,
            "state": "uploading", "category": category}


def test_source_list_hides_media_and_counts_it(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.clients = [_ListingQbit([
        _row("a", "Nintendo - Game Boy (20240101)"),
        _row("b", "The.Show.S01E01.1080p.WEB-DL", "tv-sonarr"),
        _row("c", "Some.Movie.2020.2160p.BluRay", "radarr"),
        _row("d", "Anything at all", "romarr"),
    ])]
    state = svc.collection_source_torrents()
    assert [t["hash"][0] for t in state["torrents"]] == ["a", "d"]
    assert state["hidden"] == 2
    everything = svc.collection_source_torrents(include_all=True)
    assert len(everything["torrents"]) == 4
    assert everything["hidden"] == 0


def test_torrent_with_a_job_stays_listed_whatever_its_name(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.clients = [_ListingQbit([_row("e", "weird.name.720p")])]
    svc.store.put_item("collection_sources", {
        "id": "job1", "client_id": "q1", "torrent_hash": "E" * 40,
        "status": "running"})
    state = svc.collection_source_torrents()
    assert [t["hash"] for t in state["torrents"]] == ["e" * 40]
    assert state["hidden"] == 0
