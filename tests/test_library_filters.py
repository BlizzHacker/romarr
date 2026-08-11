"""Filters over a large shelf, and honesty about what can be filtered."""

from romarr.app import ROMarr
from romarr.libraries import Game


def shelf(tmp_path):
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                    "LIBRARY_PATH": str(tmp_path)})
    s._publish_library([
        Game(id="1", name="Chrono Trigger", platform="snes",
             genres=("Role-playing (RPG)",), regions=("USA",), year=1995,
             rating=9.6),
        Game(id="2", name="Super Metroid", platform="snes",
             genres=("Platform", "Adventure"), regions=("USA",), year=1994,
             rating=9.4),
        Game(id="3", name="Doom", platform="dos", genres=("Shooter",),
             regions=("World",), year=1993, rating=9.0),
        Game(id="4", name="Unidentified Thing", platform="snes"),
        Game(id="5", name="Cloud Game", platform="nes", origin="cloud",
             genres=("Platform",), year=1985, rating=7.0),
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
