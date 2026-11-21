"""ScreenScraper (looked up by hash) and SteamGridDB (covers), from RetroArr."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from romarr import retro_metadata as rm
from romarr.dat import Match, Rom
from romarr.metadata import Metadata, PROVIDERS, SECRET_FIELDS, lookup_context

SS_CFG = {"type": "screenscraper", "dev_id": "dev", "dev_password": "devpw"}

JEU = {
    "id": "1",
    "noms": [{"region": "jp", "text": "Chrono Trigger (JP)"},
             {"region": "us", "text": "Chrono Trigger"}],
    "synopsis": [{"langue": "fr", "text": "Un jeu."}, {"langue": "en", "text": "A game."}],
    "dates": [{"region": "us", "text": "1995-08-22"}],
    "genres": [{"noms": [{"langue": "en", "text": "RPG"}]}],
    "note": {"text": "18"},
    "medias": [
        {"type": "ss", "region": "wor", "url": "https://ss/shot.png"},
        {"type": "box-2D", "region": "jp", "url": "https://ss/jp.png"},
        {"type": "box-2D", "region": "us", "url": "https://ss/us.png"},
    ],
}


def verified():
    rom = Rom(name="Chrono Trigger (USA).sfc", crc="2D206BF7", md5="a2bc", sha1="DE5822F4")
    return Match(status="verified", game="Chrono Trigger (USA)", rom=rom, dat="No-Intro")


@pytest.mark.parametrize("code,body,expected", [
    (429, "", rm.QUOTA), (430, "", rm.QUOTA), (423, "", rm.QUOTA),
    (401, "", rm.AUTH), (403, "", rm.AUTH), (500, "", rm.NETWORK),
    (200, "Quota dépassé pour aujourd'hui", rm.QUOTA),
    (200, "Erreur de login : identifiant ou mot de passe", rm.AUTH),
    (200, json.dumps({"header": {"success": "true"}, "response": {
        "ssuser": {"quotarefu": "0"}}}), rm.OK),
    (200, json.dumps({"header": {"success": "false", "error": "API closed"}}), rm.QUOTA),
    (200, json.dumps({"header": {"success": "false"}}), rm.NETWORK),
    (200, "<html>", rm.NETWORK),
])
def test_screenscraper_answers_are_classified(code, body, expected):
    assert rm.classify_screenscraper(code, body) == expected


def test_the_request_sends_the_strongest_hash_and_the_system():
    context = lookup_context(verified(), "chrono.sfc", "snes")
    url = rm.screenscraper_url({**SS_CFG, "username": "me", "password": "pw"},
                               "Chrono Trigger", context)
    query = parse_qs(urlsplit(url).query)
    assert query["sha1"] == ["de5822f4"]
    assert "md5" not in query and "crc" not in query
    assert query["systemeid"] == ["4"]
    assert query["romnom"] == ["chrono.sfc"]
    assert query["ssid"] == ["me"] and query["devid"] == ["dev"]


def test_unverified_files_send_no_hashes():
    unknown = Match(status="unknown", rom=Rom(name="x", sha1="ff"))
    assert "sha1" not in lookup_context(unknown, "x.sfc", "snes")


def test_a_jeu_object_becomes_gameinfo():
    info = rm.screenscraper_game(JEU)
    assert info.title == "Chrono Trigger"
    assert info.summary == "A game."
    assert info.released == "1995-08-22"
    assert info.genres == ("RPG",)
    assert info.rating == 9.0
    assert info.cover_url == "https://ss/us.png"
    assert info.source == "ScreenScraper"


def test_media_falls_back_to_any_region_then_to_screenshots():
    assert rm.media_url([{"type": "box-2D", "region": "br", "url": "u"}], "box-2D") == "u"
    assert rm.screenscraper_game({"nom": "X", "medias": JEU["medias"][:1]}).cover_url == "https://ss/shot.png"


def test_identify_asks_screenscraper_by_hash(monkeypatch):
    seen = []

    def fetch(url, headers=None):
        seen.append(url)
        return 200, json.dumps({"header": {"success": "true"}, "response": {"jeu": JEU}})

    monkeypatch.setattr(rm, "_fetch", fetch)
    info = Metadata([SS_CFG]).identify(verification=verified(), filename="c.sfc", platform="snes")
    assert info.title == "Chrono Trigger" and info.matched_by == "dat"
    assert "sha1=de5822f4" in seen[0]


def test_quota_or_missing_credentials_answer_nothing(monkeypatch):
    monkeypatch.setattr(rm, "_fetch", lambda url, headers=None: (430, ""))
    assert not rm.screenscraper(SS_CFG, "x").found
    assert not rm.screenscraper({"dev_id": "only"}, "x").found


def test_steamgriddb_only_fills_a_missing_cover(monkeypatch):
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        if "/search/autocomplete/" in url:
            return 200, json.dumps({"success": True, "data": [{"id": 7, "name": "Chrono Trigger"}]})
        return 200, json.dumps({"success": True, "data": [{"url": "https://sgdb/grid.png"}]})

    monkeypatch.setattr(rm, "_fetch", fetch)
    bare = {"type": "fake"}
    monkeypatch.setitem(PROVIDERS, "fake", {
        "label": "Fake", "fields": (), "help": "",
        "lookup": lambda cfg, term: rm.GameInfo(title="Chrono Trigger", source="Fake")})
    info = Metadata([{"type": "steamgriddb", "api_key": "k"}, bare]).identify(filename="Chrono Trigger.sfc")
    assert info.source == "Fake"
    assert info.cover_url == "https://sgdb/grid.png"
    assert calls[0][1]["Authorization"] == "Bearer k"
    assert "/grids/game/7" in calls[1][0]


def test_steamgriddb_alone_never_names_a_game(monkeypatch):
    monkeypatch.setattr(rm, "_fetch", lambda url, headers=None: (200, json.dumps(
        {"success": True, "data": [{"id": 1, "name": "Wrong"}]})))
    assert not Metadata([{"type": "steamgriddb", "api_key": "k"}]).identify(filename="x.sfc").found


@pytest.mark.parametrize("code,body,expected", [
    (401, "", rm.AUTH), (404, "", rm.EMPTY), (502, "", rm.NETWORK),
    (200, '{"success": false}', rm.AUTH), (200, '{"success": true}', rm.OK),
    (200, "nope", rm.NETWORK),
])
def test_steamgriddb_answers_are_classified(code, body, expected):
    assert rm.classify_steamgriddb(code, body) == expected


def test_new_credentials_are_masked():
    from romarr.app import MASKED_ITEM_FIELDS
    for field in ("dev_password", "password", "api_key"):
        assert field in SECRET_FIELDS
        assert field in MASKED_ITEM_FIELDS["metadata_providers"]
    for name in ("screenscraper", "steamgriddb"):
        spec = PROVIDERS[name]
        assert spec["label"] and spec["help"] and spec["fields"]
