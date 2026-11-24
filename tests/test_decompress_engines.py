"""Which engine decompresses what, and what the local engine writes.

Compressatorium's only archive mode takes a .7z/.zip holding one cartridge
ROM. Everything else -- RAR archives and split RAR sets (#224), zipped
cue/bins, multi-disc sets -- is extracted by ROMarr itself, and must run even
with no service configured. These tests pin that split, the CHD and tool-list
refinements of what goes to the service, and the local engine's promises:
nothing overwritten, nothing half-written under a final name, clutter left
behind, every RAR volume deleted only when the set verified.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from romarr import decompress, decompress_local, library
from romarr.dat import Dat, DatIndex, Game, Rom, hash_bytes
from tests.test_decompress import FakeClient

DISC_1 = bytes(range(256)) * 64
DISC_2 = bytes(reversed(range(256))) * 64
#: A size no test DAT lists, so the verdict is unknown rather than bad dump.
UNLISTED = b"homebrew" * 1000


def _index(files: dict[str, bytes]) -> DatIndex:
    """A DAT index that verifies exactly these name -> content pairs."""
    index = DatIndex()
    games = {}
    for name, content in files.items():
        h = hash_bytes(content, suffix=Path(name).suffix)
        rom = Rom(name=name, size=h["size"], crc=h["crc"], md5=h["md5"],
                  sha1=h["sha1"])
        games[name] = Game(name=name, roms=(rom,))
    index.add(Dat(name="Test DAT", games=games))
    return index


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _run(root, client=None, monkeypatch=None, **kwargs):
    if client is not None:
        monkeypatch.setattr(decompress, "CompressatoriumClient",
                            lambda config: client)
    kwargs.setdefault("compressatorium_url", "http://fake:8080" if client else "")
    return decompress.run_batch(root, job_timeout=5, poll_interval=0, **kwargs)


def _items(report):
    return {Path(it.source).name: it for it in report.items}


# ------------------------------------------------------------- planning --

def test_a_single_cartridge_zip_goes_to_romz_extract(tmp_path):
    source = _zip(tmp_path / "Game.gba.zip", {"Game.gba": DISC_1})

    item = decompress.plan(decompress.Work(source))

    assert (item.engine, item.mode) == (decompress.SERVICE, "romz_extract")


def test_a_multi_file_zip_is_extracted_locally(tmp_path):
    source = _zip(tmp_path / "Game.zip", {"Game.cue": b"FILE", "Game.bin": DISC_1})

    item = decompress.plan(decompress.Work(source))

    assert item.engine == decompress.LOCAL
    assert "holds 2 file(s)" in item.plan_note


def test_a_zipped_disc_image_is_extracted_locally(tmp_path):
    """One member, but not a cartridge: romz_extract would refuse it."""
    source = _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1})

    assert decompress.plan(decompress.Work(source)).engine == decompress.LOCAL


def test_an_unlistable_archive_is_left_to_the_service(tmp_path):
    source = tmp_path / "Game.zip"
    source.write_bytes(b"not a zip")

    item = decompress.plan(decompress.Work(source))

    assert (item.engine, item.mode) == (decompress.SERVICE, "romz_extract")


def test_a_dvd_chd_is_submitted_as_extractdvd(tmp_path, monkeypatch):
    header = bytearray(124)
    header[0:8] = b"MComprHD"
    header[12:16] = struct.pack(">I", 5)
    header[48:56] = struct.pack(">Q", 124)
    entry = b"DVD " + struct.pack(">I", 1) + struct.pack(">Q", 0) + b"x"
    (tmp_path / "Game.chd").write_bytes(bytes(header) + entry)
    client = FakeClient()

    _run(tmp_path, client, monkeypatch)

    assert [s["mode"] for s in client.submitted] == ["extractdvd"]


# ------------------------------------------------------------- scanning --

def test_rar_sets_are_one_unit_each(tmp_path):
    for name in ("game-grp.part1.rar", "game-grp.part2.rar", "game-grp.part3.rar",
                 "other.rar", "other.r00", "Game.chd"):
        (tmp_path / name).write_bytes(b"Rar!\x1a\x07\x00")

    work = {w.source.name: w for w in decompress.scan_work(tmp_path)}

    assert set(work) == {"game-grp.part1.rar", "other.rar", "Game.chd"}
    assert [v.name for v in work["game-grp.part1.rar"].volumes] == [
        "game-grp.part1.rar", "game-grp.part2.rar", "game-grp.part3.rar"]
    assert decompress.plan(work["other.rar"]).engine == decompress.LOCAL


def test_an_unfinished_staging_folder_is_never_scanned(tmp_path):
    staging = tmp_path / f"{decompress_local.STAGING_PREFIX}abc"
    staging.mkdir()
    (staging / "Game.zip").write_bytes(b"x")

    assert decompress.scan_work(tmp_path) == []


# --------------------------------------------------------- local engine --

def test_multi_file_zip_extracts_into_its_own_folder_and_verifies(tmp_path):
    source = _zip(tmp_path / "Game (USA).zip",
                  {"Game (Disc 1).bin": DISC_1, "Game (Disc 2).bin": DISC_2})
    dats = _index({"Game (Disc 1).bin": DISC_1, "Game (Disc 2).bin": DISC_2})

    report = _run(tmp_path, dats=dats, delete_originals=True)

    item = _items(report)["Game (USA).zip"]
    assert item.status == "deleted", item
    assert report.extracted_locally == 1 and report.deleted == 1
    assert (tmp_path / "Game (USA)" / "Game (Disc 1).bin").read_bytes() == DISC_1
    assert (tmp_path / "Game (USA)" / "Game (Disc 2).bin").read_bytes() == DISC_2
    assert not source.exists()


def test_one_payload_file_lands_beside_its_archive(tmp_path):
    _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1,
                                 "__MACOSX/._Game (USA).iso": b"junk",
                                 ".DS_Store": b"junk"})

    report = _run(tmp_path)

    assert _items(report)["Game.zip"].output_files == [str(tmp_path / "Game (USA).iso")]
    assert not (tmp_path / "__MACOSX").exists(), "clutter is not game data"
    assert not (tmp_path / ".DS_Store").exists()


def test_local_work_runs_with_no_service_configured(tmp_path):
    _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1})
    (tmp_path / "Other.rvz").write_bytes(b"x")

    report = _run(tmp_path)

    items = _items(report)
    assert items["Game.zip"].status == "kept", "no DATs: extracted and kept"
    assert items["Other.rvz"].status == "skipped"
    assert "COMPRESSATORIUM_URL" in items["Other.rvz"].error
    assert report.error == "no compressatorium URL configured"


def test_no_service_needed_means_no_error(tmp_path):
    _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1})

    assert _run(tmp_path).error == ""


def test_an_existing_output_is_never_overwritten(tmp_path):
    source = _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1})
    existing = tmp_path / "Game (USA).iso"
    existing.write_bytes(b"precious")
    dats = _index({"Game (USA).iso": DISC_1})

    report = _run(tmp_path, dats=dats, delete_originals=True)

    item = _items(report)["Game.zip"]
    assert item.status == "failed" and "nothing overwritten" in item.error
    assert existing.read_bytes() == b"precious"
    assert source.exists()
    assert not list(tmp_path.glob(f"{decompress_local.STAGING_PREFIX}*"))


def test_an_existing_set_folder_is_never_reused(tmp_path):
    _zip(tmp_path / "Game.zip", {"a.bin": DISC_1, "b.bin": DISC_2})
    (tmp_path / "Game").mkdir()
    (tmp_path / "Game" / "a.bin").write_bytes(b"precious")

    item = _items(_run(tmp_path))["Game.zip"]

    assert item.status == "failed" and "nothing overwritten" in item.error
    assert (tmp_path / "Game" / "a.bin").read_bytes() == b"precious"
    assert not (tmp_path / "Game" / "b.bin").exists()


def test_a_failed_extraction_leaves_nothing_behind(tmp_path, monkeypatch):
    _zip(tmp_path / "Game.zip", {"a.bin": DISC_1, "b.bin": DISC_2})

    real_copy = library._ZipSource.copy

    def copy_then_fail(self, name, destination):
        if name == "b.bin":
            raise OSError("disk full")
        real_copy(self, name, destination)

    monkeypatch.setattr(library._ZipSource, "copy", copy_then_fail)

    item = _items(_run(tmp_path))["Game.zip"]

    assert item.status == "failed" and "disk full" in item.error
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Game.zip"]


def test_a_member_escaping_the_archive_is_not_written(tmp_path):
    _zip(tmp_path / "Game.zip", {"../evil.bin": b"x", "Game (USA).iso": DISC_1})

    _run(tmp_path)

    assert not (tmp_path.parent / "evil.bin").exists()
    assert (tmp_path / "Game (USA).iso").exists()


def test_dry_run_names_the_engine_and_writes_nothing(tmp_path, monkeypatch):
    _zip(tmp_path / "Set.zip", {"a.bin": DISC_1, "b.bin": DISC_2})
    (tmp_path / "Game.rvz").write_bytes(b"x")
    client = FakeClient()

    report = _run(tmp_path, client, monkeypatch, dry_run=True)

    items = _items(report)
    assert "would extract locally (holds 2 file(s)" in items["Set.zip"].dat_detail
    assert items["Game.rvz"].dat_detail == "dry run -- would run dolphin_iso on Compressatorium"
    assert client.submitted == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Game.rvz", "Set.zip"]


# --------------------------------------------------------- service gating --

def test_a_tool_the_service_reports_unavailable_is_not_submitted(tmp_path, monkeypatch):
    (tmp_path / "Game.nsz").write_bytes(b"x")
    (tmp_path / "Game.rvz").write_bytes(b"x")
    client = FakeClient(tools=frozenset({"dolphin", "chdman"}))

    report = _run(tmp_path, client, monkeypatch)

    assert [Path(s["file_path"]).name for s in client.submitted] == ["Game.rvz"]
    nsz = _items(report)["Game.nsz"]
    assert nsz.status == "skipped" and "prod.keys" in nsz.error


def test_no_tool_list_means_everything_is_submitted(tmp_path, monkeypatch):
    (tmp_path / "Game.nsz").write_bytes(b"x")
    client = FakeClient(tools=None)

    _run(tmp_path, client, monkeypatch)

    assert [s["mode"] for s in client.submitted] == ["nsz_decompress"]


def test_a_match_the_service_could_not_work_out_is_reported_and_kept(tmp_path, monkeypatch):
    from romarr.compressatorium import MatchResult

    source = tmp_path / "Game.rvz"
    source.write_bytes(b"x")
    output = tmp_path / "Game.iso"
    output.write_bytes(UNLISTED)
    client = FakeClient({str(source): output})
    client.match_file = lambda path: MatchResult(
        path=path, error="status 503: stuck on storage")

    report = _run(tmp_path, client, monkeypatch,
                  dats=_index({"Other.iso": DISC_1}), delete_originals=True)

    item = _items(report)["Game.rvz"]
    assert item.status == "kept"
    assert "could not match it: status 503" in item.dat_detail
    assert source.exists()


# ------------------------------------------------------------- RAR sets --

class _FakeRarSet:
    """A RAR set as the archive reader exposes it, without needing 7-Zip."""

    def __init__(self, members):
        self.members = members

    def names(self):
        return list(self.members)

    def copy(self, name, destination):
        Path(destination).write_bytes(self.members[name])

    def close(self):
        pass


def _write_part_set(folder: Path, count: int = 3) -> list[Path]:
    paths = []
    for n in range(1, count + 1):
        path = folder / f"game-grp.part{n}.rar"
        path.write_bytes(b"Rar!\x1a\x07\x00" + bytes([n]))
        paths.append(path)
    return paths


def test_a_verified_rar_set_deletes_every_volume(tmp_path, monkeypatch):
    volumes = _write_part_set(tmp_path)
    opened = []

    def fake_open(first, vols=None):
        opened.append((first, vols))
        return _FakeRarSet({"Game (USA).iso": DISC_1})

    monkeypatch.setattr(library, "open_archive", fake_open)

    report = _run(tmp_path, dats=_index({"Game (USA).iso": DISC_1}),
                  delete_originals=True)

    item = _items(report)["game-grp.part1.rar"]
    assert item.status == "deleted" and item.engine == decompress.LOCAL
    assert opened == [(volumes[0], volumes)], "the set is read through its first volume"
    assert not any(v.exists() for v in volumes)
    assert (tmp_path / "Game (USA).iso").read_bytes() == DISC_1


def test_an_unknown_rar_set_keeps_every_volume(tmp_path, monkeypatch):
    volumes = _write_part_set(tmp_path)
    monkeypatch.setattr(library, "open_archive",
                        lambda first, vols=None: _FakeRarSet({"Game.iso": UNLISTED}))

    report = _run(tmp_path, dats=_index({"Other.iso": DISC_1}),
                  delete_originals=True)

    assert _items(report)["game-grp.part1.rar"].status == "kept"
    assert all(v.exists() for v in volumes)


def test_a_real_split_rar_set_extracts_with_7zip(tmp_path):
    """End to end through 7-Zip, when a RAR-capable one is installed."""
    tool = {t.kind: t for t in library.rar_tools()}.get("7z")
    if tool is None:
        pytest.skip("7-Zip with RAR support is not installed")
    from tests.test_split_rar_sets import DATA, write_set

    volumes = write_set(tmp_path, "part")
    assert len(volumes) == 3

    report = _run(tmp_path, dats=_index({"Game (USA).iso": DATA}),
                  delete_originals=True)

    item = _items(report)[volumes[0].name]
    assert item.status == "deleted", item
    assert (tmp_path / "Game (USA).iso").read_bytes() == DATA
    assert not any(v.exists() for v in volumes)


# --- review fixes (#232) ---------------------------------------------------

def _corrupt_member(path: Path) -> None:
    """Break the compressed bytes of a ZIP's only member, keeping its directory."""
    data = bytearray(path.read_bytes())
    start = 30 + int.from_bytes(data[26:28], "little") + int.from_bytes(data[28:30], "little")
    for i in range(start + 2, min(start + 400, len(data) - 100)):
        data[i] ^= 0x5A
    path.write_bytes(bytes(data))


