"""What a library count means, and the three that were being added together.

The bug this file pins down was not arithmetic. Every number was correct; the
label was wrong. A RomM that holds 166,548 rows holds 72,120 files and 94,428
catalogue entries for things that stream from somewhere else, and ROMarr
called the sum "games in library" on the nav badge, on the Stats page and on
the Library page -- three surfaces agreeing on a number that answered nobody's
question. The owner's question was "where are my Archive.org games", and it
could not be asked of a number that had already eaten the distinction.

The payload shapes here are trimmed copies of real rows from
romm.moveweight.com, including the two catalogues sharing one platform, one
`fs_path`, and no metadata to tell them apart.
"""

from __future__ import annotations

import json

import pytest

from romarr.app import ROMarr
from romarr.clients import Romm, RommConfig
from romarr.libraries import Game, classify_provenance, library_counts


# ----------------------------------------------------------- provenance --

def test_a_file_on_disk_is_local_whatever_it_is_called():
    """The library server counted the bytes. That beats any filename guess."""
    assert classify_provenance("Blithe Girl Escape__c50c8f26.swf", "local") == "local"
    assert classify_provenance("winx-club__Winx.swf", "local") == "local"
    assert classify_provenance("anything at all", "") == "local"


def test_flashpoint_is_recognised_by_its_game_id_suffix():
    """build_fp_index.py writes `<title>__<first 8 hex of the guid>.swf`."""
    assert classify_provenance("Aegean Adventure__01ac24d0.swf", "cloud") == "flashpoint"
    assert classify_provenance("Lataj_ca kula__00667866.swf", "cloud") == "flashpoint"
    assert classify_provenance("Crazy Jumping Cars 2__0337e848.swf",
                               "cloud") == "flashpoint"


def test_archive_org_is_recognised_by_its_identifier_and_member():
    """build_index.py writes `<item identifier>__<file inside the item>`."""
    assert classify_provenance("winx-club-hidden-stars-2__Winx_Yildizlari.swf",
                               "cloud") == "archive"
    assert classify_provenance("windows-magi-sp3__431810_SP3.swf",
                               "cloud") == "archive"
    assert classify_provenance(
        "Derbyshire_Ram_Collection_Disk_2783__Derbyshire_Ram_Side_B.d64",
        "cloud") == "archive"


def test_an_entry_that_matches_neither_stays_unknown():
    """The point of the last bucket is that it stays a guess.

    Assigning an unplaceable row to whichever catalogue is larger is how a
    breakdown starts adding up tidily and lying, which is the failure this
    whole page exists to undo.
    """
    assert classify_provenance("Super Mario 3D All-Stars [NSP]", "cloud") == "cloud"
    assert classify_provenance("gamelist.xml", "cloud") == "cloud"
    assert classify_provenance("", "cloud") == "cloud"


# ------------------------------------------------------- the RomM split --

class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class SplitSession:
    """A RomM 5.x that honours `missing`, with this library's real numbers."""

    TOTALS = {None: 166548, "false": 72120, "true": 94428}

    def __init__(self):
        self.asked = []

    def get(self, url, **kw):
        params = kw.get("params") or {}
        self.asked.append(params.get("missing"))
        return FakeResponse({"total": self.TOTALS[params.get("missing")],
                             "items": []})

    def post(self, url, **kw):
        return FakeResponse({"access_token": "t"})


class DeafSession:
    """A RomM 4.x: no `missing` parameter, so FastAPI drops it silently."""

    def get(self, url, **kw):
        return FakeResponse({"total": 166548, "items": []})

    def post(self, url, **kw):
        return FakeResponse({"access_token": "t"})


def romm(session):
    return Romm(RommConfig(base_url="http://romm", username="u", password="p"),
                session=session)


def test_counts_splits_the_library_server_side():
    session = SplitSession()
    got = romm(session).counts()
    assert got == {"total": 166548, "on_disk": 72120, "catalogued": 94428}
    assert got["on_disk"] + got["catalogued"] == got["total"], "no double count"
    assert session.asked == [None, "false", "true"], "three cheap queries"


def test_a_server_that_ignores_the_filter_reports_no_split_rather_than_a_wrong_one():
    """RomM 4.x answers every one of the three calls with the whole library.

    Taken at face value that reads as "everything is on disk, and also
    everything is catalogued" -- 333,096 games on a 166,548-row library. The
    sum is the tell, and failing to reconcile means unknown, not zero.
    """
    got = romm(DeafSession()).counts()
    assert got == {"total": 166548, "on_disk": None, "catalogued": None}


