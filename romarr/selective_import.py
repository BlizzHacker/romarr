"""Resolve an owned collection member without walking or guessing the payload."""
from pathlib import Path, PurePosixPath
from .library import map_remote_path


def selected_import_path(queue, torrent, client, mappings=None) -> Path:
    identity = queue.selected_file
    if not identity:
        raise ValueError("selection identity is missing; reselect this release")
    wanted = str(identity.get("path") or "").replace("\\", "/")
    parts = PurePosixPath(wanted)
    if (not wanted or parts.is_absolute() or ".." in parts.parts
            or ":" in wanted or "\x00" in wanted):
        raise ValueError("unsafe selected member path")
    torrent_hash = str(torrent.get("hash") or "")
    if not torrent_hash or torrent_hash.lower() != str(identity.get("hash") or "").lower():
        raise ValueError("torrent identity changed")
    if (queue.download_client
            and queue.download_client != getattr(client, "romarr_id", "")):
        raise ValueError("download client identity changed")
    if not callable(getattr(client, "torrent_files", None)):
        raise ValueError("download client cannot verify selected file metadata")
    rows = client.torrent_files(torrent_hash)
    matches = [row for row in rows if row.get("index") == identity.get("index")]
    if len(matches) != 1:
        raise ValueError("missing or ambiguous selected file index")
    if sum(str(row.get("name") or "").replace("\\", "/") == wanted
           for row in rows) != 1:
        raise ValueError("ambiguous selected file path")
    member = matches[0]
    if (str(member.get("name") or "").replace("\\", "/") != wanted
            or member.get("size") != identity.get("size")):
        raise ValueError("selected file identity changed")
    if float(member.get("progress") or 0) != 1 or int(member.get("priority") or 0) <= 0:
        raise ValueError("selected file is incomplete or deselected")
    if not torrent.get("save_path"):
        raise ValueError("download save root is missing")
    root = Path(map_remote_path(torrent["save_path"], mappings)).resolve(strict=True)
    path = root.joinpath(*parts.parts).resolve(strict=True)
    if not path.is_relative_to(root) or path == root:
        raise ValueError("selected file escapes download save root")
    size = identity.get("size")
    if not isinstance(size, int) or size <= 0:
        raise ValueError("selected file size is missing or invalid")
    if not path.is_file() or path.stat().st_size != size:
        raise ValueError("selected file is absent or has an unexpected size")
    return path
