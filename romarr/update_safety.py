"""Fail-closed Git/source updater with an independent systemd worker.

Only a clean, explicitly identified Git checkout may be updated. An atomic
on-disk claim spans the detached worker's entire execution, including service
restart and two health checks. Failed or interrupted operations retain their
claim and evidence for manual recovery; they are never silently reclaimed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

LOCK_NAME = "romarr-selfupdate.lock"
LOG_NAME = "romarr-selfupdate.log"
OFFICIAL_REMOTES = frozenset({
    "https://github.com/BlizzHacker/romarr",
    "https://github.com/BlizzHacker/romarr.git",
    "git@github.com:BlizzHacker/romarr.git",
    "ssh://git@github.com/BlizzHacker/romarr.git",
})
SAFE_REF = re.compile(r"(?:refs/tags/v[A-Za-z0-9][A-Za-z0-9._+-]*|origin/main)\Z")
SAFE_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\Z")


def _cmd(*args: str, cwd: Path | None = None) -> str:
    """Run one bounded read-only Git/systemd probe, raising on any failure."""
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=20, check=True)
    return proc.stdout.strip()


def _health_url() -> str:
    """Resolve a single explicit or local endpoint without following a proxy."""
    configured = os.environ.get("ROMARR_UPDATE_HEALTH_URL", "")
    if not configured:
        host = os.environ.get("ROMARR_HOST", "0.0.0.0")
        if host not in ("0.0.0.0", "127.0.0.1", "localhost", "::", "::1"):
            host = host.strip("[]")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise ValueError("invalid ROMARR_HOST; supply a verified health URL")
        if host in ("0.0.0.0", "::", "localhost"):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = os.environ.get("ROMARR_PORT", "6868")
        if not port.isdecimal() or not (1 <= int(port) <= 65535):
            raise ValueError("invalid ROMARR_PORT")
        if os.environ.get("ROMARR_SSL_CERT") or os.environ.get("ROMARR_SSL_KEY"):
            raise ValueError("TLS install requires a verified ROMARR_UPDATE_HEALTH_URL")
        configured = f"http://{host}:{port}/api/health"
    parsed = urlsplit(configured)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path != "/api/health"
            or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError("health URL must be http(s)://host:port/api/health")
    if re.search(r"[\r\n\x00]", configured):
        raise ValueError("invalid health URL")
    return configured


def _hidden_by_protect_home(path: Path) -> bool:
    """Identify paths hidden by the worker's systemd ProtectHome=yes option."""
    return any(path.is_relative_to(parent) for parent in
               (Path("/home"), Path("/root"), Path("/run/user")))


def _confined_layout(repo: Path, gitdir: Path, python: str) -> Path:
    """Reject layouts not writable and reachable in the detached worker sandbox.

    ProtectHome hides home-directory checkouts. ProtectSystem=strict permits
    writes only under the checkout, including Git metadata and the venv.
    """
    if _hidden_by_protect_home(repo):
        raise ValueError("checkout is hidden by systemd ProtectHome")
    if not gitdir.is_relative_to(repo):
        raise ValueError("external Git metadata is outside the updater's write scope")
    if not Path(python).is_absolute():
        raise ValueError("Python interpreter path must be absolute")
    # abspath normalizes '..' without dereferencing the venv's Python symlink.
    interpreter = Path(os.path.abspath(python))
    if not interpreter.is_relative_to(repo):
        raise ValueError("external Python environment is outside the updater's write scope")
    if (interpreter.parent.name != "bin"
            or not (interpreter.parent.parent / "pyvenv.cfg").is_file()):
        raise ValueError("Git self-update requires a checkout-local Python virtual environment")
    if not interpreter.parent.parent.resolve(strict=True).is_relative_to(repo):
        raise ValueError("virtual environment directory resolves outside the checkout")
    if _hidden_by_protect_home(interpreter.resolve(strict=True)):
        raise ValueError("Python interpreter target is hidden by systemd ProtectHome")
    return interpreter


