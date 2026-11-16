"""Official release channels and explicitly approved, pinned source PR previews."""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import time
import threading
import uuid
from pathlib import Path
import requests
from . import selfupdate, update_safety

REPO = "BlizzHacker/romarr"
API = "https://api.github.com/repos/" + REPO
CHANNELS = ("stable", "rc", "beta", "alpha", "nightly")
RANK = {"alpha": 0, "beta": 1, "rc": 2, "stable": 3}
VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
IDENT = re.compile(r"[0-9a-f]{32}\Z")
_FETCH_SLOTS = threading.BoundedSemaphore(2)


def version_key(value):
    """Parse only supported SemVer release stages; stable sorts after its RCs."""
    match = VERSION.fullmatch(str(value or ""))
    if not match:
        return None
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch), RANK[stage or "stable"], int(number or 0))


def stage(value):
    """Identify a recognized tag's stability stage."""
    match = VERSION.fullmatch(str(value or ""))
    return (match.group(4) or "stable") if match else None


def _read(path):
    """Read bounded official metadata without forwarding credentials to redirects."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ROMarr-updates"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    with requests.get(API + path, headers=headers, timeout=10, stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("GitHub metadata unavailable (HTTP %s); try again later" % response.status_code)
        raw = bytearray()
        for chunk in response.iter_content(65536):
            raw.extend(chunk)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("GitHub metadata exceeded its size budget")
        return json.loads(raw)


def _get(path):
    """Limit concurrent metadata probes without waiting indefinitely."""
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise ValueError("Another update metadata check is running; retry shortly")
    try:
        return _read(path)
    except (requests.RequestException, UnicodeError) as exc:
        # Transport exceptions may echo a malformed Authorization header.
        raise ValueError("GitHub metadata request failed; check connectivity and credentials") from exc
    finally:
        _FETCH_SLOTS.release()


def releases(channel="stable"):
    """Inspect at most three pages (180 releases) for the selected stability stage."""
    if channel not in RANK:
        raise ValueError("Choose stable, rc, beta or alpha for release tags")
    rows = []
    for page in range(1, 4):
        batch = _get("/releases?per_page=60&page=" + str(page))
        if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
            raise ValueError("Invalid release catalogue")
        rows.extend(batch[:60])
        if len(batch) < 60:
            break
    result = []
    for row in rows:
        tag = row.get("tag_name", "")
        kind = stage(tag)
        if row.get("draft") or kind is None or RANK[kind] < RANK[channel]:
            continue
        if kind == "stable" and row.get("prerelease"):
            continue
        result.append({"tag": tag, "stage": kind, "name": str(row.get("name") or tag)[:200],
                       "url": "https://github.com/" + REPO + "/releases/tag/" + tag,
                       "published": str(row.get("published_at") or "")[:40]})
    return sorted(result, key=lambda row: version_key(row["tag"]), reverse=True)


def check_channel(service, channel, running):
    """Check preview release tags; applying them remains an explicit action."""
    from .build_info import get_build_info
    try:
        rows = releases(channel)
    except (ValueError, OSError, requests.RequestException) as exc:
        service._record_update_check("error", "release_catalogue_unavailable", checked_channel=channel,
                                     error=str(exc))
        return {"ok": False, "state": "error", "message": str(exc)}
    if str(service.store.settings.get("update_channel") or "stable").lower() != channel:
        return {"ok": False, "state": "unknown", "message": "Channel changed during the check; check again"}
    current = version_key(running)
    if not rows or current is None or get_build_info(running)["type"] == "development":
        service._record_update_check("unknown", "no_comparable_release", checked_channel=channel)
        return {"ok": True, "state": "unknown", "update_available": False,
                "message": "No comparable release is available for this channel"}
    latest = rows[0]["tag"].lstrip("v")
    newer = version_key(latest) > current
    state = "available" if newer else "current"
    service._record_update_check(state, "newer_release" if newer else "current_release", checked_channel=channel, latest=latest)
    return {"ok": True, "state": state, "latest": latest, "update_available": newer,
            "message": (latest + " is available. Preview releases require manual application." if newer else "Up to date for " + channel)}


def list_prs():
    """List public official-repository PRs without fetching code or executing it."""
    rows = _get("/pulls?state=open&per_page=30")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid pull request catalogue")
    return [{"number": r["number"], "title": str(r.get("title") or "")[:200],
             "author": str((r.get("user") or {}).get("login") or "")[:100],
             "url": "https://github.com/" + REPO + "/pull/" + str(r["number"])}
            for r in rows[:30] if type(r.get("number")) is int and isinstance(r.get("user") or {}, dict)]


def inspect_pr(number):
    """Resolve one official PR's exact head/base and checks for owner review."""
    if type(number) is not int or not 1 <= number <= 999999:
        raise ValueError("Enter a valid PR number")
    row = _get("/pulls/" + str(number))
    if not isinstance(row, dict) or row.get("number") != number:
        raise ValueError("GitHub returned a different or invalid PR identity")
    head, base = row.get("head") or {}, row.get("base") or {}
    if (not isinstance(head, dict) or not isinstance(base, dict)
            or not isinstance(base.get("repo") or {}, dict)):
        raise ValueError("Invalid PR source identities")
    if row.get("state") != "open" or (base.get("repo") or {}).get("full_name") != REPO or base.get("ref") != "main":
        raise ValueError("Only open PRs targeting official ROMarr main may be previewed")
    if not SHA.fullmatch(str(head.get("sha") or "")) or not SHA.fullmatch(str(base.get("sha") or "")):
        raise ValueError("GitHub did not return exact source identities")
    response = _get("/commits/" + head["sha"] + "/check-runs?per_page=100")
    if not isinstance(response, dict):
        raise ValueError("Invalid check results")
    checks = response.get("check_runs", [])
    count = response.get("total_count", 0)
    if (not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks)
            or type(count) is not int or not 0 <= count <= 100 or count != len(checks)):
        raise ValueError("Check results could not be completely evaluated")
    passed = any(c.get("name") == "test" and c.get("conclusion") == "success" for c in checks)
    passed = passed and all(c.get("status") == "completed" and c.get("conclusion") in ("success", "neutral", "skipped") for c in checks)
    status = _get("/commits/" + head["sha"] + "/status?per_page=100")
    if not isinstance(status, dict):
        raise ValueError("Invalid commit status results")
    contexts = status.get("statuses")
    count = status.get("total_count", 0)
    if (not isinstance(contexts, list) or any(not isinstance(context, dict) for context in contexts)
            or type(count) is not int or not 0 <= count <= 100 or count != len(contexts)):
        raise ValueError("Commit status results could not be completely evaluated")
    passed = passed and (not contexts or status.get("state") == "success")
    passed = passed and all(c.get("state") == "success" for c in contexts)
    if not isinstance(row.get("user") or {}, dict):
        raise ValueError("Invalid PR author metadata")
    return {"number": number, "title": str(row.get("title") or "")[:200],
            "author": str((row.get("user") or {}).get("login") or "")[:100],
            "head": head["sha"], "base": base["sha"], "draft": bool(row.get("draft")),
            "url": "https://github.com/" + REPO + "/pull/" + str(number),
            "checks_passed": bool(passed), "checks": [{"name": str(c.get("name") or "")[:150],
                "status": c.get("status"), "conclusion": c.get("conclusion")} for c in checks],
            "source_ref": "refs/pull/" + str(number) + "/head",
            "build_guidance": "Review the author and code. Source installs can apply only a clean fast-forward PR. Docker/native apps need a separately built and verified preview package; see docs/UPDATE-CHANNELS.md."}


