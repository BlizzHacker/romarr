"""An import sweep must not hold the DAT-source lock while it works."""

from __future__ import annotations

import threading

from romarr.app import ROMarr


def test_grab_lock_is_free_during_an_import_sweep(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    inside = threading.Event()
    release = threading.Event()

    def slow_sweep(**kw):
        inside.set()
        release.wait(5)
        return []

    service._import_finished = slow_sweep
    worker = threading.Thread(target=service.import_finished)
    worker.start()
    try:
        assert inside.wait(5)
        got = service._collection_source_lock.acquire(timeout=1)
        assert got, "the source lock was held for the whole sweep"
        service._collection_source_lock.release()
        refused = service.collection_source_start("c", "a" * 40, "dat", "snes", 0)
        assert "import is running" in refused.get("error", "")
    finally:
        release.set()
        worker.join(5)
    assert getattr(service, "_import_sweeps", 0) == 0