def test_a_corrupt_member_fails_only_its_own_archive(tmp_path):
    good = _zip(tmp_path / "Good.zip", {"Good (USA).iso": DISC_1})
    bad = tmp_path / "Bad.zip"
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Bad (USA).iso", bytes(range(256)) * 4096)
    _corrupt_member(bad)

    report = _run(tmp_path)

    items = _items(report)
    assert items["Bad.zip"].status == "failed"
    assert items["Good.zip"].status == "kept", "the rest of the batch still runs"
    assert good.exists() and bad.exists()


def test_an_unexpected_extraction_error_fails_only_that_item(tmp_path, monkeypatch):
    _zip(tmp_path / "A.zip", {"A (USA).iso": DISC_1})
    _zip(tmp_path / "B.zip", {"B (USA).iso": DISC_1})
    real = decompress_local.extract_archive

    def flaky(source, volumes, root):
        if Path(source).name == "A.zip":
            raise KeyError("surprise")
        return real(source, volumes, root)

    monkeypatch.setattr(decompress_local, "extract_archive", flaky)
    report = _run(tmp_path)

    items = _items(report)
    assert items["A.zip"].status == "failed" and "unexpected" in items["A.zip"].error
    assert items["B.zip"].status == "kept"


def test_an_unreachable_service_is_no_error_when_nothing_needed_it(tmp_path, monkeypatch):
    _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1})
    client = FakeClient()
    client.reachable = lambda: False

    report = _run(tmp_path, client=client, monkeypatch=monkeypatch)

    assert _items(report)["Game.zip"].status == "kept"
    assert report.error == ""


