"""Plan selective collection acquisition from an existing qBittorrent set."""
from __future__ import annotations

import re
from pathlib import PurePosixPath


ROM_SUFFIXES = {
    ".zip", ".7z", ".rar", ".gz", ".chd", ".cue", ".bin", ".iso",
    ".img", ".ccd", ".sub", ".mds", ".mdf", ".gdi", ".cdi", ".cso",
    ".pbp", ".rvz", ".wud", ".wux", ".wua", ".nsp", ".xci", ".nes",
    ".sfc", ".smc", ".gb", ".gbc", ".gba", ".nds", ".3ds", ".cia",
    ".rom", ".a26", ".a78", ".lnx", ".dsk", ".adf", ".tap", ".tzx",
    ".m3u", ".toc", ".wav", ".ape", ".flac",
}


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _core(value: str) -> str:
    stem = PurePosixPath(str(value or "").replace(chr(92), "/")).stem
    stem = re.split(r"[([]", stem, maxsplit=1)[0]
    return _normal(stem)


def plan_source(titles: list[str], dat, files: list[dict], cap_bytes: int,
                piece_size: int = 0) -> dict:
    """Match missing DAT names to source files and pack indivisible games.

    The cap is a staging budget. Two torrent pieces are reserved for boundary
    overhead, with a 4 MiB floor when qBittorrent does not report piece size.
    """
    cap = max(0, int(cap_bytes))
    reserve = max(4 * 1024 * 1024, max(0, int(piece_size)) * 2)
    usable = cap - reserve
    if usable <= 0:
        return {"error": "staging cap must be larger than the piece reserve",
                "reserve_bytes": reserve}
    cooked = []
    for row in files:
        try:
            index = int(row.get("index"))
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        path = PurePosixPath(str(row.get("name") or "").replace(chr(92), "/"))
        if size <= 0 or path.suffix.casefold() not in ROM_SUFFIXES:
            continue
        cooked.append({"index": index, "name": str(path), "size": size,
                       "progress": float(row.get("progress") or 0),
                       "priority": int(row.get("priority") or 0)})
    groups, claimed, unmatched = [], set(), []
    for title in titles:
        game = dat.games.get(title)
        if game is None:
            unmatched.append({"title": title, "reason": "title is absent from DAT"})
            continue
        expected = {_normal(PurePosixPath(r.name).stem) for r in game.roms}
        core = _core(title)
        hits = []
        for row in cooked:
            path = PurePosixPath(row["name"])
            base = _normal(path.stem)
            if base in expected or _core(row["name"]) == core:
                hits.append(row)
        if hits:
            parents = {PurePosixPath(r["name"]).parent for r in hits}
            stems = {PurePosixPath(r["name"]).stem.casefold() for r in hits}
            # Add only recognized disc sidecars that share a base or sit beside
            # one of the DAT-named tracks. This keeps unrelated documentation
            # and neighboring games out of the transfer plan.
            sidecars = {".cue", ".ccd", ".sub", ".img", ".bin", ".iso",
                        ".mds", ".mdf", ".gdi", ".cdi", ".m3u", ".toc",
                        ".wav", ".ape", ".flac"}
            for row in cooked:
                path = PurePosixPath(row["name"])
                if (path.parent in parents and path.suffix.casefold() in sidecars
                        and (path.stem.casefold() in stems
                             or _core(row["name"]) == core)):
                    hits.append(row)
        hits = list({r["index"]: r for r in hits}.values())
        if not hits:
            unmatched.append({"title": title, "reason": "no source file matched"})
            continue
        overlap = claimed.intersection(r["index"] for r in hits)
        if overlap:
            unmatched.append({"title": title,
                              "reason": "source files also match another title"})
            continue
        claimed.update(r["index"] for r in hits)
        groups.append({"id": str(len(groups)),
                       "title": title, "files": hits,
                       "size": sum(r["size"] for r in hits)})
    batches, current, used, skipped = [], [], 0, []
    for group in groups:
        if group["size"] > usable:
            skipped.append({"title": group["title"], "size": group["size"],
                            "reason": "title exceeds staging cap after reserve"})
            continue
        if current and used + group["size"] > usable:
            batches.append({"groups": current, "bytes": used})
            current, used = [], 0
        current.append(group)
        used += group["size"]
    if current:
        batches.append({"groups": current, "bytes": used})
    return {"ok": True, "cap_bytes": cap, "reserve_bytes": reserve,
            "usable_bytes": usable, "matched_titles": len(groups) - len(skipped),
            "unmatched": unmatched, "skipped": skipped,
            "batches": batches, "total_bytes": sum(b["bytes"] for b in batches)}
