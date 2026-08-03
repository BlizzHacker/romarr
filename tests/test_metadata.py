"""Naming a game from a hash rather than a filename.

Every tool in this category parses the release title and hopes. ROMarr knows
the DAT-verified name exactly, so the lookup key is a fact rather than the
output of a regular expression. These tests are mostly about that difference.
"""

from __future__ import annotations

import pytest

from romarr.dat import Match
from romarr.metadata import (
    PROVIDERS, GameInfo, Metadata, clean_title, lookup_key, title_from_dat)


# --- the key, which is the whole idea --------------------------------------

def test_a_verified_file_is_looked_up_by_its_dat_name():
    """The difference this module exists for.

    `Chrono.Trigger.USA.Retranslated.v1.2.[!].smc` parses to something like
    "Chrono Trigger Retranslated" -- a search that finds nothing or, worse,
    finds the wrong thing. The DAT says what it is.
    """
    term, how = lookup_key(
        Match("verified", game="Chrono Trigger (USA)"),
        "Chrono.Trigger.USA.Retranslated.v1.2.[!].smc")
    assert term == "Chrono Trigger"
    assert how == "dat"


def test_an_unverified_file_falls_back_to_the_filename():
    """Still supported, because an unverified file has nothing else -- but it
    is the fallback, not the method."""
    term, how = lookup_key(Match("unknown"), "Super Metroid (USA) [!].sfc")
    assert term == "Super Metroid"
    assert how == "filename"


def test_a_bad_dump_does_not_get_to_claim_the_dat_name():
    """Its hash did not match, so the DAT's name for it is not established."""
    _, how = lookup_key(Match("bad-dump"), "Super Metroid (USA).sfc")
    assert how == "filename"


def test_no_verification_at_all_still_works():
    term, how = lookup_key(None, "Contra (USA).nes")
    assert term == "Contra" and how == "filename"


def test_nothing_to_go_on_yields_nothing():
    assert lookup_key(None, "") == ("", "")


@pytest.mark.parametrize("game,expected", [
    ("Chrono Trigger (USA)", "Chrono Trigger"),
    ("Super Metroid (Japan, USA) (En,Ja)", "Super Metroid"),
    ("Final Fantasy VII (USA) (Disc 1)", "Final Fantasy VII"),
    ("Sonic the Hedgehog", "Sonic the Hedgehog"),
    ("", ""),
])
def test_a_dat_name_splits_at_the_first_parenthesis(game, expected):
    """No-Intro and Redump both use `Title (Region) (Extras)`, so the title is
    exact rather than guessed at."""
    assert title_from_dat(game) == expected


# --- filename parsing, the fallback ---------------------------------------

@pytest.mark.parametrize("filename,expected", [
    ("Super Metroid (USA) [!].sfc", "Super Metroid"),
    ("Chrono.Trigger.USA.v1.1.smc", "Chrono Trigger"),
    ("Sonic_The_Hedgehog_(W)_[!].md", "Sonic The Hedgehog"),
    ("Final Fantasy VII (USA) (Disc 1).chd", "Final Fantasy VII"),
    ("Contra (USA) (Rev 1) (Proto).nes", "Contra"),
    ("Metal Gear Solid [MULTI5].bin", "Metal Gear Solid"),
])
def test_filenames_are_cleaned_into_something_searchable(filename, expected):
    assert clean_title(filename) == expected


def test_cleaning_something_that_is_only_decoration_yields_nothing():
    assert clean_title("(USA) [!].sfc") == ""


# --- providers -------------------------------------------------------------

def fake_provider(monkeypatch, result):
    calls = []

    def lookup(cfg, term):
        calls.append(term)
        return result

    monkeypatch.setitem(PROVIDERS, "fake",
                        {"label": "Fake", "lookup": lookup,
                         "fields": (), "help": ""})
    return calls


FOUND = GameInfo(title="Chrono Trigger", summary="A JRPG", source="Fake")


def test_a_provider_is_asked_with_the_dat_name(monkeypatch):
    calls = fake_provider(monkeypatch, FOUND)
    got = Metadata([{"type": "fake"}]).identify(
        verification=Match("verified", game="Chrono Trigger (USA)"),
        filename="Chrono.Trigger.USA.Retranslated.smc")
    assert calls == ["Chrono Trigger"]
    assert got.title == "Chrono Trigger"


def test_the_result_records_how_it_was_matched(monkeypatch):
    """"we matched a DAT name" and "we guessed from the filename" deserve
    different amounts of trust, and a UI that shows metadata without saying
    which is inviting somebody to believe the wrong cover."""
    fake_provider(monkeypatch, FOUND)
    metadata = Metadata([{"type": "fake"}])
    assert metadata.identify(
        verification=Match("verified", game="Chrono Trigger (USA)")).matched_by == "dat"
    assert metadata.identify(filename="Contra (USA).nes").matched_by == "filename"


