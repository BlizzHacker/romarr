from __future__ import annotations

import threading
import zipfile

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


def test_repeated_and_concurrent_reloads_do_not_accumulate(tmp_path):
    service = _service(tmp_path)
    root = tmp_path / "dats"
    root.mkdir()
    (root / "system.dat").write_text(_xml("2026", "Current", "bb"))

    threads = [threading.Thread(target=service.reload_dats, args=(str(root),))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    service.reload_dats(str(root))
    assert len(service.dats.dats) == 1
    assert len(service.dats.dats[0].games) == 1
