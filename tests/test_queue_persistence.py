"""Activity has to survive a restart, because the download client does.

The queue lived in the service object and nowhere else, so a restart -- or any
k8s Recreate -- emptied it while qBittorrent carried happily on seeding. That
lost more than a list: the import sweep matches a finished download to its
queue row by release title, so with the rows gone it could no longer tell
which game `Super.Metroid.USA.zip` belonged to, and skipped the file it had
just spent an hour downloading.
"""

from __future__ import annotations

import json

import pytest

from romarr.app import QueueItem, ROMarr
from romarr.selection import Release
from romarr.store import Store


def svc(tmp_path, **env):
    base = {"ROMARR_DATA": str(tmp_path / "s.json")}
    base.update(env)
    return ROMarr(base)


def rel(title, *, size=512 * 1024, seeders=20, cats=(1030,),
        protocol="torrent", url="magnet:?xt=urn:btih:abc", indexer="Test"):
    return Release(title=title, size=size, seeders=seeders, categories=cats,
                   download_url=url, protocol=protocol, indexer=indexer)


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


# --- the store keeps it ---------------------------------------------------

def test_the_queue_survives_a_restart(tmp_path):
    path = tmp_path / "r.json"
    s = Store(path)
    s.enqueue(QueueItem("Super Metroid", "snes", "Super Metroid (USA)", 40,
                        "grabbed"))

    reopened = Store(path)
    assert [q.release for q in reopened.queue] == ["Super Metroid (USA)"]
    assert reopened.queue[0].state == "grabbed"


def test_a_file_written_before_the_queue_existed_still_loads(tmp_path):
    """Every install upgrading into this has a romarr.json with no queue key,
    and an empty queue is exactly what those installs had after a restart
    anyway."""
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"settings": {}, "events": [], "wanted": []}),
                    encoding="utf-8")
    assert Store(path).queue == []


def test_a_row_written_by_a_newer_romarr_does_not_stop_this_one_starting(tmp_path):
    """A downgrade must not be a boot loop. `QueueItem(**row)` raises on a
    field this build has never heard of, and a state file that cannot be
    loaded is an install that will not start."""
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"queue": [{
        "game": "Contra", "platform": "nes", "release": "Contra (USA)",
        "seeders": 3, "state": "grabbed", "invented_in_version_12": True,
    }]}), encoding="utf-8")

    s = Store(path)
    assert [q.game for q in s.queue] == ["Contra"]


def test_the_queue_is_capped_so_it_cannot_grow_without_bound(tmp_path):
    s = Store(tmp_path / "r.json")
    for n in range(Store.MAX_QUEUE + 25):
        s.enqueue(QueueItem(f"Game {n}", "nes", f"Release {n}", 1, "grabbed"))
    assert len(s.queue) == Store.MAX_QUEUE
    assert s.queue[-1].game == f"Game {Store.MAX_QUEUE + 24}"


def test_max_queue_is_configurable_and_persists(tmp_path):
    path = tmp_path / "r.json"
    s = Store(path)
    s.update_settings({"max_queue": 750})
    for n in range(620):
        s.enqueue(QueueItem(f"Game {n}", "nes", f"Release {n}", 1, "grabbed"))
    assert len(s.queue) == 620

    reloaded = Store(path)
    assert reloaded.queue_limit == 750
    assert len(reloaded.queue) == 620


def test_max_queue_rejects_invalid_values(tmp_path):
    s = Store(tmp_path / "r.json")
    for bad in (0, 49, 50_001, -10, "nope", True, 100.5):
        with pytest.raises(ValueError):
            s.update_settings({"max_queue": bad})
    assert s.queue_limit == Store.MAX_QUEUE


def test_queue_trims_failed_rows_before_active_downloads(tmp_path):
    s = Store(tmp_path / "r.json")
    s.update_settings({"max_queue": 50})
    # Fill with 10 active downloads followed by 40 failed items.
    for n in range(10):
        s.enqueue(QueueItem(f"Active {n}", "snes", f"Active {n}", 5, "grabbed"))
    for n in range(40):
        s.enqueue(QueueItem(f"Failed {n}", "snes", f"Failed {n}", 0, "failed"))
    # Adding 5 more active items should evict 5 oldest failed rows, keeping all active rows.
    for n in range(10, 15):
        s.enqueue(QueueItem(f"Active {n}", "snes", f"Active {n}", 5, "grabbed"))
    assert len(s.queue) == 50
    active = [q.game for q in s.queue if q.state == "grabbed"]
    failed = [q.game for q in s.queue if q.state == "failed"]
    assert active == [f"Active {n}" for n in range(15)]
    assert len(failed) == 35
    assert failed[0] == "Failed 5"


