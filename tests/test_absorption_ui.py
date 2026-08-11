"""The Questarr-absorption surfaces, as the rendered page carries them."""

from romarr.ui import page


def test_the_lists_page_is_in_the_nav_and_rendered():
    html = page()
    assert 'data-page="lists"' in html
    assert "RENDER.lists" in html
    assert "lists:'Import Lists'" in html


def test_the_stats_page_is_in_the_nav_and_rendered():
    html = page()
    assert 'data-page="stats"' in html
    assert "RENDER.stats" in html
    assert "/api/v1/stats" in html


def test_the_tasks_page_reads_the_scheduler_not_a_hardcoded_list():
    """The Tasks page used to be three hardcoded rows. Now the scheduler is
    the truth, so a job added there appears here without a UI change."""
    html = page()
    assert "/api/v1/system/tasks" in html
    assert "interval_seconds" in html


def test_search_results_link_the_release_page():
    """Questarr's roadmap calls this 'indexer page linking'. The info link is
    the one URL safe for a browser -- download links carry credentials."""
    html = page()
    assert "r.info_url" in html
    assert 'rel="noopener noreferrer"' in html


def test_the_shelf_editor_is_wired_to_the_grid():
    html = page()
    assert "shelfEditor" in html
    assert "/api/v1/game/meta" in html
    for status in ("playing", "completed", "shelved"):
        assert status in html


def test_general_settings_expose_the_clock():
    html = page()
    for key in ("auto_import_interval_minutes", "search_missing_interval_hours",
                "rss_sync_interval_minutes", "list_sync_interval_hours",
                "update_check"):
        assert key in html, key


def test_the_stats_page_says_updates_are_never_automatic():
    assert "Nothing updates itself" in page()


def test_frontend_rows_survive_pathless_backend_rows(tmp_path):
    """RomM's cached rows carry no filesystem path. Building the fallback
    from library_root(None) was a 500 on every export -- caught by the live
    Playnite proof, of all things."""
    from romarr.app import ROMarr
    from romarr.libraries import Game
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                      "LIBRARY_PATH": str(tmp_path)})
    svc._library_cache = ([Game(id="1", name="Super Metroid (USA)",
                                platform="snes")], 9e18, "")
    rows = svc.frontend_rows()
    assert "snes" in rows[0]["path"]
    assert rows[0]["path"].endswith("Super Metroid (USA)")


def test_the_library_serves_organised_pages_not_the_first_100(tmp_path):
    """A 166k-game install rendered 'about the first 100 games' because the
    cache held one 200-row page. The view is platform-grouped, sorted,
    searchable and paged now."""
    from romarr.app import ROMarr
    from romarr.libraries import Game
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                      "LIBRARY_PATH": str(tmp_path)})
    shelf = [Game(id=str(n), name=f"Game {n:04d}", platform="snes")
             for n in range(300)]
    shelf += [Game(id=f"n{n}", name=f"NES Game {n}", platform="nes")
              for n in range(5)]
    svc._publish_library(shelf, "", partial=False)

    page = svc.library_view(limit=120)
    assert page["grand_total"] == 305
    assert len(page["items"]) == 120
    # nes sorts before snes, platform first then title.
    assert page["items"][0]["platform"] == "nes"
    assert {p["platform"] for p in page["platforms"]} == {"snes", "nes"}

    nes = svc.library_view(platform="nes")
    assert nes["total"] == 5 and len(nes["items"]) == 5

    found = svc.library_view(q="game 020")
    assert found["total"] == 10, "server-side search over the whole cache"

    beyond = svc.library_view(offset=290, limit=120)
    assert len(beyond["items"]) == 15, "pagination reaches past any one page"


def test_a_failing_list_records_why_on_the_list_itself(tmp_path):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.put_item("import_lists", {
        "name": "dead", "type": "psn", "npsso": "EXPIRED"})
    svc.list_sync()
    stored = svc.store.list_items("import_lists")[0]
    assert "ssocookie" in stored["last_sync"]["error"]
