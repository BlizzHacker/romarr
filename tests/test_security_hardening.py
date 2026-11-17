"""Code-scanning fixes: confined eXo2romm paths, Steam host pinning, linear redaction, safe launch args."""
from __future__ import annotations

import time

import pytest

from romarr import hub
from romarr.app import ROMarr, redact_query_credentials
from romarr.lists import _steam_public_entries


def test_exo2romm_refuses_paths_outside_the_allowed_roots(tmp_path):
    allowed = tmp_path / "library"
    allowed.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for field in ("dos_dir", "output_dir", "error_log"):
        cfg = {"dos_dir": str(allowed), "output_dir": str(allowed / "dos"),
               "input_paths": [str(allowed / "game.zip")], "dry_run": True,
               field: str(outside / "x")}
        result = hub.run_exo2romm(cfg, allowed_roots=[str(allowed)])
        assert not result["ok"] and field in result["error"]
    escaped = hub.run_exo2romm({"dos_dir": str(allowed), "output_dir": str(allowed / "dos"),
                                "input_paths": [str(allowed / ".." / "elsewhere" / "game.zip")]},
                               allowed_roots=[str(allowed)])
    assert not escaped["ok"] and "input_paths[0]" in escaped["error"]


def test_exo2romm_symlink_out_of_a_root_is_refused(tmp_path):
    allowed = tmp_path / "library"
    allowed.mkdir()
    (tmp_path / "secret").mkdir()
    (allowed / "link").symlink_to(tmp_path / "secret", target_is_directory=True)
    result = hub.run_exo2romm({"dos_dir": str(allowed), "output_dir": str(allowed / "link"),
                               "input_paths": [str(allowed)]}, allowed_roots=[str(allowed)])
    assert not result["ok"] and "output_dir" in result["error"]


def test_exo2romm_inside_the_roots_still_runs(tmp_path):
    allowed = tmp_path / "library"
    allowed.mkdir()
    result = hub.run_exo2romm({"dos_dir": str(allowed), "output_dir": str(allowed / "dos"),
                               "input_paths": [str(allowed)], "dry_run": True},
                              allowed_roots=[str(allowed)])
    assert "must be inside" not in str(result.get("error", ""))


def test_the_api_confines_exo2romm_to_configured_folders(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json"), "LIBRARY_PATH": str(tmp_path / "roms")})
    roots = svc.exo2romm_roots()
    assert str(tmp_path) in roots or str(tmp_path / "roms") in roots
    assert "/" not in roots and "/etc" not in roots


class _Http:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)

        class Response:
            status_code = 200
            headers = {}
            text = "<gamesList></gamesList>"
            content = text.encode()

        return Response()


@pytest.mark.parametrize("profile", [
    "https://evil.example/steamcommunity.com/id/gaben",
    "https://steamcommunity.com.evil.example/id/gaben",
    "ftp://steamcommunity.com/id/gaben",
    "https://steamcommunity.com/groups/gaben",
    "gaben/../../evil",
])
def test_steam_lists_never_fetch_another_host(profile):
    http = _Http()
    with pytest.raises(ValueError):
        _steam_public_entries({"profile": profile}, http)
    assert http.urls == []


@pytest.mark.parametrize("profile,expected", [
    ("https://steamcommunity.com/id/gaben/", "https://steamcommunity.com/id/gaben/games"),
    ("steamcommunity.com/profiles/76561197960287930", "https://steamcommunity.com/profiles/76561197960287930/games"),
    ("76561197960287930", "https://steamcommunity.com/profiles/76561197960287930/games"),
    ("gaben", "https://steamcommunity.com/id/gaben/games"),
])
def test_steam_profiles_resolve_on_steamcommunity(profile, expected):
    http = _Http()
    try:
        _steam_public_entries({"profile": profile}, http)
    except Exception:  # noqa: BLE001 - parsing the fake reply is not the point
        pass
    assert http.urls and http.urls[0].startswith(expected)


def test_query_redaction_is_linear_on_hostile_input():
    hostile = "&" + "?!" * 50000
    started = time.monotonic()
    redact_query_credentials(hostile)
    assert time.monotonic() - started < 1.0
    assert redact_query_credentials("/x?a=1&apikey=SECRET&b=2") == "/x?a=1&apikey=[REDACTED]&b=2"


