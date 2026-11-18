from __future__ import annotations

from pathlib import Path

import pytest

from romarr.app import ROMarr
from romarr.clients import QBittorrent, QbitConfig, QbitAuthenticationError
from romarr.collection_torrent import plan_source
from romarr.dat import DatIndex, parse_dat


DAT = """<?xml version="1.0"?>
<datafile><header><name>Test System</name><version>1</version></header>
<game name="Game (USA)"><rom name="Game (USA).bin" size="8" crc="d2023890"/></game>
<game name="Other (USA)"><rom name="Other (USA).bin" size="8" crc="d2023890"/></game>
</datafile>"""


def test_plan_selects_only_missing_game_files_and_reserves_piece_space():
    dat = parse_dat(DAT)
    out = plan_source(["Game (USA)"], dat, [
        {"index": 0, "name": "Game (USA).zip", "size": 1_000_000},
        {"index": 1, "name": "Other (USA).zip", "size": 1_000_000},
        {"index": 2, "name": "readme.txt", "size": 10_000},
    ], cap_bytes=6 * 1024 * 1024, piece_size=512 * 1024)
    assert out["reserve_bytes"] == 4 * 1024 * 1024
    assert len(out["batches"]) == 1
    group = out["batches"][0]["groups"][0]
    assert group["title"] == "Game (USA)"
    assert [f["index"] for f in group["files"]] == [0]
    assert out["batches"][0]["bytes"] <= out["usable_bytes"]


def test_plan_keeps_disc_sidecars_in_the_same_indivisible_group():
    dat = parse_dat("""<datafile><header><name>Disc</name></header>
    <game name="Disc Game (USA)"><rom name="Disc Game (USA).cue" size="1" crc="a1"/>
    <rom name="Disc Game (USA) (Track 1).bin" size="1" crc="a2"/></game></datafile>""")
    out = plan_source(["Disc Game (USA)"], dat, [
        {"index": 3, "name": "Disc Game (USA).cue", "size": 100},
        {"index": 4, "name": "Disc Game (USA) (Track 1).bin", "size": 200},
        {"index": 5, "name": "Disc Game (USA).txt", "size": 500},
    ], cap_bytes=8 * 1024 * 1024)
    files = out["batches"][0]["groups"][0]["files"]
    assert {f["index"] for f in files} == {3, 4}


def test_plan_refuses_a_game_larger_than_the_staging_budget():
    out = plan_source(["Game (USA)"], parse_dat(DAT), [
        {"index": 0, "name": "Game (USA).zip", "size": 5 * 1024 * 1024},
    ], cap_bytes=8 * 1024 * 1024)
    assert not out["batches"]
    assert out["skipped"][0]["title"] == "Game (USA)"


def test_qbit_dat_source_is_neither_deleted_nor_reselected():
    """A grouped queue removal may remove only the unowned hashes."""
    from unittest.mock import Mock

    client = QBittorrent(QbitConfig(base_url="http://fixture"))
    protected, ordinary = "a" * 40, "b" * 40
    client.owned_torrents_provider = lambda: {protected}
    client._authed = True
    deselected = {"hash": protected, "size": 0, "total_size": 100}
    client._get = Mock(return_value=Mock(ok=True, json=Mock(return_value=[deselected])))
    client._post = Mock(return_value=Mock(ok=True))

    assert client.completed() == []
    assert [call.args[0] for call in client._get.call_args_list] == ["torrents/info"]
    assert not client.reselect(deselected)
    assert not client.remove_download(protected)
    client._post.assert_not_called()
    assert client.delete_torrent(protected.upper() + "|all|" + ordinary, delete_files=True)
    assert client._post.call_args.kwargs["data"] == {"hashes": ordinary, "deleteFiles": "true"}


