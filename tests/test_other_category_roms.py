"""General trackers file ROMs under Newznab "Other"; ROMarr finds them, but only on evidence."""
from romarr import indexers
from romarr.indexers import Prowlarr, ProwlarrConfig
from romarr.platforms import by_slug
from romarr.selection import Release, judge

SNES = by_slug("snes")


def _release(title, cats=(8000,)):
    return Release(title=title, size=4_000_000, seeders=12, categories=tuple(cats),
                   download_url="magnet:?xt=urn:btih:" + "ab" * 20, protocol="torrent",
                   indexer="BitSearch")


def test_a_no_intro_name_filed_under_other_is_a_game():
    j = judge(_release("Chrono Trigger (USA)"), "Chrono Trigger", SNES)
    assert j.points > 0, j.verdict


def test_a_rom_extension_filed_under_other_is_a_game():
    j = judge(_release("Chrono Trigger.sfc", cats=(8010,)), "Chrono Trigger", SNES)
    assert j.points > 0, j.verdict


def test_an_other_release_without_rom_evidence_is_still_refused():
    j = judge(_release("Chrono Trigger OST FLAC"), "Chrono Trigger", SNES)
    assert j.points < 0 and "wrong category" in j.verdict


def test_an_other_release_is_never_taken_without_a_platform():
    j = judge(_release("Chrono Trigger (USA)"), "Chrono Trigger", None)
    assert j.points < 0


def test_a_film_category_is_still_refused():
    j = judge(_release("Chrono Trigger (USA)", cats=(2000,)), "Chrono Trigger", SNES)
    assert j.points < 0


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        if "/indexer" in url:
            return _Resp([])
        if "categories=8000" in url:
            return _Resp([{"title": "Chrono Trigger (USA)", "guid": "b", "indexerId": 59,
                           "categories": [{"id": 8000}]},
                          {"title": "dup", "guid": "a", "categories": [{"id": 8000}]}])
        return _Resp([{"title": "Chrono Trigger (USA) [SNES]", "guid": "a",
                       "categories": [{"id": 1000}]}])


def test_prowlarr_asks_for_other_separately_and_merges_without_duplicates():
    session = _Session()
    client = Prowlarr(ProwlarrConfig(base_url="http://p:9696", api_key="k"), session)
    titles = [r.title for r in client.search("Chrono Trigger")]
    searches = [u for u in session.urls if "/search" in u]
    assert len(searches) == 2
    assert sorted("categories=8000" in u for u in searches) == [False, True]
    assert any("categories=1000" in u for u in searches)
    assert titles == ["Chrono Trigger (USA) [SNES]", "Chrono Trigger (USA)"]


def test_a_failed_other_search_keeps_the_game_results(monkeypatch):
    session = _Session()
    client = Prowlarr(ProwlarrConfig(base_url="http://p:9696", api_key="k"), session)
    real = client._search_rows

    def flaky(term, cats, limit):
        if cats == indexers.OTHER_SEARCH_CATEGORIES:
            raise indexers.requests.ConnectionError("boom")
        return real(term, cats, limit)

    monkeypatch.setattr(client, "_search_rows", flaky)
    assert [r.title for r in client.search("Chrono Trigger")] == ["Chrono Trigger (USA) [SNES]"]


WIN = by_slug("win")
NDS = by_slug("nds")


def test_a_soundtrack_naming_the_platform_is_refused():
    """"Chrono Trigger DS OST ... [FLAC]" names DS, and is not a game."""
    j = judge(_release("Chrono Trigger DS OST Orchestra Extra Soundtrack [FLAC]"), "Chrono Trigger", NDS)
    assert not j.accepted, j.why()


def test_ai_art_named_after_a_game_is_refused():
    j = judge(_release("[Dots] Ayla (Chrono Trigger) [AI Generated].zip"), "Chrono Trigger", NDS)
    assert not j.accepted


def test_a_ds_no_intro_release_under_other_is_accepted():
    j = judge(_release("Chrono Trigger (USA) (En,Fr)"), "Chrono Trigger", NDS)
    assert j.accepted, j.why()


def test_a_pc_game_under_other_with_a_version_string_is_a_game():
    j = judge(_release("Hollow Knight Silksong v1.0.29909b.rar"), "Hollow Knight Silksong", WIN)
    assert j.accepted, j.why()


def test_a_gog_pc_game_under_generic_pc_is_a_game():
    j = judge(_release("Hollow Knight GOG", cats=(4000,)), "Hollow Knight", WIN)
    assert j.accepted, j.why()


def test_a_bare_name_under_generic_pc_is_still_refused():
    j = judge(_release("Hollow Knight", cats=(4000,)), "Hollow Knight", WIN)
    assert not j.accepted


def test_generic_pc_is_never_a_console_game():
    j = judge(_release("Chrono Trigger (USA)", cats=(4000,)), "Chrono Trigger", NDS)
    assert not j.accepted


def test_a_bare_dotted_build_number_is_pc_evidence():
    j = judge(_release("Hollow_Knight_1.5.78.11833_(50885)_win", cats=(4000,)), "Hollow Knight", WIN)
    assert j.accepted, j.why()


def test_a_linux_build_is_not_a_windows_game():
    j = judge(_release("hollow-knight-linux-gog-phoenix-games-lab", cats=(8010,)), "Hollow Knight", WIN)
    assert not j.accepted


def test_the_other_query_waits_for_the_game_query():
    """Concurrent fan-outs queue inside Prowlarr and the Other one timed out."""
    order = []

    class Ordered(_Session):
        def get(self, url, **kw):
            if "/search" in url:
                order.append("other" if "categories=8000" in url else "game")
            return super().get(url, **kw)

    Prowlarr(ProwlarrConfig(base_url="http://p:9696", api_key="k"), Ordered()).search("Chrono Trigger")
    assert order == ["game", "other"]
