"""The clock's jobs, exercised through the service."""

from datetime import datetime, timedelta, timezone

from romarr.app import ROMarr
from romarr.selection import Release


def svc(tmp_path, **env):
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"), **env})


def rel(title, **kw):
    defaults = dict(size=512 * 1024, seeders=25, categories=(1030,),
                    download_url="magnet:?xt=urn:btih:abc", protocol="torrent")
    defaults.update(kw)
    return Release(title=title, **defaults)


def test_the_service_registers_its_jobs(tmp_path):
    s = svc(tmp_path)
    names = {j["name"] for j in s.scheduler.status()}
    assert names == {"ImportCompleted", "MissingGameSearch", "RssSync",
                     "ListSync", "UpdateCheck", "HashIndex"}


def test_turning_off_auto_import_disables_the_job_live(tmp_path):
    s = svc(tmp_path)
    jobs = {j["name"]: j for j in s.scheduler.status()}
    assert jobs["ImportCompleted"]["enabled"]
    s.store.update_settings({"auto_import": False})
    jobs = {j["name"]: j for j in s.scheduler.status()}
    assert not jobs["ImportCompleted"]["enabled"]


def test_auto_search_honours_backoff_and_stamps(tmp_path, monkeypatch):
    s = svc(tmp_path)
    monkeypatch.setattr(s, "request", lambda g, p: {"ok": False})
    s.store.want("Chrono Trigger", "snes")          # never searched: due
    fresh = s.store.want("Earthbound", "snes")      # just searched: waiting
    fresh.attempts = 2
    fresh.searched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    out = s.search_missing(auto=True)
    assert out["searched"] == 1
    assert out["skipped"] == 1
    rows = {w["game"]: w for w in s.store.missing()}
    assert rows["Chrono Trigger"]["searched_at"], "the sweep stamps what it searched"


def test_manual_search_ignores_backoff(tmp_path, monkeypatch):
    s = svc(tmp_path)
    monkeypatch.setattr(s, "request", lambda g, p: {"ok": True})
    item = s.store.want("Earthbound", "snes")
    item.attempts = 5
    item.searched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert s.search_missing()["searched"] == 1, "pressing the button is consent"


def test_rss_sync_grabs_a_wanted_match_through_the_scorer(tmp_path, monkeypatch):
    s = svc(tmp_path, PROWLARR_URL="http://prowlarr:9696",
            QBITTORRENT_URL="http://qbit:8090")
    s.store.want("Super Metroid", "snes")
    feed = [rel("Super Metroid (USA).smc"), rel("Some Other Game (Europe)")]
    monkeypatch.setattr(s.prowlarr, "search", lambda term, **kw: feed)
    sent = []
    for c in s.clients:
        monkeypatch.setattr(c, "add", lambda url, **kw: sent.append(url) or True)

    out = s.rss_sync()
    assert "grabbed 1" in out
    assert sent == ["magnet:?xt=urn:btih:abc"]


def test_rss_sync_with_nothing_wanted_reads_no_feeds(tmp_path, monkeypatch):
    s = svc(tmp_path, PROWLARR_URL="http://prowlarr:9696")

    def explode(term, **kw):
        raise AssertionError("feed must not be read when nothing is wanted")

    monkeypatch.setattr(s.prowlarr, "search", explode)
    assert "nothing wanted" in s.rss_sync()


def test_list_sync_adds_once_and_never_resurrects(tmp_path):
    s = svc(tmp_path)
    s.store.put_item("import_lists", {
        "name": "classics", "type": "paste", "platform": "snes",
        "content": "1. Super Metroid\n2. Earthbound",
    })
    out = s.list_sync()
    assert out["added"] == 2
    assert {w["game"] for w in s.store.missing()} == \
        {"Super Metroid", "Earthbound"}

    # Acquired and fulfilled -- a later sync must not bring it back.
    s.store.fulfil("Super Metroid", "snes")
    again = s.list_sync()
    assert again["added"] == 0
    assert again["known"] == 2
    assert {w["game"] for w in s.store.missing()} == {"Earthbound"}


def test_list_sync_reports_unresolvable_platforms(tmp_path):
    s = svc(tmp_path)
    s.store.put_item("import_lists", {
        "name": "mixed", "type": "paste",
        "content": "Super Metroid\tsnes\nHalo\tplaystation 5",
    })
    out = s.list_sync()
    assert out["added"] == 1
    assert out["unknown"] == 1


def test_a_disabled_list_is_left_alone(tmp_path):
    s = svc(tmp_path)
    s.store.put_item("import_lists", {
        "name": "off", "type": "paste", "platform": "snes",
        "content": "Super Metroid", "enable": False,
    })
    assert s.list_sync()["added"] == 0
    assert s.store.missing() == []


def test_update_check_notices_a_newer_release(tmp_path, monkeypatch):
    s = svc(tmp_path)
    told = []
    monkeypatch.setattr(s, "notify", lambda m: told.append(m) or [])

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v99.0.0",
                    "html_url": "https://github.com/BlizzHacker/romarr/releases/v99.0.0"}

    import requests as _requests
    monkeypatch.setattr(_requests, "get", lambda *a, **kw: FakeResponse())

    out = s.check_update()
    assert out["update_available"]
    assert s.store.settings["_latest_version"] == "99.0.0"
    assert len(told) == 1 and "99.0.0" in told[0].title

    # The daily check repeating the same news is an alarm everybody learns
    # to ignore: once per version, not once per day.
    s.check_update()
    assert len(told) == 1


def test_update_check_survives_github_being_down(tmp_path, monkeypatch):
    s = svc(tmp_path)
    import requests as _requests

    def down(*a, **kw):
        raise _requests.ConnectionError("no route")

    monkeypatch.setattr(_requests, "get", down)
    out = s.check_update()
    assert not out["ok"]
    assert "github.com" in out["message"]


def test_grab_notifies_with_the_scorers_reasons(tmp_path, monkeypatch):
    s = svc(tmp_path, QBITTORRENT_URL="http://qbit:8090")
    told = []
    monkeypatch.setattr(s, "notify", lambda m: told.append(m) or [])
    monkeypatch.setattr(s.prowlarr, "search",
                        lambda *a, **kw: [rel("Super Metroid (USA).smc")])
    for c in s.clients:
        monkeypatch.setattr(c, "add", lambda url, **kw: True)

    assert s.request("Super Metroid", "snes")["ok"]
    assert len(told) == 1
    assert told[0].event == "grab"
    assert told[0].reasons, "the grab message carries the scorer's reasoning"
