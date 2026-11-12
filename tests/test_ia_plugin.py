"""Offline compatibility proof with the companion Archive.org plugin checkout."""
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from romarr import hub
from romarr.ia_auth import configure
from romarr.ia_session import ArchiveOrgSession, ArchiveOrgTransport
from rom_hub.manifest import load_manifest


@pytest.mark.parametrize("archive", [False, True])
def test_authenticated_set_search_plan_and_download(tmp_path, monkeypatch, archive):
    source = os.environ.get("ROMARR_ARCHIVE_ORG_PLUGIN_SOURCE")
    if not source:
        pytest.skip("Set ROMARR_ARCHIVE_ORG_PLUGIN_SOURCE to the companion plugin checkout")
    plugin_dir = Path(source)
    monkeypatch.setattr(hub, "HOME", tmp_path)
    monkeypatch.setattr(hub, "_unsandboxed", lambda: True)
    for prefix in ("ROM_HUB_ARCHIVE_ORG_", "ROMARR_ARCHIVE_ORG_"):
        for key in ("EMAIL", "PASSWORD", "EMAIL_FILE", "PASSWORD_FILE"):
            monkeypatch.delenv(prefix + key, raising=False)
    monkeypatch.setenv("ROM_HUB_SECRET_STORE", "file")
    monkeypatch.setenv("ROM_HUB_SECRET_KEY", "offline-compatibility-key")
    assert configure(tmp_path, "account@example.com", "test-password")["ok"]
    files = {"Game.chd": b"disc", "Game.m3u": b"Game.chd\n"}
    seen = []

    def remote(request):
        assert "logged-in-sig=session" in request.headers.get("cookie", "")
        seen.append(str(request.url))
        path = request.url.path
        if path.endswith("advancedsearch.php"):
            return httpx.Response(200, json={"response": {"numFound": 0, "docs": []}})
        if path.startswith("/metadata/set-one/"):
            field = path.rsplit("/", 1)[-1]
            result = {"metadata": {"collection": ["softwarelibrary"]},
                      "files_count": 1 if archive else 2,
                      "d1": "ia800408.us.archive.org", "dir": "/16/items/set-one"}
            if field == "files":
                value = [{"name": "PSX.zip", "size": "100"}] if archive else [
                    {"name": name, "size": str(len(data))} for name, data in files.items()]
            else:
                value = result[field]
            return httpx.Response(200, json={"result": value})
        if path == "/view_archive.php":
            member = request.url.params.get("file")
            if member:
                return httpx.Response(200, content=files[member])
            links = ''.join('<tr><td><a href="//archive.org/download/set-one/PSX.zip/' +
                            quote(name) + '">file</a></td></tr>' for name in files)
            return httpx.Response(200, text="<table>" + links + "</table>")
        if path.startswith("/download/set-one/"):
            return httpx.Response(200, content=files[path.rsplit("/", 1)[-1]])
        raise AssertionError(str(request.url))

    def login(request):
        return httpx.Response(200, json={"success": True, "values": {"cookies": {
            "logged-in-user": "account%40example.com", "logged-in-sig": "session"}}})

    manager = ArchiveOrgSession(tmp_path, login_transport=httpx.MockTransport(login))
    monkeypatch.setattr("romarr.ia_session.archive_transport", lambda slug, root:
                        ArchiveOrgTransport(manager, httpx.MockTransport(remote))
                        if slug == "archive-org" else None)
    plugin = SimpleNamespace(slug="archive-org", enabled=True, path=plugin_dir,
                             manifest=load_manifest(plugin_dir / "manifest.toml"),
                             config={"file_sets": ["set-one/PSX.zip:psx" if archive else "set-one:psx"]})
    monkeypatch.setattr("rom_hub.registry.Registry.installed", lambda self: [plugin])
    monkeypatch.setattr("rom_hub.registry.Registry.get", lambda self, slug: plugin)
    rows, error = hub.search("Game", platform="psx")
    assert not error and rows
    assert all(row["source_id"].startswith("zip:" if archive else "set:") for row in rows)
    result = hub.fetch(rows[0], tmp_path / "downloads")
    assert result["ok"], result
    assert {Path(path).name: Path(path).read_bytes() for path in result["files"]} == files
    assert any("/download/" in url for url in seen)
    if archive:
        assert any("view_archive.php" in url for url in seen)
