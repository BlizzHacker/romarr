"""Read-only Nightly comparison against the verified official ROMarr main branch.

This module never fetches Git objects into an installation and never starts
the updater. An image's immutable build provenance works without a .git tree.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

OFFICIAL_REPOSITORY = "BlizzHacker/romarr"
MAIN_REF = "refs/heads/main"
MAIN_URL = "https://api.github.com/repos/BlizzHacker/romarr/commits/main"
COMPARE_URL = "https://api.github.com/repos/BlizzHacker/romarr/compare/"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SSH_DENY_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")
_OFFICIAL_REMOTES = frozenset({
    "https://github.com/BlizzHacker/romarr",
    "https://github.com/BlizzHacker/romarr.git",
    "git@github.com:BlizzHacker/romarr.git",
    "ssh://git@github.com/BlizzHacker/romarr.git",
})


def _local_git(repo: Path, *args: str) -> str:
    """Use a bounded, unredirected Git read without inherited path overrides."""
    git = shutil.which("git")
    if not git:
        return ""
    environment = os.environ.copy()
    for name in _SSH_DENY_ENV:
        environment.pop(name, None)
    try:
        result = subprocess.run([git, "-C", str(repo), *args],
                                capture_output=True, text=True, timeout=10,
                                check=False, env=environment)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def running_main_revision(build: dict, env=None) -> str:
    """Trust a full official build SHA, or a clean verified official Git checkout."""
    revision = str(build.get("source_revision") or "").lower()
    if (build.get("type") == "development"
            and build.get("source_repository") == OFFICIAL_REPOSITORY
            and build.get("source_ref") == MAIN_REF
            and _SHA.fullmatch(revision)):
        return revision

    if (env is None or env.kind != "git" or env.branch != "main"
            or not env.repo):
        return ""
    root = Path(env.repo).resolve()
    if (_local_git(root, "rev-parse", "--show-toplevel") != str(root)
            or _local_git(root, "symbolic-ref", "--quiet", "--short", "HEAD") != "main"
            or _local_git(root, "remote", "get-url", "origin") not in _OFFICIAL_REMOTES
            or _local_git(root, "status", "--porcelain", "--untracked-files=all")):
        return ""
    full = _local_git(root, "rev-parse", "--verify", "HEAD").lower()
    if (not _SHA.fullmatch(full) or not env.commit
            or not full.startswith(env.commit.lower())):
        return ""
    return full


class NightlyCheckError(Exception):
    """A categorized API or response failure that carries no private credentials."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _github_json(request_get, url: str, headers: dict) -> dict:
    """Fetch one bounded GitHub API response, retaining rate-limit distinctions."""
    try:
        response = request_get(url, timeout=10, headers=headers)
        code = getattr(response, "status_code", None)
        if code in (403, 429):
            raise NightlyCheckError(
                "rate_limited" if code == 429
                or response.headers.get("X-RateLimit-Remaining") == "0"
                else "request_denied",
                "github.com limited or denied the Nightly revision check")
        response.raise_for_status()
        answer = response.json()
    except NightlyCheckError:
        raise
    except Exception as err:
        raise NightlyCheckError(
            "request_failed", f"could not verify Nightly revision: {err.__class__.__name__}") from err
    if not isinstance(answer, dict):
        raise NightlyCheckError("invalid_response", "invalid GitHub revision response")
    return answer


def compare_main_revision(running: str, request_get, headers: dict) -> dict:
    """Return current/available/unknown only after verifying commit ancestry.

    A different commit alone does not mean the checked-out revision is older.
    Both the main pointer and the ancestry comparison must be valid.
    """
    if not _SHA.fullmatch(str(running)):
        return {"state": "unknown", "reason": "nightly_build_unverified",
                "latest_revision": ""}
    try:
        latest = str(_github_json(request_get, MAIN_URL, headers).get("sha") or "").lower()
        if not _SHA.fullmatch(latest):
            raise NightlyCheckError("invalid_revision", "invalid official main revision")
        if latest == running:
            return {"state": "current", "reason": "nightly_revision_equal",
                    "latest_revision": latest}
        compared = _github_json(request_get, COMPARE_URL + running + "..." + latest,
                                headers)
        base = compared.get("base_commit") or {}
        ancestor = compared.get("merge_base_commit") or {}
        ahead = compared.get("ahead_by")
        behind = compared.get("behind_by")
        if (not isinstance(base, dict) or not isinstance(ancestor, dict)
                or base.get("sha") != running
                or not isinstance(ahead, int) or isinstance(ahead, bool)
                or not isinstance(behind, int) or isinstance(behind, bool)
                or ahead < 0 or behind < 0):
            raise NightlyCheckError("invalid_comparison", "invalid commit comparison response")
        if (compared.get("status") == "ahead" and ahead > 0
                and behind == 0 and ancestor.get("sha") == running):
            return {"state": "available", "reason": "nightly_newer_main",
                    "latest_revision": latest}
        return {"state": "unknown", "reason": "nightly_history_inconclusive",
                "latest_revision": latest}
    except NightlyCheckError as err:
        state = "error" if err.reason in ("request_failed", "request_denied",
                                         "rate_limited") else "unknown"
        return {"state": state, "reason": err.reason, "latest_revision": "",
                "error": str(err)}