def test_providers_are_tried_in_order_until_one_answers(monkeypatch):
    order = []

    def empty(cfg, term):
        order.append("empty")
        return GameInfo()

    def finds(cfg, term):
        order.append("finds")
        return FOUND

    monkeypatch.setitem(PROVIDERS, "empty",
                        {"label": "E", "lookup": empty, "fields": (), "help": ""})
    monkeypatch.setitem(PROVIDERS, "finds",
                        {"label": "F", "lookup": finds, "fields": (), "help": ""})

    got = Metadata([{"type": "empty"}, {"type": "finds"}]).identify(
        filename="Contra.nes")
    assert order == ["empty", "finds"]
    assert got.found


def test_a_provider_that_raises_does_not_end_the_lookup(monkeypatch):
    def boom(cfg, term):
        raise OSError("refused")

    monkeypatch.setitem(PROVIDERS, "boom",
                        {"label": "B", "lookup": boom, "fields": (), "help": ""})
    fake_provider(monkeypatch, FOUND)
    got = Metadata([{"type": "boom"}, {"type": "fake"}]).identify(
        filename="Contra.nes")
    assert got.found, "a broken provider must not hide a working one"


def test_a_disabled_provider_is_skipped(monkeypatch):
    calls = fake_provider(monkeypatch, FOUND)
    Metadata([{"type": "fake", "enable": False}]).identify(filename="Contra.nes")
    assert calls == []


def test_an_unknown_provider_type_is_skipped_not_fatal():
    assert not Metadata([{"type": "nonsense"}]).identify(
        filename="Contra.nes").found


def test_no_providers_configured_is_not_an_error():
    """Metadata is an enhancement. A missing cover must never cost an import."""
    got = Metadata([]).identify(filename="Contra (USA).nes")
    assert not got.found
    assert got.matched_by == "filename"


def test_a_provider_with_no_credential_is_skipped_rather_than_failing():
    """RAWG needs a key and IGDB needs two. Unconfigured is the normal state
    on day one, and it must look like "no metadata yet" rather than an error
    on every import."""
    assert not Metadata([{"type": "rawg"}]).identify(
        filename="Contra.nes").found
    assert not Metadata([{"type": "igdb"}]).identify(
        filename="Contra.nes").found


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_every_provider_is_declared_completely(name):
    spec = PROVIDERS[name]
    assert spec["label"] and callable(spec["lookup"]) and spec["help"]


# --- wired, not merely written --------------------------------------------

def test_the_service_builds_a_metadata_chain(tmp_path):
    """This test exists because the module shipped unwired.

    249 lines and 27 green tests, and nothing in app.py imported it -- the
    exact "configuration that tests green and does nothing" failure this
    project keeps finding elsewhere, committed here. A unit-tested module
    nobody calls is dead code with a certificate.
    """
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    assert hasattr(service, "metadata")
    assert hasattr(service, "identify")


def test_identify_returns_the_shape_the_api_serves(tmp_path):
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    got = service.identify(filename="Super Metroid (USA) [!].sfc")
    assert set(got) >= {"found", "title", "cover_url", "matched_by", "source"}
    assert got["matched_by"] == "filename"


def test_adding_a_provider_and_reloading_takes_effect(tmp_path):
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    assert service.metadata.providers == []
    service.store.put_item("metadata_providers",
                           {"type": "rawg", "api_key": "k", "enable": True})
    service.reload_metadata()
    assert len(service.metadata.providers) == 1


# --- calendar --------------------------------------------------------------

def test_the_calendar_reports_why_it_is_empty():
    """"No games" and "you have not configured a provider" look identical in
    a UI unless one of them says so."""
    from romarr.metadata import calendar

    got = calendar([])
    assert got["items"] == []
    assert "api key" in got["error"].lower()


def test_the_calendar_window_looks_backwards_as_well_as_forwards():
    """"Upcoming" alone is the obvious reading and the less useful one: most
    of what somebody wants to acquire came out last month."""
    import datetime

    from romarr.metadata import calendar

    got = calendar([], days_back=30, days_ahead=60)
    today = datetime.date.today()
    assert got["from"] < today.isoformat() < got["to"]


def test_the_calendar_marks_which_entries_are_still_upcoming(monkeypatch):
    import datetime

    from romarr import metadata as module

    soon = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
    past = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    monkeypatch.setitem(
        module.CALENDAR_PROVIDERS, "rawg",
        lambda cfg, start, end, limit: [
            {"title": "Future Game", "released": soon},
            {"title": "Past Game", "released": past}])

    rows = module.calendar([{"type": "rawg", "api_key": "k"}])["items"]
    assert {r["title"]: r["upcoming"] for r in rows} == {
        "Future Game": True, "Past Game": False}


def test_a_calendar_provider_that_raises_does_not_break_the_page(monkeypatch):
    from romarr import metadata as module

    def boom(cfg, start, end, limit):
        raise OSError("refused")

    monkeypatch.setitem(module.CALENDAR_PROVIDERS, "rawg", boom)
    got = module.calendar([{"type": "rawg", "api_key": "k"}])
    assert got["items"] == [] and got["error"]
