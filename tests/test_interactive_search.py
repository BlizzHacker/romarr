"""Interactive search: show the candidates, show the reasoning, let a human pick.

The scorer is opinionated, and every opinion in it is one somebody may disagree
with. Two wrong picks were reported by users in a single day -- a Wii Virtual
Console WAD and a Steam compilation -- and in both cases the information needed
to overrule the ranking existed inside the scorer and was thrown away.
"""

from romarr.app import ROMarr
from romarr.selection import Release, explain, judge, score
from romarr.platforms import resolve


def rel(title, *, size=512 * 1024, seeders=20, cats=(1030,),
        protocol="torrent", url="magnet:?xt=urn:btih:abc", indexer="Test"):
    return Release(title=title, size=size, seeders=seeders, categories=cats,
                   download_url=url, protocol=protocol, indexer=indexer)


def svc(tmp_path, **env):
    base = {"ROMARR_DATA": str(tmp_path / "s.json")}
    base.update(env)
    return ROMarr(base)


# --- the scorer explains itself ---------------------------------------------

def test_judge_and_score_never_disagree():
    """One implementation produces both views. A separate explain() that
    mirrored the scoring would drift, and a scorer that contradicts its own
    explanation is worse than no explanation."""
    snes = resolve("snes")
    for title, size in (("Super Metroid (USA) [!].smc", 3_000_000),
                        ("Super Metroid (J) [T+Eng]", 3_000_000),
                        ("Super Nintendo Complete Romset", 8_000_000),
                        ("Super Metroid Wii Virtual Console", 14_000_000)):
        r = rel(title, size=size)
        assert judge(r, "super metroid", snes).points == score(r, "super metroid", snes)


def test_a_rejected_release_says_which_rule_refused_it():
    snes = resolve("snes")
    why = explain(rel("Super Metroid Collection", size=8_000_000), "super metroid", snes)
    assert len(why) == 1 and "compilation" in why[0]

    why = explain(rel("Super Metroid (USA) PS2 port"), "super metroid", snes)
    assert "another platform" in why[0]


def test_an_accepted_release_shows_what_earned_its_points():
    snes = resolve("snes")
    why = explain(rel("Super Metroid (USA) [!].smc", size=3_000_000),
                  "super metroid", snes)
    joined = " ".join(why)
    assert "seeders" in joined
    assert "ROM extension" in joined
    assert "good dump" in joined


def test_reasons_are_worst_first_so_the_problem_reads_first():
    snes = resolve("snes")
    why = explain(rel("Super Metroid (J) [T+Eng]", size=3_000_000, seeders=1),
                  "super metroid", snes)
    deltas = [int(line.split()[0]) for line in why]
    assert deltas == sorted(deltas)
    assert deltas[0] < 0


# --- candidates -------------------------------------------------------------

class FakeProwlarr:
    def __init__(self, releases):
        self._releases = releases
        self._config = type("C", (), {"base_url": "http://prowlarr", "api_key": "k"})()

    def search(self, *a, **kw):
        return list(self._releases)


def test_candidates_returns_every_release_with_its_reasoning(tmp_path):
    s = svc(tmp_path)
    s.prowlarr = FakeProwlarr([
        rel("Super Metroid (USA) [!].smc", size=3_000_000),
        rel("Super Metroid Collection", size=8_000_000),
    ])
    out = s.candidates("Super Metroid", "snes")

    assert out["found"] == 2
    assert out["accepted"] == 1
    assert [i["accepted"] for i in out["items"]] == [True, False]
    assert all(i["reasons"] for i in out["items"])


def test_a_download_url_never_reaches_the_browser(tmp_path):
    """Prowlarr's downloadUrl carries its API key in the query string. It is
    handed to the download client server-side, and must not appear in a reply
    a browser can read -- which is why grabbing works by an issued id."""
    s = svc(tmp_path)
    secret = "http://prowlarr:9696/api/v1/indexer/1/download?apikey=SUPERSECRET"
    s.prowlarr = FakeProwlarr([rel("Super Metroid (USA).smc", url=secret)])

    out = s.candidates("Super Metroid", "snes")
    assert "SUPERSECRET" not in repr(out)
    assert "apikey" not in repr(out)
    assert out["items"][0]["id"]


