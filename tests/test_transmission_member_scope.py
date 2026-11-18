"""Multifile Transmission imports use the torrent's own files."""

from __future__ import annotations

import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from romarr.app import ROMarr
from romarr.downloaders import Transmission
from romarr.import_intent import preview_import
from romarr.platforms import resolve
from romarr.store import QueueItem
from test_transmission_completion import HASH, client, success, torrent


def test_member_paths_list_only_the_torrent_files():
    """Exclude unrelated files from the client's member list."""
    torrent = {"downloadDir": "/dl", "percentDone": 1, "leftUntilDone": 0,
               "metadataPercentComplete": 1, "totalSize": 2, "status": 6,
               "files": [{"name": "Album/01.flac", "length": 1, "bytesCompleted": 1},
                         {"name": "Album/02.flac", "length": 1, "bytesCompleted": 1}]}
    if not Transmission._content_path(torrent):
        raise AssertionError("fixture should describe a complete payload")
    assert Transmission._member_paths(torrent) == ["/dl/Album/01.flac", "/dl/Album/02.flac"]


def test_queue_previews_and_imports_only_mapped_members(tmp_path, monkeypatch):
    """Ignore siblings during indexing, preview identity and import."""
    payload = tmp_path / "pack"
    payload.mkdir()
    discs = payload / "games"
    discs.mkdir()
    with zipfile.ZipFile(discs / "Game.zip", "w") as archive:
        archive.writestr("Game.gb", b"x" * 32768)
    (payload / "notes.txt").write_text("Torrent member")
    (payload / "Stranger.gb").write_bytes(b"y" * 32768)
    (payload / "unrelated.zip").write_bytes(b"This isn't a ZIP")

    source = client()
    source.romarr_id = "transmission"
    source._call = Mock(return_value=success(torrents=[torrent(downloadDir="/remote",
        files=[{"name": "pack/games/Game.zip"}, {"name": "pack/notes.txt"}])]))
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.clients = [source]
    service.queue.append(QueueItem("Game", "gb", "pack", 1, "grabbed",
                                   download_id=HASH, download_client=source.romarr_id))
    library = tmp_path / "library"
    monkeypatch.setattr(service, "library_for", lambda _: (
        {"path": str(library)}, SimpleNamespace(rescan=lambda _: None)))
    service.store.settings.update(remote_path_mappings=[{"remote": "/remote", "local": str(tmp_path)}],
                                  reconcile_after_import=False)

    preview = service.queue_import_plan(0, {})["preview"]
    assert [item["primary"] for item in preview["items"]] == ["games/Game.zip/Game.gb"]
    assert preview["extras"] == ["notes.txt"]
    assert not preview["requires_review"]
    (payload / "Stranger.gb").write_bytes(b"unrelated file changed")
    assert service.queue_import_plan(0, {})["preview"]["token"] == preview["token"]
    assert service.import_finished()[0]["ok"]
    assert [p.name for p in (library / "gb").iterdir()] == ["Game.gb"]


@pytest.mark.parametrize("multifile", [True, False])
def test_scoped_rar_refuses_an_unreported_volume(tmp_path, multifile):
    """Prevent the RAR reader from following a same-family volume on disk."""
    first = tmp_path / "Game.part01.rar"
    first.touch()
    (tmp_path / "Game.part02.rar").touch()
    scope = [first]
    download = first
    if multifile:
        download = tmp_path
        notes = tmp_path / "notes.txt"
        notes.touch()
        scope.append(notes)
    with pytest.raises(ValueError, match="RAR volume is outside the reported download"):
        preview_import(download, resolve("psx"), tmp_path / "library", member_paths=scope)