def test_an_archive_with_an_unextracted_member_is_never_deleted(tmp_path):
    source = _zip(tmp_path / "Game.zip", {"../evil.bin": b"x", "Game (USA).iso": DISC_1})
    dats = _index({"Game (USA).iso": DISC_1})

    report = _run(tmp_path, dats=dats, delete_originals=True)

    item = _items(report)["Game.zip"]
    assert item.dat_match == "verified"
    assert source.exists(), "an incomplete extraction never costs the original"
    assert "could not be extracted safely" in item.error


def test_a_failed_set_keeps_files_another_writer_added(tmp_path, monkeypatch):
    _zip(tmp_path / "Set.zip", {"a.bin": DISC_1, "b.bin": DISC_2})
    real = decompress_local._place
    calls = []

    def place(staged, target):
        calls.append(target)
        if len(calls) == 1:
            real(staged, target)
            (target.parent / "synced.txt").write_text("not ours")
            return
        raise OSError("disk full")

    monkeypatch.setattr(decompress_local, "_place", place)
    report = _run(tmp_path)

    assert _items(report)["Set.zip"].status == "failed"
    folder = tmp_path / "Set"
    assert (folder / "synced.txt").read_text() == "not ours"
    assert not (folder / "a.bin").exists(), "this run's own file is rolled back"


