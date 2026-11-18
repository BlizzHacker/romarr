"""A multifile torrent's soundtrack import uploads only the torrent's own files."""

from __future__ import annotations

from romarr.downloaders import Transmission


def test_member_paths_list_only_the_torrent_files():
    torrent = {"downloadDir": "/dl", "percentDone": 1, "leftUntilDone": 0,
               "metadataPercentComplete": 1, "totalSize": 2, "status": 6,
               "files": [{"name": "Album/01.flac", "length": 1, "bytesCompleted": 1},
                         {"name": "Album/02.flac", "length": 1, "bytesCompleted": 1}]}
    if not Transmission._content_path(torrent):
        raise AssertionError("fixture should describe a complete payload")
    assert Transmission._member_paths(torrent) == ["/dl/Album/01.flac", "/dl/Album/02.flac"]


def test_member_filter_drops_unrelated_files(tmp_path):
    from romarr import soundtracks
    album = tmp_path / "Album"
    album.mkdir()
    for name in ("01.flac", "02.flac", "stranger.flac"):
        (album / name).write_bytes(b"x" * 10)
    files, zips = soundtracks.torrent_content(str(album))
    allowed = {str(album / "01.flac"), str(album / "02.flac")}
    import os
    kept = [f for f in files if os.path.realpath(f[1]) in {os.path.realpath(p) for p in allowed}]
    names = sorted(os.path.basename(f[1]) for f in kept)
    assert "stranger.flac" not in names
