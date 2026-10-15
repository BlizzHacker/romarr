"""Self-update: pulling the latest ROMarr (and ROM Hub plugins) from GitHub.

The UpdateCheck task always answered "a newer version exists" and stopped
there, on the theory that an *arr that updates itself is an *arr that
restarts mid-import. That theory is right about the danger and wrong about
the answer: the danger is manageable, and an appliance that cannot update
itself rots. So this module applies updates, with the three guards that make
it safe:

  1. DEFERRAL. An update never starts while the queue holds work in flight
     (a grab downloading, an import pending). It waits for the next sweep.
  2. DETACHED APPLY. The update runs as its own session, outliving the
     process it restarts: fetch, fast-forward, reinstall requirements, then
     restart the service. No partial state: the merge is `--ff-only`, so a
     diverged checkout refuses rather than creating one.
  3. ROLLBACK HANDLE. The pre-update commit is recorded before anything
     moves, so "the new version is broken" has a one-command answer.

How the update happens depends on how ROMarr is installed, detected rather
than configured:

  git      a clone (the LXC/bare-metal shape): fetch + ff-merge + pip +
           service restart. This is the only shape that can truly
           self-apply, because the code on disk is mutable.
  docker   the ghcr image: the code is a read-only layer, so "updating"
           means re-pulling the image OUTSIDE the container. ROMarr says
           exactly that, with the commands, and does not pretend otherwise.
  none     no git, no container marker: refuse with the reason. An update
           that cannot explain how it would apply is an update that should
           not run.

Channels: `stable` fast-forwards to the latest release tag; `nightly`
fast-forwards to origin/main. Both are GitHub's refs -- nothing is trusted
but what `git fetch` verifies against the remote.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The unit restarted after a git-channel update. Overridable because the
#: service file's name belongs to the operator, not to ROMarr.
SERVICE = os.environ.get("ROMARR_SERVICE", "romarr")

#: Where the detached apply script and its log live, inside the checkout.
#: Dot-prefixed and never committed; the log is tailed by the Update page so
#: an operator watching a restart sees WHY it restarted.
SCRIPT_NAME = ".romarr-selfupdate.sh"
LOG_NAME = ".romarr-selfupdate.log"


@dataclass(frozen=True)
class Environment:
    """How this ROMarr is installed, as detected -- never as configured."""

    kind: str                 # "git" | "docker" | "none"
    repo: str = ""            # git checkout root, when kind == "git"
    python: str = ""          # the interpreter running right now
    branch: str = ""
    commit: str = ""          # short sha of HEAD
    service: str = SERVICE
    detail: str = ""          # why, for the UI

    @property
    def can_apply(self) -> bool:
        return self.kind == "git"


def _container_marker() -> bool:
    """Whether this process runs inside a container. Its own function rather
    than an inline exists() so the detection rule is testable and stated
    once."""
    return (Path("/.dockerenv").exists()
            or Path("/run/.containerenv").exists())


def detect(root: Path | None = None) -> Environment:
    """Classify the install. Order matters: a git clone INSIDE a container is
    still a container (the image owns the code on a real deployment); the
    container markers are checked first so the answer matches how the operator
    actually updates it."""
    # sys.executable FIRST: it is the interpreter actually running ROMarr,
    # which on the common LXC/venv install is /opt/romarr/.venv/bin/python.
    # shutil.which("python3") would find the SYSTEM python3, and pip-installing
    # into that hits PEP 668 ("externally-managed-environment") and fails --
    # so a real update adding a dependency would restart into broken code.
    # Proved on a live install: the log showed the venv was bypassed.
    python = sys.executable or shutil.which("python3") or "python3"
    if _container_marker():
        return Environment(kind="docker", python=python,
                           detail="running inside a container; the image owns "
                                  "the code")
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    git = shutil.which("git")
    if git and (root / ".git").exists():
        branch = _git(git, root, "rev-parse", "--abbrev-ref", "HEAD")
        commit = _git(git, root, "rev-parse", "--short", "HEAD")
        return Environment(kind="git", repo=str(root), python=python,
                           branch=branch, commit=commit,
                           detail=f"git checkout at {commit} on {branch}")
    return Environment(kind="none", python=python,
                       detail="no git checkout and no container marker; "
                              "ROMarr cannot tell how it was installed")


def _git(git: str, cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run([git, "-C", str(cwd), *args],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


#: The states that mean "work is in flight right now". An update during any
#: of these is the mid-import restart the old docstring warned about, so the
#: scheduler defers instead of forcing.
BUSY_STATES = frozenset({"grabbed", "importing"})


def _row_age_minutes(item, now) -> float | None:
    """How long this row has been sitting, or None if it will not say."""
    from datetime import datetime, timezone
    try:
        stamped = datetime.fromisoformat(getattr(item, "at", "") or "")
    except (TypeError, ValueError):
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return (now - stamped).total_seconds() / 60.0


def queue_busy(queue, *, stall_minutes: int = 0) -> bool:
    """Whether any queue row is mid-flight. Reads states, never the network.

    `stall_minutes` (the same setting retire_dead_downloads uses) makes the
    answer honest about dead weight: a `grabbed` row older than the stall
    timeout is not work in flight, it is a download that died and has not
    been swept yet. Counting those as busy starved the auto-update forever on
    a live install -- nine mislabeled grabs from public trackers cycled
    grabbed/stalled/failed/regrabbed for days, each cycle minting a fresh
    `grabbed` row, and every daily UpdateCheck deferred behind a queue that
    would never empty. A stalled row blocks nothing; the sweep retires it.
    """
    from datetime import datetime, timezone
    try:
        now = datetime.now(timezone.utc)
        for item in queue:
            state = getattr(item, "state", "")
            if state not in BUSY_STATES:
                continue
            if state == "grabbed" and stall_minutes > 0:
                age = _row_age_minutes(item, now)
                if age is not None and age >= stall_minutes:
                    continue
            return True
        return False
    except Exception:
        # An unreadable queue is not an idle one: when in doubt, defer.
        return True


def resolve_target(env: Environment, channel: str, latest_tag: str) -> tuple[str, str]:
    """(git ref to fast-forward to, human reason), or ("", why not).

    stable  -> the release tag the UpdateCheck already found on GitHub,
               which is a ref this checkout can fetch and verify.
    nightly -> origin/main, whatever it is right now.
    """
    if channel == "nightly":
        return "origin/main", "nightly channel: fast-forward to origin/main"
    if not latest_tag:
        return "", "stable channel but no release tag is known yet -- " \
                   "run UpdateCheck first"
    tag = latest_tag if latest_tag.startswith("v") else f"v{latest_tag}"
    return f"refs/tags/{tag}", f"stable channel: fast-forward to {tag}"


def build_script(env: Environment, target: str, log_path: str) -> str:
    """The detached updater.

    Written as one small sh script rather than driven from Python because
    its last act restarts the service that would be driving it. Every step
    is fail-stop, and the log path is where the UI reads progress from.

    `sleep 3` first: the API response that triggered this must reach the
    browser before the service dies, or the operator sees a failed request
    instead of "update started".
    """
    return f"""#!/bin/sh
