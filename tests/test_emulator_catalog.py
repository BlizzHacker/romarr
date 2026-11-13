"""Catalog coverage, forge boundaries and core-install regression checks."""
import hashlib
import io
import json
import zipfile
import zlib

import pytest

from romarr.emulator_catalog import RepositoryCatalog, api_url, bundled
from romarr.emulators import EmulatorHub
from romarr.libretro import CoreHub, metadata


class Response:
    """Return an isolated forge or binary response."""
    headers = {}
    links = {}

    def __init__(self, payload=b"", data=None):
        self.payload, self.data = payload, data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.payload

    def json(self):
        return self.data


def test_catalog_pages_cover_every_record_without_duplicates(tmp_path):
    """Paging must expose the full inventory rather than silently cap at 26."""
    hub = EmulatorHub(tmp_path)
    total = hub.catalog()["total"]
    ids = [item["id"] for start in range(0, total, 100)
           for item in hub.catalog(offset=start, limit=100)["items"]]
    assert total > 1000
    assert len(ids) == len(set(ids)) == total
    for name in ("Lichii", "Eglantine", "AnyPS5", "RetroBat", "Playnite"):
        assert hub.catalog(query=name)["total"] > 0
    assert hub.catalog(query="DuckStation")["items"][0]["license"]


def test_forge_endpoints_keep_namespace_and_reject_paths():
    """Repository names cannot inject an endpoint path or query."""
    assert api_url({"provider": "gitlab", "repo": "group/sub/project",
                    "api_base": "https://gitlab.com/api/v4"}, "/forks").endswith(
                        "/projects/group%2Fsub%2Fproject/forks")
    with pytest.raises(ValueError):
        api_url({"repo": "owner/repo?token=bad"}, "/forks")


def test_github_forks_accept_forge_host_not_project_homepage(tmp_path):
    """Featured projects can use a separate homepage without losing all forks."""
    class Session:
        def get(self, url, **kwargs):
            assert url == "https://api.github.com/repos/libretro/RetroArch/forks"
            assert kwargs["params"]["page"] == 2
            return Response(data=[
                {"full_name": "alice/RetroArch", "name": "RetroArch",
                 "html_url": "https://github.com/alice/RetroArch"},
                {"full_name": "evil/RetroArch", "html_url": "https://evil.example/fork"}])
    catalog = RepositoryCatalog(tmp_path)
    result = catalog.forks("retroarch", page=2, session=Session())
    assert [row["repo"] for row in result["items"]] == ["alice/RetroArch"]
    assert result["page"] == 2
    assert catalog.get(result["items"][0]["id"])["fork_of"]


def test_core_catalog_includes_all_upstream_records(tmp_path):
    """All metadata entries, licenses and firmware requirements remain discoverable."""
    hub = CoreHub(tmp_path)
    rows = hub.catalog(limit=100)
    assert rows["available"] == len(metadata()["items"]) >= 300
    assert hub.catalog(query="PlayStation")["total"] > 0
    missing = hub.readiness("pcsx2_libretro")
    assert "firmware" in missing
    config = hub.configure()
    assert '--appendconfig' in config["usage"]
    assert (hub.root / "system").is_dir()


def core_server(monkeypatch, payload=b"native-library", archive_name="2048_libretro.so", wrong_crc=False):
    """Serve an official-shaped core index and one bounded package."""
    from romarr import libretro
    monkeypatch.setattr(libretro, "host_build", lambda: ("linux/x86_64/latest/", ".so"))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(archive_name, payload)
    crc = "00000000" if wrong_crc else format(zlib.crc32(payload) & 0xffffffff, "08x")

    class Session:
        def get(self, url, **kwargs):
            if url.endswith(".index-extended"):
                return Response(("2026-11-12 " + crc + " 2048_libretro.so.zip\n").encode())
            return Response(out.getvalue())
    return Session()


def test_core_verifies_native_crc_scans_and_records_local_sha(tmp_path, monkeypatch):
    """The index hashes the extracted core, not its ZIP container."""
    session = core_server(monkeypatch)
    scanned = []
    hub = CoreHub(tmp_path, session=session)
    result = hub.install("2048_libretro", scanner=lambda path: scanned.append(path.name))
    assert result["sha256"] == hashlib.sha256(b"native-library").hexdigest()
    assert scanned == ["2048_libretro.so.zip", "2048_libretro.so"]
    assert hub.catalog(query="2048")["items"][0]["installed"]
    assert result["readiness"]["ready"]


@pytest.mark.parametrize("options", [{"wrong_crc": True}, {"archive_name": "../2048_libretro.so"}])
def test_core_rejects_corruption_and_traversal(tmp_path, monkeypatch, options):
    """Unverified or malformed packages cannot publish an installed core."""
    hub = CoreHub(tmp_path, session=core_server(monkeypatch, **options))
    with pytest.raises(ValueError):
        hub.install("2048_libretro")
    assert not list(hub.root.glob("cores/*"))
    assert not list(hub.root.glob(".core-*"))


def test_core_scanner_failure_prevents_publish(tmp_path, monkeypatch):
    """A configured scanner must clear the core before it becomes available."""
    hub = CoreHub(tmp_path, session=core_server(monkeypatch))

    def reject(path):
        raise ValueError("scanner rejected")

    with pytest.raises(ValueError, match="scanner rejected"):
        hub.install("2048_libretro", scanner=reject)
    assert not list(hub.root.glob("cores/*"))


def test_firmware_paths_cannot_escape_system_directory(tmp_path, monkeypatch):
    """An upstream metadata mistake cannot expose files outside system storage."""
    from romarr import libretro
    monkeypatch.setattr(libretro, "metadata", lambda: {"items": [
        {"id": "fixture", "firmware": [{"path": "../../secret", "opt": "false", "desc": "Bad path"}]}]})
    result = CoreHub(tmp_path).readiness("fixture")
    assert result["missing_required"] == ["../../secret"]
    assert not result["ready"]