def test_lowering_max_queue_preserves_active_downloads(tmp_path):
    path = tmp_path / "r.json"
    s = Store(path)
    s.update_settings({"max_queue": 120})
    for n in range(80):
        s.enqueue(QueueItem(f"Active {n}", "snes", f"Active {n}", 5, "grabbed"))
    for n in range(20):
        s.enqueue(QueueItem(f"Failed {n}", "snes", f"Failed {n}", 0, "failed"))
    s.update_settings({"max_queue": 50})
    # All 20 failed rows are trimmed, while all 80 active downloads survive on disk.
    assert [q.game for q in s.queue] == [f"Active {n}" for n in range(80)]
    reloaded = Store(path)
    assert [q.game for q in reloaded.queue] == [f"Active {n}" for n in range(80)]


# --- the service uses the stored one --------------------------------------

def test_a_grab_lands_in_a_queue_that_outlives_the_process(tmp_path):
    data = tmp_path / "s.json"
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([rel("Super Metroid (USA) [!].smc",
                                         size=3_000_000)])
    service.clients = [FakeClient()]

    assert service.request("Super Metroid", "snes")["ok"] is True

    restarted = ROMarr({"ROMARR_DATA": str(data)})
    assert [q.release for q in restarted.queue] == ["Super Metroid (USA) [!].smc"]
    assert restarted.queue[0].state == "grabbed"


def test_the_service_queue_and_the_stored_queue_are_the_same_list(tmp_path):
    """Two lists kept in step by hand is the drift this removes."""
    service = svc(tmp_path)
    service.store.enqueue(QueueItem("Contra", "nes", "Contra (USA)", 5, "grabbed"))
    assert service.queue is service.store.queue


def test_removing_a_row_is_written_down(tmp_path):
    data = tmp_path / "s.json"
    service = svc(tmp_path)
    service.store.enqueue(QueueItem("Contra", "nes", "Contra (USA)", 5, "failed"))

    assert service.queue_action(0, "remove")["ok"] is True
    assert ROMarr({"ROMARR_DATA": str(data)}).queue == []


def test_clearing_the_queue_is_written_down(tmp_path):
    data = tmp_path / "s.json"
    service = svc(tmp_path)
    service.store.enqueue(QueueItem("A", "nes", "A (USA)", 5, "failed"))
    service.store.enqueue(QueueItem("B", "nes", "B (USA)", 5, "grabbed"))

    assert service.clear_queue("failed")["removed"] == 1

    restarted = ROMarr({"ROMARR_DATA": str(data)})
    assert [q.game for q in restarted.queue] == ["B"]


def test_a_grab_records_what_it_took_so_the_row_can_be_acted_on_later(tmp_path):
    """A dead download is retired long after the Release object is gone; all
    that survives is the row, so the row has to carry the release's
    identity."""
    service = svc(tmp_path)
    service.prowlarr = FakeProwlarr([
        rel("Super Metroid (USA) [!].smc", size=3_000_000,
            url="magnet:?xt=urn:btih:" + "a" * 40, indexer="TrackerOne")])
    service.clients = [FakeClient()]

    service.request("Super Metroid", "snes")

    row = service.queue[-1]
    assert row.release_id == "btih:" + "a" * 40
    assert row.indexer == "TrackerOne"
    assert row.size == 3_000_000


def test_an_import_mark_survives_a_restart(tmp_path):
    """The mark is what stops a one-minute sweep re-importing every finished
    download forever; in memory only, a restart re-imported the lot."""
    data = tmp_path / "s.json"
    service = svc(tmp_path)
    service.store.enqueue(QueueItem("Contra", "nes", "Contra (USA)", 5, "grabbed"))
    service.queue[0].state = "imported"
    service.store.save()

    assert ROMarr({"ROMARR_DATA": str(data)}).queue[0].state == "imported"