class FakeQbit(QBittorrent):
    def __init__(self, source):
        super().__init__(QbitConfig(base_url="http://qbit"))
        self.romarr_id = "q1"
        self.romarr_name = "qBittorrent test"
        self.source = Path(source)
        self.priority_calls = []
        self.pauses = 0
        self.resumes = 0
        self.priorities = {0: 1}

    def torrents(self):
        return [{"hash": "a" * 40, "name": "Set torrent",
                 "save_path": str(self.source), "state": "uploading",
                 "piece_size": 0}]

    def torrent_files(self, torrent_hash):
        return [{"index": 0, "name": "Game (USA).zip",
                 "size": (self.source / "Game (USA).zip").stat().st_size,
                 "progress": 1.0, "priority": self.priorities[0]}]

    def set_file_priorities(self, torrent_hash, priorities):
        self.priority_calls.append(dict(priorities))
        self.priorities.update(priorities)
        return True

    def pause_torrent(self, torrent_hash):
        self.pauses += 1
        return True

    def resume_torrent(self, torrent_hash):
        self.resumes += 1
        return True


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMARR_DATA", str(tmp_path / "state.json"))
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                      "LIBRARY_PATH": str(tmp_path / "library")})
    index = DatIndex()
    index.add(parse_dat(DAT))
    svc.dats = index
    (tmp_path / "library" / "snes").mkdir(parents=True)
    return svc


