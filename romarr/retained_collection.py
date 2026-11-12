"""Persisted ownership checks for additive collection selections."""
import base64
import re
from urllib.parse import parse_qs, urlsplit


def magnet_hash(magnet):
    for xt in parse_qs(urlsplit(magnet).query).get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            value = xt[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", value):
                return value.lower()
            if re.fullmatch(r"[A-Za-z2-7]{32}", value):
                return base64.b32decode(value.upper()).hex()
        if xt.lower().startswith("urn:btmh:1220"):
            value = xt[13:]
            if re.fullmatch(r"[0-9a-fA-F]{64}", value):
                return value.lower()
    return ""


def _tag_from_id(download_id: str) -> str:
    raw = str(download_id or "")
    for prefix in ("qbit-tag:", "mdm-tag:"):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return ""


def owner_records(owners):
    out = []
    for owner in owners:
        if isinstance(owner, dict):
            record = owner
        else:
            record = {"client": getattr(owner, "download_client", ""),
                      "download_id": getattr(owner, "download_id", ""),
                      "selected_file": getattr(owner, "selected_file", {})}
        if record.get("selected_file") and _tag_from_id(record.get("download_id")):
            out.append(record)
    return out


def validate_owners(row, files, owners, client, category):
    torrent_hash = str(row.get("hash") or "")
    known = [o for o in owner_records(owners) if o.get("client") == client
             and o["selected_file"].get("hash") == torrent_hash]
    tags = {t.strip() for t in str(row.get("tags") or "").split(",") if t.strip()}
    known_tags = {_tag_from_id(o["download_id"]) for o in known}
    if not client or not known or row.get("category") != category or not tags or tags != known_tags:
        raise ValueError("retained torrent ownership/category/tags are missing or ambiguous")
    indexes = [f.get("index") for f in files]
    if len(set(indexes)) != len(indexes) or any(not isinstance(i, int) or i < 0 for i in indexes):
        raise ValueError("retained collection file indexes are ambiguous")
    for owner in known:
        identity = owner["selected_file"]
        found = [f for f in files if f.get("index") == identity.get("index")]
        if (len(found) != 1 or found[0].get("name") != identity.get("path")
                or found[0].get("size") != identity.get("size")
                or int(found[0].get("priority") or 0) <= 0):
            raise ValueError("retained selected file identity changed; review required")