def test_count_still_answers_the_way_it_always_did():
    """Nothing downstream of the old single number breaks."""
    assert romm(SplitSession()).count() == 166548


# --------------------------------------------------- the backend helper --

class Splitter:
    name = "splits"

    def count(self):
        return 100

    def counts(self):
        return {"total": 100, "on_disk": 40, "catalogued": 60}


class Plain:
    name = "counts only"

    def count(self):
        return 7


def test_library_counts_prefers_a_backend_that_can_split():
    assert library_counts(Splitter()) == {"total": 100, "on_disk": 40,
                                          "catalogued": 60}


def test_a_backend_without_a_split_holds_every_file_it_lists():
    """Not a shrug: a folder library indexes files it actually has."""
    assert library_counts(Plain()) == {"total": 7, "on_disk": 7, "catalogued": 0}


# --------------------------------------------------- what the UI is told --

def shelf(tmp_path, split=None):
    """A service with this library's shape in miniature: some of each kind."""
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                    "LIBRARY_PATH": str(tmp_path)})
    s._publish_library([
        Game(id="1", name="Chrono Trigger", platform="snes"),
        Game(id="2", name="Super Metroid", platform="snes"),
        Game(id="3", name="Aegean Adventure__01ac24d0.swf",
             platform="Browser (Flash/HTML5)", origin="cloud",
             provenance="flashpoint"),
        Game(id="4", name="Crazy Jumping Cars 2__0337e848.swf",
             platform="Browser (Flash/HTML5)", origin="cloud",
             provenance="flashpoint"),
        Game(id="5", name="winx-club__Winx.swf",
             platform="Browser (Flash/HTML5)", origin="cloud",
             provenance="archive"),
        Game(id="6", name="Super Mario 3D All-Stars [NSP]", platform="switch",
             origin="cloud", provenance="cloud"),
    ], "", partial=False)
    if split is not None:
        s._count_split = split
        s._count_cache = (split["total"], 1.0)
    return s


def test_the_library_page_reports_three_numbers_not_one(tmp_path):
    totals = shelf(tmp_path).library_view()["totals"]
    assert totals["on_disk"] == 2
    assert totals["catalogued"] == 4
    assert totals["total"] == 6
    assert totals["on_disk"] + totals["catalogued"] == totals["total"]


def test_the_server_total_and_the_cached_total_are_labelled_separately(tmp_path):
    """They differ for the minutes a large library takes to walk.

    Publishing one number for both is how the shelf came to disagree with the
    nav badge on the same screen.
    """
    s = shelf(tmp_path, split={"total": 166548, "on_disk": 72120,
                               "catalogued": 94428})
    view = s.library_view()
    assert view["totals"]["total"] == 166548, "what the server holds"
    assert view["cached_total"] == 6, "what is browsable right now"
    assert view["grand_total"] == 6, "the old field keeps its old meaning"


def test_the_source_breakdown_names_each_catalogue(tmp_path):
    view = shelf(tmp_path).library_view()
    sources = {s["value"]: s["count"] for s in view["facets"]["sources"]}
    assert sources == {"local": 2, "flashpoint": 2, "archive": 1, "cloud": 1}
    assert view["totals"]["sources"] == sources


def test_the_shelf_can_be_cut_by_source(tmp_path):
    s = shelf(tmp_path)
    assert s.library_view(source="flashpoint")["total"] == 2
    assert s.library_view(source="archive")["total"] == 1
    assert s.library_view(source="local")["total"] == 2
    assert s.library_view(source="cloud")["total"] == 1


def test_a_platform_chip_says_how_much_of_it_is_actually_here(tmp_path):
    """"Browser 94,415" beside "Nintendo DS 8,112" looked like one claim.

    Not one of the first is a file on this disk.
    """
    rows = {p["platform"]: p for p in shelf(tmp_path).library_view()["platforms"]}
    assert rows["Browser (Flash/HTML5)"]["on_disk"] == 0
    assert rows["Browser (Flash/HTML5)"]["catalogued"] == 3
    assert rows["snes"]["on_disk"] == 2
    assert rows["snes"]["catalogued"] == 0


