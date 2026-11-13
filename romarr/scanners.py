"""Import scanning through local antivirus and explicitly configured services."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import clamav

ScanUnavailable = clamav.ScanUnavailable
Finding = clamav.Finding


def defender_path() -> str:
    """Find the current Windows Defender command without executing a shell."""
    if sys.platform != "win32":
        return ""
    base = Path(os.environ.get("ProgramData", r"C:\\ProgramData"))
    candidates = sorted((base / "Microsoft/Windows Defender/Platform").glob("*/MpCmdRun.exe"),
                        reverse=True)
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\\Program Files"))
                      / "Windows Defender/MpCmdRun.exe")
    return next((str(path) for path in candidates if path.is_file()), "")


def _files(target: Path) -> list[Path]:
    """List regular files and refuse links, vanished files and empty targets."""
    if target.is_symlink():
        raise ScanUnavailable("Linked scan targets require review")
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise ScanUnavailable("Scan target does not exist")
    paths = sorted(target.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ScanUnavailable("Linked files in a download require review")
    files = [path for path in paths if path.is_file()]
    if not files:
        raise ScanUnavailable("Scan target has no files")
    return files


def mwm_call(settings: dict, env: dict, command: str, arguments: dict, session=None):
    """Call the user's authenticated MWM installation using its documented API."""
    url = str(settings.get("scanner_mwm_url") or "").rstrip("/")
    parts = urlsplit(url)
    token = str(env.get("ROMARR_MWM_TOKEN") or "")
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (not token or parts.username or parts.password or parts.query or parts.fragment
            or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local)):
        raise ScanUnavailable("Set an HTTPS MWM URL and ROMARR_MWM_TOKEN on the ROMarr host")
    try:
        with (session or requests).post(url + "/api/" + command, json=arguments,
                headers={"X-MWM-Token": token}, timeout=(5, 120),
                allow_redirects=False) as response:
            if response.status_code != 200:
                raise ScanUnavailable(f"MWM returned HTTP {response.status_code}")
            return response.json()
    except (requests.RequestException, ValueError) as err:
        raise ScanUnavailable("MWM did not return a usable response") from err


def scan(settings: dict, env: dict, target: Path, session=None) -> tuple[list[Finding], list[str]]:
    """Require a verdict from each enabled scanner before an import continues."""
    enabled = any(settings.get(key) for key in
                  ("clamav_enabled", "scanner_defender_enabled",
                   "scanner_mwm_enabled", "scanner_vt_enabled"))
    if not enabled:
        return [], []
    files = _files(target)
    findings = []
    skipped = []
    if settings.get("clamav_enabled"):
        address = str(settings.get("clamav_address") or "").strip()
        if not address:
            raise ScanUnavailable("ClamAV is enabled but has no address")
        found, large = clamav.scan(address, target)
        findings.extend(found)
        # An enabled scanner must not silently pass an unscanned large package.
        if large:
            raise ScanUnavailable("ClamAV size limit exceeded. Increase StreamMaxLength before importing")
    if settings.get("scanner_defender_enabled"):
        executable = defender_path()
        if not executable:
            raise ScanUnavailable("Microsoft Defender is unavailable on this ROMarr host")
        try:
            result = subprocess.run([executable, "-Scan", "-ScanType", "3",
                                     "-File", str(target.resolve()), "-DisableRemediation"],
                                    capture_output=True, timeout=600,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as err:
            raise ScanUnavailable("Microsoft Defender could not finish the scan") from err
        if result.returncode == 2:
            output = (getattr(result, "stdout", b"") or b"") + (getattr(result, "stderr", b"") or b"")
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            # Exit 2 also covers operational errors. Only explicit threat output
            # establishes a detection; an ambiguous result still holds import.
            if re.search(r"(?im)^\s*(?:Threat(?:\s+Name)?|ThreatID)\s*[:=]\s*\S+", output):
                findings.append(Finding(str(target), "Microsoft Defender reported a threat"))
            else:
                raise ScanUnavailable("Microsoft Defender exited with code 2 without explicit detection evidence")
        elif result.returncode != 0:
            raise ScanUnavailable(f"Microsoft Defender exited with code {result.returncode}")
    if settings.get("scanner_mwm_enabled"):
        for path in files:
            result = mwm_call(settings, env, "lab_av_scan", {"path": str(path.resolve())}, session)
            if not isinstance(result, str):
                raise ScanUnavailable("MWM antivirus result was not recognized")
            if "THREAT FOUND" in result:
                findings.append(Finding(str(path), "MWM antivirus reported a threat"))
            elif result not in ("Microsoft Defender: no threats found.", "ClamAV: clean."):
                raise ScanUnavailable("MWM antivirus is unavailable or did not finish scanning")
    if settings.get("scanner_vt_enabled"):
        token = str(env.get("ROMARR_VIRUSTOTAL_API_KEY") or "")
        if not token:
            raise ScanUnavailable("VirusTotal requires ROMARR_VIRUSTOTAL_API_KEY")
        http = session or requests
        for path in files:
            sha = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    sha.update(chunk)
            try:
                with http.get("https://www.virustotal.com/api/v3/files/" + sha.hexdigest(),
                              headers={"x-apikey": token}, timeout=(5, 30),
                              allow_redirects=False) as response:
                    if response.status_code == 404:
                        raise ScanUnavailable("VirusTotal has no report for this file hash")
                    if response.status_code != 200:
                        raise ScanUnavailable(f"VirusTotal returned HTTP {response.status_code}")
                    stats = response.json()["data"]["attributes"]["last_analysis_stats"]
                    if stats.get("malicious", 0) or stats.get("suspicious", 0):
                        findings.append(Finding(str(path), "VirusTotal reports suspicious or malicious detections"))
                    elif not stats.get("undetected", 0) and not stats.get("harmless", 0):
                        raise ScanUnavailable("VirusTotal report has no completed scanner results")
            except (requests.RequestException, ValueError, KeyError, TypeError) as err:
                raise ScanUnavailable("VirusTotal report could not be checked") from err
    return findings, skipped
