"""Tests for ROMarr browser-extension multi-file download & completed capture."""
from __future__ import annotations

import os

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from romarr.extension_downloads import (
    ExtensionDownloadManager,
    check_remote_url,
    resolve_dest_dir,
    sanitize_filename,
    sanitize_rel_subdir,
)
from romarr.store import Store


def test_sanitization_and_ssrf_guards(tmp_path):
    assert sanitize_filename("../../evil/Game: [1].zip") == "Game_ [1].zip"
    assert sanitize_rel_subdir("../a/b\\c") == "a/b/c"

    assert check_remote_url("https://example.com/mod.zip", resolve_dns=False) == "https://example.com/mod.zip"
    for bad in (
        "ftp://example.com/file.zip",
        "http://127.0.0.1:7878/secret",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/internal",
    ):
        with pytest.raises(ValueError):
            check_remote_url(bad, resolve_dns=False)

    svc = SimpleNamespace(
        library=tmp_path / "lib",
        store=Store(tmp_path / "romarr.json"),
    )
    dest = resolve_dest_dir(svc, platform="nes", destination="Guides/Chrono")
    assert dest == (tmp_path / "lib" / "Guides" / "Chrono").resolve()
    assert dest.is_dir()


def test_start_batch_downloads_and_records_completed(tmp_path):
    svc = SimpleNamespace(
        library=tmp_path / "lib",
        store=Store(tmp_path / "romarr.json"),
        adopt=MagicMock(return_value={"ok": True}),
    )
    mgr = ExtensionDownloadManager()

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    fake_resp.status_code = 200
    fake_resp.iter_content.return_value = [b"PK\x03\x04test"]
    fake_resp.raise_for_status.return_value = None

    with patch("romarr.extension_downloads.requests.get", return_value=fake_resp):
        res = mgr.start_batch(
            svc,
            [{"url": "https://example.com/files/mod.zip", "filename": "mod.zip"}],
            page_url="https://example.com/mods",
            platform="pc",
        )
        assert res["ok"] is True
        assert res["queued"] == 1
        job_id = res["job_id"]
        for _ in range(40):
            st = mgr.get_status(job_id)
            if st["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.05)
        assert st["status"] == "done"
        assert st["completed"] == 1
        assert (tmp_path / "lib" / "pc" / "mod.zip").read_bytes() == b"PK\x03\x04test"

        # Second download of same filename must not overwrite existing file.
        res2 = mgr.start_batch(
            svc,
            [{"url": "https://example.com/files/mod.zip", "filename": "mod.zip"}],
            platform="pc",
        )
        for _ in range(40):
            st2 = mgr.get_status(res2["job_id"])
            if st2["status"] in ("done", "partial", "failed"):
                break
            time.sleep(0.05)
        assert st2["status"] == "done"
        assert (tmp_path / "lib" / "pc" / "mod_1.zip").read_bytes() == b"PK\x03\x04test"

    local_file = tmp_path / "local.nes"
    local_file.write_bytes(b"NES\x1a")
    svc.extension_import_roots = lambda: [str(tmp_path)]   # adopt only imports from allowed folders
    comp = mgr.record_completed(
        svc,
        [{"url": "https://example.com/local.nes", "filename": "local.nes",
          "local_path": str(local_file), "file_size": 4}],
        platform="nes",
        adopt=True,
    )
    assert comp["ok"] is True
    assert comp["recorded"] == 1
    assert comp["adopted"] == 1
    svc.adopt.assert_called_once_with(os.path.realpath(local_file), "nes")