def test_start_selects_only_planned_files_and_step_verifies_before_import(
        tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Game (USA).zip").write_bytes(b"compressed rom")
    qbit = FakeQbit(source)
    svc.clients = [qbit]
    imported = []
    monkeypatch.setattr(svc, "adopt", lambda path, platform, **kw:
                        imported.append((path, platform, kw)) or {"ok": True,
                                                                 "imported": [path]})
    started = svc.collection_source_start(
        "q1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert started["ok"]
    assert qbit.priorities == {0: 1}
    assert qbit.pauses == 1 and qbit.resumes == 1

    progress = svc.collection_source_step(started["id"])
    assert progress["status"] == "done"
    assert imported and imported[0][1] == "snes"
    assert imported[0][2]["require_verified"] is True
    assert qbit.priorities == {0: 1}
    job = svc.store.get_item("collection_sources", started["id"])
    assert job["batches"][0]["groups"][0]["status"] == "imported"
    assert (source / "Game (USA).zip").exists(), "the source torrent is preserved"


def test_cancel_restores_priorities_without_removing_source_or_torrent(
        tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Game (USA).zip").write_bytes(b"source")
    qbit = FakeQbit(source)
    qbit.priorities[0] = 7
    svc.clients = [qbit]
    started = svc.collection_source_start(
        "q1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert started["ok"]
    result = svc.collection_source_control(started["id"], "cancel")
    assert result["status"] == "cancelled"
    assert qbit.priorities[0] == 7
    assert (source / "Game (USA).zip").exists()


def test_active_job_state_can_resume_after_service_restart(tmp_path, monkeypatch):
    data = tmp_path / "state.json"
    library = tmp_path / "library"
    svc = ROMarr(env={"ROMARR_DATA": str(data), "LIBRARY_PATH": str(library)})
    index = DatIndex()
    index.add(parse_dat(DAT))
    svc.dats = index
    (library / "snes").mkdir(parents=True)
    source = tmp_path / "source-restart"
    source.mkdir()
    (source / "Game (USA).zip").write_bytes(b"source")
    first = FakeQbit(source)
    svc.clients = [first]
    monkeypatch.setattr(svc, "adopt", lambda path, platform, **kw:
                        {"ok": True, "imported": [path]})
    started = svc.collection_source_start(
        "q1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert started["ok"]

    restarted = ROMarr(env={"ROMARR_DATA": str(data), "LIBRARY_PATH": str(library)})
    restarted.dats = index
    second = FakeQbit(source)
    second.priorities[0] = 1
    restarted.clients = [second]
    monkeypatch.setattr(restarted, "adopt", lambda path, platform, **kw:
                        {"ok": True, "imported": [path]})
    result = restarted.collection_source_step(started["id"])
    assert result["status"] == "done"
    assert result["done"] == 1


@pytest.mark.parametrize("title", [
    "Dr. Mario (USA)", "(USA)", "日本語 (Japan)", "★ (USA)",
])
def test_source_matches_full_title_without_losing_punctuation_or_unicode(title):
    from xml.sax.saxutils import escape

    dat = parse_dat('<datafile><game name="' + escape(title, {'"': '&quot;'})
                    + '"><rom name="different track.bin" size="8" crc="aa"/>'
                    + '</game></datafile>')
    out = plan_source([title], dat, [
        {"index": 0, "name": title + ".zip", "size": 8},
        {"index": 1, "name": "別タイトル (Japan).zip", "size": 8},
        {"index": 2, "name": "☆ (USA).zip", "size": 8},
        {"index": 3, "name": "Dr (USA).zip", "size": 8},
        {"index": 4, "name": "(Europe).zip", "size": 8},
    ], 8 * 1024 * 1024)
    assert [row["index"] for row in out["batches"][0]["groups"][0]["files"]] == [0]


def test_source_keeps_region_revision_and_disc_siblings_separate():
    dat = parse_dat("""<datafile>
      <game name="Game (USA)"><rom name="Game (USA).bin" size="8" crc="aa"/></game>
      <game name="Game (Europe)"><rom name="Game (Europe).bin" size="8" crc="ab"/></game>
      </datafile>""")
    out = plan_source(["Game (USA)", "Game (Europe)"], dat, [
        {"index": 0, "name": "Game (USA).zip", "size": 8},
        {"index": 1, "name": "Game (Europe).zip", "size": 8},
        {"index": 2, "name": "Game (USA) (Beta).zip", "size": 8},
        {"index": 3, "name": "Game (USA) (Disc 2).bin", "size": 8},
        {"index": 4, "name": "Game (USA).cue", "size": 8},
    ], 8 * 1024 * 1024)
    groups = out["batches"][0]["groups"]
    assert {f["index"] for f in groups[0]["files"]} == {0, 4}
    assert {f["index"] for f in groups[1]["files"]} == {1}
    assert out["unmatched"] == []


@pytest.mark.parametrize("platform", ["", "   ", "not-a-platform"])
def test_source_rejects_invalid_platform_before_reading_or_changing_torrent(
        tmp_path, monkeypatch, platform):
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "_source_client", lambda *_:
                        pytest.fail("invalid platform must not access torrent client"))
    result = svc.collection_source_start(
        "q1", "a" * 40, "Test System", platform, 8 * 1024 * 1024)
    assert "platform" in result["error"]
    assert not svc.store.list_items("collection_sources")


@pytest.mark.parametrize("status", ["running", "restore-needed"])
def test_source_rejects_second_active_job_for_same_torrent(
        tmp_path, monkeypatch, status):
    svc = _service(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Game (USA).zip").write_bytes(b"source")
    qbit = FakeQbit(source)
    svc.clients = [qbit]
    svc.store.put_item("collection_sources", {
        "id": "existing", "client_id": "q1", "torrent_hash": "A" * 40,
        "status": status,
    })
    result = svc.collection_source_start(
        "q1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert "already has an active" in result["error"]
    assert qbit.priority_calls == []
    assert qbit.pauses == qbit.resumes == 0


def test_failed_qbit_login_is_reported_instead_of_an_empty_torrent_list(
        tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    qbit = QBittorrent(QbitConfig(base_url="http://qbit", username="user"))
    qbit.romarr_id = "q1"
    qbit.romarr_name = "Test qBittorrent"
    monkeypatch.setattr(qbit, "login", lambda: False)
    svc.clients = [qbit]
    with pytest.raises(QbitAuthenticationError, match="authentication failed"):
        qbit.torrents()
    with pytest.raises(QbitAuthenticationError, match="authentication failed"):
        qbit.torrent_files("a" * 40)
    state = svc.collection_source_torrents()
    assert state["torrents"] == []
    assert state["errors"] == ["Test qBittorrent: authentication failed"]
    result = svc._collection_source_plan(
        "q1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert "authentication failed" in result["error"]


@pytest.mark.parametrize(("request_applied", "was_paused"), [(False, False), (True, True)])
def test_transmission_source_restores_selection_and_pause_after_restart(
        tmp_path, monkeypatch, request_applied, was_paused):
    """Cancellation can recover on either side of the daemon's restore."""
    from romarr.downloaders import Transmission, TransmissionConfig

    class Interrupted(BaseException):
        """Simulate exit after the daemon accepts the restore."""

    svc = _service(tmp_path, monkeypatch)
    (tmp_path / "Game (USA).zip").write_bytes(b"source")
    client = Transmission(TransmissionConfig(base_url="http://transmission"))
    client.romarr_id = "t1"
    state = {"paused": was_paused, "priority": 2, "native_priority": -1}
    client.torrents = lambda: [{"hash": "a" * 40, "save_path": str(tmp_path),
                               "state": "pausedDL" if was_paused else "downloading"}]
    client.torrent_files = lambda _: [{"index": 0, "name": "Game (USA).zip",
                                       "size": 6, "progress": 1, **state}]
    client.pause_torrent = lambda _: state.update(paused=True) or True
    client.resume_torrent = lambda _: state.update(paused=False) or True

    def select(_, values, *, native_priorities=None):
        """Apply one atomic selection or restoration."""
        state.update(priority=values[0], native_priority=(native_priorities or {0: 0})[0])
        return True

    client.set_file_priorities = select
    svc.clients = [client]
    started = svc.collection_source_start("t1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert started["ok"] and state == {"paused": False, "priority": 1, "native_priority": 0}

    from unittest.mock import Mock
    replacement = Transmission(TransmissionConfig(base_url="http://replacement"))
    replacement.romarr_id = client.romarr_id
    replacement._call = Mock()
    svc.clients = [replacement]
    assert "endpoint has changed" in svc.collection_source_step(started["id"])["error"]
    assert "endpoint has changed" in svc.collection_source_control(started["id"], "cancel")["error"]
    replacement._call.assert_not_called()
    svc.clients = [client]

    def interrupt(*args, **kwargs):
        """Exit before ROMarr observes the daemon's response."""
        if request_applied:
            select(*args, **kwargs)
        raise Interrupted()

    client.set_file_priorities = interrupt
    with pytest.raises(Interrupted):
        svc.collection_source_control(started["id"], "cancel")
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                            "LIBRARY_PATH": str(tmp_path / "library")})
    restarted.clients = [replacement]
    saved = restarted.store.get_item("collection_sources", started["id"])
    assert (saved["status"], saved["restore_status"]) == ("restore-needed", "cancelled")
    assert "endpoint has changed" in restarted.collection_source_control(started["id"], "restore")["error"]
    replacement._call.assert_not_called()
    restarted.clients = [client]
    client.set_file_priorities = select
    assert restarted.collection_source_control(started["id"], "restore")["status"] == "cancelled"
    assert state == {"paused": was_paused, "priority": 2, "native_priority": -1}


def test_external_selection_change_is_preserved_on_step_and_cancel(tmp_path, monkeypatch):
    """External selection change is preserved on step and cancel."""
    svc = _service(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Game (USA).zip").write_bytes(b"source")
    client = FakeQbit(source)
    svc.clients = [client]
    started = svc.collection_source_start("q1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert started["ok"]
    client.priorities[0] = 7
    calls = list(client.priority_calls)
    result = svc.collection_source_step(started["id"])
    assert "selection changed" in result["error"]
    result = svc.collection_source_control(started["id"], "cancel")
    assert result["status"] == "restore-needed"
    assert "selection changed" in result["errors"][0]
    assert client.priorities == {0: 7}
    assert client.priority_calls == calls


@pytest.mark.parametrize(("status", "other_status", "allowed"), [
    ("running", "done", True), ("running", "restore-needed", False),
    ("running", "running", False), ("done", "running", False),
    ("cancelled", "done", False), ("restore-needed", "done", False),
])
def test_retry_only_resets_a_running_source_that_solely_owns_the_torrent(
        tmp_path, monkeypatch, status, other_status, allowed):
    """A retry preserves ownership and only resets failed groups."""
    svc = _service(tmp_path, monkeypatch)
    job = {"id": "first", "torrent_hash": "a" * 40, "client_id": "q1",
           "status": status, "errors": ["failure"],
           "batches": [{"groups": [{"status": "failed", "error": "failure"},
                                   {"status": "imported"}]}]}
    svc.store.put_item("collection_sources", job)
    svc.store.put_item("collection_sources", {"id": "other", "torrent_hash": "A" * 40,
                        "client_id": "q1", "status": other_status})
    result = svc.collection_source_control("first", "retry")
    saved = svc.store.get_item("collection_sources", "first")
    if allowed:
        assert (result["status"], result["done"], result["errors"]) == ("running", 1, [])
        assert saved["batches"][0]["groups"] == [{"status": ""}, {"status": "imported"}]
    else:
        assert "error" in result
        assert saved == job


def test_failed_retry_write_preserves_nested_source_state(tmp_path, monkeypatch):
    """A failed durable retry leaves the previous group state intact."""
    svc = _service(tmp_path, monkeypatch)
    groups = [{"status": "failed", "error": "failure"}]
    svc.store.put_item("collection_sources", {"id": "first", "status": "running",
                        "batches": [{"groups": groups}]})

    def fail_save(*, raise_on_error):
        """Reject the durable retry write."""
        if raise_on_error:
            raise OSError("disk full")

    monkeypatch.setattr(svc.store, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        svc.collection_source_control("first", "retry")
    assert svc.store.get_item("collection_sources", "first")["batches"][0]["groups"] == [
        {"status": "failed", "error": "failure"}]


@pytest.mark.parametrize("phase", ["prepare", "restore"])
@pytest.mark.parametrize("failed_request_applied", [False, True])
def test_qbit_partial_priority_update_can_restore_after_restart(
        tmp_path, monkeypatch, phase, failed_request_applied):
    """Qbit partial priority update can restore after restart."""
    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    client.priorities = {0: 6, 1: 1}
    client.torrent_files = lambda _: [
        {"index": i, "name": str(i), "size": 8, "priority": p}
        for i, p in client.priorities.items()]
    job = {"id": "recover", "torrent_hash": "a" * 40,
           "original_priorities": {"0": 6, "1": 1}, "was_paused": False,
           "batches": [{"groups": [{"files": [{"index": 0}]}]}]}
    if phase == "restore":
        svc._prepare_collection_source_batch(client, job, force=True)
    calls = 0
    original = client.set_file_priorities

    def fail_second(torrent_hash, priorities):
        """Fail second."""
        nonlocal calls
        calls += 1
        if calls == 2:
            if failed_request_applied:
                original(torrent_hash, priorities)
            return False
        return original(torrent_hash, priorities)

    client.set_file_priorities = fail_second
    if phase == "prepare":
        with pytest.raises(RuntimeError, match="refused"):
            svc._prepare_collection_source_batch(client, job, force=True)
    else:
        assert not svc._restore_source_priorities(client, job)
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                            "LIBRARY_PATH": str(tmp_path / "library")})
    recovered = restarted.store.get_item("collection_sources", job["id"])
    assert recovered["priority_transition"]
    client.set_file_priorities = original
    interrupted = dict(client.priorities)
    client.priorities = {0: 7, 1: 0}
    before = list(client.priority_calls)
    assert not restarted._restore_source_priorities(client, recovered)
    assert client.priorities == {0: 7, 1: 0}
    assert client.priority_calls == before
    client.priorities = interrupted
    assert restarted._restore_source_priorities(client, recovered)
    assert client.priorities == {0: 6, 1: 1}
    assert client.resumes > 0
    assert "priority_transition" not in recovered


def test_legacy_source_job_restores_after_upgrade(tmp_path, monkeypatch):
    """Jobs saved by upstream retain their expected batch selection on upgrade."""
    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    client.priorities = {0: 1, 1: 0}
    client.torrent_files = lambda _: [
        {"index": i, "name": str(i), "size": 8, "priority": p}
        for i, p in client.priorities.items()]
    job = {"id": "legacy", "torrent_hash": "a" * 40,
           "original_priorities": {"0": 6, "1": 1}, "was_paused": False,
           "batches": [{"groups": [{"files": [{"index": 0, "name": "0", "size": 8}]}]}]}
    svc._prepare_collection_source_batch(client, job)
    assert svc._restore_source_priorities(client, job)
    assert client.priorities == {0: 6, 1: 1}


@pytest.mark.parametrize('change', [{"name": "Other.sfc"}, {"size": 9}])
def test_planned_source_identity_is_checked_before_selection(tmp_path, monkeypatch, change):
    """A new listing cannot silently replace the file selected by the plan."""
    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    row = {"index": 0, "name": "Game.sfc", "size": 8, "priority": 6, **change}
    client.torrent_files = lambda _: [row]
    job = {"torrent_hash": "a" * 40, "original_priorities": {"0": 6},
           "source_files": {"0": [row["name"], row["size"]]},
           "batches": [{"groups": [{"files": [{"index": 0, "name": "Game.sfc", "size": 8}]}]}]}
    with pytest.raises(ValueError, match="planned source file identity changed"):
        svc._prepare_collection_source_batch(client, job, force=True)
    assert client.pauses == 0 and client.priority_calls == []


def test_priority_transition_requires_a_durable_recovery_record(tmp_path, monkeypatch):
    """A disk failure prevents the next daemon mutation."""
    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    job = {"id": "durable", "torrent_hash": "a" * 40}
    def fail_save(*, raise_on_error=False):
        """Model the store's normal swallowed errors and durable write errors."""
        if raise_on_error:
            raise OSError("disk full")
    monkeypatch.setattr(svc.store, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        svc._apply_source_priorities(client, job, [{0: 0}], {"0": 6})
    assert client.priority_calls == []


@pytest.mark.parametrize('resume_pending', [False, True])
def test_selected_batch_resumes_only_an_interrupted_resume(tmp_path, monkeypatch, resume_pending):
    """Restart retries ROMarr's pending resume without overriding an external pause."""
    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    client.torrent_files = lambda _: [{"index": 0, "name": "Game.sfc", "size": 8, "priority": 1}]
    job = {"id": "resume", "torrent_hash": "a" * 40,
           "original_priorities": {"0": 6}, "applied_priorities": {"0": 1},
           "batches": [{"groups": [{"files": [{"index": 0, "name": "Game.sfc", "size": 8}]}]}]}
    if resume_pending:
        job["resume_pending"] = True
    svc.store.put_item("collection_sources", job)
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                            "LIBRARY_PATH": str(tmp_path / "library")})
    recovered = restarted.store.get_item("collection_sources", "resume")
    resume = client.resume_torrent

    def confirm_pending(torrent_hash):
        """Observe the durable journal before confirming the resume."""
        reloaded = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                               "LIBRARY_PATH": str(tmp_path / "library")})
        assert reloaded.store.get_item("collection_sources", "resume")["resume_pending"]
        return resume(torrent_hash)

    client.resume_torrent = confirm_pending
    assert restarted._prepare_collection_source_batch(client, recovered)
    assert client.resumes == int(resume_pending)
    reloaded = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                           "LIBRARY_PATH": str(tmp_path / "library")})
    assert not reloaded.store.get_item("collection_sources", "resume").get("resume_pending")


@pytest.mark.parametrize('request_applied', [False, True])
@pytest.mark.parametrize('action', ['restore', 'continue'])
def test_transmission_second_batch_recovers_after_restart(tmp_path, monkeypatch, request_applied, action):
    """An uncertain atomic selection preserves both batch states as paired maps."""
    from romarr.downloaders import Transmission, TransmissionConfig

    svc = _service(tmp_path, monkeypatch)
    client = Transmission(TransmissionConfig(base_url="http://fixture"))
    state = {"wanted": [True, True], "native": [1, 1]}
    client.pause_torrent = lambda _: True
    client.resume_torrent = lambda _: True
    client.torrent_files = lambda _: [
        {"index": i, "name": str(i), "size": 8,
         "priority": {-1: 2, 0: 1, 1: 6}[state["native"][i]] if state["wanted"][i] else 0,
         "native_priority": state["native"][i]} for i in range(2)]
    def apply(_, values, *, native_priorities=None):
        """Apply Transmission's atomic wanted and native-priority changes."""
        for i, value in values.items():
            state["wanted"][i] = value != 0
            if value:
                state["native"][i] = {2: -1, 1: 0, 6: 1}[value]
        for i, value in (native_priorities or {}).items():
            state["native"][i] = value
        return True
    client.set_file_priorities = apply
    job = {"id": "second-batch", "torrent_hash": "a" * 40, "was_paused": False,
           "source_files": {"0": ["0", 8], "1": ["1", 8]},
           "original_priorities": {"0": 6, "1": 6},
           "original_native_priorities": {"0": 1, "1": 1},
           "batches": [{"groups": [{"files": [{"index": i, "name": str(i), "size": 8}]}]}
                       for i in range(2)]}
    svc._prepare_collection_source_batch(client, job, force=True)
    job["batch"] = 1
    def fail(_, values):
        """Model either outcome of a disconnected atomic request."""
        if request_applied:
            apply(_, values)
        return False
    client.set_file_priorities = fail
    with pytest.raises(RuntimeError, match="refused"):
        svc._prepare_collection_source_batch(client, job)
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                            "LIBRARY_PATH": str(tmp_path / "library")})
    recovered = restarted.store.get_item("collection_sources", job["id"])
    client.set_file_priorities = apply
    if action == 'restore':
        assert restarted._restore_source_priorities(client, recovered)
        assert state == {"wanted": [True, True], "native": [1, 1]}
    else:
        assert restarted._prepare_collection_source_batch(client, recovered)
        assert state == {"wanted": [False, True], "native": [0, 0]}


def test_transmission_source_ownership_survives_client_rebuild(tmp_path, monkeypatch):
    """An unlabelled persisted source cannot be claimed by a rebuilt client."""
    from unittest.mock import Mock

    svc = _service(tmp_path, monkeypatch)
    svc.store.put_item("collection_sources", {"id": "source", "client_id": "t1",
                        "torrent_hash": "a" * 40, "status": "running"})
    client = svc._build_client({"type": "transmission", "host": "fixture", "id": "t1"})
    client.romarr_id = "t1"
    client._rpc_version = 17
    client._call = Mock(return_value={"result": "success", "arguments": {
        "torrent-duplicate": {"hashString": "a" * 40}}})
    assert not client.add("magnet:?xt=urn:btih:" + "a" * 40)
    assert not client.last_job_id
    assert [call.args[0] for call in client._call.call_args_list] == ["torrent-add"]


def test_completion_restoration_remains_recoverable_until_resume(tmp_path, monkeypatch):
    """A crash before restoring the running state cannot persist a done job."""
    class Interrupted(BaseException):
        """Simulate process exit rather than a handled RPC failure."""

    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    client.priorities = {0: 1}
    client.torrent_files = lambda _: [{"index": 0, "name": "0", "size": 8,
                                      "priority": client.priorities[0]}]
    def crash(_):
        """Exit between durable priority restoration and the final resume."""
        raise Interrupted()
    client.resume_torrent = crash
    svc.clients = [client]
    job = {"id": "finishing", "client_id": "q1", "torrent_hash": "a" * 40,
           "status": "running", "batch": 1, "was_paused": False,
           "original_priorities": {"0": 6}, "applied_priorities": {"0": 1},
           "batches": [{"groups": [{"files": [{"index": 0, "name": "0", "size": 8}]}]}]}
    svc.store.put_item("collection_sources", job)
    with pytest.raises(Interrupted):
        svc.collection_source_step(job["id"])
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                            "LIBRARY_PATH": str(tmp_path / "library")})
    assert restarted.store.get_item("collection_sources", job["id"])["status"] == "restore-needed"
    client.resume_torrent = lambda _: True
    restarted.clients = [client]
    assert restarted.collection_source_control(job["id"], "restore")["status"] == "done"
    assert client.priorities == {0: 6}


