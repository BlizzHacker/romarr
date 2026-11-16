"""Cartridge player integration, auto-installation, and plug-and-play emulator ecosystem for ROMarr."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import requests

CARTRIDGE_DOWNLOAD_URL = (
    "https://github.com/BlizzHacker/romarr/releases/download/v1.8.0/Cartridge-Windows-Portable-0.2.0-x64.exe"
)
CARTRIDGE_EXE_NAME = "Cartridge-Windows-Portable-0.2.0-x64.exe"
CARTRIDGE_EXPECTED_SHA256 = "af024bfa3ceaf88dd99feb32ed068fb3dc5b75454830c5373ab219e8627d1471"
CARTRIDGE_SIZE = 109985956


class CartridgeHub:
    """Manages Cartridge player installation, status, and plug-and-play launch."""

    def __init__(self, data_dir: Path, session: Optional[requests.Session] = None):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "tools" / "cartridge"
        self.http = session or requests.Session()

    def binary_path(self) -> Optional[Path]:
        """Return the path to the installed Cartridge executable if present."""
        candidates = [
            self.root / "Cartridge.exe",
            self.root / CARTRIDGE_EXE_NAME,
        ]
        for c in candidates:
            if c.is_file() and c.stat().st_size > 0:
                return c
        return None

    def status(self) -> dict:
        """Report Cartridge installation, platform availability, and launch status."""
        installed_binary = self.binary_path()
        installed = installed_binary is not None
        host_system = platform.system().lower()

        receipt_file = self.root / "cartridge-install.json"
        receipt = {}
        if receipt_file.is_file():
            try:
                receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        return {
            "installed": installed,
            "path": str(installed_binary) if installed_binary else None,
            "version": receipt.get("version", "0.2.0" if installed else None),
            "host_platform": host_system,
            "can_auto_install": host_system == "windows" or "win" in host_system,
            "download_url": CARTRIDGE_DOWNLOAD_URL,
            "expected_sha256": CARTRIDGE_EXPECTED_SHA256,
            "platforms": {
                "windows": {
                    "installed": installed,
                    "type": "portable_exe",
                    "download_url": CARTRIDGE_DOWNLOAD_URL,
                    "repo": "https://github.com/BlizzHacker/RommForDesktop",
                },
                "xbox": {
                    "store_id": "9MXC51W17LH4",
                    "store_url": "https://apps.microsoft.com/detail/9MXC51W17LH4",
                    "repo": "https://github.com/BlizzHacker/Cartridge",
                },
                "android": {
                    "repo": "https://github.com/BlizzHacker/Cartridge",
                    "apk_releases": "https://github.com/BlizzHacker/Cartridge/releases",
                },
            },
        }

    def install(self, scanner: Optional[Callable[[Path], None]] = None) -> dict:
        """Download, verify SHA256, scan, and register the portable Cartridge player."""
        self.root.mkdir(parents=True, exist_ok=True)
        installed = self.binary_path()
        if installed:
            return {"ok": True, "path": str(installed), "already_installed": True, "version": "0.2.0"}

        target_file = self.root / "Cartridge.exe"
        temp_dir = Path(tempfile.mkdtemp(prefix=".cartridge-install-", dir=self.root))
        try:
            temp_file = temp_dir / CARTRIDGE_EXE_NAME
            sha = hashlib.sha256()
            downloaded = 0

            with self.http.get(CARTRIDGE_DOWNLOAD_URL, timeout=(10, 120), stream=True) as resp:
                resp.raise_for_status()
                with temp_file.open("wb") as f:
                    for chunk in resp.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > CARTRIDGE_SIZE + (10 * 1024 * 1024):
                            raise ValueError("Downloaded Cartridge package exceeds allowed size limit")
                        sha.update(chunk)
                        f.write(chunk)

            if sha.hexdigest() != CARTRIDGE_EXPECTED_SHA256:
                raise ValueError("Downloaded Cartridge binary SHA256 checksum mismatch")

            if scanner:
                scanner(temp_file)

            os.replace(temp_file, target_file)

            receipt = {
                "project": "cartridge",
                "version": "0.2.0",
                "asset": CARTRIDGE_EXE_NAME,
                "sha256": CARTRIDGE_EXPECTED_SHA256,
                "source": "https://github.com/BlizzHacker/romarr/releases",
                "installed_path": str(target_file),
            }
            (self.root / "cartridge-install.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            return {"ok": True, "path": str(target_file), "version": "0.2.0"}
        finally:
            if temp_dir.exists():
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)

    def launch(self, port: int = 6868, game_id: Optional[str] = None) -> dict:
        """Launch the Cartridge player connected to ROMarr's embedded portal."""
        binary = self.binary_path()
        if not binary:
            raise FileNotFoundError("Cartridge player is not installed yet. Call auto-install first.")

        server_url = f"http://127.0.0.1:{port}"
        args = [str(binary)]
        if game_id:
            args.extend(["--game", str(game_id)])

        try:
            subprocess.Popen(args, cwd=str(self.root))
            return {"ok": True, "launched": True, "server_url": server_url}
        except Exception as e:
            return {"ok": False, "error": f"Failed to spawn Cartridge player: {e}"}

    def plug_and_play_setup(self, library_dir: Optional[Path] = None) -> dict:
        """Turnkey plug-and-play setup: configure Cartridge, RetroArch cores and emulator paths."""
        tools_dir = self.data_dir / "tools"
        retroarch_dir = tools_dir / "retroarch"
        for sub in ("cores", "system", "saves", "states", "configs"):
            (retroarch_dir / sub).mkdir(parents=True, exist_ok=True)

        self.root.mkdir(parents=True, exist_ok=True)

        # Standard system mappings for plug-and-play
        config = {
            "version": "1.0.0",
            "configured": True,
            "cartridge": self.status(),
            "retroarch": {
                "root": str(retroarch_dir.resolve()),
                "cores_directory": str((retroarch_dir / "cores").resolve()),
                "system_directory": str((retroarch_dir / "system").resolve()),
                "savefile_directory": str((retroarch_dir / "saves").resolve()),
                "savestate_directory": str((retroarch_dir / "states").resolve()),
            },
            "plug_and_play_systems": [
                {"platform": "nes", "core": "fceumm", "system_name": "Nintendo Entertainment System"},
                {"platform": "snes", "core": "snes9x", "system_name": "Super Nintendo Entertainment System"},
                {"platform": "genesis", "core": "genesis_plus_gx", "system_name": "Sega Genesis / Mega Drive"},
                {"platform": "gb", "core": "gambatte", "system_name": "Game Boy"},
                {"platform": "gba", "core": "mgba", "system_name": "Game Boy Advance"},
                {"platform": "n64", "core": "mupen64plus_next", "system_name": "Nintendo 64"},
                {"platform": "ps1", "core": "mednafen_psx_hw", "system_name": "Sony PlayStation"},
                {"platform": "ps2", "standalone": "pcsx2", "system_name": "Sony PlayStation 2"},
                {"platform": "gamecube", "standalone": "dolphin", "system_name": "Nintendo GameCube"},
                {"platform": "psp", "standalone": "ppsspp", "system_name": "PlayStation Portable"},
            ],
        }

        config_path = self.data_dir / "plug-and-play.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        return {
            "ok": True,
            "ready": True,
            "manifest": str(config_path),
            "systems_configured": len(config["plug_and_play_systems"]),
            "cartridge_installed": config["cartridge"]["installed"],
            "retroarch_configured": True,
        }
