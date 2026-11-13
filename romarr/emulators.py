"""Curated emulator discovery and verified portable installation on the ROMarr host."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import requests

MAX_PACKAGE = 1024 * 1024 * 1024
MAX_EXPANDED = 4 * MAX_PACKAGE
INSTALL_LOCK = threading.Lock()

# Sources are maintained by the emulator projects. Releases are resolved on demand.
CATALOG = [
    ("retroarch", "RetroArch / libretro", "libretro/RetroArch", "https://www.retroarch.com/?page=platforms", "Many classic systems", "https://docs.libretro.com/guides/bios/"),
    ("duckstation", "DuckStation", "stenzek/duckstation", "https://www.duckstation.org/", "PlayStation", "https://www.duckstation.org/"),
    ("pcsx2", "PCSX2", "PCSX2/pcsx2", "https://pcsx2.net/downloads/", "PlayStation 2", "https://pcsx2.net/docs/usage/setup/"),
    ("rpcs3", "RPCS3", "RPCS3/rpcs3-binaries-win", "https://rpcs3.net/download", "PlayStation 3", "https://rpcs3.net/quickstart"),
    ("shadps4", "shadPS4", "shadps4-emu/shadPS4", "https://shadps4.net/", "PlayStation 4", "https://github.com/shadps4-emu/shadPS4/wiki"),
    ("vita3k", "Vita3K", "Vita3K/Vita3K", "https://vita3k.org/", "PlayStation Vita", "https://vita3k.org/quickstart.html"),
    ("ppsspp", "PPSSPP", "hrydgard/ppsspp", "https://www.ppsspp.org/download/", "PSP", "https://www.ppsspp.org/docs/getting-started/"),
    ("dolphin", "Dolphin", "dolphin-emu/dolphin", "https://dolphin-emu.org/download/", "GameCube and Wii", "https://dolphin-emu.org/docs/guides/"),
    ("cemu", "Cemu", "cemu-project/Cemu", "https://cemu.info/", "Wii U", "https://cemu.cfw.guide/"),
    ("melonds", "melonDS", "melonDS-emu/melonDS", "https://melonds.kuribo64.net/downloads.php", "Nintendo DS", "https://melonds.kuribo64.net/faq.php"),
    ("azahar", "Azahar", "azahar-emu/azahar", "https://azahar-emu.org/", "Nintendo 3DS", "https://azahar-emu.org/"),
    ("lime3ds", "Lime3DS", "", "https://github.com/Lime3DS/Lime3DS", "Nintendo 3DS · succeeded by Azahar", "https://azahar-emu.org/"),
    ("mgba", "mGBA", "mgba-emu/mgba", "https://mgba.io/downloads.html", "Game Boy and Game Boy Advance", "https://mgba.io/docs.html"),
    ("flycast", "Flycast", "flyinghead/flycast", "https://github.com/flyinghead/flycast", "Dreamcast and Naomi", "https://github.com/flyinghead/flycast/wiki"),
    ("xemu", "xemu", "xemu-project/xemu", "https://xemu.app/", "Xbox", "https://xemu.app/docs/required-files/"),
    ("xenia", "Xenia", "xenia-project/release-builds-windows", "https://xenia.jp/download/", "Xbox 360", "https://github.com/xenia-project/xenia/wiki/Quickstart"),
    ("scummvm", "ScummVM", "scummvm/scummvm", "https://www.scummvm.org/downloads/", "Adventure game engines", "https://docs.scummvm.org/"),
    ("dosbox-x", "DOSBox-X", "joncampbell123/dosbox-x", "https://dosbox-x.com/", "DOS and Windows 3.x/9x", "https://dosbox-x.com/wiki/"),
    ("dosbox-staging", "DOSBox Staging", "dosbox-staging/dosbox-staging", "https://www.dosbox-staging.org/", "DOS", "https://www.dosbox-staging.org/getting-started/introduction/"),
    ("mame", "MAME", "mamedev/mame", "https://www.mamedev.org/release.html", "Arcade and classic computers", "https://docs.mamedev.org/"),
    ("ares", "ares", "ares-emulator/ares", "https://ares-emu.net/", "Multi-system", "https://ares-emu.net/"),
    ("lichii", "Lichii", "zortos293/Lichii", "https://github.com/zortos293/Lichii", "Switch and Switch 2 · experimental", "https://github.com/zortos293/Lichii"),
    ("eglantine", "Eglantine", "Eglantine-Community/Eglantine", "https://github.com/Eglantine-Community/Eglantine", "Switch 2 · experimental", "https://github.com/Eglantine-Community/Eglantine"),
    ("eden", "Eden", "", "https://eden-emu.dev/download", "Switch", "https://eden-emu.dev/"),
    ("citron", "Citron", "", "https://citron-emu.org/", "Switch · check upstream availability", "https://citron-emu.org/"),
    ("ryubing", "Ryubing", "", "https://git.ryujinx.app/ryubing/ryujinx", "Switch", "https://git.ryujinx.app/ryubing/ryujinx"),
]


def _entry(slug: str) -> dict:
    """Find a curated project by its stable identifier."""
    for key, name, repo, website, systems, guide in CATALOG:
        if key == slug:
            return dict(id=key, name=name, repo=repo, website=website,
                        systems=systems, guide=guide)
    raise ValueError("Unknown emulator")


def _component(value: str) -> str:
    """Validate a path component without changing its meaning."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,119}", value) or value in (".", ".."):
        raise ValueError("Invalid release or file name")
    return value