def test_a_lookalike_host_does_not_add_the_moveweight_portal(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json"),
                  "ROMM_URL": "https://evil.example/?next=moveweight.com"})
    from urllib.parse import urlsplit
    hosts = {urlsplit(str(key)).hostname for key in svc._portal_targets()}
    assert "romm.moveweight.com" not in hosts


@pytest.mark.parametrize("game", ["--exec=evil", "-x", "a b", "x" * 80])
def test_cartridge_refuses_option_like_game_ids(tmp_path, monkeypatch, game):
    from romarr import cartridge
    hub_ = cartridge.CartridgeHub(tmp_path) if hasattr(cartridge, "CartridgeHub") else None
    if hub_ is None:
        pytest.skip("CartridgeHub unavailable")
    monkeypatch.setattr(hub_, "binary_path", lambda: tmp_path / "cartridge")
    started = []
    monkeypatch.setattr(cartridge.subprocess, "Popen", lambda *a, **k: started.append(a))
    with pytest.raises(ValueError):
        hub_.launch(6868, game)
    assert started == []


def test_nntp_tls_never_allows_old_protocols(monkeypatch):
    import ssl
    from romarr import nntp
    made = []
    real = ssl.create_default_context

    def capture(*args, **kwargs):
        ctx = real(*args, **kwargs)
        made.append(ctx)
        return ctx

    monkeypatch.setattr(nntp.ssl, "create_default_context", capture)
    monkeypatch.setattr(nntp.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    client = None
    for name in dir(nntp):
        obj = getattr(nntp, name)
        if isinstance(obj, type) and hasattr(obj, "connect") and "use_ssl" in getattr(obj.__init__, "__code__", type("", (), {"co_varnames": ()})).co_varnames:
            client = obj
            break
    if client is None:
        pytest.skip("NNTP client class not found")
    import inspect
    params = inspect.signature(client.__init__).parameters
    kwargs = {k: v for k, v in {"host": "news.invalid", "port": 563, "use_ssl": True}.items() if k in params}
    conn = client(**kwargs)
    monkeypatch.setattr(nntp.socket, "create_connection", lambda *a, **k: _Sock())
    try:
        conn.connect()
    except Exception:  # noqa: BLE001 - the handshake itself is not under test
        pass
    assert made and all(ctx.minimum_version >= ssl.TLSVersion.TLSv1_2 for ctx in made)


class _Sock:
    def close(self):
        pass

    def fileno(self):
        return -1


def test_extra_exo2romm_folders_come_only_from_the_operator_environment(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json"), "EXO2ROMM_ROOTS": str(tmp_path / "eXo")})
    assert str(tmp_path / "eXo") in svc.exo2romm_roots()
    svc.store.update_settings({"exo2romm_roots": ["/"]})       # an API client tries to widen it
    assert "/" not in svc.exo2romm_roots()


def test_download_client_staging_folders_are_allowed(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json")})
    svc.store.put_item("download_clients", {"type": "hub", "name": "ROM Hub", "save_path": str(tmp_path / "rom-hub")})
    assert str(tmp_path / "rom-hub") in svc.exo2romm_roots()


def test_a_symlinked_zip_inside_an_allowed_folder_is_skipped(tmp_path):
    allowed = tmp_path / "library"
    allowed.mkdir()
    (tmp_path / "private.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (allowed / "game.zip").symlink_to(tmp_path / "private.zip")
    result = hub.run_exo2romm({"dos_dir": str(allowed), "output_dir": str(allowed / "dos"),
                               "input_paths": [str(allowed)], "dry_run": True},
                              allowed_roots=[str(allowed)])
    details = " ".join(row.get("detail", "") for row in result.get("results", []))
    assert "links outside the allowed folders" in details


def test_hyphenated_libretro_core_ids_are_accepted(tmp_path, monkeypatch):
    from romarr import libretro
    monkeypatch.setattr(libretro, "metadata", lambda: {"items": [{"id": "bsnes-jg_libretro"}]})
    store = libretro.CoreHub(tmp_path)
    if store is None:
        pytest.skip("libretro manager class not found")
    monkeypatch.setattr(store, "index", lambda: (_ for _ in ()).throw(RuntimeError("reached the build lookup")))
    with pytest.raises(RuntimeError, match="build lookup"):
        store.install("bsnes-jg_libretro")


def test_a_stricter_system_tls_minimum_is_kept(monkeypatch):
    import ssl
    from romarr import nntp
    made = []
    real = ssl.create_default_context

    def strict(*args, **kwargs):
        ctx = real(*args, **kwargs)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        made.append(ctx)
        return ctx

    monkeypatch.setattr(nntp.ssl, "create_default_context", strict)
    monkeypatch.setattr(nntp.socket, "create_connection", lambda *a, **k: _Sock())
    import inspect
    client = next(obj for obj in vars(nntp).values() if isinstance(obj, type) and hasattr(obj, "connect")
                  and "use_ssl" in inspect.signature(obj.__init__).parameters)
    params = inspect.signature(client.__init__).parameters
    conn = client(**{k: v for k, v in {"host": "news.invalid", "port": 563, "use_ssl": True}.items() if k in params})
    try:
        conn.connect()
    except Exception:  # noqa: BLE001 - the handshake itself is not under test
        pass
    assert made and made[0].minimum_version == ssl.TLSVersion.TLSv1_3


def test_exo2romm_without_api_roots_still_confines_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("EXO2ROMM_ROOTS", raising=False)
    result = hub.run_exo2romm({"dos_dir": str(tmp_path), "output_dir": str(tmp_path / "out"),
                               "input_paths": [str(tmp_path)]})
    assert not result["ok"] and "must be inside" in result["error"]


def test_extension_adopt_refuses_files_outside_import_folders(tmp_path):
    from romarr.extension_downloads import ExtensionDownloadManager
    secret = tmp_path / "secret.gba"
    secret.write_bytes(b"x")
    adopted = []

    class Service:
        def extension_import_roots(self):
            return [str(tmp_path / "downloads")]

        def adopt(self, path, platform):
            adopted.append(path)
            return {"ok": True}

    out = ExtensionDownloadManager().record_completed(
        Service(), [{"local_path": str(secret), "filename": "secret.gba"}], platform="gba", adopt=True)
    assert adopted == [] and "outside" in out["results"][0]["adopt_error"]


def test_extension_adopt_imports_files_in_allowed_folders(tmp_path):
    from romarr.extension_downloads import ExtensionDownloadManager
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    rom = downloads / "Game.gba"
    rom.write_bytes(b"x")
    adopted = []

    class Service:
        def extension_import_roots(self):
            return [str(downloads)]

        def adopt(self, path, platform):
            adopted.append(path)
            return {"ok": True}

    out = ExtensionDownloadManager().record_completed(
        Service(), [{"local_path": str(rom)}], platform="gba", adopt=True)
    assert adopted == [str(rom.resolve())] and out["adopted"] == 1


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX system folders")
def test_settings_cannot_widen_roots_to_the_disk_or_system_folders(tmp_path):
    from pathlib import Path as _Path
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json")})
    svc.library = _Path("/")
    svc.store.update_settings({"download_path": "/etc"})
    svc.store.put_item("download_clients", {"type": "hub", "name": "x", "save_path": "/usr/share"})
    for roots in (svc.exo2romm_roots(), svc.extension_import_roots()):
        assert "/" not in roots and "/etc" not in roots and "/usr/share" not in roots
    result = hub.run_exo2romm({"dos_dir": "/etc", "output_dir": "/etc/x", "input_paths": ["/etc"]},
                              allowed_roots=svc.exo2romm_roots())
    assert not result["ok"]


def test_mapped_download_client_folders_are_import_roots(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json")})
    local = tmp_path / "srv" / "downloads"
    svc.store.update_settings({"remote_path_mappings": [{"remote": "/downloads", "local": str(local)}]})
    svc.store.put_item("download_clients", {"type": "qbittorrent", "name": "q", "save_path": "/downloads/roms"})
    assert str(local / "roms") in svc.extension_import_roots()

