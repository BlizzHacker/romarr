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


def test_transmission_source_cancel_restores_selection_priorities_and_pause(tmp_path, monkeypatch):
    """Transmission source cancel restores selection priorities and pause."""
    from romarr.downloaders import Transmission, TransmissionConfig

    svc = _service(tmp_path, monkeypatch)
    source = tmp_path / "transmission-source"
    source.mkdir()
    (source / "Game (USA).zip").write_bytes(b"source")
    client = Transmission(TransmissionConfig(base_url="http://transmission"))
    client.romarr_id = "t1"
    state = {"paused": True, "wanted": True, "priority": -1}
    client.torrents = lambda: [{"hash": "a" * 40, "name": "Set", "save_path": str(source),
                                "state": "pausedDL" if state["paused"] else "downloading"}]
    client.torrent_files = lambda _: [{"index": 0, "name": "Game (USA).zip", "size": 6,
                                       "progress": 1.0, "priority": (2 if state["priority"] == -1 else 1)
                                       if state["wanted"] else 0,
                                       "native_priority": state["priority"]}]
    def select(_, values, *, native_priorities=None):
        """Select."""
        value = values[0]
        state["wanted"] = value != 0
        if value:
            state["priority"] = -1 if value == 2 else 0
        if native_priorities is not None:
            state["priority"] = native_priorities[0]
        return True
    client.set_file_priorities = select
    client.pause_torrent = lambda _: state.update(paused=True) or True
    client.resume_torrent = lambda _: state.update(paused=False) or True
    svc.clients = [client]
    started = svc.collection_source_start("t1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
    assert started["ok"] and state == {"paused": False, "wanted": True, "priority": 0}
    assert svc.collection_source_control(started["id"], "cancel")["status"] == "cancelled"
    assert state == {"paused": True, "wanted": True, "priority": -1}


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


@pytest.mark.parametrize("request_applied", [False, True])
@pytest.mark.parametrize("was_paused", [False, True])
def test_transmission_restore_retries_one_combined_request(
        tmp_path, monkeypatch, request_applied, was_paused):
    """Transmission restore retries one combined request."""
    from romarr.downloaders import Transmission, TransmissionConfig

    svc = _service(tmp_path, monkeypatch)
    client = Transmission(TransmissionConfig(base_url="http://transmission"))
    state = {"wanted": [True, False], "native": [0, 1], "paused": False}
    client.torrent_files = lambda _: [
        {"index": i, "name": str(i), "size": 8,
         "priority": ({-1: 2, 0: 1, 1: 6}[state["native"][i]] if state["wanted"][i] else 0),
         "native_priority": state["native"][i]} for i in range(2)]
    client.pause_torrent = lambda _: state.update(paused=True) or True
    client.resume_torrent = lambda _: state.update(paused=False) or True
    calls = []

    def call(method, args):
        """Call."""
        calls.append((method, args))
        if len(calls) > 1 or request_applied:
            for i in args["files-wanted"]:
                state["wanted"][i] = True
            for i in args["files-unwanted"]:
                state["wanted"][i] = False
            for priority, key in ((-1, "priority-low"), (0, "priority-normal"), (1, "priority-high")):
                for i in args.get(key, []):
                    state["native"][i] = priority
        return {"result": "failure" if len(calls) == 1 else "success"}

    client._call = call
    job = {"id": "recover", "torrent_hash": "a" * 40, "was_paused": was_paused,
           "original_priorities": {"0": 2, "1": 0},
           "original_native_priorities": {"0": -1, "1": 1},
           "applied_priorities": {"0": 1, "1": 0},
           "applied_native_priorities": {"0": 0, "1": 1}}
    assert not svc._restore_source_priorities(client, job)
    assert svc._restore_source_priorities(client, job)
    assert calls == [("torrent-set", {"ids": ["a" * 40], "files-unwanted": [1],
                      "files-wanted": [0], "priority-low": [0], "priority-high": [1]})] * 2
    assert state == {"wanted": [True, False], "native": [-1, 1], "paused": was_paused}


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
    assert restarted._prepare_collection_source_batch(client, recovered)
    assert client.resumes == int(resume_pending)
    assert not recovered.get("resume_pending")


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
    client = svc._build_client({"type": "transmission", "host": "fixture", "id": "t1"})
    client.romarr_id = "t1"
    client._rpc_version = 17
    client._call = Mock(return_value={"result": "success", "arguments": {
        "torrent-added": {"hashString": "a" * 40}}})
    svc.clients = [client]
    svc.prowlarr = SimpleNamespace(is_prowlarr_download_url=lambda _: False)
    attempted, finished = Event(), Event()
    enqueue = svc.store.enqueue
    future = None
    def start_source():
        """Attempt source ownership during the handoff persistence window."""
        attempted.set()
        try:
            return svc.collection_source_start("t1", "a" * 40, "Test System", "snes", 8 * 1024 * 1024)
        finally:
            finished.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def enqueue_with_competitor(item):
            """Confirm source startup waits until the queue owner exists."""
            nonlocal future
            future = pool.submit(start_source)
            assert attempted.wait(5)
            assert not finished.wait(.1)
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