def test_transmission_handoff_records_queue_ownership_before_source_start(tmp_path, monkeypatch):
    """Source startup cannot slip between torrent submission and queue persistence."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace
    from unittest.mock import Mock
    from romarr.selection import Release

    svc = _service(tmp_path, monkeypatch)
    contended = Event()
    lock = svc._collection_source_lock

    class ObservedLock:
        """Signal when a competing request reaches the held source lock."""

        def __enter__(self):
            if not lock.acquire(blocking=False):
                contended.set()
                lock.acquire()

        def __exit__(self, *_):
            lock.release()

    svc._collection_source_lock = ObservedLock()
    client = svc._build_client({"type": "transmission", "host": "fixture", "id": "t1"})
    client.romarr_id = "t1"
    client._rpc_version = 17
    client._call = Mock(return_value={"result": "success", "arguments": {
        "torrent-added": {"hashString": "a" * 40}}})
    svc.clients = [client]
    svc.prowlarr = SimpleNamespace(is_prowlarr_download_url=lambda _: False)
    enqueue = svc.store.enqueue
    future = None
    def start_source():
        """Attempt source ownership during the handoff persistence window."""
        return svc.collection_source_start("t1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def enqueue_with_competitor(item):
            """Confirm source startup waits until the queue owner exists."""
            nonlocal future
            future = pool.submit(start_source)
            assert contended.wait(5)
            enqueue(item)
        monkeypatch.setattr(svc.store, "enqueue", enqueue_with_competitor)
        release = Release("Game (USA).sfc", 100, 1, (), "magnet:?xt=urn:btih:" + "a" * 40, "torrent")
        assert svc.grab(release, "Game", "snes")["ok"]
        assert "active download request" in future.result(timeout=5)["error"]


def test_active_source_is_excluded_from_queue_previews_and_imports(tmp_path, monkeypatch):
    """An existing source stays exclusively in its DAT workflow while active."""
    from romarr.downloaders import Transmission, TransmissionConfig
    from romarr.store import QueueItem
    from unittest.mock import Mock

    svc = _service(tmp_path, monkeypatch)
    client = Transmission(TransmissionConfig(base_url="http://fixture"))
    client.romarr_id = "t1"
    row = {"id": "a" * 40, "hash": "a" * 40, "name": "Game",
           "category": "romarr", "content_path": str(tmp_path)}
    client.completed = lambda **_: [row]
    client.progress = lambda **_: {}
    svc.clients = [client]
    svc.store.enqueue(QueueItem("Game", "snes", "Game", 1, "grabbed",
                                download_id="a" * 40, download_client="t1"))
    svc.store.put_item("collection_sources", {"id": "source", "client_id": "t1",
                        "torrent_hash": "a" * 40, "status": "running"})
    svc.adopt = Mock(side_effect=AssertionError("ordinary import must not read this source"))
    assert not svc.queue_rows()[0]["can_preview_import"]
    assert "error" in svc.queue_import_plan(0, {})
    assert svc.import_finished() == []
    assert svc.queue[0].state == "grabbed"
    svc.adopt.assert_not_called()


@pytest.mark.parametrize("request_applied", [False, True])
def test_qbit_step_recovers_interrupted_second_batch(tmp_path, monkeypatch, request_applied):
    """Step recovers either side of a journaled priority change after restart."""
    svc = _service(tmp_path, monkeypatch)
    client = FakeQbit(tmp_path)
    client.priorities = {0: 6, 1: 6}
    client.torrent_files = lambda _: [
        {"index": i, "name": str(i), "size": 8, "progress": 0, "priority": p}
        for i, p in client.priorities.items()]
    job = {"id": "second-batch", "client_id": "q1", "torrent_hash": "a" * 40,
           "status": "running", "original_priorities": {"0": 6, "1": 6},
           "source_files": {"0": ["0", 8], "1": ["1", 8]},
           "batches": [{"groups": [{"files": [{"index": i, "name": str(i), "size": 8}]}]}
                       for i in range(2)]}
    svc._prepare_collection_source_batch(client, job, force=True)
    job["batch"] = 1
    apply = client.set_file_priorities

    class Interrupted(BaseException):
        """Model an abrupt process exit."""

    def interrupt(identity, priorities):
        """Exit before or after the first request reaches the daemon."""
        if request_applied:
            apply(identity, priorities)
        raise Interrupted()

    client.set_file_priorities = interrupt
    with pytest.raises(Interrupted):
        svc._prepare_collection_source_batch(client, job)
    restarted = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json"),
                            "LIBRARY_PATH": str(tmp_path / "library")})
    restarted.clients = [client]
    client.set_file_priorities = apply
    interrupted = dict(client.priorities)
    client.priorities[0] = 7
    rejected = restarted.collection_source_step(job["id"])
    assert "selection changed" in rejected["error"]
    client.priorities = interrupted
    assert restarted.collection_source_step(job["id"])["waiting"]
    assert client.priorities == {0: 0, 1: 1}
    saved = restarted.store.get_item("collection_sources", job["id"])
    assert "priority_transition" not in saved


@pytest.mark.parametrize("ownership", ["soundtrack", "queue-tag"])
def test_dat_source_rejects_qbit_soundtracks_and_tagged_rom_requests(tmp_path, monkeypatch, ownership):
    """A qBittorrent category or exact queued tag prevents source selection."""
    from romarr.store import QueueItem

    svc = _service(tmp_path, monkeypatch)
    (tmp_path / "Game (USA).zip").write_bytes(b"source")
    client = FakeQbit(tmp_path)
    row, = client.torrents()
    if ownership == "soundtrack":
        row["category"] = svc.SOUNDTRACK_CATEGORY
    else:
        row["tags"] = "unrelated, romarr-request-id"
        svc.store.enqueue(QueueItem("Game", "snes", "a release title", 1, "grabbed",
                                    download_id="qbit-tag:romarr-request-id", download_client="q1"))
    client.torrents = lambda: [row]
    svc.clients = [client]
    result = svc.collection_source_start("q1", row["hash"], "Test System", "snes", 8 * 1024 * 1024)
    expected = "soundtrack" if ownership == "soundtrack" else "active download"
    assert expected in result["error"]
    assert not client.priority_calls


def test_qbit_duplicate_attachment_respects_persisted_dat_source_owner(tmp_path, monkeypatch):
    """An active DAT source without a tag cannot be attached to another request."""
    from unittest.mock import Mock

    svc = _service(tmp_path, monkeypatch)
    identity = "a" * 40
    svc.store.put_item("collection_sources", {"id": "dat-job", "client_id": "q1",
                       "torrent_hash": identity, "status": "running"})
    client = svc._build_client({"type": "qbittorrent", "host": "fixture", "id": "q1"})
    client.romarr_id = "q1"
    client.torrents = lambda: [{"hash": identity, "tags": ""}]
    client._post = Mock()
    assert not client._adopt_duplicate_magnet("magnet:?xt=urn:btih:" + identity, "romarr-request-next")
    client._post.assert_not_called()