def preflight(env, target: str) -> dict:
    """Verify exact Git root, branch, origin, clean worktree and service before launch."""
    if env.kind != "git" or not env.repo:
        raise ValueError("only an identified Git/source install can self-update")
    if not SAFE_REF.fullmatch(target):
        raise ValueError("invalid or unsupported update target")
    if not SAFE_SERVICE.fullmatch(env.service) or env.service in (".", ".."):
        raise ValueError("invalid service unit name")
    if not env.branch or env.branch == "HEAD":
        raise ValueError("detached HEAD or unknown branch")
    if not env.python or not Path(env.python).is_file():
        raise ValueError("running Python interpreter missing")
    repo = Path(env.repo).resolve(strict=True)
    if not repo.is_dir() or repo.is_symlink():
        raise ValueError("invalid Git checkout directory")
    if repo == Path("/") or any(char.isspace() for char in str(repo)):
        raise ValueError("checkout path is unsafe for a scoped systemd write allowance")
    if not all(shutil.which(x) for x in ("git", "systemctl", "systemd-run")):
        raise ValueError("Git and systemd manager tools are required")
    git = shutil.which("git")
    if Path(_cmd(git, "rev-parse", "--show-toplevel", cwd=repo)).resolve() != repo:
        raise ValueError("Git checkout root does not match running installation")
    try:
        observed_branch = _cmd(git, "symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo)
    except subprocess.CalledProcessError as exc:
        raise ValueError("Git branch missing or HEAD detached") from exc
    if observed_branch != env.branch:
        raise ValueError("Git branch changed or HEAD detached")
    full = _cmd(git, "rev-parse", "--verify", "HEAD", cwd=repo)
    if (not re.fullmatch(r"[0-9a-f]{40,64}", full)
            or not full.startswith(env.commit)):
        raise ValueError("Git HEAD does not match detected installation")
    if _cmd(git, "status", "--porcelain", "--untracked-files=all", cwd=repo):
        raise ValueError("working tree contains tracked or untracked changes")
    remote = _cmd(git, "remote", "get-url", "origin", cwd=repo)
    configured = os.environ.get("ROMARR_UPDATE_REMOTE_URL", "").strip()
    if (remote != configured if configured else remote not in OFFICIAL_REMOTES):
        raise ValueError("origin URL not explicitly trusted for updates")
    gitdir = Path(_cmd(git, "rev-parse", "--absolute-git-dir", cwd=repo)).resolve(strict=True)
    if not gitdir.is_dir():
        raise ValueError("Git metadata directory missing")
    interpreter = _confined_layout(repo, gitdir, env.python)
    service = env.service if env.service.endswith(".service") else env.service + ".service"
    if _cmd("systemctl", "show", service, "--property=LoadState", "--value") != "loaded":
        raise ValueError("expected systemd service is not loaded")
    if _cmd("systemctl", "is-active", service) != "active":
        raise ValueError("expected systemd service is not active")
    health = _health_url()
    return {"repo": repo, "gitdir": gitdir, "branch": env.branch,
            "commit": full, "remote": remote, "service": service,
            "python": str(interpreter),
            "target": target, "health_url": health}


def build_script(state: dict, run_id: str, log_path: Path, lock: Path) -> str:
    """Build a fail-stop worker that retains its claim on any unresolved error."""
    q = lambda s: shlex.quote(str(s))
    py = q(state["python"])
    # The worker is run by systemd-run as a transient *separate* service.
    # Keep the output for inspection and preserve the lock for any failure.
    health_probe = (
        'import json,sys,urllib.request; '
        'opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); '
        'r=opener.open(urllib.request.Request(sys.argv[1],'
        'headers={"Cache-Control":"no-cache"}),timeout=4); '
        'sys.exit(0 if r.status==200 and json.load(r).get("ok") is True else 1)'
    )
    script = f'''#!/bin/sh
set -eu
exec >>{q(log_path)} 2>&1
RUN={q(run_id)}
LOCK={q(lock)}
PRE={q(state["commit"])}
BRANCH={q(state["branch"])}
REMOTE={q(state["remote"])}
TARGET={q(state["target"])}
SERVICE={q(state["service"])}
HEALTH={q(state["health_url"])}
GITDIR={q(state["gitdir"])}
cd {q(state["repo"])}
echo "START: $RUN; target=$TARGET; service=$SERVICE"
# A worker can only unlock its own claim after a verified successful result.
finish_success() {{
  test "$(cat "$LOCK/run_id")" = "$RUN" || exit 1
  echo 'STATUS: APPLIED; two health passes succeeded'
  rm -- "$LOCK/run.sh" "$LOCK/run_id"
  rmdir -- "$LOCK"
}}
valid_state() {{
  test "$(git rev-parse --show-toplevel)" = {q(state["repo"])} &&
  test "$(git rev-parse --absolute-git-dir)" = "$GITDIR" &&
  test "$(git symbolic-ref --quiet --short HEAD)" = "$BRANCH" &&
  test "$(git remote get-url origin)" = "$REMOTE" &&
  STATUS=$(git status --porcelain --untracked-files=all) &&
  test -z "$STATUS"
}}
health_two() {{
  n=0
  consecutive=0
  while test "$n" -lt 16; do
    n=$((n+1))
    if {py} -c {q(health_probe)} "$HEALTH"; then
      consecutive=$((consecutive+1))
      echo "HEALTH: attempt=$n consecutive=$consecutive"
      if test "$consecutive" -ge 2; then return 0; fi
    else
      consecutive=0
      echo "HEALTH: attempt=$n failed"
    fi
    sleep 2
  done
  return 1
}}
if ! valid_state || test "$(git rev-parse HEAD)" != "$PRE"; then
  echo 'ERROR: source identity, branch or worktree changed; RECOVERY REQUIRED'
  exit 1
fi
if ! git fetch --tags origin; then
  echo 'ERROR: fetch failed; checkout unchanged; RECOVERY REQUIRED'
  exit 1
fi
# Do not trust names or a moving ref after fetch; resolve one exact object.
POST=$(git rev-parse --verify "$TARGET^{{commit}}") || {{
  echo 'ERROR: target ref cannot be resolved; RECOVERY REQUIRED'; exit 1;
}}
if ! valid_state || test "$(git rev-parse HEAD)" != "$PRE"; then
  echo 'ERROR: checkout changed during fetch; RECOVERY REQUIRED'
  exit 1
fi
if ! git merge-base --is-ancestor "$PRE" "$POST"; then
  echo 'ERROR: target is not a fast-forward descendant; RECOVERY REQUIRED'
  exit 1
fi
if ! git merge --ff-only "$POST"; then
  echo 'ERROR: fast-forward refused; RECOVERY REQUIRED'
  exit 1
fi
if ! valid_state || test "$(git rev-parse HEAD)" != "$POST"; then
  echo 'ERROR: post-merge Git state invalid; RECOVERY REQUIRED'
  exit 1
fi
echo "UPDATED: pre=$PRE post=$POST"
if test -f requirements.txt; then
  if ! {py} -m pip install -q -r requirements.txt; then
    echo 'ERROR: dependencies failed; attempting guarded code-only rollback'
    if valid_state && test "$(git rev-parse HEAD)" = "$POST" && git reset --keep "$PRE"; then
      echo 'STATUS: CODE ROLLED BACK; pip environment may have changed; RECOVERY REQUIRED'
    else
      echo 'STATUS: ROLLBACK BLOCKED; preserve checkout and inspect; RECOVERY REQUIRED'
    fi
    exit 1
  fi
fi
if ! valid_state || test "$(git rev-parse HEAD)" != "$POST"; then
  echo 'ERROR: checkout modified during dependencies; RECOVERY REQUIRED'
  exit 1
fi
if ! systemctl restart "$SERVICE"; then
  echo 'ERROR: service restart failed; RECOVERY REQUIRED'
  exit 1
fi
if ! health_two; then
  echo 'ERROR: post-restart health failed; RECOVERY REQUIRED'
  exit 1
fi
if ! valid_state || test "$(git rev-parse HEAD)" != "$POST"; then
  echo 'ERROR: final source identity mismatch; RECOVERY REQUIRED'
  exit 1
fi
finish_success
'''
    return script


def launch(env, target: str) -> dict:
    """Claim a single-flight update and launch an independent transient worker."""
    try:
        state = preflight(env, target)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"update preflight blocked: {exc}"}
    lock = state["gitdir"] / LOCK_NAME
    run_id = uuid.uuid4().hex
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        return {"ok": False, "busy": True, "deferred": True,
                "error": "an updater is already running or needs recovery; inspect its log and claim"}
    except OSError as exc:
        return {"ok": False, "error": f"cannot reserve updater ownership: {exc.__class__.__name__}"}
    started = False
    attempted = False
    try:
        # Recheck Git identity after atomic acquisition, prior to scheduling.
        second = preflight(env, target)
        if second != state:
            raise ValueError("source or service identity changed during reservation")
        (lock / "run_id").write_text(run_id, encoding="ascii")
        log_path = state["gitdir"] / LOG_NAME
        script = lock / "run.sh"
        script.write_text(build_script(state, run_id, log_path, lock), encoding="utf-8")
        script.chmod(0o700)
        unit = "romarr-selfupdate-" + run_id[:16]
        attempted = True
        proc = subprocess.run(
            ["systemd-run", "--collect", f"--unit={unit}",
             "--property=Type=exec",
             "--property=NoNewPrivileges=yes",
             "--property=PrivateTmp=yes",
             "--property=ProtectSystem=strict",
             "--property=ProtectHome=yes",
             f"--property=ReadWritePaths={state['repo']}",
             "/bin/sh", str(script)],
            capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode:
            raise RuntimeError("systemd-run rejected the independent worker")
        started = True
        return {"ok": True, "state": "launched", "target": target,
                "from_commit": state["commit"], "run_id": run_id,
                "message": "Git update worker launched; completion and service health not yet verified"}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"update launch blocked: {exc}"}
    finally:
        if not started and not attempted:
            # Never discard an ambiguous systemd-run launch claim.
            # Only release a reservation that was never handed to systemd.
            if (lock / "run_id").exists():
                if (lock / "run_id").read_text(encoding="ascii") == run_id:
                    (lock / "run_id").unlink()
                    (lock / "run.sh").unlink(missing_ok=True)
            try:
                lock.rmdir()
            except OSError:
                pass


def read_log(env, tail_lines: int = 40) -> str:
    """Read the durable Git-metadata worker log without touching the checkout."""
    try:
        root = Path(env.repo).resolve(strict=True)
        gitdir = _cmd("git", "rev-parse", "--absolute-git-dir", cwd=root)
        log = Path(gitdir) / LOG_NAME
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:])
    except (OSError, subprocess.SubprocessError):
        return ""