def test_an_unknown_platform_is_refused_rather_than_scored_blind(tmp_path):
    s = svc(tmp_path)
    s.prowlarr = FakeProwlarr([])
    # `psx` was the example here until it became a supported platform. The
    # rule under test is unchanged: a name nothing recognises must be refused,
    # not scored as though no platform had been asked for.
    out = s.candidates("Astro Bot", "playstation 5")
    assert out["unknown_platform"] == "playstation 5"
    assert out["items"] == []


def test_a_failing_search_is_reported_not_raised(tmp_path):
    """A search where every source fails looks exactly like one that found
    nothing, so the difference has to be said out loud."""
    class Broken:
        _config = type("C", (), {"base_url": "http://p", "api_key": "k"})()

        def search(self, *a, **kw):
            raise RuntimeError("indexer exploded")

    s = svc(tmp_path)
    s.prowlarr = Broken()
    out = s.candidates("Super Metroid")
    assert "search failed" in out["error"]
    assert out["items"] == []


# --- grabbing by hand -------------------------------------------------------

class FakeClient:
    name = "fake"
    protocol = "torrent"
    configured = True

    def __init__(self):
        self.added = []

    def add(self, url, **kw):
        self.added.append(url)
        return True


def test_grabbing_a_chosen_release_uses_the_same_path_as_an_automatic_one(tmp_path):
    """A release chosen by hand must be queued, recorded and fulfilled exactly
    like one the scorer picked, or Activity and Wanted disagree depending on how
    the game was requested."""
    s = svc(tmp_path)
    secret = "magnet:?xt=urn:btih:CHOSEN"
    s.prowlarr = FakeProwlarr([
        rel("Super Metroid (USA) [!].smc", size=3_000_000, url=secret),
    ])
    client = FakeClient()
    s.clients = [client]

    out = s.candidates("Super Metroid", "snes")
    result = s.grab_candidate(out["items"][0]["id"])

    assert result["ok"] is True
    assert client.added == [secret]
    assert any(e["kind"] == "grabbed" and e["detail"] == "chosen by hand"
               for e in s.store.history())
    assert s.queue and s.queue[-1].state == "grabbed"


def test_a_rejected_release_can_still_be_grabbed_by_hand(tmp_path):
    """The whole point. The scorer refused it; the human overrules."""
    s = svc(tmp_path)
    s.prowlarr = FakeProwlarr([rel("Super Metroid Collection", size=8_000_000)])
    s.clients = [FakeClient()]

    out = s.candidates("Super Metroid", "snes")
    assert out["items"][0]["accepted"] is False
    assert s.grab_candidate(out["items"][0]["id"])["ok"] is True


def test_an_expired_search_says_so_rather_than_failing(tmp_path):
    s = svc(tmp_path)
    assert s.grab_candidate("nosuchid")["ok"] is False
    assert "expired" in s.grab_candidate("nosuchid")["error"]


def test_only_a_bounded_number_of_searches_stays_grabbable(tmp_path):
    """This is a handle for a button somebody is looking at, not a cache."""
    s = svc(tmp_path)
    s.prowlarr = FakeProwlarr([rel("Super Metroid (USA).smc")])
    for n in range(s.CANDIDATE_SEARCHES + 4):
        s.candidates(f"Game {n}", "snes")
    assert len(s._candidates) == s.CANDIDATE_SEARCHES


def test_the_search_page_is_reachable_in_the_ui():
    from romarr.ui import page
    html = page()
    assert 'data-page="search"' in html
    assert "RENDER.search" in html
    assert "/api/v1/release" in html