def test_a_replaced_original_is_not_deleted(tmp_path, monkeypatch):
    source = _zip(tmp_path / "Game.zip", {"Game (USA).iso": DISC_1})
    dats = _index({"Game (USA).iso": DISC_1})
    real = decompress._verify_all

    def verify_then_replace(items, dats, client, report):
        real(items, dats, client, report)
        source.unlink()
        source.write_bytes(b"a different download, same name")

    monkeypatch.setattr(decompress, "_verify_all", verify_then_replace)
    report = _run(tmp_path, dats=dats, delete_originals=True)

    item = _items(report)["Game.zip"]
    assert source.read_bytes() == b"a different download, same name"
    assert not item.deleted and "changed since it was extracted" in item.error


def test_the_first_rar_volume_is_deleted_last(tmp_path):
    item = decompress.DecompressItem(
        source=str(tmp_path / "Game.part1.rar"),
        volumes=[str(tmp_path / f"Game.part{i}.rar") for i in (1, 2, 3)])
    order = [p.name for p in decompress._originals(item)]
    assert order == ["Game.part2.rar", "Game.part3.rar", "Game.part1.rar"]


def test_one_zip_reader_serves_the_whole_extraction(tmp_path, monkeypatch):
    archive = _zip(tmp_path / "Set.zip", {f"{i}.bin": b"x" * 10 for i in range(20)})
    opened = []
    real = zipfile.ZipFile

    class Counting(real):
        def __init__(self, *a, **k):
            opened.append(a[0])
            super().__init__(*a, **k)

    monkeypatch.setattr(library.zipfile, "ZipFile", Counting)
    source = library.open_archive(archive)
    try:
        with source.session():
            for name in source.names():
                source.copy(name, tmp_path / f"out-{name}")
            assert len(opened) == 1, "one reader for the whole session"
        assert source._archive is None, "the session closes its reader"
    finally:
        source.close()


def test_listing_outside_a_session_keeps_no_archive_open(tmp_path):
    # A directory import lists many archives; none may stay open afterwards.
    sources = [library.open_archive(_zip(tmp_path / f"{i}.zip", {"a.bin": b"x"}))
               for i in range(5)]
    for source in sources:
        assert source.names() == ["a.bin"]
        assert source._archive is None
