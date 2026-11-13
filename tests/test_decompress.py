"""Batch decompression: the gates that decide what gets deleted.

The workflow is scan -> submit to compressatorium -> verify outputs against
the DAT index -> delete originals, and the whole design argument lives in
the last two steps: deletion happens ONLY for originals whose every output
matched a DAT entry, and NEVER when no DATs are loaded at all. A test suite
for this module is a test suite for those gates, so the compressatorium
service is faked at the client boundary -- the HTTP contract it speaks is
covered against the real service in deployment, and what matters here is
that ROMarr's decisions do not change when the service misbehaves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romarr.compressatorium import JobResult, MatchResult, mode_for
from romarr.dat import DatIndex, Rom, Game, VERIFIED
from romarr import decompress


# ----------------------------------------------------------------- modes --

def test_mode_for_every_scanned_extension():
    """Every extension the scan accepts has a mode the service can run.

    The two lists (COMPRESSED_EXTENSIONS and the client's suffix map) are
    maintained apart from each other, and a file scanned but unmappable is
    a job that can never be submitted -- caught here rather than in a
    production batch.
    """
    for ext in decompress.COMPRESSED_EXTENSIONS:
        assert mode_for(f"game{ext}"), f"no mode for {ext}"


def test_mode_for_rejects_uncompressed_files():
    """Plain images and decompressed originals are not work to do."""
    for name in ("game.iso", "game.cci", "game.cxi", "game.cia", "game.nsp",
                 "game.xci", "game.n64", "game.gba", "game.nes"):
        assert mode_for(name) == "", name


def test_nkit_compound_extension_beats_plain_iso():
    """.nkit.iso is NKit-shrunk; .iso is not compressed at all."""
    assert mode_for("game.nkit.iso") == "nkit_restore"
    assert mode_for("game.iso") == ""


# ------------------------------------------------------------------ scan --

def test_scan_finds_only_decompressable_files(tmp_path):
    for name in ("A.chd", "B.7z", "C.iso", "D.nkit.iso", "E.nsp",
                 "notes.txt", "F.rvz", "G.cci"):
        (tmp_path / name).write_bytes(b"x")

    found = {p.name for p in decompress.scan_compressed(tmp_path)}

    assert found == {"A.chd", "B.7z", "D.nkit.iso", "F.rvz"}


def test_scan_walks_subdirectories(tmp_path):
    """Platform folders under the library root are the normal shape."""
    (tmp_path / "gba").mkdir()
    (tmp_path / "gba" / "game.gba.zip").write_bytes(b"x")

    assert [p.name for p in decompress.scan_compressed(tmp_path)] == \
        ["game.gba.zip"]


# ------------------------------------------------- the fake service -------

class FakeClient:
    """Compressatorium as ROMarr's batch runner sees it.

    `outputs` maps a source path -> (output path, content) the service
    "produced"; `remote_matches` is the set of paths its own DAT store
    claims. Anything not listed fails the job, the way an unsupported
    archive or an unreadable volume would.
    """

    name = "FakeCompressatorium"

    def __init__(self, outputs=None, remote_matches=()):
        self.outputs = outputs or {}
        self.remote_matches = set(remote_matches)
        self.submitted: list[dict] = []
        self._jobs: dict[str, JobResult] = {}

    def reachable(self):
        return True

    def submit_job(self, *, file_path, mode, output_dir=None,
                   duplicate_action="skip", delete_on_verify=False):
        self.submitted.append({"file_path": file_path, "mode": mode,
                               "delete_on_verify": delete_on_verify})
        # ROMarr never delegates deletion to the service -- the gate is
        # ROMarr's, and a submit that says otherwise is a bug.
        assert delete_on_verify is False
        job_id = f"job{len(self._jobs)}"
        out = self.outputs.get(file_path)
        if out is None:
            self._jobs[job_id] = JobResult(
                job_id=job_id, status="failed", file_path=file_path,
                error="service could not handle this file")
        else:
            self._jobs[job_id] = JobResult(
                job_id=job_id, status="completed", file_path=file_path,
                mode=mode, output_path=str(out))
        return JobResult(job_id=job_id, status="queued", file_path=file_path)

    def job_status(self, job_id):
        return self._jobs.get(job_id, JobResult(job_id=job_id,
                                                status="unknown"))

    def wait_for_jobs(self, job_ids, *, timeout=3600, poll_interval=5):
        return {jid: self.job_status(jid) for jid in job_ids}

    def match_file(self, path):
        if path in self.remote_matches:
            return MatchResult(path=path, matched=True, game="Remote Game",
                               dat="Server DAT")
        return MatchResult(path=path, matched=False)


def _dat_index_for(name: str, hashes: dict) -> DatIndex:
    """A one-game index that verifies exactly `hashes`."""
    from romarr.dat import Dat

    index = DatIndex()
    dat = Dat(name="Test DAT")
    rom = Rom(name=name, size=hashes["size"], crc=hashes["crc"],
              md5=hashes["md5"], sha1=hashes["sha1"])
    dat.games[name] = Game(name=name, roms=(rom,))
    dat._by_sha1[rom.sha1] = (name, rom)
    dat._by_md5[rom.md5] = (name, rom)
    dat._by_crc[(rom.crc, rom.size)] = (name, rom)
    dat._sizes[rom.size] = name
    index.add(dat)
    return index


@pytest.fixture
def library(tmp_path):
    """One compressed source whose decompressed content is known."""
    from romarr.dat import hash_bytes

    games = tmp_path / "games"
    games.mkdir()
    content = bytes(range(256)) * 128
    hashes = hash_bytes(content, suffix=".gba")  # GBA: no header arithmetic
    source = games / "TestGame.gba.zip"
    source.write_bytes(b"fake zip bytes")
    output = games / "TestGame.gba"
    return {
        "root": games, "source": source, "output": output,
        "content": content, "hashes": hashes,
        "dats": _dat_index_for("TestGame.gba", hashes),
    }


def _run(library, client, **kwargs):
    return decompress.run_batch(
        library["root"],
        compressatorium_url="http://fake:8080",
        dats=library["dats"],
        job_timeout=5, poll_interval=0,
        **kwargs)


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(decompress, "CompressatoriumClient",
                        lambda config: client)


# ------------------------------------------------------- the happy path --

def test_verified_output_allows_deletion(library, monkeypatch):
    library["output"].write_bytes(library["content"])
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=True)
    d = report.to_dict()

    assert d["verified"] == 1 and d["deleted"] == 1, d
    assert not library["source"].exists(), "verified original must be gone"
    assert library["output"].exists()


def test_without_the_flag_nothing_is_deleted(library, monkeypatch):
    library["output"].write_bytes(library["content"])
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=False)

    assert report.verified == 1 and report.deleted == 0
    assert library["source"].exists(), "the flag is the only thing that deletes"


# ------------------------------------------------------- the safety gates --

def test_unknown_output_keeps_the_original(library, monkeypatch):
    """No DAT claims the output: homebrew, translation, out-of-date DAT.

    The original is the only copy of whatever this is. Deleting it because
    the index did not recognise the replacement would destroy the file the
    index could not protect. A different size too, not just different bytes:
    same-size-wrong-hash is the BAD_DUMP case, covered next.
    """
    library["output"].write_bytes(b"a" * 5000)  # no DAT entry at this size
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=True)
    d = report.to_dict()

    assert d["deleted"] == 0 and d["kept"] == 1, d
    assert d["items"][0]["dat_match"] == "unknown", d["items"][0]
    assert library["source"].exists(), "unknown output must keep the original"


def test_bad_dump_keeps_the_original_and_fails_loudly(library, monkeypatch):
    """Right size, wrong bytes: corrupt or tampered. Kept, and reported."""
    # Same length as the DAT entry, different content -> BAD_DUMP verdict.
    library["output"].write_bytes(bytes(reversed(library["content"])))
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=True)
    d = report.to_dict()

    assert d["deleted"] == 0 and d["failed"] == 1, d
    item = d["items"][0]
    assert item["dat_match"] == "bad-dump", item
    assert library["source"].exists()


def test_empty_dat_index_never_deletes(library, monkeypatch):
    """The gate that matters most: no DATs, no deletions, whatever the flag.

    An empty index is the common day-one state, and a batch run against it
    must degrade to "extracted, kept everything" rather than "extracted,
    deleted everything it could not check".
    """
    library["output"].write_bytes(library["content"])
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = decompress.run_batch(
        library["root"], compressatorium_url="http://fake:8080",
        dats=DatIndex(),          # empty on purpose
        delete_originals=True,
        job_timeout=5, poll_interval=0)
    d = report.to_dict()

    assert d["deleted"] == 0, d
    assert library["source"].exists(), "empty index must keep every original"


def test_no_dats_at_all_never_deletes(library, monkeypatch):
    """dats=None -- verification skipped entirely -- deletion stays shut."""
    library["output"].write_bytes(library["content"])
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = decompress.run_batch(
        library["root"], compressatorium_url="http://fake:8080",
        dats=None, delete_originals=True,
        job_timeout=5, poll_interval=0)

    assert report.to_dict()["deleted"] == 0
    assert library["source"].exists()


def test_remote_match_verifies_what_local_hashes_cannot(library, monkeypatch):
    """CHD/Dolphin outputs: the container's file hash matches no DAT of its
    contents, and the service's embedded-hash matching is the second opinion
    that answers instead."""
    library["output"].write_bytes(b"container bytes, not the DAT payload")
    client = FakeClient(
        {str(library["source"]): library["output"]},
        remote_matches={str(library["output"])})
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=True)
    d = report.to_dict()

    assert d["verified"] == 1 and d["deleted"] == 1, d
    assert not library["source"].exists()


# ------------------------------------------------------------ failure modes --

def test_service_failure_keeps_the_original(library, monkeypatch):
    """The job fails: nothing extracted, nothing verified, nothing deleted."""
    client = FakeClient(outputs={})  # every source fails
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=True)
    d = report.to_dict()

    assert d["failed"] == 1 and d["deleted"] == 0, d
    assert library["source"].exists()


def test_missing_output_file_is_a_bad_dump(library, monkeypatch):
    """Job "completed" but produced nothing readable: trust nothing."""
    client = FakeClient({str(library["source"]):
                         library["root"] / "never_written.gba"})
    _patch_client(monkeypatch, client)

    report = _run(library, client, delete_originals=True)
    d = report.to_dict()

    assert d["deleted"] == 0, d
    assert library["source"].exists()


def test_unreachable_service_reports_cleanly(tmp_path):
    report = decompress.run_batch(
        tmp_path, compressatorium_url="http://127.0.0.1:59999",
        job_timeout=1, poll_interval=0)
    # Nothing scanned, so the error is about the empty library, not the
    # service -- either way: no exception, no deletion, honest report.
    assert isinstance(report.to_dict(), dict)


def test_no_url_is_an_error_not_a_crash(tmp_path):
    (tmp_path / "game.chd").write_bytes(b"x")
    report = decompress.run_batch(tmp_path)
    assert report.error == "no compressatorium URL configured"


def test_dry_run_submits_nothing(library, monkeypatch):
    client = FakeClient({str(library["source"]): library["output"]})
    _patch_client(monkeypatch, client)

    report = _run(library, client, dry_run=True, delete_originals=True)

    assert client.submitted == [], "a dry run must never reach the service"
    assert report.to_dict()["skipped"] == 1
    assert library["source"].exists()
    assert not library["output"].exists()


def test_a_nup_is_never_decompression_work(tmp_path):
    """A NUP may hold zstd extents, but it is one container RomM reads whole.
    Unpacking it is not a Compressatorium job, and a scan must not queue one."""
    assert ".nup" not in decompress.COMPRESSED_EXTENSIONS
    assert mode_for("Game.nup") == ""
    (tmp_path / "Game.nup").write_bytes(b"NUP\x01")

    assert decompress.scan_compressed(tmp_path) == []