def _portable(name: str) -> bool:
    """Accept formats extracted without executing upstream installation scripts."""
    return name.lower().endswith((".zip", ".tar.gz", ".tar.xz", ".appimage"))


def _matches_host(name: str) -> bool:
    """Select only explicitly named host builds and reject source/debug packages."""
    lower = name.lower()
    if any(token in lower for token in ("source", "symbols", "debug", ".zsync", "mingw-src")):
        return False
    system = platform.system().lower()
    os_tokens = {"windows": ("windows", "win64", "win-x64", "win32"),
                 "linux": ("linux", "ubuntu", ".appimage"),
                 "darwin": ("macos", "mac-", "osx", "darwin")}
    if not any(token in lower for token in os_tokens.get(system, ())):
        return False
    arm = platform.machine().lower() in ("arm64", "aarch64")
    named_arm = bool(re.search(r"(?:arm64|aarch64|armhf)", lower))
    return arm == named_arm


class EmulatorHub:
    """Keep verified portable builds separate from game libraries and system packages."""

    def __init__(self, data_dir: Path, session=None):
        self.root = Path(data_dir) / "tools" / "emulators"
        self.http = session or requests.Session()

    def catalog(self) -> dict:
        """Return the curated roster without making external network calls."""
        items = []
        for entry in CATALOG:
            row = _entry(entry[0])
            records = sorted((self.root / row["id"]).glob("*/romarr-install.json"))
            row["installed"] = [p.parent.name for p in records]
            row["managed_releases"] = bool(row["repo"])
            items.append(row)
        return {"host": platform.system() + " " + platform.machine(),
                "install_dir": str(self.root), "items": items}

    def release(self, slug: str) -> dict:
        """Resolve a project's own GitHub release assets and their published digests."""
        entry = _entry(slug)
        if not entry["repo"]:
            return {**entry, "assets": [], "note": "Use the project's downloads page."}
        url = "https://api.github.com/repos/" + entry["repo"] + "/releases/latest"
        with self.http.get(url, timeout=15) as response:
            response.raise_for_status()
            release = response.json()
        assets = []
        for asset in release.get("assets", []):
            name = str(asset.get("name") or "")
            digest = str(asset.get("digest") or "")
            assets.append({"name": name, "bytes": asset.get("size", 0),
                           "sha256": digest.removeprefix("sha256:") if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) else "",
                           "url": asset.get("browser_download_url", ""),
                           "installable": _portable(name) and _matches_host(name)
                                          and bool(re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest))})
        return {**entry, "tag": release.get("tag_name", ""), "assets": assets}

    def install(self, slug: str, asset_name: str, expected_sha256: str, scanner=None) -> dict:
        """Verify the selected release and bytes, scan them, then publish atomically."""
        with INSTALL_LOCK:
            release = self.release(slug)
            asset = next((a for a in release["assets"] if a["name"] == asset_name), None)
            if not asset or not asset["installable"]:
                raise ValueError("This asset cannot be installed on the ROMarr host")
            if not expected_sha256 or asset["sha256"] != expected_sha256:
                raise ValueError("The release changed. Check its assets again before installing")
            if not 0 < int(asset["bytes"]) <= MAX_PACKAGE:
                raise ValueError("Package exceeds the 1 GiB download limit")
            tag = _component(str(release["tag"]))
            _component(asset_name)
            parts = urlsplit(asset["url"])
            if parts.scheme != "https" or parts.hostname != "github.com":
                raise ValueError("Release asset must be hosted by GitHub")
            if not parts.path.startswith("/" + release["repo"] + "/releases/download/"):
                raise ValueError("Release asset belongs to another repository")
            parent = self.root / slug
            parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(parent).free < 2 * int(asset["bytes"]) + 128 * 1024 * 1024:
                raise ValueError("Insufficient free space for this package")
            destination = parent / (tag + "-" + expected_sha256[:12])
            if destination.exists():
                return {"ok": True, "path": str(destination), "already_installed": True}
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=parent))
            try:
                archive = stage / asset_name
                sha = hashlib.sha256()
                downloaded = 0
                with self.http.get(asset["url"], timeout=(10, 60), stream=True) as response:
                    response.raise_for_status()
                    with archive.open("xb") as handle:
                        for chunk in response.iter_content(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_PACKAGE or downloaded > int(asset["bytes"]):
                                raise ValueError("Release package exceeds its published size")
                            sha.update(chunk)
                            handle.write(chunk)
                if downloaded != int(asset["bytes"]) or sha.hexdigest() != expected_sha256:
                    raise ValueError("Release package checksum or size mismatch")
                if scanner:
                    scanner(archive)
                app = stage / "app"
                app.mkdir()
                if asset_name.lower().endswith(".appimage"):
                    shutil.move(str(archive), app / asset_name)
                    (app / asset_name).chmod(0o755)
                else:
                    _extract(archive, app)
                    archive.unlink()
                if scanner:
                    scanner(app)
                record = {"project": slug, "tag": release["tag"], "asset": asset_name,
                          "sha256": expected_sha256, "source": release["website"]}
                (stage / "romarr-install.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                os.replace(stage, destination)
                return {"ok": True, "path": str(destination / "app"), "tag": release["tag"]}
            finally:
                if stage.exists():
                    shutil.rmtree(stage)


def _extract(archive: Path, root: Path) -> None:
    """Extract regular files with bounded expansion and no links or traversal."""
    total = 0
    paths = set()

    def destination(name, size):
        """Validate one archive member and account for its disk usage."""
        nonlocal total
        normalized = name.replace(chr(92), "/")
        parts = PurePosixPath(normalized)
        if parts.is_absolute() or ".." in parts.parts or ":" in normalized or chr(0) in normalized:
            raise ValueError("Unsafe path in emulator package")
        target = root.joinpath(*parts.parts)
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Emulator package escapes its installation folder")
        key = str(target).casefold()
        if key in paths:
            raise ValueError("Duplicate path in emulator package")
        paths.add(key)
        total += size
        if total > MAX_EXPANDED or len(paths) > 50000:
            raise ValueError("Emulator package expands beyond its safety limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if entry.is_dir():
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("Linked files are unsupported in emulator packages")
                path = destination(entry.filename, entry.file_size)
                with bundle.open(entry) as src, path.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                path.chmod((entry.external_attr >> 16) & 0o755 or 0o644)
    else:
        with tarfile.open(archive, "r:*") as bundle:
            for entry in bundle:
                if entry.isdir():
                    continue
                if not entry.isfile():
                    raise ValueError("Linked and special files are unsupported in emulator packages")
                path = destination(entry.name, entry.size)
                with bundle.extractfile(entry) as src, path.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                path.chmod(entry.mode & 0o755 or 0o644)
    if not paths:
        raise ValueError("Emulator package contains no regular files")
