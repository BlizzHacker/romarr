from __future__ import annotations

import threading
import zipfile

import pytest

from romarr.app import ROMarr


def _xml(version, title, crc):
    return ('<datafile><header><name>Duplicate System</name><version>'
            + version + '</version></header><game name="' + title
            + '"><rom name="' + title + '.bin" size="1" crc="'
            + crc + '"/></game></datafile>')


def _service(tmp_path):
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json")})
    service.reload_titled = lambda: {"loaded": 0}
    return service


def test_loose_and_archived_copies_load_once_with_newest_version(tmp_path):
    service = _service(tmp_path)
    root = tmp_path / "dats"
    root.mkdir()
    (root / "old.dat").write_text(_xml("2025", "Old", "aa"))
    with zipfile.ZipFile(root / "pack.zip", "w") as archive:
        archive.writestr("current.dat", _xml("2026", "Current", "bb"))

    result = service.reload_dats(str(root))
    assert result["loaded"] == 1
    assert len(service.dats.dats) == 1
    assert "Current" in service.dats.dats[0].games
    assert "Old" not in service.dats.dats[0].games


def test_repeated_and_concurrent_reloads_do_not_accumulate(tmp_path, monkeypatch):
    service = _service(tmp_path)
    root = tmp_path / "dats"
    root.mkdir()
    (root / "system.dat").write_text(_xml("2026", "Current", "bb"))

    # Hold the first reload inside the critical section until every worker
    # has attempted to enter. Without serialization the active count reaches
    # four even if all callers eventually publish the same one-DAT index.
    workers = 4
    attempted = threading.Barrier(workers + 1)
    entered = threading.Event()
    all_contending = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    errors, results = [], []
    active = maximum = calls = contenders = 0
    reload_locked = service._reload_dats
    reload_lock = service._dat_reload_lock

    class ObservedLock:
        def __enter__(self):
            nonlocal contenders
            with counter_lock:
                contenders += 1
                if contenders >= workers:
                    all_contending.set()
            reload_lock.acquire()

        def __exit__(self, *exc):
            reload_lock.release()

    monkeypatch.setattr(service, "_dat_reload_lock", ObservedLock())

    def measured_reload(directory):
        nonlocal active, maximum, calls
        with counter_lock:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        entered.set()
        try:
            assert release.wait(5), "reload workers were not released"
            return reload_locked(directory)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(service, "_reload_dats", measured_reload)

    def worker():
        try:
            attempted.wait(timeout=5)
            result = service.reload_dats(str(root))
            with counter_lock:
                results.append(result)
        except BaseException as exc:
            with counter_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    attempted.wait(timeout=5)
    assert entered.wait(5)
    try:
        assert all_contending.wait(5), "workers did not reach the reload lock"
    finally:
        release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not errors, errors
    assert maximum == 1
    assert calls == workers
    assert len(results) == workers
    assert all(result["loaded"] == 1 for result in results)

    service.reload_dats(str(root))
    assert len(service.dats.dats) == 1
    assert len(service.dats.dats[0].games) == 1


@pytest.mark.parametrize("old_version, new_version", [
    ("1.9", "1.10"), ("2026.9", "2026.10"),
])
@pytest.mark.parametrize("new_in_zip", [False, True])
@pytest.mark.parametrize("new_first", [False, True])
def test_numeric_versions_select_newest_across_loose_and_zip_dats(
        tmp_path, monkeypatch, old_version, new_version, new_in_zip, new_first):
    from romarr import app

    service = _service(tmp_path)
    root = tmp_path / "dats"
    root.mkdir()
    paths = []
    for label, version, archived in [
            ("Old", old_version, not new_in_zip),
            ("Current", new_version, new_in_zip)]:
        path = root / (label + (".zip" if archived else ".dat"))
        content = _xml(version, label, "aa" if label == "Old" else "bb")
        if archived:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(label + ".dat", content)
        else:
            path.write_text(content)
        paths.append(path)
    if new_first:
        paths.reverse()
    monkeypatch.setattr(app, "_find_dats", lambda directory: (paths, ""))

    result = service.reload_dats(str(root))

    assert result["loaded"] == 1
    assert len(service.dats.dats) == 1
    selected = service.dats.dats[0]
    assert selected.version == new_version
    assert set(selected.games) == {"Current"}