def test_the_nav_badge_carries_the_split_beside_the_sum(tmp_path):
    got = shelf(tmp_path, split={"total": 166548, "on_disk": 72120,
                                 "catalogued": 94428}).counts()
    assert got["games"] == 166548, "the badge keeps meaning what it meant"
    assert got["games_on_disk"] == 72120
    assert got["games_catalogued"] == 94428


def test_the_stats_page_no_longer_reports_a_conflated_total_alone(tmp_path):
    got = shelf(tmp_path, split={"total": 166548, "on_disk": 72120,
                                 "catalogued": 94428}).stats()
    assert got["library_games"] == 166548
    assert got["library_on_disk"] == 72120
    assert got["library_catalogued"] == 94428
    assert got["library_sources"]["flashpoint"] == 2
    assert got["library_sources"]["archive"] == 1


def test_the_shelf_answers_when_the_backend_cannot_split(tmp_path):
    """A backend that cannot separate the two must not make the page lie.

    The walked rows carry `origin` per game, so the split is recoverable
    from the cache even when the server would not answer it.
    """
    s = shelf(tmp_path, split={"total": 6, "on_disk": None, "catalogued": None})
    split = s.library_split()
    assert split["on_disk"] == 2
    assert split["catalogued"] == 4
    # And all three come from the same place, so they still add up.
    assert split["total"] == 6
    assert split["counted_from"] == "cached rows"


def test_the_three_numbers_never_come_from_two_different_places(tmp_path):
    """A server total beside a cache-derived split does not reconcile.

    That is the shape of the original bug wearing a new hat: numbers that
    look authoritative, disagree with each other, and move while the walk
    runs. Whichever source answers, it answers for all three.
    """
    s = shelf(tmp_path, split={"total": 166548, "on_disk": 72120,
                               "catalogued": 94428})
    split = s.library_split()
    assert split["counted_from"] == "library server"
    assert split["on_disk"] + split["catalogued"] == split["total"]

    s._count_split = {"total": 166548, "on_disk": None, "catalogued": None}
    split = s.library_split()
    assert split["counted_from"] == "cached rows"
    assert split["on_disk"] + split["catalogued"] == split["total"]


def test_nothing_is_known_before_the_first_refresh(tmp_path):
    """A dash, not a zero. "Not counted yet" and "empty" are different."""
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    split = s.library_split()
    assert split["total"] is None
    assert split["on_disk"] is None and split["catalogued"] is None


# ------------------------------------------------- end to end, from RomM --

CATALOGUED = {
    "id": 112151,
    "name": "Blithe Girl Escape__c50c8f26.swf",
    "fs_name": "Blithe Girl Escape__c50c8f26.swf",
    "fs_path": "roms/flash",
    "platform_display_name": "Browser (Flash/HTML5)",
    "platform_slug": "browser",
    # Both catalogues land here with these two empty. There is no
    # `flashpoint_id` to read -- it is null on all 94,415 live rows.
    "flashpoint_id": None,
    "flashpoint_metadata": {},
    "missing_from_fs": True,
    "metadatum": {},
}

ON_DISK = {
    "id": 1,
    "name": "Gex",
    "fs_name": "3DO_Gamepack.iso",
    "fs_path": "roms/3do",
    "platform_display_name": "3DO Interactive Multiplayer",
    "platform_slug": "3do",
    "missing_from_fs": False,
    "metadatum": {},
}

ARCHIVE = {
    "id": 90001,
    "name": "winx-club-hidden-stars-2__Winx_Yildizlari.swf",
    "fs_name": "winx-club-hidden-stars-2__Winx_Yildizlari.swf",
    "fs_path": "roms/flash",
    "platform_display_name": "Browser (Flash/HTML5)",
    "platform_slug": "browser",
    "missing_from_fs": True,
    "metadatum": {},
}


class PageSession:
    def __init__(self, items):
        self.items = items

    def get(self, url, **kw):
        return FakeResponse({"items": self.items, "total": len(self.items)})

    def post(self, url, **kw):
        return FakeResponse({"access_token": "t"})


@pytest.mark.parametrize("row,origin,provenance", [
    (ON_DISK, "local", "local"),
    (CATALOGUED, "cloud", "flashpoint"),
    (ARCHIVE, "cloud", "archive"),
])
def test_romm_rows_carry_both_axes(row, origin, provenance):
    """`origin` says whether it is here; `provenance` says where it came from.

    Two questions, two fields. One of them used to answer both badly.
    """
    got = romm(PageSession([row])).games()
    assert got[0].origin == origin
    assert got[0].provenance == provenance
