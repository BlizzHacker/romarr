"""Libretro core discovery, bounded official downloads and firmware readiness."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import tempfile
import threading
import zipfile
import zlib
from functools import lru_cache
from pathlib import Path

import requests

CORE_LOCK = threading.Lock()
MAX_CORE = 128 * 1024 * 1024
BUILD_ROOT = "https://buildbot.libretro.com/nightly/"


@lru_cache(maxsize=1)
def metadata() -> dict:
    """Read all upstream core-info records without a network request."""
    return json.loads(Path(__file__).with_name("libretro_cores.json").read_text(encoding="utf-8"))


def host_build() -> tuple[str, str]:
    """Choose the official build directory and native shared-library suffix."""
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
    system = platform.system().lower()
    if system == "linux" and arch:
        return "linux/" + arch + "/latest/", ".so"
    if system == "windows" and arch == "x86_64":
        return "windows/x86_64/latest/", ".dll"
    if system == "darwin":
        return "apple/osx/" + ("arm64" if arch == "aarch64" else "x86_64") + "/latest/", ".dylib"
    raise ValueError("Use RetroArch's Online Updater on this host")


class CoreHub:
    """Manage cores and a separate RetroArch configuration under ROMarr's data."""

    def __init__(self, data_dir: Path, session=None):
        self.root = Path(data_dir) / "tools" / "retroarch"
        self.http = session or requests.Session()

    def entry(self, key: str) -> dict:
        """Accept only identifiers from the upstream core metadata."""
        for row in metadata()["items"]:
            if row["id"] == key:
                return row
        raise ValueError("Unknown libretro core")

    def catalog(self, query="", offset=0, limit=60) -> dict:
        """Expose every core with its license and firmware requirements."""
        rows = []
        for item in metadata()["items"]:
            row = dict(item)
            if query.strip().casefold() not in " ".join(str(row.get(k) or "") for k in
                                                      ("name", "systems", "license", "description")).casefold():
                continue
            row["installed"] = (self.root / "receipts" / (row["id"] + ".json")).is_file()
            rows.append(row)
        offset, limit = max(0, int(offset)), min(100, max(1, int(limit)))
        return {"items": rows[offset:offset + limit], "total": len(rows),
                "available": len(metadata()["items"]), "offset": offset, "limit": limit,
                "source": metadata()["source"], "verified_date": metadata()["verified_date"],
                "system_directory": str(self.root / "system")}

    def index(self) -> dict:
        """Read the buildbot's CRC32 index with a strict response-size bound."""
        folder, suffix = host_build()
        with self.http.get(BUILD_ROOT + folder + ".index-extended", timeout=20, stream=True) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("Core index exceeds its size limit")
        rows = {}
        for line in content.decode("utf-8").splitlines():
            parts = line.split()
            if len(parts) == 3 and re.fullmatch(r"[a-fA-F0-9]{8}", parts[1]):
                name = parts[2]
                if re.fullmatch(r"[A-Za-z0-9_-]+" + re.escape(suffix) + r"\.zip", name):
                    rows[name] = parts[1].lower()
        return {"folder": folder, "suffix": suffix, "checksums": rows}

    def readiness(self, key: str) -> dict:
        """Report required files that are absent; presence does not verify a BIOS dump."""
        entry = self.entry(key)
        system = (self.root / "system").resolve()
        firmware = []
        for item in entry["firmware"]:
            relative = Path(item["path"])
            target = (system / relative).resolve()
            safe = bool(item["path"]) and not relative.is_absolute() and target.is_relative_to(system)
            present = safe and target.is_file() and target.stat().st_size > 0
            firmware.append({**item, "present": present, "required": item["opt"].lower() != "true"})
        missing = [row["path"] for row in firmware if row["required"] and not row["present"]]
        return {"id": key, "firmware": firmware, "missing_required": missing,
                "system_directory": str(system), "ready": not missing,
                "note": "Presence check only. Use the core guide to verify firmware hashes and compatibility.",
                "guide": "https://docs.libretro.com/library/"}

    def configure(self) -> dict:
        """Write an optional dedicated configuration without changing a user's existing setup."""
        self.root.mkdir(parents=True, exist_ok=True)
        lines = []
        for key, folder in (("libretro_directory", "cores"), ("system_directory", "system"),
                            ("savefile_directory", "saves"), ("savestate_directory", "states")):
            path = self.root / folder
            path.mkdir(exist_ok=True)
            lines.append(key + ' = "' + str(path.resolve()).replace("\\", "/").replace('"', '') + '"')
        lines.extend(['config_save_on_exit = "false"', 'menu_show_core_updater = "true"'])
        config = self.root / "romarr-retroarch.cfg"
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            os.replace(temp, config)
        finally:
            Path(temp).unlink(missing_ok=True)
        return {"ok": True, "config": str(config), "executable": shutil.which("retroarch") or "",
                "usage": 'retroarch --appendconfig "' + str(config) + '" -L "<installed core>" "<game file>"'}

    def install(self, key: str, scanner=None) -> dict:
        """Verify the native core CRC, scan it, and atomically publish one host library."""
        self.entry(key)
        with CORE_LOCK:
            index = self.index()
            name = key + index["suffix"] + ".zip"
            expected = index["checksums"].get(name)
            if not expected:
                raise ValueError("This core has no build for the ROMarr host; use its source or another device")
            self.root.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.root).free < 3 * MAX_CORE:
                raise ValueError("Insufficient free space for a core installation")
            stage = Path(tempfile.mkdtemp(dir=self.root, prefix=".core-"))
            try:
                payload = bytearray()
                with self.http.get(BUILD_ROOT + index["folder"] + name, stream=True, timeout=(10, 60)) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(1024 * 1024):
                        payload.extend(chunk)
                        if len(payload) > MAX_CORE:
                            raise ValueError("Core package exceeds the 128 MiB limit")
                archive = stage / name
                archive.write_bytes(payload)
                if scanner:
                    scanner(archive)
                with zipfile.ZipFile(io.BytesIO(payload)) as package:
                    files = package.infolist()
                    binary = key + index["suffix"]
                    if len(files) != 1 or files[0].filename != binary or files[0].file_size > MAX_CORE:
                        raise ValueError("Core archive must contain exactly its named native library")
                    if (files[0].external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Core archive cannot contain a symbolic link")
                    target = stage / binary
                    checksum = 0
                    count = 0
                    with package.open(files[0]) as source, target.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            count += len(chunk)
                            if count > MAX_CORE:
                                raise ValueError("Expanded core exceeds the 128 MiB limit")
                            checksum = zlib.crc32(chunk, checksum)
                            output.write(chunk)
                    if format(checksum & 0xffffffff, "08x") != expected:
                        raise ValueError("Core CRC32 differs from the official index; retry after the build finishes")
                if scanner:
                    scanner(target)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                cores = self.root / "cores"
                cores.mkdir(exist_ok=True)
                destination = cores / binary
                receipt = {"id": key, "path": str(destination), "sha256": digest,
                           "core_crc32": expected, "source": BUILD_ROOT + index["folder"] + name,
                           "verification": "Official HTTPS buildbot index CRC32; SHA-256 recorded locally, not publisher-signed"}
                receipts = self.root / "receipts"
                receipts.mkdir(exist_ok=True)
                # Finish all fallible setup before replacing a working core.
                readiness = self.readiness(key)
                configuration = self.configure()
                staged_receipt = stage / "receipt.json"
                with staged_receipt.open("x", encoding="utf-8") as handle:
                    json.dump(receipt, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._publish(target, destination, staged_receipt,
                              receipts / (key + ".json"), stage)
                return {"ok": True, **receipt, "readiness": readiness, "configuration": configuration}
            finally:
                # A failed rollback leaves recovery copies for an administrator.
                if not (stage / "RECOVERY.txt").exists():
                    shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _publish(target: Path, destination: Path, receipt: Path,
                 receipt_destination: Path, stage: Path) -> None:
        """Publish both files together, restoring prior bytes on an update failure."""
        previous = []
        for path, backup_name in ((destination, "previous-core"),
                                  (receipt_destination, "previous-receipt.json")):
            backup = stage / backup_name
            exists = path.exists()
            if exists:
                shutil.copy2(path, backup)
            previous.append((path, backup, exists))
        recovery = stage / "RECOVERY.txt"
        recovery.write_text(
            "Interrupted core update. Previous files, when present, are saved as "
            "previous-core and previous-receipt.json. Core destination: "
            + str(destination) + "; receipt destination: " + str(receipt_destination) + "\n",
            encoding="utf-8")
        changed = []
        try:
            os.replace(target, destination)
            changed.append(previous[0])
            os.replace(receipt, receipt_destination)
            changed.append(previous[1])
        except OSError:
            recovery_errors = []
            for path, backup, existed in reversed(changed):
                try:
                    if existed:
                        os.replace(backup, path)
                    else:
                        path.unlink(missing_ok=True)
                except OSError as error:
                    recovery_errors.append(error)
            if recovery_errors:
                raise OSError("Core update rollback failed; recovery copies retained in "
                              + str(stage)) from recovery_errors[0]
            recovery.unlink(missing_ok=True)
            raise
        # Keeping a redundant recovery folder is safer than treating cleanup as
        # an installation failure after both new files were published.
        try:
            recovery.unlink()
        except OSError:
            pass
