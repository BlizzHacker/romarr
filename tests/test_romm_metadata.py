"""What ROMarr keeps from a RomM rom payload, and what it gets wrong if not.

RomM identifies games against IGDB, MobyGames, ScreenScraper, LaunchBox and
HowLongToBeat and serves the merged result on every listing row. Most of it
used to be dropped on the floor at the mapping step, which is why the
Calendar had no dates to draw and Discover had one axis to cut on.

The payloads here are trimmed copies of real rows from a live RomM 5.x
install -- in particular the units and the scale, which are the two things
this code got wrong.
"""

from __future__ import annotations

import json

from romarr.clients import Romm, RommConfig


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, page):
        self.page = page
        self.calls = []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return FakeResponse(self.page)

    def post(self, url, **kw):  # only the token exchange lands here
        return FakeResponse({"access_token": "t"})


GEX = {
    "id": 1,
    "name": "Gex",
    "platform_display_name": "3DO Interactive Multiplayer",
    "platform_slug": "3do",
    "path_cover_small": "/assets/roms/1/cover/small.png",
    "created_at": "2025-12-02T01:14:58+00:00",
    "updated_at": "2026-05-03T17:51:33+00:00",
    "regions": ["USA"],
    "metadatum": {
        "genres": ["Adventure", "Platform"],
        "franchises": ["Gex"],
        "companies": ["Crystal Dynamics", "Crystal Dynamics", "BMG Interactive"],
        "game_modes": ["Single player"],
        "player_count": "1",
        # Milliseconds, which is what the listing serves.
        "first_release_date": 797212800000,
        # A percentage. This is the field, and this is the scale.
        "average_rating": 63.86,
    },
}


def client(rows):
    session = FakeSession({"items": rows, "total": len(rows)})
    return Romm(RommConfig(base_url="http://romm", api_token="t"), session)


def one(row):
    return client([row]).games()[0]


def test_the_release_date_survives_in_full_not_just_the_year():
    """The Calendar cannot place a game on a day it was never given."""
    game = one(GEX)
    assert game.released == "1995-04-07"
    assert game.year == 1995


def test_the_release_stamp_is_read_in_seconds_or_milliseconds():
    """RomM serves the same instant in both, depending on the provider that
    filled it in. Reading one of them is why some games had a year and some
    had nothing."""
    ms = one({**GEX, "metadatum": {**GEX["metadatum"],
                                   "first_release_date": 797212800000}})
    sec = one({**GEX, "metadatum": {**GEX["metadatum"],
                                    "first_release_date": 797212800}})
    assert ms.released == sec.released == "1995-04-07"


def test_a_release_date_is_utc_not_the_servers_local_zone():
    """A midnight-UTC stamp rendered locally slides a day backwards west of
    Greenwich, which puts the wrong games on a calendar square."""
    # 1995-01-01T00:00:00Z exactly.
    game = one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "first_release_date": 788918400}})
    assert game.released == "1995-01-01"


def test_a_provider_that_hands_back_a_string_is_understood_too():
    assert one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "first_release_date": "1995-04-07"}}
               ).released == "1995-04-07"
    # A bare year is padded rather than dropped: 1995 beats knowing nothing,
    # and the Calendar reports its own coverage.
    assert one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "first_release_date": "1995"}}
               ).released == "1995-01-01"


def test_nonsense_in_the_date_field_is_no_date_rather_than_a_crash():
    for bad in (None, 0, -1, "", "soon", True, {}, 10 ** 20):
        assert one({**GEX,
                    "metadatum": {**GEX["metadatum"],
                                  "first_release_date": bad}}).released == ""


def test_the_rating_is_rescaled_from_the_percentage_romm_serves():
    """Measured across a live library, average_rating runs 15.0 to 100.0 and
    never lands in 0-10. ROMarr shows it as "★9.6" and Discover called
    anything over 7.5 a hidden gem -- so every rated game was a hidden gem
    and every poster claimed a score of 63.9 out of 10."""
    assert one(GEX).rating == 6.4
    assert one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "average_rating": 100}}).rating == 10.0
    # An older RomM storing 0-10 is left alone rather than divided again.
    assert one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "average_rating": 9.6}}).rating == 9.6
    assert one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "average_rating": None}}).rating == 0.0


def test_the_dates_romm_holds_for_every_row_come_through():
    """Fewer than half a large library carries a release date; all of it
    carries these two, which is what makes them worth a calendar."""
    game = one(GEX)
    assert game.added == "2025-12-02"
    assert game.updated == "2026-05-03"
    bare = one({"id": "9", "name": "Unidentified", "created_at": "2026-08-04"})
    assert bare.added == "2026-08-04"
    assert bare.released == "" and bare.rating == 0.0


def test_the_rest_of_the_identified_metadata_comes_through():
    game = one(GEX)
    assert game.genres == ("Adventure", "Platform")
    assert game.franchises == ("Gex",)
    assert game.modes == ("Single player",)
    assert game.players == "1"
    assert game.regions == ("USA",)


def test_duplicate_and_blank_metadata_values_are_collapsed():
    """RomM merges providers, so the same company arrives more than once."""
    game = one({**GEX, "metadatum": {**GEX["metadatum"],
                                     "companies": ["Sega", "Sega", "", "  ",
                                                   None, "Sonic Team"]}})
    assert game.companies == ("Sega", "Sonic Team")


def test_repeated_vocabulary_is_shared_not_copied_per_row():
    """7,776 distinct company names spread over 166,548 rows: without
    interning, the shelf cache pays to say "Nintendo" a hundred thousand
    times."""
    rows = [{**GEX, "id": str(n)} for n in range(3)]
    games = client(rows).games()
    first = games[0].companies[0]
    assert all(g.companies[0] is first for g in games)


def test_a_row_romm_could_not_identify_still_becomes_a_game():
    """Most of a fresh library is unidentified. No metadata must never mean
    no game."""
    game = one({"id": "42", "fs_name": "mystery.sfc", "platform_slug": "snes",
                "missing_from_fs": True})
    assert game.name == "mystery.sfc"
    assert game.origin == "cloud"
    assert (game.released, game.genres, game.franchises,
            game.players) == ("", (), (), "")
