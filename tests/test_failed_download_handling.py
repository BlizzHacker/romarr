"""Retiring a download that died, and grabbing something else instead.

The Blocklist existed and nothing ever added to it on its own, which made a
failed download a loop: `best_release` is deterministic, so removing the dead
torrent by hand and waiting for the next sweep scored the same results the
same way and grabbed the same dead file again.

Worse, the Blocklist was decorative even when an operator filled it in by
hand: `reload_policy` built one and every call to `best_release` and `judge`
left it out, so a blocked release went on being chosen. Both halves are
tested here, because either one alone fixes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from romarr.app import QueueItem, ROMarr
from romarr.selection import Release
from romarr.store import SeerrRequest


def svc(tmp_path, **env):
    base = {"ROMARR_DATA": str(tmp_path / "s.json")}
    base.update(env)
    return ROMarr(base)


def rel(title, *, size=3_000_000, seeders=20, cats=(1030,),
        protocol="torrent", url=None, indexer="Test"):
    return Release(title=title, size=size, seeders=seeders, categories=cats,
                   download_url=url or "magnet:?xt=urn:btih:" + "a" * 40,
                   protocol=protocol, indexer=indexer)


class FakeProwlarr:
    def __init__(self, releases):
        self._releases = releases
        self._config = type("C", (), {"base_url": "http://prowlarr", "api_key": "k"})()

    def search(self, *a, **kw):
        return list(self._releases)


class FakeClient:
    name = "fake"
    protocol = "torrent"
    configured = True

    def __init__(self, accept=True):
        self.added = []
        self._accept = accept

    def add(self, url, **kw):
        self.added.append(url)
        return self._accept


def ago(minutes):
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --- the half that was missing: the blocklist reaches the scorer ----------

def test_a_blocked_release_is_not_chosen_again(tmp_path):
    """reload_policy has always built a Blocklist and nothing ever passed it
    to the scorer, so blocking a release changed no later pick."""
    service = svc(tmp_path)
    dead = rel("Super Metroid (USA) [!].smc", url="magnet:?xt=urn:btih:" + "d" * 40)
    alive = rel("Super Metroid (USA) (Rev 1).smc",
                url="magnet:?xt=urn:btih:" + "e" * 40, seeders=5)
    service.prowlarr = FakeProwlarr([dead, alive])
    client = FakeClient()
    service.clients = [client]

    service.block(dead, reason="stalled")
    assert service.request("Super Metroid", "snes")["ok"] is True

    assert client.added == ["magnet:?xt=urn:btih:" + "e" * 40]


def test_a_blocked_release_says_so_on_the_search_page(tmp_path):
    """A page that prints reasoning nothing acts on is worse than no
    reasoning at all."""
    service = svc(tmp_path)
    dead = rel("Super Metroid (USA) [!].smc")
    service.prowlarr = FakeProwlarr([dead])
    service.block(dead, reason="stalled for 3 hours")

    items = service.candidates("Super Metroid", "snes")["items"]
    assert items and items[0]["accepted"] is False
    assert any("blocklisted" in line and "stalled for 3 hours" in line
               for line in items[0]["reasons"])


def test_blocking_nothing_leaves_an_ordinary_grab_alone(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([rel("Super Metroid (USA) [!].smc")])
    service.clients = [FakeClient()]
    assert service.request("Super Metroid", "snes")["ok"] is True


# --- retiring what died ---------------------------------------------------

def test_a_client_that_refused_the_release_retires_it(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([rel("Super Metroid (USA) [!].smc")])
    service.clients = [FakeClient(accept=False)]

    service.request("Super Metroid", "snes")
    assert service.queue[-1].state == "failed"

    out = service.retire_dead_downloads()

    assert out["blocklisted"] == 1
    assert len(service.blocklist) == 1
    assert service.store.list_items("blocklist")[0]["reason"]


def test_a_missing_download_client_is_not_the_fault_of_the_release(tmp_path):
    """Blocklisting here would punish a perfectly good torrent because the
    operator has not set up a client yet, and the block outlives the
    mistake."""
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([rel("Super Metroid (USA) [!].smc")])
    service.clients = []

    service.request("Super Metroid", "snes")
    assert service.queue[-1].state == "failed"

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert len(service.blocklist) == 0


def test_an_archive_with_no_roms_in_it_is_the_fault_of_the_release(tmp_path):
    service = svc(tmp_path)
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "import-failed",
        detail="no ROMs found in download",
        release_id="btih:" + "c" * 40, release_fault=True))

    assert service.retire_dead_downloads()["blocklisted"] == 1


def test_a_download_that_stalled_is_retired(tmp_path):
    service = svc(tmp_path)
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "grabbed",
        at=ago(60 * 5), release_id="btih:" + "c" * 40))

    assert service.retire_dead_downloads()["blocklisted"] == 1
    assert service.queue[0].state == "failed"
    assert "stalled" in service.queue[0].detail


def test_a_download_still_within_the_timeout_is_left_alone(tmp_path):
    """A large disc image on a thin swarm is slow, not dead."""
    service = svc(tmp_path)
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "grabbed",
        at=ago(10), release_id="btih:" + "c" * 40))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


def test_a_zero_timeout_turns_stall_detection_off(tmp_path):
    service = svc(tmp_path)
    service.store.update_settings({"stalled_timeout_minutes": 0})
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "grabbed",
        at=ago(60 * 48), release_id="btih:" + "c" * 40))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


def test_the_whole_thing_can_be_turned_off(tmp_path):
    service = svc(tmp_path)
    service.store.update_settings({"blocklist_failed_downloads": False})
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "failed",
        release_id="btih:" + "c" * 40, release_fault=True))

    out = service.retire_dead_downloads()
    assert out["blocklisted"] == 0
    assert "off" in out["message"]


def test_a_row_is_never_retired_twice(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([])
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "failed",
        release_id="btih:" + "c" * 40, release_fault=True))

    assert service.retire_dead_downloads()["blocklisted"] == 1
    assert service.retire_dead_downloads()["blocklisted"] == 0


def test_a_row_with_no_release_identity_is_left_alone(tmp_path):
    """A row reading "no usable release among 30 results" never took a
    release, so there is nothing to block."""
    service = svc(tmp_path)
    service.store.enqueue(QueueItem(
        "Contra", "nes", "", 0, "failed",
        detail="no usable release among 30 result(s)"))

    assert service.retire_dead_downloads()["blocklisted"] == 0


# --- and grabbing the next one -------------------------------------------

def test_retiring_a_dead_download_grabs_the_next_best_release(tmp_path):
    """The whole point of the issue: without the blocklist the replacement
    search picks the same dead file straight back."""
    service = svc(tmp_path)
    dead_url = "magnet:?xt=urn:btih:" + "d" * 40
    next_url = "magnet:?xt=urn:btih:" + "e" * 40
    service.prowlarr = FakeProwlarr([
        rel("Super Metroid (USA) [!].smc", url=dead_url, seeders=50),
        rel("Super Metroid (USA) (Rev 1).smc", url=next_url, seeders=9),
    ])
    client = FakeClient(accept=False)
    service.clients = [client]

    service.request("Super Metroid", "snes")      # takes the dead one, refused
    assert client.added == [dead_url]

    service.clients = [FakeClient()]
    out = service.retire_dead_downloads()

    assert out["blocklisted"] == 1 and out["regrabbed"] == 1
    assert service.clients[0].added == [next_url]


def test_replacement_keeps_the_external_request_id(tmp_path):
    service = svc(tmp_path)
    dead = rel("Super Metroid (USA) [!].smc",
               url="magnet:?xt=urn:btih:" + "d" * 40)
    replacement = rel("Super Metroid (USA) (Rev 1).smc",
                      url="magnet:?xt=urn:btih:" + "e" * 40)
    service.prowlarr = FakeProwlarr([dead, replacement])
    service.clients = [FakeClient()]
    service.store.put_seerr_request(SeerrRequest(
        "peer-retry", "snes", game="Super Metroid", status="downloading"))
    service.store.enqueue(QueueItem(
        "Super Metroid", "snes", dead.title, dead.seeders, "failed",
        detail="SABnzbd reported failure",
        release_id="btih:" + "d" * 40, release_fault=True,
        external_request_id="peer-retry"))

    result = service.retire_dead_downloads()

    assert result["regrabbed"] == 1
    replacement_row = next(q for q in service.queue
                           if q.state == "grabbed")
    assert replacement_row.release == replacement.title
    assert replacement_row.external_request_id == "peer-retry"
    assert service.store.get_seerr_request("peer-retry").status == "downloading"


def test_the_request_goes_back_on_wanted_before_the_replacement_search(tmp_path):
    """The replacement search can fail too, and a request that has lost its
    download and never reached Wanted is one nothing will look for again."""
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([])           # nothing to replace it with
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "failed",
        release_id="btih:" + "c" * 40, release_fault=True))

    service.retire_dead_downloads()

    assert [w["game"] for w in service.store.missing()] == ["Contra"]


def test_one_sweep_will_not_hammer_the_trackers(tmp_path):
    """Every replacement costs a full indexer search, and twenty at once is
    how a tracker decides ROMarr is a scraper."""
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([])
    for n in range(8):
        service.store.enqueue(QueueItem(
            f"Game {n}", "nes", f"Release {n}", 5, "failed",
            release_id=f"btih:{n:040d}", release_fault=True))

    out = service.retire_dead_downloads()

    assert out["blocklisted"] == 8          # all retired straight away
    assert out["regrabbed"] <= ROMarr.MAX_REGRABS_PER_SWEEP


def test_retiring_is_recorded_in_history(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([])
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "failed",
        detail="fake rejected the release",
        release_id="btih:" + "c" * 40, release_fault=True))

    service.retire_dead_downloads()

    blocked = [e for e in service.store.history()
               if e["kind"] == "ignored" and "blocklisted" in e["detail"]]
    assert blocked and blocked[0]["game"] == "Contra"


def test_the_blocklist_outlives_a_restart(tmp_path):
    data = tmp_path / "s.json"
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([])
    service.store.enqueue(QueueItem(
        "Contra", "nes", "Contra (USA)", 5, "failed",
        release_id="btih:" + "c" * 40, release_fault=True))
    service.retire_dead_downloads()

    restarted = ROMarr({"ROMARR_DATA": str(data)})
    assert len(restarted.blocklist) == 1


def test_the_tasks_page_can_run_it(tmp_path):
    service = svc(tmp_path)
    out = service.run_command("FailedDownloads")
    assert "blocklisted" in out
