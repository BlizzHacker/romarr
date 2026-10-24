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
