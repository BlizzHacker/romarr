"""MiNERVA Archive as a first-class indexer.

The driver talks to minerva-archive.org's live search API -- the
Myrient continuation's `/v1/api/rom/search` with its filter vocabulary
(group/console/region/tokenised) -- and turns collection-torrent rows
into releases. Verified against the live site: results carry no sizes
and no seeders, and one magnet covers a whole dump group, so rows
sharing an `so_id` collapse to one release and results report as
private with a nominal seeder (0 would read as "dead" to the scorer).
"""

import json

import pytest
import requests

from romarr.indexers import (INDEXER_TYPES, Minerva, MinervaConfig,
                             build_indexer, driver_for)

BASE = "https://minerva.example"

ROW_A = {"id": 823208, "so_id": 29812,
         "full_path": "./TOSEC/Nintendo/Super Famicom/Games/"
                      "Chrono Trigger (1995)(Square)(US).zip",
         "magnet": "magnet:?xt=urn:btih:aaaa&dn=Minerva_Myrient"}
ROW_A_DUP = {"id": 823244, "so_id": 29812,   # same torrent, another row
             "full_path": "./TOSEC/Nintendo/Super Famicom/Games/"
                          "Chrono Trigger (1995)(Square)(US)[a8].zip",
             "magnet": "magnet:?xt=urn:btih:aaaa&dn=Minerva_Myrient"}
ROW_B = {"id": 900001, "so_id": 30001,
         "full_path": "./No-Intro/Nintendo 64/Games/"
                      "Super Mario 64 (USA).z64",
         "magnet": "magnet:?xt=urn:btih:bbbb&dn=Minerva_Myrient"}


class Session:
    """Records requests; answers from a canned map of path -> payload."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        path = url.split("/v1/api/rom/", 1)[-1]
        payload = self.answers.get(path)
        resp = requests.Response()
        if callable(payload):
            payload = payload(dict(params or {}))
        resp.status_code = 200
        resp._content = json.dumps(payload).encode()
        return resp


def client(answers, **cfg):
    session = Session(answers)
    return Minerva(MinervaConfig(base_url=BASE, **cfg), session=session), session


# -- registration ---------------------------------------------------------------

def test_minerva_is_a_registered_indexer_type():
    assert driver_for("minerva") == "minerva"
    spec = INDEXER_TYPES["minerva"]
    assert spec["protocol"] == "torrent"
    fields = {f["name"] for f in spec["fields"]}
    assert {"url", "group", "console", "region", "tokenised"} <= fields


def test_build_indexer_makes_a_minerva_client_from_a_stored_row():
    idx = build_indexer({"type": "minerva", "name": "MiNERVA",
                         "url": BASE, "group": "TOSEC", "enable": True})
    assert isinstance(idx, Minerva)
    assert idx._config.group == "TOSEC"


# -- search ---------------------------------------------------------------------

def test_search_returns_one_release_per_collection_torrent():
    c, s = client({"search": [ROW_A, ROW_A_DUP, ROW_B]})
    out = c.search("chrono trigger")
    assert [r.title for r in out] == [
        "Chrono Trigger (1995)(Square)(US).zip",
        "Super Mario 64 (USA).z64"]
    assert out[0].download_url.startswith("magnet:?xt=urn:btih:aaaa")
    assert out[0].private is True
    assert out[0].seeders == 1, "unknown seeder count must not read as dead"
    assert out[0].info_url == f"{BASE}/rom?id=823208"


def test_the_query_is_lowercased_like_the_site_does():
    c, s = client({"search": []})
    c.search("Chrono Trigger")
    assert s.calls[0][1]["query"] == "chrono trigger"


def test_filters_are_passed_through_and_blanks_are_omitted():
    c, s = client({"search": []}, group="TOSEC", console="Nintendo 64",
                  region="USA", tokenised=True)
    c.search("mario")
    params = s.calls[0][1]
    assert params["group"] == "TOSEC"
    assert params["console"] == "Nintendo 64"
    assert params["region"] == "USA"
    assert params["tokenised"] == "true"
    # an unset filter is not sent at all (None/"" filtered in _api)
    c2, s2 = client({"search": []})
    c2.search("mario")
    assert "group" not in s2.calls[0][1]


def test_pagination_walks_full_pages_and_stops_on_a_short_one():
    def rows(start, count):
        return [{"id": start + i, "so_id": start + i,
                 "full_path": f"./x/g{start + i}.zip",
                 "magnet": f"magnet:?xt=urn:btih:{(start + i):040x}"}
                for i in range(count)]

    pages = {"n": 0}

    def search(params):
        pages["n"] += 1
        if pages["n"] == 1:
            return rows(0, 100)            # full page -> fetch the next
        return rows(1000, 3)               # short page, new torrents -> stop

    c, s = client({"search": search}, )
    c._config.max_pages = 5
    out = c.search("anything", limit=200)
    assert len(out) == 103
    assert [call[1]["offset"] for call in s.calls] == [0, 100]


def test_rows_sharing_a_magnet_collapse_even_with_different_so_ids():
    # The live archive's [a8]/[a9] alternates: one torrent, two so_ids.
    c, _ = client({"search": [ROW_A, dict(ROW_A_DUP, so_id=99999), ROW_B]})
    out = c.search("chrono trigger")
    assert len(out) == 2


def test_max_pages_caps_the_walk():
    full = [{"id": i, "so_id": i, "full_path": f"./x/g{i}.zip",
             "magnet": f"magnet:?xt=urn:btih:{i:040x}"} for i in range(100)]
    c, s = client({"search": full})
    out = c.search("anything")             # default max_pages=1
    assert len(out) == 100
    assert len(s.calls) == 1


def test_rows_without_a_magnet_or_path_are_dropped():
    c, _ = client({"search": [
        {"id": 1, "so_id": 1, "full_path": "", "magnet": ROW_A["magnet"]},
        {"id": 2, "so_id": 2, "full_path": "./x/g.zip", "magnet": ""},
        {"id": 3, "so_id": 3, "full_path": "./x/g.zip",
         "magnet": "http://not-a-magnet"},
        "junk", None,
        ROW_B,
    ]})
    out = c.search("mario")
    assert [r.title for r in out] == ["Super Mario 64 (USA).z64"]


def test_an_empty_term_never_hits_the_network():
    c, s = client({"search": []})
    assert c.search("") == []
    assert s.calls == []


# -- caps -------------------------------------------------------------------------

def test_caps_accepts_the_filters_shape():
    c, s = client({"filters": {"groups": ["TOSEC"], "consoles": [],
                               "regions": [], "manufacturers": []}})
    assert c.caps() is True
    assert s.calls[0][0].endswith("/v1/api/rom/filters")


def test_caps_rejects_a_json_body_that_is_not_minerva():
    # Some captive portal answering {"ok": true} must not pass as MiNERVA.
    c, _ = client({"filters": {"ok": True}})
    assert c.caps() is False


# -- the Cloudflare reality ----------------------------------------------------------

def test_a_real_session_identifies_as_a_browser_class_client():
    # The live site answers urllib's default UA with a 403/1010 challenge.
    # Verified against minerva-archive.org; this locks the workaround in.
    c = Minerva(MinervaConfig(base_url=BASE))
    ua = c._session.headers.get("User-Agent", "")
    assert ua.startswith("Mozilla/5.0")
