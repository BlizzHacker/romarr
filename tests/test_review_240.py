"""Regressions for the PR #240 review: guards, editions, NZB links, Test buttons."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler
from romarr.game_packages import held_parts, release_parts
from romarr.indexers import Torznab, TorznabConfig
from romarr.metadata import PROVIDERS
from romarr.platforms import resolve
from romarr.release_guards import demo_refusal
from romarr.selection import Release, best_release, judge


def _rel(title, seeders=10, size=4 * 1024 ** 2, cats=(1000,)):
    return Release(title=title, size=size, seeders=seeders, categories=cats,
                   download_url="magnet:?xt=urn:btih:" + "b" * 40, protocol="torrent")


# -- demo and trial ------------------------------------------------------------

def test_a_demo_with_a_rom_extension_is_refused_and_loses_to_the_full_game():
    assert demo_refusal("Super Metroid (USA) Demo.smc", "Super Metroid")
    assert demo_refusal("Super Metroid (USA).smc", "Super Metroid") == ""
    snes = resolve("snes")
    demo = _rel("Super Metroid (USA) Demo.smc", seeders=50)
    full = _rel("Super Metroid (USA).smc", seeders=2)
    assert judge(demo, "Super Metroid", snes).points < 0
    assert best_release([demo, full], "Super Metroid", snes) is full


def test_an_explicit_trial_tag_is_refused_even_when_the_name_says_trial():
    assert demo_refusal("Trial of Mana (USA) (Trial).nsp", "Trial of Mana")
    assert demo_refusal("TRIAL.OF.MANA.USA.NSW-GRP", "Trial of Mana") == ""
    assert demo_refusal("Trial of Mana (USA).nsp", "Trial of Mana") == ""


# -- complete editions -----------------------------------------------------------

def test_an_edition_named_in_the_request_is_matched_after_normalising():
    assert release_parts("Game_Gold_Edition", "Game_Gold_Edition") == frozenset({"base"})
    assert release_parts("Game.Gold.Edition", "Game Gold Edition") == frozenset({"base"})
    # Whole words: "GOTYcoon" does not name a GOTY edition.
    assert release_parts("GOTYcoon GOTY", "GOTYcoon") == frozenset({"base", "dlc"})


def test_an_encoded_update_stays_an_update():
    assert release_parts("Game.Gold.Edition.A0100-V0200.pkg", "Game") == frozenset({"update"})


def test_a_title_id_beats_edition_inference():
    assert held_parts("Game Complete Edition [0004000E001B4000]", "Game", "3ds") == frozenset({"update"})
    assert release_parts("Game Complete Edition", "Game") == frozenset({"base", "dlc"})


def test_frozen_ranking_of_edition_releases_for_a_switch_request():
    switch = resolve("switch")
    titles = [
        "Hollow Knight Complete Edition [0100633007D48000][v0] (2.1 GB) (NSP)",
        "Hollow Knight [0100633007D48000][v0] (1.9 GB) (NSP)",
        "Hollow Knight Update v1.5.78 [0100633007D48800][v393216] (NSP)",
        "Hollow Knight + Celeste [NSP]",
    ]
    scored = {t: judge(_rel(t, size=2 * 1024 ** 3), "Hollow Knight", switch).points for t in titles}
    ranked = [t for t in sorted(scored, key=lambda t: -scored[t]) if scored[t] > 0]
    assert ranked[:2] == [titles[0], titles[1]], scored
    assert scored[titles[2]] < 0, "an update alone is not the game"
    assert titles[3] not in ranked[:2], "a two-game compilation never outranks the game"


# -- NZB links in Torznab feeds ---------------------------------------------------

FEED = """<?xml version="1.0"?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Ratchet Clank (USA)</title>
<enclosure url="https://idx.example/get/abc" type="application/x-nzb" length="1000"/>
<torznab:attr name="magneturl" value="magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc"/></item>
<item><title>Chrono Trigger (USA)</title>
<enclosure url="https://idx.example/dl/9.torrent" type="application/x-bittorrent" length="1"/>
<torznab:attr name="magneturl" value="magnet:?xt=urn:btih:dddddddddddddddddddddddddddddddddddddddd"/></item>
</channel></rss>"""


def test_a_usenet_release_keeps_its_nzb_link_and_a_torrent_keeps_its_magnet():
    rows = {r.title: r for r in Torznab(TorznabConfig("https://idx.example/api", "k")).parse(FEED)}
    nzb, torrent = rows["Ratchet Clank (USA)"], rows["Chrono Trigger (USA)"]
    assert nzb.protocol == "usenet" and nzb.download_url == "https://idx.example/get/abc"
    assert torrent.protocol == "torrent" and torrent.download_url.startswith("magnet:")


# -- Test buttons and the lookup route -------------------------------------------------

@pytest.fixture
def server(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET")
    req.add_header("X-Api-Key", "k")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_testing_a_saved_webhook_signs_with_the_stored_secret(server, monkeypatch):
    base, svc = server
    saved = svc.store.put_item("connections", {"type": "webhook", "name": "hook",
                                               "url": "https://hook.example/x",
                                               "secret": "s" * 24})
    seen = {}
    monkeypatch.setattr("romarr.app.send_test", lambda cfg: seen.update(cfg) or (True, "ok"))
    code, out = call(base, "/api/v1/connection/test",
                     {"id": saved["id"], "type": "webhook", "url": "********", "secret": "********"})
    assert code == 200 and out["ok"]
    assert seen["secret"] == "s" * 24 and seen["url"] == "https://hook.example/x"


def test_testing_a_saved_screenscraper_restores_its_developer_id(server, monkeypatch):
    base, svc = server
    saved = svc.store.put_item("metadata_providers", {
        "type": "screenscraper", "dev_id": "devname", "dev_password": "devpw",
        "username": "u", "password": "p"})
    seen = {}

    def lookup(cfg, *a, **k):
        seen.update(cfg)
        return type("Info", (), {"found": False, "title": ""})()

    monkeypatch.setitem(PROVIDERS["screenscraper"], "lookup", lookup)
    call(base, "/api/v1/metadataprovider/test", {"id": saved["id"], "type": "screenscraper"})
    assert seen["dev_id"] == "devname" and seen["dev_password"] == "devpw"
    # A value the form sent is kept as sent.
    call(base, "/api/v1/metadataprovider/test",
         {"id": saved["id"], "type": "screenscraper", "dev_id": "other"})
    assert seen["dev_id"] == "other"


def test_the_lookup_route_hands_verified_hashes_to_the_metadata_lookup(server, monkeypatch):
    base, svc = server
    seen = {}

    def identify(filename="", verification=None, platform=""):
        seen.update(filename=filename, platform=platform,
                    crc=getattr(getattr(verification, "rom", None), "crc", None),
                    status=getattr(verification, "status", None))
        return {"found": False}

    monkeypatch.setattr(svc, "identify", identify)
    code, _ = call(base, "/api/v1/metadata/lookup?filename=Super%20Metroid.sfc&crc=D63ED5F8&platform=snes")
    assert code == 200
    assert seen == {"filename": "Super Metroid.sfc", "platform": "snes",
                    "crc": "d63ed5f8", "status": "verified"}
    assert call(base, "/api/v1/metadata/lookup?filename=x&md5=nothex")[0] == 400
