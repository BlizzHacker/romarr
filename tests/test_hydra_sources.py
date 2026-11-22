"""Hydra Launcher sources as an indexer, and file-host routing."""
from __future__ import annotations

import json

import pytest

from romarr import hydra
from romarr.downloaders import AllDebrid, DebridConfig, RealDebrid, RealDebridConfig, pick_client
from romarr.hosters import HosterRoute, host_label, is_file_host, looks_like_file, public_file_url
from romarr.hydra import HydraSource, HydraSourceConfig, parse_size, parse_source, platform_slug
from romarr.indexers import INDEXER_TYPES, build_indexer
from romarr.platforms import resolve
from romarr.selection import best_release, infer_platform, judge

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
DOC = {
    "name": "Test source",
    "downloads": [
        {"title": "Ookami (USA)", "platform": "ps2", "fileSize": "4.1 GB",
         "uris": ["https://archive.org/download/PS2/Ookami%20%28USA%29.zip"]},
        {"title": "Ookami (USA)", "platform": "ps3", "fileSize": "8 GB",
         "uris": ["https://archive.org/download/PS3/Ookami%20HD%20%28USA%29.zip"]},
        {"title": "Zelda &amp; Friends [NSP]", "platform": "ns", "fileSize": "13.2 GB",
         "uris": [MAGNET]},
        {"title": "Hades Free Download (Build 1)", "fileSize": "15 GB",
         "uris": ["https://www.filecrypt.cc/Container/X.html",
                  "https://gofile.io/d/abc", "https://pixeldrain.com/u/xyz", MAGNET]},
        {"title": "No links", "uris": []},
        {"title": "", "uris": [MAGNET]},
        "not a row",
    ],
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    hydra.clear_cache()
    yield
    hydra.clear_cache()


# -- parsing ------------------------------------------------------------------

@pytest.mark.parametrize("code, slug", [
    ("ps2", "ps2"), ("ns", "switch"), ("ns2", "switch-2"), ("gc", "ngc"),
    ("psv", "psvita"), ("ps1", "psx"), ("x360", "xbox360"), ("MAME", "arcade"),
    ("GB/GBC", ""), ("Sega Mega Drive/Genesis", "genesis-slash-megadrive"),
    ("", ""), ("not a console", ""),
])
def test_platform_codes_map_to_romarr_slugs(code, slug):
    assert platform_slug(code) == slug


@pytest.mark.parametrize("text, size", [
    ("1.79 GB", int(1.79 * 1024 ** 3)), ("752 MB", 752 * 1024 ** 2),
    ("From 3,5 GB", int(3.5 * 1024 ** 3)), (2048, 2048), ("", 0), (None, 0), ("big", 0),
])
def test_sizes(text, size):
    assert parse_size(text) == size


def test_parse_keeps_usable_rows_and_reads_links_best_first():
    rows = parse_source(DOC, indexer="Hydra")
    assert [r.title for r in rows] == ["Ookami (USA)", "Ookami (USA)",
                                       "Zelda & Friends [NSP]", "Hades Free Download (Build 1)"]
    ps2, ps3, zelda, hades = rows
    assert (ps2.protocol, ps2.platform_hint, ps2.categories) == ("direct", "ps2", (1000,))
    assert zelda.protocol == "torrent" and zelda.private and zelda.seeders == 1
    assert zelda.platform_hint == "switch"
    # The magnet wins; file-host links ride along as mirrors (containers last).
    assert hades.protocol == "torrent" and hades.download_url == MAGNET
    assert hades.categories == (4050,) and hades.platform_hint == ""
    assert hades.mirrors == ("https://gofile.io/d/abc", "https://pixeldrain.com/u/xyz",
                             "https://www.filecrypt.cc/Container/X.html") or \
        set(hades.mirrors) == {"https://gofile.io/d/abc", "https://pixeldrain.com/u/xyz",
                               "https://www.filecrypt.cc/Container/X.html"}


def test_operator_categories_win():
    rows = parse_source(DOC, categories=(1080,))
    assert {r.categories for r in rows} == {(1080,)}


def test_a_list_document_is_read_too():
    assert len(parse_source(DOC["downloads"])) == 4


# -- the indexer ---------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes, length=None):
        self._body = body
        self.headers = {"Content-Length": str(length if length is not None else len(body))}

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        pass


class _Session:
    def __init__(self, body: bytes, length=None):
        self.calls = 0
        self._body, self._length = body, length

    def get(self, url, **kw):
        self.calls += 1
        return _Response(self._body, self._length)


def _source(body=None, **kw):
    session = _Session(json.dumps(DOC if body is None else body).encode(), **kw)
    return HydraSource(HydraSourceConfig(url="https://example.org/s.json", name="S"),
                       session=session), session


def test_search_filters_here_and_reads_the_source_once_an_hour():
    src, session = _source()
    assert [r.title for r in src.search("Ookami")] == ["Ookami (USA)", "Ookami (USA)"]
    assert src.search("zelda friends")[0].protocol == "torrent"
    assert src.search("nothing like this") == []
    assert session.calls == 1


def test_an_oversized_source_is_refused():
    src, _ = _source(length=hydra.MAX_SOURCE_BYTES + 1)
    with pytest.raises(ValueError, match="larger than"):
        src.releases()


def test_a_page_that_is_not_json_says_so():
    src = HydraSource(HydraSourceConfig(url="https://example.org/s.json", name="S"),
                      session=_Session(b"<html>blocked</html>"))
    with pytest.raises(ValueError, match="not JSON"):
        src.releases()


def test_caps_fails_on_a_source_with_nothing_usable():
    src, _ = _source(body={"downloads": [{"title": "x", "uris": []}]})
    with pytest.raises(ValueError, match="no downloads"):
        src.caps()


def test_the_hydra_indexer_type_is_registered_and_built():
    spec = INDEXER_TYPES["hydra"]
    assert spec["driver"] == "hydra"
    assert {f["name"] for f in spec["fields"]} >= {"name", "url", "enable", "categories"}
    built = build_indexer({"type": "hydra", "url": "https://example.org/s.json", "name": "S"})
    assert isinstance(built, HydraSource) and built.name == "S"


# -- scoring --------------------------------------------------------------------

def test_the_source_platform_is_evidence_and_refuses_other_platforms():
    ps2_row, ps3_row = parse_source(DOC)[:2]
    ps2 = resolve("ps2")
    assert judge(ps2_row, "Ookami", ps2).points > 0
    assert any("files it under" in w for w in judge(ps2_row, "Ookami", ps2).why())
    refused = judge(ps3_row, "Ookami", ps2)
    assert refused.points < 0 and "another platform" in refused.verdict
    assert best_release([ps3_row, ps2_row], "Ookami", ps2) is ps2_row
    assert infer_platform(ps3_row).slug == "ps3"


# -- file hosts ----------------------------------------------------------------------

def test_host_classification():
    assert host_label("https://www.1fichier.com/?x") == "1fichier"
    assert host_label("https://bzzhr.to/abc") == "BuzzHeavier"
    assert is_file_host("https://gofile.io/d/abc")
    assert not is_file_host("https://archive.org/download/x.zip")
    assert looks_like_file("https://archive.org/download/x/Game%20(USA).zip")
    assert looks_like_file("https://fileditchfiles.st/abc/file")
    assert not looks_like_file("https://example.org/game-page/")
    assert public_file_url("https://pixeldrain.com/u/xyz") == "https://pixeldrain.com/api/file/xyz?download"
    assert public_file_url("https://pixeldrain.com/l/lst") == "https://pixeldrain.com/api/list/lst/zip"
    assert public_file_url("https://gofile.io/d/abc") == ""


class _Client:
    def __init__(self, protocol, *, takes=False, ok=True, hosts=None, rid="c"):
        self.protocol, self.takes_hoster_links, self._ok = protocol, takes, ok
        self.TAKES_NAME, self.configured, self.romarr_id = True, True, rid
        self.last_job_id, self.calls, self._hosts = "", [], hosts

    def add(self, url, *, name=""):
        self.calls.append((url, name))
        return self._ok

    def supports_host(self, host):
        return self._hosts is None or host in self._hosts


def test_pixeldrain_goes_to_the_direct_client_as_a_file():
    direct = _Client("direct", rid="mdm")
    route = HosterRoute(direct=direct)
    assert route.add("https://pixeldrain.com/u/xyz", name="Hades")
    assert direct.calls == [("https://pixeldrain.com/api/file/xyz?download", "Hades")]
    assert route.romarr_id == "mdm"


def test_a_host_without_a_public_api_goes_to_debrid_and_containers_are_skipped():
    direct = _Client("direct", rid="mdm")
    debrid = _Client("torrent", takes=True, rid="rd")
    route = HosterRoute(direct=direct, debrid=[debrid])
    assert route.add("https://www.filecrypt.cc/Container/X.html", name="Hades",
                     mirrors=("https://gofile.io/d/abc",))
    assert debrid.calls == [("https://gofile.io/d/abc", "Hades")] and direct.calls == []
    assert route.romarr_id == "rd"


def test_mirrors_fall_through_when_debrid_cannot_unlock_a_host():
    debrid = _Client("torrent", takes=True, hosts={"rapidgator.net"}, rid="rd")
    route = HosterRoute(debrid=[debrid])
    assert route.add("https://gofile.io/d/abc", name="G", mirrors=("https://rapidgator.net/file/1",))
    assert debrid.calls == [("https://rapidgator.net/file/1", "G")]


def test_no_route_explains_what_to_add():
    route = HosterRoute(debrid=[_Client("torrent", takes=True, hosts=set())])
    assert not route.add("https://gofile.io/d/abc", name="G")
    assert "Gofile" in route.last_error and "direct download client" in route.last_error


def test_pick_client_builds_the_route_only_when_something_can_take_links():
    assert pick_client("hoster", [_Client("torrent")]) is None
    assert isinstance(pick_client("hoster", [_Client("direct")]), HosterRoute)
    assert isinstance(pick_client("hoster", [_Client("torrent", takes=True)]), HosterRoute)


# -- debrid file-host jobs -------------------------------------------------------------

def test_real_debrid_records_a_file_host_link_and_finishes_it_without_asking_the_api(tmp_path, monkeypatch):
    rd = RealDebrid(RealDebridConfig(api_token="t", save_path=str(tmp_path)))
    monkeypatch.setattr(rd, "_get", lambda *a, **k: pytest.fail("no API call for a link job"))
    assert rd.add("https://1fichier.com/?abc", name="Game (USA)")
    assert rd._finished() == [("Game (USA)", ["https://1fichier.com/?abc"], "")]


def test_real_debrid_reads_its_host_list_once(tmp_path, monkeypatch):
    rd = RealDebrid(RealDebridConfig(api_token="t", save_path=str(tmp_path)))
    calls = []
    monkeypatch.setattr(rd, "_get", lambda path, *a, **k: calls.append(path) or ["1fichier.com", "gofile.io"])
    assert rd.supports_host("www.1fichier.com") and not rd.supports_host("example.org")
    assert calls == ["/hosts/domains"]


def test_alldebrid_records_a_file_host_link(tmp_path):
    ad = AllDebrid(DebridConfig(api_token="t", save_path=str(tmp_path)))
    assert ad.add("https://gofile.io/d/abc", name="Game")
    assert ad._hoster_jobs() == [("Game", ["https://gofile.io/d/abc"])]


def test_a_source_title_cannot_leave_the_download_folder(tmp_path):
    rd = RealDebrid(RealDebridConfig(api_token="t", save_path=str(tmp_path)))
    assert rd.add("https://1fichier.com/?abc", name="../../Ookami (USA)")
    (name, links, _), = rd._finished()
    assert ".." not in name and "/" not in name and name.endswith("Ookami (USA)")


def test_an_unwritable_ledger_fails_the_grab(tmp_path, monkeypatch):
    rd = RealDebrid(RealDebridConfig(api_token="t", save_path=str(tmp_path)))
    monkeypatch.setattr(rd, "_remember", lambda *a, **k: None)
    assert rd.add("https://1fichier.com/?abc", name="Game") is False


def test_a_dead_link_falls_through_to_the_next_mirror():
    class Debrid(_Client):
        def link_available(self, url):
            return "dead" not in url
    debrid = Debrid("torrent", takes=True, rid="rd")
    route = HosterRoute(debrid=[debrid])
    assert route.add("https://1fichier.com/?dead", name="G", mirrors=("https://gofile.io/d/alive",))
    assert debrid.calls == [("https://gofile.io/d/alive", "G")]


def test_a_two_machine_tag_gives_no_exclusive_platform():
    rows = parse_source({"downloads": [
        {"title": "Pokemon - Crystal Version (USA, Europe)", "platform": "GB/GBC",
         "uris": ["https://archive.org/download/x/Pokemon%20Crystal.gbc"]}]})
    assert rows[0].platform_hint == ""
    gbc = resolve("gbc")
    assert judge(rows[0], "Pokemon - Crystal Version", gbc).verdict == "" or \
        "another platform" not in judge(rows[0], "Pokemon - Crystal Version", gbc).verdict


def test_search_folds_accents_both_ways():
    src, _ = _source(body={"downloads": [
        {"title": "Pokemon Emerald (USA)", "uris": [MAGNET]},
        {"title": "Pokémon Ruby (USA)", "uris": [MAGNET]}]})
    assert [r.title for r in src.search("Pokémon Emerald")] == ["Pokemon Emerald (USA)"]
    assert [r.title for r in src.search("Pokemon Ruby")] == ["Pokémon Ruby (USA)"]


def test_an_edited_indexer_reparses_without_refetching():
    session = _Session(json.dumps(DOC).encode())
    first = HydraSource(HydraSourceConfig(url="https://example.org/s.json", name="Old"), session=session)
    assert {r.indexer for r in first.releases()} == {"Old"}
    edited = HydraSource(HydraSourceConfig(url="https://example.org/s.json", name="New",
                                           categories=(1080,)), session=session)
    rows = edited.releases()
    assert {r.indexer for r in rows} == {"New"} and {r.categories for r in rows} == {(1080,)}
    assert session.calls == 1


def test_an_empty_query_returns_rows_for_rss_sync():
    src, _ = _source()
    assert len(src.search("", limit=2)) == 2


def test_a_magnet_with_no_torrent_client_is_fetched_from_a_mirror(tmp_path):
    from romarr.app import ROMarr
    from romarr.selection import Release
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    direct = _Client("direct", rid="mdm")
    direct.name = "Mister Download Manager"
    svc.clients = [direct]
    pick = Release(title="Hades Free Download (Build 1)", size=1, seeders=1, categories=(4050,),
                   download_url=MAGNET, protocol="torrent",
                   mirrors=("https://pixeldrain.com/u/xyz",))
    out = svc._grab_impl(pick, "Hades", "win")
    assert out.get("ok"), out
    assert direct.calls == [("https://pixeldrain.com/api/file/xyz?download", "Hades Free Download (Build 1)")]
