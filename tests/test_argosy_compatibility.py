"""Tests for Argosy Launcher & RomM ecosystem API compatibility."""

import json
import os
import tempfile
from romarr.app import ROMarr, is_portal_subpath


def test_is_portal_subpath_covers_argosy_and_romm_mobile_routes():
    routes = [
        "/api/heartbeat",
        "/api/users/me",
        "/api/permissions/me",
        "/api/platforms",
        "/api/platforms/identifiers",
        "/api/platforms/1",
        "/api/roms",
        "/api/roms/identifiers",
        "/api/roms/1",
        "/api/roms/1/content/smb.nes",
        "/api/roms/1/files/content/smb.nes",
        "/api/search/cover",
        "/api/client-tokens/exchange",
        "/api/auth/device/init",
        "/api/auth/device/token",
    ]
    for r in routes:
        assert is_portal_subpath(r) is True, f"{r} must be recognized as portal/argosy subpath"


def test_standalone_portal_proxy_answers_argosy_heartbeat_and_platforms():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "romarr.json")
        service = ROMarr(env={"LIBRARY_KIND": "folder", "LIBRARY_PATH": td, "ROMARR_DATA": cfg})

        code, ctype, body = service.proxy_portal(url="", path="/api/heartbeat")
        assert code == 200
        assert "application/json" in ctype
        heartbeat = json.loads(body.decode("utf-8"))
        assert "SYSTEM" in heartbeat
        assert heartbeat["SYSTEM"]["romm_version"] == "5.1.0-romarr"

        code, ctype, body = service.proxy_portal(url="", path="/api/users/me")
        assert code == 200
        user = json.loads(body.decode("utf-8"))
        assert user["username"] == "romarr"
        assert user["role"] == "admin"

        code, ctype, body = service.proxy_portal(url="", path="/api/platforms")
        assert code == 200
        plats = json.loads(body.decode("utf-8"))
        assert len(plats) > 50
        assert any(p["slug"] == "nes" for p in plats)
