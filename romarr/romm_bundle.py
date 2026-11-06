"""Explicit reviewed Switch packages in RomM's multi-file game hierarchy."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
from .library import platform_dir

PACKAGE_SUFFIXES = {".nsp", ".xci"}


def _folder(name):
    if (any(ord(c) < 32 for c in name) or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?",name) or not name or name in {".", ".."} or name != Path(name).name
            or any(c in name for c in '\\/:*?"<>|') or name.endswith((".", " "))):
        raise ValueError("Choose a safe single game-folder name")
    return name


def decorate_preview(preview, platform, library_root, *, kind="", layout="flat", platform_folders=None, translation=False):
    if kind != "romm" or platform.slug != "switch":
        return preview
    root = platform_dir(library_root, platform, layout=layout, platform_folders=platform_folders, translation=translation)
    existing = []
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if folder.is_symlink() or not folder.is_dir() or folder.name.startswith("."):
                continue
            bases = [p for p in folder.iterdir() if not p.is_symlink() and p.is_file() and p.suffix.lower() in PACKAGE_SUFFIXES and p.stat().st_size > 0]
            if bases:
                existing.append({"name": folder.name, "bases": [p.name for p in bases],
                                 "identity": [(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(bases)]})
    preview["romm_routing"] = True
    preview["platform_root"] = str(root)
    preview["existing_games"] = existing
    preview["requires_review"] = True
    preview["token"] = hashlib.sha256(json.dumps([preview["token"],str(root.resolve()),existing],sort_keys=True).encode()).hexdigest()
    preview["note"] += " Reviewed Switch packages targeting RomM are grouped under one game folder, with update/ and dlc/ subfolders. Review the final destinations before confirming."
    return preview


def route_selection(preview, intent):
    if not preview.get("romm_routing"):
        return {}
    choices = intent["choices"]
    bases = [c for c in choices if c["role"] in {"base", "additional-game"}]
    optional = [c for c in choices if c["role"] in {"update", "dlc"}]
    attachment = intent.get("existing_game", "")
    if len(bases) > 1 and optional:
        raise ValueError("Updates/DLC with multiple base games need an explicit association; review each base as a separate import")
    if attachment:
        if attachment not in {g["name"] for g in preview["existing_games"]}:
            raise ValueError("Existing game folder changed or has no base package; refresh and review")
        if bases:
            raise ValueError("Attach only updates/DLC to an existing game; do not reimport its base")
    else:
        if not bases:
            raise ValueError("Select a base game or identify an existing game folder")
        attachment = _folder(Path(bases[0]["primary"]).stem)
    root = Path(preview["platform_root"])
    if root.exists() and not root.is_dir():
        raise ValueError("Platform library destination is not a directory; reconcile it first")
    routes = {}
    for choice in choices:
        primary = choice["primary"]
        name = Path(primary).name
        if Path(name).suffix.lower() not in PACKAGE_SUFFIXES:
            raise ValueError("Reviewed RomM Switch routing supports NSP/XCI packages; other payloads need review")
        game = attachment if choice["role"] in {"update", "dlc"} else _folder(Path(primary).stem)
        relative = PurePosixPath(game) / ("update" if choice["role"] == "update" else "dlc") / name if choice["role"] in {"update", "dlc"} else PurePosixPath(game) / name
        destination = root / str(relative)
        if any(parent.is_symlink() for parent in [destination, *destination.parents] if parent != root and root in parent.parents):
            raise ValueError("A reviewed game destination contains a symlink; review the library layout")
        if not destination.resolve().is_relative_to(root.resolve()):
            raise ValueError("Destination escapes the platform library")
        for parent in destination.parents:
            if parent == root:
                break
            if parent.exists() and not parent.is_dir():
                raise ValueError(f"Destination parent is not a directory; reconcile first: {parent}")
        if choice["role"] in {"base", "additional-game"} and (root / game).exists() and any((root / game).iterdir()):
            raise ValueError("A game folder already exists with this name; reconcile its base explicitly before importing another base")
        if destination.exists():
            raise ValueError(f"Destination already exists; reconcile before retry: {destination}")
        if choice["role"] in {"base", "additional-game"} and (root / name).exists():
            raise ValueError("The existing base is a loose file. Review and move it into a game folder first, then use Attach to existing game; ROMarr will not silently reorganize it")
        routes[primary] = str(relative)
    if len(set(routes.values())) != len(routes):
        raise ValueError("Selected packages collide at the same destination")
    return routes
