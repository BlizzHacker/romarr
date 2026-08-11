"""Filters over a large shelf, and honesty about what can be filtered."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from romarr.app import ROMarr, make_handler
from romarr.libraries import Game


def shelf(tmp_path):
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                    "LIBRARY_PATH": str(tmp_path)})
    s._publish_library([
        Game(id="1", name="Chrono Trigger", platform="snes",
             genres=("Role-playing (RPG)",), regions=("USA",), year=1995,
             rating=9.6, released="1995-03-11", added="2026-01-05",
             updated="2026-02-09", franchises=("Chrono",),
             companies=("Square",), modes=("Single player",), players="1"),
        Game(id="2", name="Super Metroid", platform="snes",
             genres=("Platform", "Adventure"), regions=("USA",), year=1994,
             rating=9.4, released="1994-03-19", added="2026-01-05",
             updated="2026-02-09", franchises=("Metroid",),
             companies=("Nintendo",), modes=("Single player",), players="1"),
        Game(id="3", name="Doom", platform="dos", genres=("Shooter",),
             regions=("World",), year=1993, rating=9.0,
             released="1993-12-10", added="2026-01-06",
             updated="2026-02-09", companies=("id Software",),
             modes=("Single player", "Multiplayer", "Co-operative"),
             players="1-4"),
        Game(id="4", name="Unidentified Thing", platform="snes",
             added="2026-03-11"),
        Game(id="5", name="Cloud Game", platform="nes", origin="cloud",
             genres=("Platform",), year=1985, rating=7.0,
             released="1985-03-11", added="2026-01-05",
             updated="2026-02-09", modes=("Local Multiplayer",),
             players="2"),
    ], "", partial=False)
    return s


def test_genre_filter_cuts_the_shelf(tmp_path):
    s = shelf(tmp_path)
    out = s.library_view(genre="Platform")
    assert {g["name"] for g in out["items"]} == {"Super Metroid", "Cloud Game"}
    assert out["total"] == 2


def test_region_and_decade_filters(tmp_path):
    s = shelf(tmp_path)
    assert s.library_view(region="USA")["total"] == 2
    assert s.library_view(decade="1990s")["total"] == 3
    assert s.library_view(decade="1980s")["total"] == 1


def test_origin_separates_local_from_cloud(tmp_path):
    """The distinction people actually want: plays now vs must be fetched."""
    s = shelf(tmp_path)
    assert s.library_view(origin="cloud")["total"] == 1
    assert s.library_view(origin="local")["total"] == 4


def test_sorting_is_over_the_whole_filtered_set_not_the_page(tmp_path):
    s = shelf(tmp_path)
    top = s.library_view(sort="rating", limit=1)
    assert top["items"][0]["name"] == "Chrono Trigger"
    newest = s.library_view(sort="year", limit=1)
    assert newest["items"][0]["name"] == "Chrono Trigger"  # 1995


def test_filters_combine(tmp_path):
    s = shelf(tmp_path)
    out = s.library_view(platform="snes", genre="Platform")
    assert [g["name"] for g in out["items"]] == ["Super Metroid"]


def test_facets_report_coverage_honestly(tmp_path):
    """A genre list covering a fraction of the shelf must say so, or it
    reads as 'you own almost nothing'."""
    s = shelf(tmp_path)
    f = s.library_view()["facets"]
    assert f["identified"] == 4, "the unidentified game is not counted"
    genres = {g["value"]: g["count"] for g in f["genres"]}
    assert genres["Platform"] == 2
    assert {d["value"] for d in f["decades"]} == {"1990s", "1980s"}
    assert {o["value"] for o in f["origins"]} == {"local", "cloud"}


def test_an_unidentified_library_still_lists_everything(tmp_path):
    """Most libraries are unidentified. No genre must never mean no games."""
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    s._publish_library([Game(id=str(n), name=f"G{n}", platform="snes")
                        for n in range(5)], "", partial=False)
    out = s.library_view()
    assert out["total"] == 5
    assert out["facets"]["identified"] == 0
    assert out["facets"]["genres"] == []


# -- Discover and Calendar from your own shelf --------------------------------

def test_discover_needs_no_api_key(tmp_path):
    """The point: RAWG/IGDB need a key nobody has on day one, and the
    library already knows its own genres and ratings."""
    s = shelf(tmp_path)
    top = s.discover_library("top-rated")
    assert top["items"][0]["title"] == "Chrono Trigger"
    assert top["items"][0]["owned"] is True

    recent = s.discover_library("recent")
    assert recent["items"][0]["year"] == 1995


def test_hidden_gems_surfaces_well_rated_games_on_small_platforms(tmp_path):
    s = shelf(tmp_path)
    gems = {g["title"] for g in s.discover_library("hidden-gems")["items"]}
    assert "Doom" in gems and "Chrono Trigger" in gems


def test_discover_by_genre_and_its_genre_list(tmp_path):
    s = shelf(tmp_path)
    out = s.discover_library("by-genre", genre="Platform")
    assert {g["title"] for g in out["items"]} == {"Super Metroid", "Cloud Game"}
    assert "Platform" in out["genres"]


def test_an_unknown_shelf_names_the_real_ones(tmp_path):
    out = shelf(tmp_path).discover_library("trending")
    assert "top-rated" in out["error"]


def test_the_calendar_is_your_library_by_year(tmp_path):
    s = shelf(tmp_path)
    cal = s.library_calendar()
    years = {y["year"]: y["count"] for y in cal["years"]}
    assert years == {1985: 1, 1993: 1, 1994: 1, 1995: 1}
    assert set(cal["decades"]) == {"1980s", "1990s"}

    nineties = s.library_calendar(decade="1990s")
    assert len(nineties["items"]) == 3
    assert nineties["items"][0]["year"] == 1995


def test_both_say_so_before_the_library_is_read(tmp_path):
    from romarr.app import ROMarr
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    s._library_cache = (None, 0.0, "")
    assert "not been read" in s.discover_library()["error"]
    assert "not been read" in s.library_calendar()["error"]


def test_recently_added_is_not_newest_release(tmp_path):
    """Two different questions. 'Newest' is 1995; the thing added last is a
    game from 1993, and after a scan finishes that is what people look for."""
    s = shelf(tmp_path)
    assert s.discover_library("recent")["items"][0]["year"] == 1995
    added = s.discover_library("recently-added")["items"]
    assert added[0]["title"] == "Unidentified Thing"
    assert added[0]["added"] == "2026-03-11"


def test_multiplayer_shelf_leaves_out_single_player_only(tmp_path):
    s = shelf(tmp_path)
    titles = {g["title"] for g in s.discover_library("multiplayer")["items"]}
    assert titles == {"Doom", "Cloud Game"}


def test_franchise_and_company_shelves_offer_their_own_chips(tmp_path):
    """Offering genres while somebody browses franchises is a row of buttons
    that do nothing."""
    s = shelf(tmp_path)
    out = s.discover_library("by-franchise", value="Metroid")
    assert [g["title"] for g in out["items"]] == ["Super Metroid"]
    assert "Metroid" in out["facet"] and "Chrono" in out["facet"]

    out = s.discover_library("by-company", value="id Software")
    assert [g["title"] for g in out["items"]] == ["Doom"]
    assert "Square" in out["facet"]


def test_by_genre_still_answers_to_the_old_parameter_name(tmp_path):
    """Bookmarked URLs carry ?genre=; they must not silently return nothing."""
    s = shelf(tmp_path)
    assert {g["title"] for g in
            s.discover_library("by-genre", genre="Platform")["items"]} == {
        "Super Metroid", "Cloud Game"}


# -- the Calendar, over dates the library server actually holds ---------------

def test_the_calendar_places_games_on_a_day_not_just_a_year(tmp_path):
    """A year cannot answer 'what came out on this day', which is the only
    question a calendar is for. RomM carries the whole date."""
    s = shelf(tmp_path)
    march = s.library_calendar(view="releases", month="1999-03", day="11")
    assert march["month"] == "1999-03"
    assert march["days_in_month"] == 31
    counts = {d["day"]: d["count"] for d in march["days"]}
    # Two games share March 11th across two different years -- that is the
    # anniversary the year-only version could never show.
    assert counts[11] == 2 and counts[19] == 1
    assert {g["title"] for g in march["items"]} == {"Chrono Trigger",
                                                   "Cloud Game"}
    assert march["day"] == "03-11"


def test_the_calendar_reports_how_much_of_the_shelf_it_can_place(tmp_path):
    """A calendar drawn over part of a library that presents itself as the
    whole library is how somebody concludes they own nothing from 1993."""
    s = shelf(tmp_path)
    cov = s.library_calendar(view="releases")["coverage"]
    assert cov == {"dated": 4, "total": 5, "field": "released"}


def test_added_and_updated_are_their_own_calendars(tmp_path):
    s = shelf(tmp_path)
    added = s.library_calendar(view="added", month="2026-01", day="05")
    assert {g["title"] for g in added["items"]} == {
        "Chrono Trigger", "Super Metroid", "Cloud Game"}
    assert {d["day"]: d["count"] for d in added["days"]}[6] == 1
    # Every row carries these, where only the identified ones carry a
    # release date -- so this view covers more of the shelf, not less.
    assert added["coverage"]["dated"] == 5

    updated = s.library_calendar(view="updated", month="2026-02", day="09")
    assert len(updated["items"]) == 4


def test_the_calendar_opens_on_a_day_that_has_something_on_it(tmp_path):
    """Against the real library the Updated view opened on today, the 11th,
    which was blank -- while the 4th of the same month held 11,155 rows. A
    calendar with plenty in it must not open looking empty."""
    s = shelf(tmp_path)
    out = s.library_calendar(view="added", month="2026-01")
    assert out["selected_day"] == 5, "the busiest day, not an empty one"
    assert len(out["items"]) == 3
    # An explicit day is still obeyed, empty or not.
    assert s.library_calendar(view="added", month="2026-01",
                              day="20")["items"] == []


def test_a_month_with_nothing_in_it_is_empty_rather_than_wrong(tmp_path):
    s = shelf(tmp_path)
    out = s.library_calendar(view="added", month="2020-07")
    assert sum(d["count"] for d in out["days"]) == 0
    assert out["items"] == []


def test_a_malformed_month_falls_back_to_a_real_one(tmp_path):
    s = shelf(tmp_path)
    out = s.library_calendar(view="added", month="not-a-month")
    assert len(out["month"]) == 7 and out["days_in_month"] >= 28


def test_upcoming_says_romm_has_no_release_schedule_rather_than_faking_one(tmp_path):
    """RomM has no concept of an unreleased game: every row is a file, or a
    catalogue entry for something that already shipped."""
    s = shelf(tmp_path)
    out = s.library_calendar(view="upcoming")
    assert out["items"] == []
    assert "will not invent one" in out["note"]


def test_upcoming_does_show_a_future_dated_row_if_one_exists(tmp_path):
    """The honesty runs both ways: if the library server does hold a future
    date, it is shown rather than suppressed to keep the message tidy."""
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    s._publish_library([Game(id="1", name="Later", platform="snes",
                             released="2999-01-01", year=2999)],
                       "", partial=False)
    out = s.library_calendar(view="upcoming")
    assert [g["title"] for g in out["items"]] == ["Later"]
    assert "note" not in out


def test_an_unknown_view_falls_back_rather_than_erroring(tmp_path):
    s = shelf(tmp_path)
    assert s.library_calendar(view="horoscope")["view"] == "releases"


def test_the_routes_pass_every_parameter_through(tmp_path):
    """A method that answers correctly and a route that drops half its query
    string is exactly as useless as no method -- and it fails silently, by
    quietly answering about the default month instead of the asked-for one."""
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "http.json"),
                          "ROMARR_API_KEY": "testkey"})
    service._publish_library(list(shelf(tmp_path)._library_cache[0]),
                             "", partial=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def get(path):
            request = urllib.request.Request(base + path)
            request.add_header("X-Api-Key", "testkey")
            with urllib.request.urlopen(request, timeout=10) as r:
                return json.loads(r.read())

        cal = get("/api/v1/calendar/library?view=added&month=2026-01&day=06")
        assert cal["view"] == "added" and cal["month"] == "2026-01"
        assert [g["title"] for g in cal["items"]] == ["Doom"]

        disc = get("/api/v1/discover/library?shelf=by-company"
                   "&value=id%20Software&limit=5")
        assert [g["title"] for g in disc["items"]] == ["Doom"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_grid_carries_enough_to_lay_out_a_real_month(tmp_path):
    """Monday-first weekday offset and month length, so the browser does not
    recompute a calendar the server already knows."""
    s = shelf(tmp_path)
    out = s.library_calendar(view="added", month="2026-02")
    # 1 February 2026 is a Sunday: index 6 in calendar.monthrange terms.
    assert out["first_weekday"] == 6
    assert out["days_in_month"] == 28
    assert out["month_name"] == "February"


# -- platform chips come from the server, not the partial cache ---------------

def test_platform_chips_show_everything_the_server_has(tmp_path):
    """A 166k-game walk reaches platforms one at a time; chips derived from
    the cache showed eight platforms for minutes and a person looking for
    PC or Xbox concluded they were unsupported."""
    from romarr.app import ROMarr
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    # The server knows about three platforms; the walk has reached one.
    s._server_platforms = [
        {"platform": "PC (Windows)", "count": 900},
        {"platform": "Xbox", "count": 400},
        {"platform": "SNES", "count": 100},
    ]
    s._publish_library([Game(id="1", name="A", platform="SNES")],
                       "", partial=True)
    chips = {p["platform"]: p for p in s.library_view()["platforms"]}
    assert set(chips) == {"PC (Windows)", "Xbox", "SNES"}
    assert chips["PC (Windows)"]["count"] == 900
    # ...and the UI can say how much of each is browsable right now.
    assert chips["PC (Windows)"]["cached"] == 0
    assert chips["SNES"]["cached"] == 1


def test_without_a_server_list_the_cache_still_answers(tmp_path):
    """A folder library has no platform endpoint; chips must still work."""
    s = shelf(tmp_path)
    chips = {p["platform"]: p for p in s.library_view()["platforms"]}
    assert chips["snes"]["count"] == chips["snes"]["cached"] == 3
