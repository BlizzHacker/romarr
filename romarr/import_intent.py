"""A reviewable import plan. Filename words never claim package identity; a title ID can."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from . import nup
from . import game_packages
from .library import _source_for, platform_dir, _digital_set_name, _set_name, package_routes, package_title
from .switch_attach import ATTACH_PLATFORMS, attach_dir as switch_attach_dir


def queue_identity(item):
    """Bind approvals to a durable queue row, not its current list position."""
    return [item.game, item.platform, item.at, item.download_id, item.release_id,
            item.release, item.indexer, item.external_request_id]


def preview_import(download, platform, library_root, *, layout="flat", platform_folders=None, queue_identity=None,
                   switch_extras=False, member_paths=None, package_folders=False, game_title="",
                   package_roles=None, selected_primaries=None):
    """Build a reviewable preview without hashing game payloads.

    `package_roles` (from a review) changes the package destinations shown, so a
    reviewed preview shows exactly where the importer will write.
    """
    source = _source_for(download, platform, member_paths=member_paths)
    try:
        return _preview_import_from_source(
            source, download, platform, library_root, layout=layout,
            platform_folders=platform_folders, queue_identity=queue_identity,
            switch_extras=switch_extras, member_paths=member_paths,
            package_folders=package_folders, game_title=game_title, package_roles=package_roles,
            selected_primaries=selected_primaries)
    finally:
        source.close()


def _preview_import_from_source(source, download, platform, library_root, *,
                                layout="flat", platform_folders=None,
                                queue_identity=None, switch_extras=False, member_paths=None,
                                package_folders=False, game_title="", package_roles=None,
                                selected_primaries=None):
    """Create a preview from an owned source before its scratch files close."""
    names = source.names()
    sets = source.rom_sets(platform)
    sizes = {}
    identity = []
    # Stat the archive itself or every source member. No game payload is hashed
    # just to display a preview; DAT verification remains in the actual importer.
    paths = member_paths
    if paths is None:
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
    package_routing = bool(package_folders and platform.slug in game_packages.PACKAGE_PLATFORMS)
    # Route the sets the importer will import: a review that leaves a second
    # base game out plans the folders without it, exactly as the import does.
    keep = None if selected_primaries is None else set(selected_primaries)
    routed = [s for s in sets if keep is None or s.primary in keep]
    folder_routes = (package_routes(source, routed, platform, target, package_title(download, game_title),
                                    roles=package_roles)
                     if package_routing else None)
    items = []
    used = set()
    for group in sets:
        members = list(group.members)
        used.update(members)
        unknown = Path(group.primary).suffix.lower() in ambiguous_packages
        role, evidence, confidence = (
            ("unknown", "Package title, type and version have not been inspected; choose its role manually.", "unknown")
            if unknown else
            ("base", "Platform-compatible ROM set; companions are kept together. Game identity is unverified.", "format-only"))
        header_role = (_nup_role(download, group.primary) or _package_role(source, platform, group.primary)
                       or (_title_id_role(platform, group.primary) if unknown else None))
        if header_role is not None:
            role, evidence, confidence = header_role
            unknown = role == "unknown"
        items.append({"primary": group.primary, "members": members,
                      "role": role,
                      "evidence": evidence,
                      "confidence": confidence,
                      "bytes": sum(sizes[m] for m in members) if all(m in sizes for m in members) else None,
                      "destination": str(_preview_destination(download, platform, target, group, switch_extras,
                                                              folder_routes)),
                      "selected": (len(sets) == 1 or folder_routes is not None) and not unknown})
    _review_mixed_title_ids(items, platform)
    # Several packages need a person to say how they relate, unless every one
    # is identified by its header or title ID and they file into one game folder.
    return {"token": token, "items": items,
            "extras": [n for n in names if n not in used],
            "requires_review": ((len(sets) > 1 and folder_routes is None)
                                or any(i["role"] == "unknown" for i in items)),
            # Package-platform reviews confirm the destination tree their roles produce.
            "package_routing": package_routing,
            "note": "This imports package files; it does not install or apply updates/DLC. Excluded files and the source download stay intact."}


def _package_role(source, platform, primary):
    """(role, evidence, confidence) from a PS4 package header, or None.

    Only a file on disk is read, and only its first 128 bytes. A header that
    names a content type this does not know returns None, which leaves the
    package for review; a file name never decides its role.
    """
    header = game_packages.header_of(source, platform.slug, primary)
    if header is None or not header.role:
        return None
    return (header.role,
            f"PS4 package header: {game_packages.describe_parts({header.role})} ({header.content_id})",
            "header")


#: Platforms whose package file names carry a title ID that says base, update or DLC.
TITLE_ID_PLATFORMS = ("switch", "switch-2", "3ds")
_MIXED_TITLE_IDS = "Packages in this download name different title IDs; choose their roles manually."


def _title_id_role(platform, primary):
    """(role, evidence, confidence) from the title ID in a package's name, or None.

    Only Switch, Switch 2 and 3DS title IDs are read: the ID itself says base,
    update or DLC (`game_packages.identify`). A name without exactly one such
    ID returns None, which leaves the package for review.
    """
    if platform.slug not in TITLE_ID_PLATFORMS:
        return None
    family, role = game_packages.identify(Path(primary.replace("\\", "/")).name, platform.slug)
    if not family or role not in game_packages.PARTS:
        return None
    return (role,
            f"Title ID: {game_packages.describe_parts({role})} of title {family.upper()}.",
            "title-id")


def _review_mixed_title_ids(items, platform):
    """Send title-ID roles back to review when the IDs name different games.

    An update or DLC for another game must not join this one, so a download
    whose packages belong to more than one title ID family is never trusted
    by title ID alone.
    """
    tid_items = [i for i in items if i["confidence"] == "title-id"]
    families = {game_packages.identify(Path(i["primary"].replace("\\", "/")).name, platform.slug)[0]
                for i in tid_items}
    if len(families) <= 1:
        return
    for item in tid_items:
        item.update(role="unknown", evidence=_MIXED_TITLE_IDS, confidence="unknown", selected=False)


_NUP_UNREADABLE = "Could not read this NUP header; choose its role manually."
_NUP_NO_GAME = "This NUP holds no game, update or DLC package; choose its role manually."


def _nup_role(download, primary):
    """(role, evidence, confidence) for a loose `.nup` set, or None if not one.

    A NUP's package records are validated plain data, unlike an encrypted NSP's,
    so the role comes from them rather than the name. A container whose header
    cannot be read, or that holds only system packages, stays "unknown" and so
    goes to review. Files inside an archive are not opened.
    """
    if Path(primary).suffix.lower() != ".nup":
        return None
    if download.is_dir():
        path = download / primary
    elif download.is_file() and download.suffix.lower() == ".nup":
        path = download
    else:
        return None
    try:
        header = nup.read_header(path)
    except (nup.NupError, OSError):
        return "unknown", _NUP_UNREADABLE, "unknown"
    if header.role == "unknown":
        return "unknown", _NUP_NO_GAME, "unknown"
    kinds = "+".join(p.kind for p in header.packages)
    return (header.role,
            f"NUP header: {kinds}, title ID {header.primary.title_id}.",
            "header")


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


def reviewed_roles(intent):
    """The role chosen for each package in a saved review.

    "additional-game" is kept: it tells the importer not to group that
    package with the base game.
    """
    return {c["primary"]: c["role"] for c in (intent or {}).get("choices", [])
            if isinstance(c, dict) and c.get("role") in (*game_packages.PARTS, "additional-game")}


def reviewed_selection(intent):
    """The packages a saved review chose, or None when there is no review."""
    chosen = [c["primary"] for c in (intent or {}).get("choices", []) if isinstance(c, dict)]
    return chosen or None


def package_destinations(preview, intent):
    """{primary: destination} the preview shows for the packages a review chose."""
    chosen = {c["primary"] for c in intent["choices"]}
    return {i["primary"]: i["destination"] for i in preview["items"] if i["primary"] in chosen}


def _preview_destination(download, platform, target, group, switch_extras, folder_routes=None):
    """The folder the importer will use, including update/DLC filing."""
    if group.is_multi_file:
        return target / (_digital_set_name(download) if platform.media == "digital"
                         else _set_name(group.primary))
    name = Path(group.primary.replace("\\", "/")).name
    if folder_routes is not None and group.primary in folder_routes:
        return target / folder_routes[group.primary] / name
    if switch_extras and platform.slug in ATTACH_PLATFORMS:
        attach = switch_attach_dir(name, target)
        if attach is not None:
            return attach / name
    return target / name
