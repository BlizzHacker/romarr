"""Read-only download progress, scoped to the recorded downloader identity.

Fuzzy title matching is deliberately excluded: progress must describe our job.
"""
from __future__ import annotations

import time

from .downloaders import active_torrent_queue_hash, torrent_queue_hash


def annotate(service, rows):
    now = time.monotonic()
    generation = tuple((id(c), str(getattr(c,"romarr_id","") or getattr(c,"config_id",""))) for c in service.clients)
    # Serialize refresh ownership, without making other HTTP callers wait for a client.
    with service._lock:
        cache = getattr(service, "_queue_progress_cache", (0, {}, ()))
        if cache[2] != generation:
            cache = (0, {}, generation)
        refresh = now - cache[0] >= 3 and not getattr(service, "_queue_progress_refresh", False)
        if refresh:
            service._queue_progress_refresh = True
    if refresh:
        live = {}
        deadline = now + 2
        try:
            for client in service.clients:
                identity = str(getattr(client, "romarr_id", "") or getattr(client, "config_id", ""))
                if not identity or not callable(getattr(client, "progress", None)):
                    continue
                if not getattr(client, "configured", True) or time.monotonic() >= deadline:
                    continue
                try:
                    live[identity] = client.progress(deadline=deadline)
                except Exception:
                    # Missing progress is unknown, never a zero percent/error transition.
                    continue
            with service._lock:
                cache = service._queue_progress_cache = (time.monotonic(), live, generation)
        finally:
            with service._lock:
                service._queue_progress_refresh = False
    owners: dict[tuple[str, str], int] = {}
    for row in rows:
        client_id = str(row.get("download_client") or "")
        if identity := active_torrent_queue_hash(row, client_id):
            key = (client_id, identity)
            owners[key] = owners.get(key, 0) + 1
    for row in rows:
        if row.get("state") not in ("grabbed", "downloading", "queued"):
            continue
        pool = cache[1].get(str(row.get("download_client") or ""), {})
        did, rid = str(row.get("download_id") or ""), str(row.get("release_id") or "")
        if did.startswith("qbit-tag:"):
            hits = [p for p in pool.values() if did[9:] in p.get("tags", [])]
        else:
            candidates = {did, rid[5:] if rid.startswith("btih:") else ""} - {""}
            exact_hash = torrent_queue_hash(did, rid)
            sole_owner = owners.get((str(row.get("download_client") or ""), exact_hash), 0) == 1
            hits = [p for h, p in pool.items()
                    if ((h == exact_hash and sole_owner) if p.get("_romarr_exact_torrent_owner")
                        else h in candidates)]
        if len(hits) == 1:
            row.update({k:hits[0][k] for k in ("progress", "eta", "speed", "dl_state") if k in hits[0]})
    return rows
