"""Emulator catalog records and paginated public repository-fork discovery."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

CATALOG_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def bundled() -> dict:
    """Load the versioned, attributed catalog snapshot."""
    return json.loads(Path(__file__).with_name("emulator_catalog.json").read_text(encoding="utf-8"))


def request_headers(provider: str) -> dict:
    """Keep forge credentials scoped to their own API."""
    token = os.environ.get("ROMARR_GITHUB_TOKEN", "") if provider == "github" else ""
    return {"Authorization": "Bearer " + token} if token else {}


def api_url(entry: dict, suffix: str) -> str:
    """Build a forge endpoint from a catalog-owned repository identifier."""
    repo = str(entry.get("repo") or "")
    if not repo or not re.fullmatch(r"[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)+", repo):
        raise ValueError("This project has no supported source repository")
    provider = entry.get("provider", "github")
    if provider == "github":
        return "https://api.github.com/repos/" + repo + suffix
    if provider == "gitlab":
        return entry["api_base"].rstrip("/") + "/projects/" + quote(repo, safe="") + suffix
    if provider == "forgejo":
        return entry["api_base"].rstrip("/") + "/repos/" + repo + suffix
    raise ValueError("Use this project's source or download link")


class RepositoryCatalog:
    """Combine the shipped inventory with fork pages explicitly requested by a user."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "tools" / "emulators" / "discovered.json"

    def discovered(self) -> dict:
        """Read optional local fork records, preserving the shipped inventory."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("The local emulator discovery cache is invalid")
        return data

    def records(self) -> list[dict]:
        """Return all catalog and discovered records without an outbound request."""
        rows = {row["id"]: dict(row) for row in bundled()["items"]}
        rows.update(self.discovered())
        return list(rows.values())

    def get(self, key: str) -> dict:
        """Resolve an installed/catalog project without accepting an arbitrary URL."""
        for row in self.records():
            if row["id"] == key:
                return row
        raise ValueError("Unknown emulator")

    def forks(self, key: str, page: int = 1, session=None) -> dict:
        """Fetch a complete page and expose the next page instead of truncating a fork network."""
        entry = self.get(key)
        page = max(1, int(page))
        provider = entry.get("provider", "")
        per_page = 50
        params = {"page": page, "per_page": per_page, "limit": per_page}
        with (session or requests).get(api_url(entry, "/forks"), params=params,
                                      headers=request_headers(provider), timeout=20) as response:
            response.raise_for_status()
            raw = response.json()
            more = "next" in getattr(response, "links", {})
            total = response.headers.get("X-Total-Count") or response.headers.get("X-Total")
        if not isinstance(raw, list):
            raise ValueError("The forge did not return a fork list")
        rows = []
        origin = urlsplit("https://github.com" if provider == "github" else entry["api_base"])
        for item in raw:
            repo = item.get("path_with_namespace") if provider == "gitlab" else item.get("full_name")
            if not repo:
                continue
            url = item.get("web_url") if provider == "gitlab" else item.get("html_url")
            parsed = urlsplit(str(url or ""))
            if parsed.scheme != "https" or parsed.hostname != origin.hostname:
                continue
            identifier = "fork-" + hashlib.sha256(url.encode()).hexdigest()[:20]
            license = item.get("license") or {}
            rows.append({
                "id": identifier, "name": item.get("name") or repo,
                "repo": repo, "provider": provider, "api_base": entry.get("api_base", ""),
                "website": url, "guide": url, "systems": entry.get("systems", "See project"),
                "description": item.get("description") or "",
                "license": license.get("spdx_id") or license.get("name") or "See repository terms",
                "fork": True, "fork_of": entry["website"], "stars": item.get("stargazers_count", item.get("stars_count", 0)),
                "archived": bool(item.get("archived")),
                "sources": [{"source": api_url(entry, "/forks"), "page": page}],
            })
        with CATALOG_LOCK:
            existing = self.discovered()
            existing.update({row["id"]: row for row in rows})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".catalog-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(existing, handle)
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
        has_next = more or (len(raw) == per_page and not total)
        if total and str(total).isdigit():
            has_next = page * per_page < int(total)
        return {"items": rows, "page": page, "has_next": has_next, "total": total,
                "source": entry["website"]}


def resource_catalog(query: str = "", offset: int = 0, limit: int = 60) -> dict:
    """List companion references separately from installable emulator projects."""
    data = json.loads(Path(__file__).with_name("emulator_resources.json").read_text(encoding="utf-8"))
    needle = query.strip().casefold()
    rows = [row for row in data["items"] if needle in " ".join(
        str(row.get(key) or "") for key in ("name", "description", "category")).casefold()]
    offset, limit = max(0, int(offset)), min(100, max(1, int(limit)))
    return {"items": rows[offset:offset + limit], "total": len(rows), "available": len(data["items"]),
            "offset": offset, "limit": limit, "verified_date": data["verified_date"]}
