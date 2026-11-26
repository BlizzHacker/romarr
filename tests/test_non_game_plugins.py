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


def test_hub_search_skips_non_game_and_incompatible_platform_plugins():
    from types import SimpleNamespace

    def make(slug, platforms=()):
        return SimpleNamespace(
            slug=slug,
            enabled=True,
            manifest=SimpleNamespace(capabilities=("search",), platforms=platforms),
        )

    assert not hub._plugin_supports_platform(make("libretro-cores"), "snes")
    assert not hub._plugin_supports_platform(make("youtube-tutorials"), None)
    assert not hub._plugin_supports_platform(make("demozoo"), "snes")
    assert not hub._plugin_supports_platform(make("aminet"), "switch")
    assert not hub._plugin_supports_platform(make("repacks"), "snes")
    assert hub._plugin_supports_platform(make("repacks"), "win")
    assert not hub._plugin_supports_platform(make("custom-amiga", ("amiga",)), "snes")
    assert hub._plugin_supports_platform(make("demozoo"), "amiga")
    assert hub._plugin_supports_platform(make("demozoo"), None)
    assert hub._plugin_supports_platform(make("archive-org"), "snes")
