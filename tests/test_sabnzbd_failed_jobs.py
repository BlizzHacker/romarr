"""A download SABnzbd failed is retired on the next sweep, not after the timeout.

`SABnzbd.completed` reads only Completed history, so a job SABnzbd failed --
usually within seconds, with its articles gone from every server -- left its
queue row `grabbed` until the stall timeout fired, three hours by default.
A title whose every upload is dead cost that long per release.

Two limits keep this from touching anything it should not. A failure is paired
with its row by the client that took the job and that client's job id, never
by title, and only a reason that blames the release counts; everything else
waits for the stall timeout as it always did.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from romarr.app import QueueItem, ROMarr
from romarr.downloaders import SabConfig, SABnzbd
from romarr.selection import Release

NOT_COMPLETE = "Aborted, cannot be completed - https://sabnzbd.org/not-complete"


def svc(tmp_path):
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})


def ago(minutes):
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def grabbed(job_id="SABnzbd_nzo_1", *, client="sab-1",
            release="Contra.USA.NES-GRP", minutes=1):
    return QueueItem("Contra", "nes", release, 0, "grabbed", at=ago(minutes),
                     release_id=f"Test|{release.lower()}|1000",
                     indexer="Test", size=1000, download_id=job_id,
                     download_client=client)


class FakeProwlarr:
    def __init__(self, releases=()):
        self._releases = list(releases)
        self._config = type("C", (), {"base_url": "http://prowlarr", "api_key": "k"})()

    def search(self, *a, **kw):
        return list(self._releases)


class FakeUsenet:
    name = "SABnzbd"
    protocol = "usenet"
    configured = True

    def __init__(self, failures=(), romarr_id="sab-1"):
        self.romarr_id = romarr_id
        self.failures = list(failures)
        self.added = []
        self.last_job_id = ""

    def add(self, url, **kw):
        self.added.append(url)
        self.last_job_id = f"SABnzbd_nzo_{len(self.added)}"
        return True

    def failed_jobs(self):
        return list(self.failures)


def failure(job_id="SABnzbd_nzo_1", detail=NOT_COMPLETE, *, release_fault=True,
            name="Contra.USA.NES-GRP"):
    return {"id": job_id, "name": name, "detail": detail,
            "release_fault": release_fault}


# --- the sweep --------------------------------------------------------------

def test_a_job_sabnzbd_failed_is_retired_without_waiting_for_the_timeout(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr()
    service.clients = [FakeUsenet([failure()])]
    service.store.enqueue(grabbed(minutes=1))

    assert service.retire_dead_downloads()["blocklisted"] == 1
    row = service.queue[0]
    assert row.state == "failed" and row.release_fault
    assert row.detail == f"SABnzbd failed the download: {NOT_COMPLETE}"
    assert "cannot be completed" in service.store.list_items("blocklist")[0]["reason"]


def test_the_replacement_is_grabbed_in_the_same_sweep(tmp_path):
    service = svc(tmp_path)
    dead = Release(title="Contra.USA.NES-GRP", size=3_000_000, seeders=0,
                   categories=(1030,), download_url="http://nzb/dead",
                   protocol="usenet", indexer="Test")
    alive = Release(title="Contra (USA).nes", size=3_000_000, seeders=0,
                    categories=(1030,), download_url="http://nzb/alive",
                    protocol="usenet", indexer="Test")
    service.prowlarr = FakeProwlarr([dead, alive])
    client = FakeUsenet()
    service.clients = [client]
    assert service.request("Contra", "nes")["ok"] is True
    taken = client.added[0]
    client.failures = [failure(service.queue[-1].download_id)]

    out = service.retire_dead_downloads()

    assert out["blocklisted"] == 1 and out["regrabbed"] == 1
    assert len(client.added) == 2 and client.added[1] != taken


def test_a_local_failure_is_left_to_the_stall_timeout(tmp_path):
    """A full disc fails every release alike; blocklisting each in turn would
    spend good releases on a problem none of them caused."""
    service = svc(tmp_path)
    service.clients = [FakeUsenet([failure(
        detail="Unpacking failed, write error or disk is full?",
        release_fault=False)])]
    service.store.enqueue(grabbed(minutes=1))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


def test_a_local_failure_still_stalls_out_as_before(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr()
    service.clients = [FakeUsenet([failure(release_fault=False)])]
    service.store.enqueue(grabbed(minutes=60 * 5))

    assert service.retire_dead_downloads()["blocklisted"] == 1
    assert "stalled" in service.queue[0].detail


def test_a_failure_is_paired_by_job_id_never_by_title(tmp_path):
    """The same release name arrives from several indexers. An earlier failed
    job must not be pinned on a new grab that shares its title, nor on a row
    that recorded no job id at all."""
    service = svc(tmp_path)
    service.clients = [FakeUsenet([failure("SABnzbd_nzo_old")])]
    service.store.enqueue(grabbed("SABnzbd_nzo_new"))
    service.store.enqueue(grabbed(""))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert [row.state for row in service.queue] == ["grabbed", "grabbed"]


def test_a_failure_from_another_client_with_the_same_job_id_is_ignored(tmp_path):
    """Job ids are only unique within one client, and two SABnzbd rows can
    both be enabled."""
    service = svc(tmp_path)
    service.clients = [FakeUsenet(romarr_id="sab-1"),
                       FakeUsenet([failure()], romarr_id="sab-2")]
    service.store.enqueue(grabbed(client="sab-1"))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


def test_a_row_queued_before_its_client_was_recorded_waits_for_the_timeout(tmp_path):
    service = svc(tmp_path)
    service.clients = [FakeUsenet([failure()])]
    service.store.enqueue(grabbed(client=""))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


def test_a_grab_records_the_client_that_took_it(tmp_path):
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([Release(
        title="Contra (USA).nes", size=3_000_000, seeders=0, categories=(1030,),
        download_url="http://nzb/contra", protocol="usenet", indexer="Test")])
    service.clients = [FakeUsenet(romarr_id="sab-7")]

    assert service.request("Contra", "nes")["ok"] is True
    assert service.queue[-1].download_client == "sab-7"


def test_a_grab_still_matches_its_failure_after_a_restart(tmp_path):
    """The pair a failure is matched on has to survive the state file."""
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([Release(
        title="Contra (USA).nes", size=3_000_000, seeders=0, categories=(1030,),
        download_url="http://nzb/contra", protocol="usenet", indexer="Test")])
    service.clients = [FakeUsenet(romarr_id="sab-7")]
    assert service.request("Contra", "nes")["ok"] is True

    restarted = svc(tmp_path)
    row = restarted.queue[-1]
    assert (row.download_client, row.download_id) == ("sab-7", "SABnzbd_nzo_1")

    restarted.prowlarr = FakeProwlarr()
    restarted.clients = [FakeUsenet([failure("SABnzbd_nzo_1")], romarr_id="sab-7")]
    assert restarted.retire_dead_downloads()["blocklisted"] == 1


def test_a_queue_row_without_download_client_loads_from_old_state():
    from romarr.store import _rows
    rows = _rows([{"game": "g", "platform": "nes", "release": "r", "seeders": 1,
                   "state": "grabbed", "download_id": "SABnzbd_nzo_1"}], QueueItem)
    assert rows[0].download_client == ""


def test_a_row_already_past_grabbed_is_not_rewritten(tmp_path):
    service = svc(tmp_path)
    service.clients = [FakeUsenet([failure()])]
    row = grabbed()
    row.state = "imported"
    service.store.enqueue(row)

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "imported"


def test_a_client_that_cannot_report_failures_changes_nothing(tmp_path):
    """qBittorrent and the other clients have no failed_jobs; for them the
    stall timeout stays the only signal."""
    class Torrent:
        name = "qBittorrent"
        protocol = "torrent"
        configured = True

    service = svc(tmp_path)
    service.clients = [Torrent()]
    service.store.enqueue(grabbed(minutes=1))

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


def test_a_client_that_errors_does_not_stop_the_sweep(tmp_path):
    class Broken(FakeUsenet):
        def failed_jobs(self):
            raise RuntimeError("boom")

    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr()
    service.clients = [Broken()]
    service.store.enqueue(grabbed(minutes=60 * 5))

    assert service.retire_dead_downloads()["blocklisted"] == 1


def test_an_unconfigured_client_is_not_asked(tmp_path):
    class Unconfigured(FakeUsenet):
        configured = False

        def failed_jobs(self):
            raise AssertionError("asked an unconfigured client")

    service = svc(tmp_path)
    service.clients = [Unconfigured()]
    service.store.enqueue(grabbed())
    assert service.retire_dead_downloads()["blocklisted"] == 0


def test_turning_failed_download_handling_off_still_turns_this_off(tmp_path):
    service = svc(tmp_path)
    service.store.update_settings({"blocklist_failed_downloads": False})
    service.clients = [FakeUsenet([failure()])]
    service.store.enqueue(grabbed())

    assert service.retire_dead_downloads()["blocklisted"] == 0
    assert service.queue[0].state == "grabbed"


# --- the client -------------------------------------------------------------

class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Sess:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.params = []

    def get(self, url, params=None, **kw):
        self.params.append(params)
        if self.error:
            raise self.error
        return _Resp(self.body)


def sab(session, **config):
    return SABnzbd(SabConfig(base_url="http://sab", api_key="k", **config),
                   session=session)


def test_sabnzbd_asks_for_failed_history_in_its_own_category():
    session = _Sess({"history": {"slots": []}})
    sab(session, category="roms").failed_jobs()
    asked = session.params[0]
    assert asked["mode"] == "history"
    assert asked["failed_only"] == 1
    assert asked["category"] == "roms"


def test_sabnzbd_reports_the_job_id_and_reason():
    body = {"history": {"slots": [
        {"name": "Contra.USA.NES-GRP", "nzo_id": "SABnzbd_nzo_1",
         "status": "Failed", "fail_message": NOT_COMPLETE},
    ]}}
    assert sab(_Sess(body)).failed_jobs() == [{
        "name": "Contra.USA.NES-GRP", "id": "SABnzbd_nzo_1",
        "detail": NOT_COMPLETE, "release_fault": True}]


def test_sabnzbd_ignores_anything_not_failed_if_the_filter_is_ignored():
    body = {"history": {"slots": [
        {"name": "a", "nzo_id": "1", "status": "Completed", "fail_message": ""},
        {"name": "b", "nzo_id": "2", "status": "Failed", "fail_message": NOT_COMPLETE},
    ]}}
    assert [job["id"] for job in sab(_Sess(body)).failed_jobs()] == ["2"]


def test_sabnzbd_that_is_down_reports_nothing():
    assert sab(_Sess(error=requests.ConnectionError())).failed_jobs() == []


def test_sabnzbd_without_a_key_is_not_asked():
    session = _Sess({"history": {"slots": []}})
    SABnzbd(SabConfig(base_url="http://sab", api_key=""),
            session=session).failed_jobs()
    assert session.params == []


@pytest.mark.parametrize("reason", [
    NOT_COMPLETE,
    "Download failed - Not on your server(s) - https://sabnzbd.org/not-complete",
    "Download might fail, only 41.2% of required 100.0% available"
    " - https://sabnzbd.org/not-complete",
    # Translated wording keeps the untranslated link.
    "Abgebrochen, kann nicht abgeschlossen werden - https://sabnzbd.org/not-complete",
    "Aborted, encryption detected",
    "Aborted, unwanted extension detected",
    "Unpacking failed, archive requires a password",
    "Unpacking failed, CRC error",
    "Corrupt RAR file",
    "Unusable RAR file",
    "RAR files failed to verify",
    "Repairing failed, Not enough repair blocks (12 short)",
])
def test_reasons_that_blame_the_release(reason):
    assert SABnzbd._release_fault(reason) is True


@pytest.mark.parametrize("reason", [
    "",
    "Repairing failed, Disk full",
    "Unpacking failed, write error or disk is full?",
    "Unpacking failed, disk full",
    "Failed to move files",
    "Exit(1): permission denied ",
    "Post-processing was aborted",
    "Duplicate NZB",
])
def test_reasons_that_are_left_to_the_stall_timeout(reason):
    assert SABnzbd._release_fault(reason) is False
