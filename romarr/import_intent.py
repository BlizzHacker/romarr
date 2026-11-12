"""A reviewable import plan. Filename hints never claim package identity."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .library import _source_for, platform_dir, _digital_set_name, _set_name


def queue_identity(item):
    """Bind approvals to a durable queue row, not its current list position."""
    return [item.game, item.platform, item.at, item.download_id, item.release_id,
            item.release, item.indexer, item.external_request_id]


def preview_import(download, platform, library_root, *, layout="flat", platform_folders=None, queue_identity=None):
    """Build a reviewable preview without hashing game payloads."""
    source = _source_for(download, platform)
    try:
        return _preview_import_from_source(
            source, download, platform, library_root, layout=layout,
            platform_folders=platform_folders, queue_identity=queue_identity)
    finally:
        source.close()


def _preview_import_from_source(source, download, platform, library_root, *,
                                layout="flat", platform_folders=None,
                                queue_identity=None):
    """Create a preview from an owned source before its scratch files close."""
    names = source.names()
    sets = source.rom_sets(platform)
    sizes = {}
    identity = []
    # Stat the archive itself or every source member. No game payload is hashed
    # just to display a preview; DAT verification remains in the actual importer.
    paths = [download] if download.is_file() else sorted(download.rglob("*"))
    for path in paths:
        if path.is_file():
            st = path.stat()
            identity.append((str(path.relative_to(download)) if path != download else ".", st.st_size, st.st_mtime_ns))
            sizes[path.relative_to(download).as_posix() if path != download else download.name] = st.st_size
    import zipfile
    if download.is_file() and zipfile.is_zipfile(download):
        with zipfile.ZipFile(download) as archive:
            sizes = {i.filename: i.file_size for i in archive.infolist()}
    # A nested ZIP member is not a direct outer-ZIP entry. The source index
    # already has its declared size; never decompress a ROM for a UI preview.
    member_size = getattr(source, "member_size", None)
    if member_size is not None:
        for name in names:
            size = member_size(name)
            if size is not None:
                sizes[name] = size
    token = hashlib.sha256(json.dumps([identity, names, queue_identity], sort_keys=True).encode()).hexdigest()
    ambiguous_packages = {".nsp", ".xci", ".pkg", ".cia"}
    target = platform_dir(library_root, platform, layout=layout, platform_folders=platform_folders)
    items = []
    used = set()
    for group in sets:
        members = list(group.members)
        used.update(members)
        unknown = Path(group.primary).suffix.lower() in ambiguous_packages
        items.append({"primary": group.primary, "members": members,
                      "role": "unknown" if unknown else "base",
                      "evidence": "Package title, type and version have not been inspected; choose its role manually." if unknown else "Platform-compatible ROM set; companions are kept together. Game identity is unverified.",
                      "confidence": "unknown" if unknown else "format-only",
                      "bytes": sum(sizes[m] for m in members) if all(m in sizes for m in members) else None,
                      "destination": str(target / ((_digital_set_name(download) if platform.media == "digital" else _set_name(group.primary)) if group.is_multi_file else Path(group.primary.replace("\\", "/")).name)),
                      "selected": len(sets) == 1 and not unknown})
    return {"token": token, "items": items,
            "extras": [n for n in names if n not in used],
            "requires_review": len(sets) > 1 or any(i["role"] == "unknown" for i in items),
            "note": "This imports package files; it does not install or apply updates/DLC. Excluded files and the source download stay intact."}


def choose_import(preview, choices, mode="single", existing_game=""):
    if mode not in ("single", "collection"):
        raise ValueError("Choose single game or collection mode")
    allowed = {i["primary"] for i in preview["items"]}
    if not isinstance(choices, list) or not choices:
        raise ValueError("Select a base game and its optional packages")
    seen = set()
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("primary") not in allowed:
            raise ValueError("Selection no longer matches the preview")
        if choice["primary"] in seen:
            raise ValueError("A package was selected twice")
        seen.add(choice["primary"])
        if choice.get("role") not in ("base", "update", "dlc", "additional-game"):
            raise ValueError("Classify each selected package; unknown packages require review")
    bases = sum(c["role"] == "base" for c in choices)
    attachment = bool(existing_game)
    if attachment and (not preview.get("romm_routing") or existing_game not in {g["name"] for g in preview.get("existing_games", [])}):
        raise ValueError("Select an identified existing game folder")
    if attachment and any(c["role"] not in {"update", "dlc"} for c in choices):
        raise ValueError("Existing game attachments accept updates/DLC only")
    if not attachment and (not bases or (mode == "single" and (bases != 1 or any(c["role"] == "additional-game" for c in choices)))):
        raise ValueError("Single game mode needs exactly one base; use collection mode for additional games")
    intent = {"token": preview["token"], "choices": choices, "mode": mode}
    if attachment:
        intent["existing_game"] = existing_game
    return intent