def active_preview(env=None):
    """Read preview identity only when the current Git commit matches the record."""
    try:
        root = (Path(env.repo) if getattr(env, "repo", "") else Path(__file__).resolve().parent.parent).resolve(strict=True)
        entry = root / ".git"
        # Normal non-preview installs need no subprocess on every build-info read.
        if entry.is_dir() and not (entry / "romarr-preview.json").is_file():
            return None
        if not entry.is_dir() and not entry.is_file():
            return None
        if Path(update_safety._cmd("git", "rev-parse", "--show-toplevel", cwd=root)).resolve(strict=True) != root:
            return None
        gitdir = Path(update_safety._cmd("git", "rev-parse", "--absolute-git-dir", cwd=root)).resolve(strict=True)
        if not gitdir.is_dir() or not gitdir.is_relative_to(root):
            return None
        marker = gitdir / "romarr-preview.json"
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 65536:
            return None
        value = json.loads(marker.read_text())
        if (not isinstance(value, dict) or type(value.get("number")) is not int
                or not 1 <= value["number"] <= 999999
                or not isinstance(value.get("head"), str) or not SHA.fullmatch(value["head"])
                or not isinstance(value.get("backup_id"), str) or not IDENT.fullmatch(value["backup_id"])
                or not isinstance(value.get("previous_commit"), str) or not SHA.fullmatch(value["previous_commit"])):
            return None
        actual = update_safety._cmd("git", "rev-parse", "HEAD", cwd=root)
        if actual != value["head"]:
            return None
        return {"number": value["number"], "head": value["head"], "backup_id": value["backup_id"],
                "previous_commit": value["previous_commit"], "automatic_updates_disabled": True}
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        return None


def preview_provenance():
    """Make a preview distinguishable from the baked stable version it started from."""
    preview = active_preview()
    if not preview:
        return None
    return {"romarr_build_kind": "development", "romarr_source_ref": "refs/pull/%s/head" % preview["number"],
            "romarr_source_revision": preview["head"], "romarr_source_repository": REPO}


