"""Guide, video and mod plugins never become game releases."""
from romarr import hub
from romarr.app import ROMarr


def test_non_game_plugin_rows_are_not_release_candidates(tmp_path, monkeypatch):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    rows = [
        {"title": "Tobu Tobu Girl Deluxe - Game Boy Homebrew Review!", "plugin": "youtube-tutorials",
         "source_id": "v1", "url": "https://youtube.com/watch?v=x"},
        {"title": "Super Metroid Walkthrough", "plugin": "strategywiki", "source_id": "s1"},
        {"title": "Tobu Tobu Girl", "plugin": "homebrew", "source_id": "h1", "platform": "gb"},
    ]
    monkeypatch.setattr(hub, "available", lambda: True)
    monkeypatch.setattr(svc, "_hub_enabled", lambda: True)
    monkeypatch.setattr(hub, "search", lambda *a, **k: (rows, ""))
    monkeypatch.setattr(hub, "resolve_torrents", lambda r: r, raising=False)
    got = svc._hub_releases("Tobu Tobu Girl", None)
    assert [r.indexer for r in got] == ["hub:homebrew"]


def test_the_set_names_guides_videos_and_mods():
    for slug in ("youtube-tutorials", "strategywiki", "moddb", "gamebanana", "game-manuals"):
        assert slug in hub.NON_GAME_PLUGINS
    for slug in ("archive-org", "homebrew", "libretro-content", "theromdepot", "minerva-archive"):
        assert slug not in hub.NON_GAME_PLUGINS