# ROMarr self-update -- detached; survives the restart it performs.
set -e
exec >>{log_path!r} 2>&1
echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') self-update start: target {target} ==="
sleep 3
cd {env.repo!r}
git fetch --tags origin
PRE=$(git rev-parse HEAD)
echo "pre-update commit: $(git rev-parse --short HEAD)"
git checkout {env.branch or 'main'} 2>/dev/null || git checkout main
git merge --ff-only {target}
echo "post-update commit: $(git rev-parse --short HEAD)"
# Dependencies must install into the SAME interpreter that runs ROMarr
# ({env.python!r} -- sys.executable, i.e. the venv on a normal install).
# If they cannot, a restart would come up on new code with missing imports,
# which is worse than staying on the old version: roll the checkout back and
# stop here, leaving the running service untouched.
if [ -f requirements.txt ]; then
  if ! {env.python!r} -m pip install -q -r requirements.txt; then
    echo "ERROR: dependency install failed; rolling back to $PRE, NOT restarting"
    git reset --hard "$PRE"
    exit 1
  fi
fi
echo "restarting {env.service}..."
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart {env.service} || echo "WARN: systemctl restart failed"
else
  echo "WARN: no systemctl; the operator must restart ROMarr manually"
fi
echo "=== self-update done ==="
"""


def launch(env: Environment, target: str) -> dict:
    """Write and start the detached updater. Returns immediately; the script
    outlives this process by design (start_new_session)."""
    if not env.can_apply:
        return {"ok": False, "error": f"cannot self-update: {env.detail}"}
    repo = Path(env.repo)
    log_path = str(repo / LOG_NAME)
    script = repo / SCRIPT_NAME
    script.write_text(build_script(env, target, log_path), encoding="utf-8")
    script.chmod(0o755)
    # Truncate the old log so the Update page never shows a stale run as the
    # current one; the script appends from here.
    Path(log_path).write_text("", encoding="utf-8")
    subprocess.Popen(["/bin/sh", str(script)], cwd=str(repo),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    log.info("self-update launched: %s -> %s", env.commit, target)
    return {"ok": True, "target": target, "from_commit": env.commit,
            "log": log_path,
            "message": f"update to {target} started; ROMarr will restart"}


def read_log(env: Environment, tail_lines: int = 40) -> str:
    """The tail of the last (or running) self-update, for the UI."""
    path = Path(env.repo) / LOG_NAME if env.repo else None
    if not path or not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail_lines:])
    except OSError:
        return ""


def docker_guidance(image: str = "ghcr.io/blizzhacker/romarr:latest") -> dict:
    """What a container install CAN do: nothing from inside, everything from
    the host. The commands are the answer, so the answer is the commands."""
    return {
        "ok": False,
        "error": "ROMarr runs from a container image; the code is a read-only "
                 "layer and cannot update itself from inside.",
        "guidance": [
            f"docker pull {image}",
            "docker stop romarr && docker rm romarr",
            "# recreate with the SAME volumes/env (compose users: "
            "`docker compose up -d --pull always`)",
        ],
        "message": "container install: re-pull the image on the host",
    }