def apply_pr(service, body):
    """Revalidate a manually approved PR, back up configuration and launch a pinned update."""
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise ValueError("Explicit confirmation is required")
    info = inspect_pr(body.get("number"))
    if body.get("head") != info["head"] or body.get("base") != info["base"]:
        raise ValueError("PR head or base changed. Inspect the PR again before applying it")
    if info["draft"] or not info["checks_passed"]:
        raise ValueError("The PR is draft, has pending/failed checks, or has no passing test job")
    if body.get("acknowledgement") != "APPLY PR #" + str(info["number"]):
        raise ValueError("Type APPLY PR # followed by the PR number to confirm running its code")
    env = selfupdate.detect()
    if not env.can_apply:
        return {"ok": False, "error": env.detail or "This installation cannot change its bundled code",
                "guidance": info["build_guidance"]}
    with service._lock:
        if selfupdate.queue_busy(service.queue):
            return {"ok": False, "deferred": True, "error": "Wait for all downloads and imports to finish"}
        ident = uuid.uuid4().hex
        prepared = {}
        def prepare(state):
            """Only the atomically reserved updater may create provenance or backups."""
            if active_preview(env):
                raise ValueError("Restore the active preview before applying a different PR")
            marker = state["gitdir"] / "romarr-preview.json"
            if marker.is_symlink():
                raise ValueError("Preview marker must not be a link")
            update_safety._cmd("git", "fetch", "--no-tags", "origin", info["source_ref"], cwd=state["repo"])
            if update_safety._cmd("git", "rev-parse", "FETCH_HEAD", cwd=state["repo"]) != info["head"]:
                raise ValueError("PR changed while fetching. Inspect it again")
            try:
                update_safety._cmd("git", "merge-base", "--is-ancestor", state["commit"], info["head"], cwd=state["repo"])
            except subprocess.CalledProcessError as exc:
                raise ValueError("This PR diverges from your build. Use a separate preview build; local commits were preserved") from exc
            with service.store.path.open("rb") as source:
                raw = source.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Configuration is too large for an automatic preview backup; export it first")
            if not isinstance(json.loads(raw), dict):
                raise ValueError("Configuration backup is not a valid state object")
            folder = state["gitdir"] / "romarr-preview-backups"
            folder.mkdir(mode=0o700, exist_ok=True)
            if folder.is_symlink():
                raise ValueError("Preview backup directory must not be a link")
            folder.chmod(0o700)
            config = folder / (ident + ".config.json")
            with config.open("xb") as output:
                os.chmod(config, 0o600)
                output.write(raw); output.flush(); os.fsync(output.fileno())
            if hashlib.sha256(config.read_bytes()).digest() != hashlib.sha256(raw).digest():
                raise ValueError("Preview configuration backup verification failed")
            record = {"number": info["number"], "head": info["head"], "base": info["base"], "backup_id": ident,
                      "previous_commit": state["commit"], "branch": state["branch"],
                      "config_sha256": hashlib.sha256(raw).hexdigest(), "created": int(time.time())}
            marker.write_text(json.dumps(record)); marker.chmod(0o600)
            record_path = folder / (ident + ".json")
            record_path.write_text(json.dumps(record)); record_path.chmod(0o600)
            update_safety._cmd("git", "update-ref", "refs/romarr/rollback/" + ident, state["commit"], cwd=state["repo"])
            prepared.update(marker=marker, lock=state["gitdir"] / update_safety.LOCK_NAME,
                            auto=service.store.settings.get("auto_update", False))
            service.store.update_settings({"auto_update": False})
        result = update_safety.launch(env, info["source_ref"], expected_revision=info["head"], expected_base=info["base"], prepare=prepare)
        if not result.get("ok") and prepared:
            # A retained claim means systemd may have started the worker even if
            # its acknowledgement failed. Preserve provenance and disable auto-apply.
            if prepared["lock"].exists():
                result["recovery_required"] = True
                result["backup_id"] = ident
            else:
                prepared["marker"].unlink(missing_ok=True)
                service.store.update_settings({"auto_update": prepared["auto"]})
        elif result.get("ok"):
            result["backup_id"] = ident
        return result



def restore_preview(service, body):
    """Restore only the recorded previous code, never overwrite newer owner changes."""
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise ValueError("Confirm restoring the previous code")
    env = selfupdate.detect()
    info = active_preview(env)
    if not info or body.get("backup_id") != info["backup_id"]:
        raise ValueError("No matching active preview exists, or the checkout changed")
    with service._lock:
        if selfupdate.queue_busy(service.queue):
            raise ValueError("Wait for downloads and imports to finish")
        return update_safety.launch(env, "refs/romarr/rollback/" + info["backup_id"])
