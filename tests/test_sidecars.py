"""Sidecar boundary tests and proof through the authenticated HTTP handler."""
import json
import os
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from romarr.app import ROMarr, make_handler
from romarr.libraries import FolderConfig, FolderLibrary, Game
from romarr.sidecars import MAX_BYTES, read_nfo

pytestmark = pytest.mark.skipif(sys.platform != "win32" and os.open not in os.supports_dir_fd, reason="secure filesystem reading unavailable")


def fixture_game(tmp_path):
    """A real folder library game with a neighbouring NFO."""
    root = tmp_path / "library"
    root.mkdir()
    game = root / "Example.zip"
    game.write_bytes(b"rom")
    return root, game


def test_utf8_nfo_and_uppercase_extension(tmp_path):
    """UTF-8 BOM and uppercase NFO extensions remain readable."""
    root, game = fixture_game(tmp_path)
    game.with_suffix(".NFO").write_bytes(b"\xef\xbb\xbfRelease <script>alert(1)</script>\n")
    result = read_nfo(root, str(game))
    assert result["text"] == "Release <script>alert(1)</script>\n"
    assert result["filename"].lower() == "example.nfo"
    assert not result["truncated"]


def test_cp437_ascii_art_controls_and_bound(tmp_path):
    """Legacy art decodes and huge notes stop at the byte budget."""
    root, game = fixture_game(tmp_path)
    game.with_suffix(".nfo").write_bytes(b"\xdb\x1b\x00\n" + b"x" * MAX_BYTES)
    result = read_nfo(root, str(game))
    assert result["encoding"] == "cp437"
    assert result["text"].startswith("█\n")
    assert result["truncated"]
    assert len(result["text"]) <= MAX_BYTES


def test_no_arbitrary_neighbour_fallback_or_stale_game(tmp_path):
    """A generic folder NFO is never associated with the wrong release."""
    root, game = fixture_game(tmp_path)
    (root / "other.nfo").write_text("unrelated")
    assert read_nfo(root, str(game)) == {"available": False}
    game.with_suffix(".nfo").write_text("notes")
    game.unlink()
    assert read_nfo(root, str(game)) == {"available": False}


@pytest.mark.parametrize("attack", ["file", "parent", "root", "traversal", "fifo"])
def test_escape_and_special_files_fail_closed(tmp_path, attack):
    """Symlinks, parent traversal and blocking special files cannot be read."""
    if sys.platform == "win32" and attack in {"file", "parent", "root", "fifo"}:
        pytest.skip("POSIX link/FIFO fixtures; Windows reparse coverage is in file-report path tests")
    root, game = fixture_game(tmp_path)
    secret = tmp_path / "private.nfo"
    secret.write_text("private")
    if attack == "file":
        game.with_suffix(".nfo").symlink_to(secret)
    elif attack == "parent":
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / "private.zip").write_bytes(b"rom")
        game = root / "link/private.zip"
    elif attack == "root":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root, game = alias, alias / game.name
    elif attack == "traversal":
        (tmp_path / "private.zip").write_bytes(b"rom")
        game = root / "../private.zip"
    else:
        os.mkfifo(game.with_suffix(".nfo"))
    assert read_nfo(root, str(game)) == {"available": False}


def test_live_http_auth_cache_boundary_and_preview(tmp_path):
    """A real authenticated request reads only the cached game's bounded notes."""
    root, game = fixture_game(tmp_path)
    game.with_suffix(".nfo").write_text("Release notes")
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": "testkey"})
    service.game_libraries = [({"id": "local"}, FolderLibrary(FolderConfig(str(root))))]
    service._library_cache = ([Game(id=str(game), name="Example")], 0, 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}/api/v1/game/nfo?"
    try:
        with pytest.raises(HTTPError) as err:
            urlopen(base + urlencode({"id": str(game)}), timeout=5)
        assert err.value.code == 401
        def get(game_id):
            """Fetch notes using the documented API-key header."""
            request = Request(base + urlencode({"id": game_id}), headers={"X-Api-Key": "testkey"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        assert get(str(game))["text"] == "Release notes"
        assert get(str(game.with_suffix(".nfo"))) == {"available": False}
        assert get(str(tmp_path / "private.zip")) == {"available": False}
        service._library_cache = ([Game(id=str(game), name="Example", origin="cloud")], 0, 0)
        assert get(str(game)) == {"available": False}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor race fixture")
def test_parent_replacement_cannot_redirect_pinned_read(tmp_path, monkeypatch):
    """A directory swapped after its open cannot redirect the NFO lookup."""
    root, game = fixture_game(tmp_path)
    game.with_suffix(".nfo").write_text("public")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / game.name).write_bytes(b"rom")
    (outside / "Example.nfo").write_text("private")
    real_open = os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        """Replace the parent only after the reader has securely pinned it."""
        nonlocal swapped
        fd = real_open(path, flags, *args, **kwargs)
        if path == root.name and not swapped:
            swapped = True
            root.rename(tmp_path / "original")
            root.symlink_to(outside, target_is_directory=True)
        return fd

    # The platform capability check must still recognize the wrapped opener.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {raced_open})
    monkeypatch.setattr(os, "open", raced_open)
    assert read_nfo(root, str(game)) == {"available": False}
    assert swapped


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses secure native handles")
def test_unsupported_platform_is_explicit(tmp_path, monkeypatch):
    """Lack of secure opens never silently falls back to a racy path read."""
    root, game = fixture_game(tmp_path)
    game.with_suffix(".nfo").write_text("notes")
    monkeypatch.setattr(os, "supports_dir_fd", set())
    result = read_nfo(root, str(game))
    assert not result["available"]
    assert "unavailable" in result["reason"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native handle proof")
def test_windows_parent_handle_blocks_replacement(tmp_path):
    """Windows pins deny parent renames while a sidecar's native handle is open."""
    from romarr.file_reports import _PinnedLibrary
    root, game = fixture_game(tmp_path)
    notes = game.with_suffix(".nfo")
    notes.write_text("Windows notes")
    with _PinnedLibrary(root) as pins:
        with pins.open(pins.parent(notes.name)) as stream:
            with pytest.raises(OSError):
                root.rename(tmp_path / "replacement")
            assert stream.read() == b"Windows notes"
    assert read_nfo(root, str(game))["text"] == "Windows notes"


def test_relative_library_and_cached_game_id(tmp_path, monkeypatch):
    root, game = fixture_game(tmp_path)
    game.with_suffix(".nfo").write_text("Relative library notes")
    monkeypatch.chdir(tmp_path)
    assert read_nfo(Path("library"), "library/Example.zip")["text"] == "Relative library notes"


def test_utf8_character_split_at_preview_limit(tmp_path):
    root, game = fixture_game(tmp_path)
    game.with_suffix(".nfo").write_bytes(("é" * 65535 + "€").encode("utf-8"))
    result = read_nfo(root, str(game))
    assert result["encoding"] == "utf-8" and result["truncated"]
    assert result["text"] == "é" * 65535


def test_nfo_during_initial_library_load_is_unavailable():
    service = ROMarr.__new__(ROMarr)
    service._library_cache = (None, 0, 0)
    assert service.game_nfo("missing") == {"available": False}
