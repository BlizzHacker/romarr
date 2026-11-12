"""Tests for the absorbed eXo2romm ROM Hub plugin & ROMarr integration."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
import warnings
import zipfile
from pathlib import Path

import pytest

from romarr import ecosystem, hub, openapi
from romarr.app import ROMarr, make_handler
from romarr.ui import page


def test_exo2romm_status_credits_derkommutator_and_notes_inspiration():
    status = hub.exo2romm_status()
    assert status["available"] is True
    assert status["plugin_slug"] == "exo2romm"
    assert status["upstream_author"] == "derkommutator"
    assert status["upstream_url"] == "https://github.com/derkommutator/eXo2romm"
    assert status["plugin_repo"] == "https://github.com/BlizzHacker/rom-hub-exo2romm"
    assert "derkommutator" in status["credit"]
    assert "inspired to assist with more users having this" in status["credit"]
    assert "inspired to assist with more users having" in status["inspired_note"]


def test_ui_and_ecosystem_credit_derkommutator_for_exo2romm():
    html = page()
    assert "/api/v1/hub/exo2romm" in html
    assert "https://github.com/derkommutator/eXo2romm" in html
    assert "https://github.com/BlizzHacker/rom-hub-exo2romm" in html
    assert "inspired to assist with more users having this" in html
    for fid in ("exo-in", "exo-dos", "exo-out", "exo-log", "exo-overwrite"):
        assert f'for="{fid}"' in html and f'id="{fid}"' in html
    assert "exoJobSeq" in html
    assert "split(/[\\r\\n]+/)" in html

    projects = {p.name: p for p in ecosystem.all_projects()}
    assert "eXo2romm" in projects
    assert projects["eXo2romm"].repo == "https://github.com/derkommutator/eXo2romm"
    assert "inspired to assist with more users having this" in projects["eXo2romm"].blurb


def test_rewrite_dosbox_conf_rules_and_eol_preservation():
    raw = (
        "[autoexec]\r\n"
        "# comment .\\eXoDOS\\Abaron\\DISK1\r\n"
        "rem mount c .\\eXoDOS\\Abaron\r\n"
        "mount c .\\eXoDOS\\Abaron\r\n"
        '@IMGMOUNT d ".\\exodos\\ABARON\\CD\\ABARON.CUE" -t cdrom\r\n'
        "mount e .\\eXoDOS\\OtherGame\\Extra\r\n"
        "boot .\\eXoDOS\\\r\n"
        "; raw bytes \x80\xff kept\n"
    )
    raw_bytes = raw.encode("latin-1")
    edited, count = hub.rewrite_exo_dosbox_conf_bytes(raw_bytes, "Abaron")
    assert count == 3
    text = edited.decode("latin-1")
    assert "# comment .\\eXoDOS\\Abaron\\DISK1\r\n" in text
    assert "rem mount c .\\eXoDOS\\Abaron\r\n" in text
    assert "mount c .\\\r\n" in text
    assert '@IMGMOUNT d ".\\CD\\ABARON.CUE" -t cdrom\r\n' in text
    assert "mount e .\\eXoDOS\\OtherGame\\Extra\r\n" in text
    assert "boot .\\\r\n" in text
    assert "; raw bytes \x80\xff kept\n" in text


def test_rewrite_dosbox_conf_literal_metacharacters():
    raw = b"mount c .\\eXoDOS\\Doom (1993) [v1.9]+\\WAD\n"
    edited, count = hub.rewrite_exo_dosbox_conf_bytes(raw, "Doom (1993) [v1.9]+")
    assert count == 1
    assert edited == b"mount c .\\WAD\n"


def test_inspect_exo_zip_rejects_invalid_and_traversal_archives(tmp_path):
    missing = tmp_path / "missing.zip"
    with pytest.raises(ValueError, match="not found"):
        hub.inspect_exo_zip(missing)

    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(ValueError, match="empty zip"):
        hub.inspect_exo_zip(empty)

    flat = tmp_path / "flat.zip"
    with zipfile.ZipFile(flat, "w") as zf:
        zf.writestr("GAME.EXE", b"mz")
    with pytest.raises(ValueError, match="zip root"):
        hub.inspect_exo_zip(flat)

    multi = tmp_path / "multi.zip"
    with zipfile.ZipFile(multi, "w") as zf:
        zf.writestr("A/1.exe", b"1")
        zf.writestr("B/2.exe", b"2")
    with pytest.raises(ValueError, match="single top-level directory"):
        hub.inspect_exo_zip(multi)

    trav = tmp_path / "trav.zip"
    with zipfile.ZipFile(trav, "w") as zf:
        zf.writestr("../outside/1.exe", b"1")
    with pytest.raises(ValueError, match="traversal"):
        hub.inspect_exo_zip(trav)

    backslash = tmp_path / "backslash.zip"
    with zipfile.ZipFile(backslash, "w") as zf:
        zi = zipfile.ZipInfo("Top/GAME.EXE")
        zi.filename = "Top\\GAME.EXE"
        zf.writestr(zi, b"1")
    with pytest.raises(ValueError, match="backslash"):
        hub.inspect_exo_zip(backslash)

    file_conflict = tmp_path / "conflict.zip"
    with zipfile.ZipFile(file_conflict, "w") as zf:
        zf.writestr("Top", b"file-at-root")
        zf.writestr("Top/GAME.EXE", b"1")
    with pytest.raises(ValueError, match="conflicts"):
        hub.inspect_exo_zip(file_conflict)


def test_run_exo2romm_dry_run_repackage_resume_overwrite_and_error_log(tmp_path):
    in_dir = tmp_path / "zips"
    dos_dir = tmp_path / "!dos"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (dos_dir / "Abaron").mkdir(parents=True)
    (dos_dir / "Abaron" / "dosbox.conf").write_bytes(
        b"[autoexec]\r\nmount c .\\eXoDOS\\Abaron\r\n@c:\r\nABARON.EXE\r\n"
    )

    in_zip = in_dir / "Abaron (1995).zip"
    with zipfile.ZipFile(in_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Abaron/ABARON.EXE", b"PAYLOAD")
        zf.writestr("Abaron/dosbox.conf", b"old-pack-root-conf")
    before = in_zip.read_bytes()

    no_inputs = hub.run_exo2romm({"dos_dir": str(dos_dir), "output_dir": str(out_dir)})
    assert no_inputs["ok"] is False

    dry = hub.run_exo2romm({
        "input_paths": [str(in_dir)],
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
        "dry_run": True,
    })
    assert dry["ok"] is True
    assert dry["processed"] == 1
    assert dry["results"][0]["status"] == "dry_run"
    assert "mount c .\\" in dry["results"][0]["preview_conf"]
    assert not out_dir.exists()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run1 = hub.run_exo2romm({
            "input_paths": [str(in_zip)],
            "dos_dir": str(dos_dir),
            "output_dir": str(out_dir),
            "dry_run": False,
        })
    assert not [w for w in caught if "Duplicate name" in str(w.message)]
    assert run1["ok"] is True
    assert run1["processed"] == 1
    assert run1["skipped"] == 0
    assert in_zip.read_bytes() == before

    out_zip = out_dir / "Abaron (1995).zip"
    assert out_zip.is_file()
    with zipfile.ZipFile(out_zip, "r") as zf:
        assert zf.namelist().count("Abaron/dosbox.conf") == 1
        assert zf.read("Abaron/ABARON.EXE") == b"PAYLOAD"
        assert b"mount c .\\\r\n" in zf.read("Abaron/dosbox.conf")

    # Resume-safe skip on second run (clean stays True since no archive errored)
    run2 = hub.run_exo2romm({
        "input_paths": [str(in_zip)],
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
        "dry_run": False,
    })
    assert run2["processed"] == 0
    assert run2["skipped"] == 1
    assert run2["clean"] is True
    assert run2["results"][0]["status"] == "skipped_exists"

    # Explicit overwrite reprocesses the archive
    run3 = hub.run_exo2romm({
        "input_paths": [str(in_zip)],
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
        "dry_run": False,
        "overwrite": True,
    })
    assert run3["processed"] == 1

    # Missing template writes to error log and reports all-failed batch as ok=False
    bad_zip = in_dir / "MissingTemplate.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("NoTemplate/GAME.EXE", b"x")
    run4 = hub.run_exo2romm({
        "input_paths": [str(bad_zip)],
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
        "dry_run": False,
    })
    assert run4["ok"] is False
    assert run4["clean"] is False
    assert run4["skipped"] == 1
    assert "MissingTemplate" in run4["error"]
    log_text = Path(run4["error_log"]).read_text(encoding="utf-8")
    assert "MissingTemplate.zip" in log_text

    # Omitting dry_run uses default_profile (dry_run=True), even when output exists
    default_preview = hub.run_exo2romm({
        "input_paths": [str(in_zip)],
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
    })
    assert default_preview["ok"] is True
    assert default_preview["dry_run"] is True
    assert default_preview["results"][0]["status"] == "dry_run"
    assert "mount c .\\" in default_preview["results"][0]["preview_conf"]

    # Non-boolean flags are rejected explicitly
    bad_over = hub.run_exo2romm({
        "input_paths": [str(in_zip)],
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
        "overwrite": "false",
    })
    assert bad_over["ok"] is False
    assert "boolean" in bad_over["error"]

    # Refuses replacing source archive in-place when output_dir == input zip parent
    same_path = hub.run_exo2romm({
        "input_paths": [str(in_zip)],
        "dos_dir": str(dos_dir),
        "output_dir": str(in_zip.parent),
        "dry_run": False,
        "overwrite": True,
    })
    assert same_path["ok"] is False
    assert "overwrite input" in same_path["error"]

    # Preserves commas inside zip filenames
    comma_zip = in_dir / "Tetris, The.zip"
    with zipfile.ZipFile(comma_zip, "w") as zf:
        zf.writestr("Abaron/GAME.EXE", b"mz")
    comma_res = hub.run_exo2romm({
        "input_paths": str(comma_zip),
        "dos_dir": str(dos_dir),
        "output_dir": str(out_dir),
    })
    assert comma_res["ok"] is True
    assert comma_res["processed"] == 1


def test_http_endpoints_and_openapi_and_mcp_manifest(tmp_path):
    spec = openapi.spec("1.0.0")
    assert "get" in spec["paths"]["/api/v1/hub/exo2romm"]
    post_op = spec["paths"]["/api/v1/hub/exo2romm"]["post"]
    props = post_op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "input_paths" in props and "dry_run" in props and "overwrite" in props
    tool_names = {t["name"] for t in hub.mcp_manifest()["tools"]}
    assert "romarr_exo2romm_repackage" in tool_names

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json"), "ROMARR_API_KEY": "secret-key"})
    handler_cls = make_handler(service)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/v1/hub/exo2romm")
        assert exc.value.code == 401

        req = urllib.request.Request(
            f"{base}/api/v1/hub/exo2romm",
            headers={"X-Api-Key": "secret-key"},
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
        assert data["upstream_author"] == "derkommutator"

        dos_dir = tmp_path / "!dos"
        out_dir = tmp_path / "out"
        (dos_dir / "Demo").mkdir(parents=True)
        (dos_dir / "Demo" / "dosbox.conf").write_text("[autoexec]\nmount c .\\eXoDOS\\Demo\n", encoding="utf-8")
        zip_path = tmp_path / "Demo.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Demo/GAME.COM", b"com")

        post_req = urllib.request.Request(
            f"{base}/api/v1/hub/exo2romm",
            method="POST",
            data=json.dumps({
                "input_paths": [str(zip_path)],
                "dos_dir": str(dos_dir),
                "output_dir": str(out_dir),
                "dry_run": False,
            }).encode("utf-8"),
            headers={"X-Api-Key": "secret-key", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(post_req) as resp:
            assert resp.status == 200
            result = json.loads(resp.read().decode("utf-8"))
        assert result["processed"] == 1
        assert (out_dir / "Demo.zip").is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_repackage_edge_cases_symlink_noclobber_mkdir_and_unreadable_dir(tmp_path, monkeypatch):
    import os

    dos_dir = tmp_path / "!dos"
    out_dir = tmp_path / "out"
    (dos_dir / "Demo").mkdir(parents=True)
    (dos_dir / "Demo" / "dosbox.conf").write_text("[autoexec]\nmount c .\\eXoDOS\\Demo\n", encoding="utf-8")
    zip_path = tmp_path / "Demo.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Demo/GAME.COM", b"com")

    def race_link(src, dst):
        Path(dst).write_bytes(b"concurrent")
        raise FileExistsError("exists")

    monkeypatch.setattr(os, "link", race_link)
    raced = hub.repackage_exo_zip(zip_path, dos_dir, out_dir)
    assert raced["status"] == "skipped_error"
    assert (out_dir / "Demo.zip").read_bytes() == b"concurrent"
    monkeypatch.undo()

    sym_dir = tmp_path / "sym"
    sym_dir.mkdir()
    real_target = sym_dir / "Other.zip"
    real_target.write_bytes(b"keep")
    sym_out = sym_dir / "Demo.zip"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == sym_out)
    sym_res = hub.repackage_exo_zip(zip_path, dos_dir, sym_dir, overwrite=True)
    assert sym_res["status"] == "skipped_error"
    assert real_target.read_bytes() == b"keep"
    monkeypatch.undo()

    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    mk_res = hub.repackage_exo_zip(zip_path, dos_dir, blocker / "sub")
    assert mk_res["status"] == "skipped_error"

    bad_dir = tmp_path / "bad_dir"
    bad_dir.mkdir()
    orig_iterdir = Path.iterdir

    def flaky_iterdir(self):
        if self == bad_dir:
            raise PermissionError("denied")
        return orig_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)
    batch = hub.run_exo2romm({
        "input_paths": [str(bad_dir), str(zip_path)],
        "dos_dir": str(dos_dir),
        "output_dir": str(tmp_path / "out2"),
        "dry_run": False,
    })
    assert batch["processed"] == 1
    assert batch["skipped"] == 1
    assert batch["clean"] is False
