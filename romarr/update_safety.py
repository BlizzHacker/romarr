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
SAFE_REF = re.compile(r"(?:refs/tags/v[A-Za-z0-9][A-Za-z0-9._+-]*|origin/main|refs/pull/[1-9][0-9]{0,5}/head|refs/romarr/rollback/[a-f0-9]{32})\Z")
SAFE_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\Z")


_BUILD_STAMP = r"""import os,re,subprocess,sys,tempfile
from pathlib import Path
root=Path.cwd();package=root/'romarr'
if not package.is_dir() or package.is_symlink():
    raise ValueError('Missing or linked ROMarr package; identity not written')
target,revision=sys.argv[1:]
actual=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
if actual!=revision or not re.fullmatch('[0-9a-f]{40}',revision):
    raise ValueError('Source changed before identity publication')
pattern=re.compile(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?\Z')
version='0.0.0+unknown';kind='development';ref=target
if target.startswith('refs/romarr/rollback/'):
    tags=subprocess.check_output(['git','tag','--points-at',revision],text=True).splitlines()
    tags=[tag for tag in tags if pattern.fullmatch(tag)]
    if tags:
        rank={'alpha':0,'beta':1,'rc':2,None:3}
        def key(tag):
            a,b,c,stage,number=pattern.fullmatch(tag).groups()
            return int(a),int(b),int(c),rank[stage],int(number or 0)
        ref='refs/tags/'+max(tags,key=key)
    else:
        ref='refs/heads/'+subprocess.check_output(['git','symbolic-ref','--short','HEAD'],text=True).strip()
elif target=='origin/main':
    ref='refs/heads/main'
if ref.startswith('refs/tags/'):
    tag=ref[len('refs/tags/'):]
    if not pattern.fullmatch(tag):
        raise ValueError('Unsupported release version; identity not written')
    tagged=subprocess.check_output(['git','rev-parse','--verify',ref+'^{commit}'],text=True).strip()
    if tagged!=revision:
        raise ValueError('Release tag changed before identity publication')
    kind='release';version=tag[1:]
fields={'romarr_build_kind':kind,'romarr_source_ref':ref,'romarr_source_revision':revision,'romarr_source_repository':'BlizzHacker/romarr'}
outputs={package/'_version.py':'__version__ = '+repr(version)+'\n',package/'_build_info.py':''.join(k+' = '+repr(v)+'\n' for k,v in fields.items())}
for path in outputs:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Build metadata must be regular files')
    if subprocess.run(['git','check-ignore','-q','--',str(path.relative_to(root))]).returncode:
        raise ValueError('Generated metadata must be ignored by Git')
for path,content in outputs.items():
    descriptor,name=tempfile.mkstemp(prefix='.romarr-identity-',dir=package)
    temporary=Path(name)
    try:
        os.fchmod(descriptor,0o644)
        with os.fdopen(descriptor,'w') as output:
            output.write(content);output.flush();os.fsync(output.fileno())
        if path.is_symlink():
            raise ValueError('Build metadata changed during publication')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
"""

_GIT_LOCATION_OVERRIDES = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
})


def _safe_git_env() -> dict[str, str]:
    """Omit inherited repository-location overrides from Git probes and workers."""
    return {key: value for key, value in os.environ.items()
            if key not in _GIT_LOCATION_OVERRIDES}


def _is_ssh_remote(remote: str) -> bool:
    """Detect URL and Git SCP-style SSH origins without blocking local paths."""
    if remote.startswith(("ssh://", "git+ssh://", "ssh+git://", "git@")):
        return True
    # Git recognizes host:path and user@host:path as SSH transport. Absolute
    # and ./relative filesystem paths containing colons are not remote hosts.
    return ("://" not in remote and ":" in remote
            and "/" not in remote.split(":", 1)[0])


def _cmd(*args: str, cwd: Path | None = None) -> str:
    """Run one bounded read-only Git/systemd probe without hostile Git paths."""
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=20, check=True, env=_safe_git_env())
    return proc.stdout.strip()


def _health_url() -> str:
    """Resolve a single explicit or local endpoint without following a proxy."""
    configured = os.environ.get("ROMARR_UPDATE_HEALTH_URL", "")
    if not configured:
        host = os.environ.get("ROMARR_HOST") or "0.0.0.0"
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


def preflight(env, target: str, *, expected_revision: str = "", expected_base: str = "") -> dict:
    """Verify exact Git root, branch, origin, clean worktree and service before launch."""
    if env.kind != "git" or not env.repo:
        raise ValueError("only an identified Git/source install can self-update")
    if not SAFE_REF.fullmatch(target):
        raise ValueError("invalid or unsupported update target")
    pull_preview = target.startswith("refs/pull/")
    if pull_preview and (not re.fullmatch(r"[0-9a-f]{40}", expected_revision) or not re.fullmatch(r"[0-9a-f]{40}", expected_base)):
        raise ValueError("PR previews require exact head and base revisions")
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
    if _is_ssh_remote(remote):
        # ProtectHome=yes can hide the agent, keys and known_hosts expected
        # by URL and SCP-style SSH origins. Refuse before reservation.
        raise ValueError("SSH Git remote is unsupported by the confined updater; use an explicitly trusted HTTPS origin")
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
    result = {"repo": repo, "gitdir": gitdir, "branch": env.branch,
              "commit": full, "remote": remote, "service": service,
              "python": str(interpreter), "target": target, "health_url": health}
    if pull_preview:
        result.update(expected_revision=expected_revision, expected_base=expected_base)
    if target.startswith("refs/romarr/rollback/"):
        ident = target.rsplit("/", 1)[-1]
        metadata = gitdir / "romarr-preview-backups" / (ident + ".json")
        if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 65536:
            raise ValueError("Preview rollback record is missing or unsafe")
        record = json.loads(metadata.read_text())
        if not isinstance(record, dict):
            raise ValueError("Invalid preview rollback record")
        if record.get("backup_id") != ident or record.get("head") != full or record.get("branch") != env.branch:
            raise ValueError("Checkout changed after the preview; rollback refused")
        previous = record.get("previous_commit", "")
        if not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{40}", previous) or _cmd(git, "rev-parse", "--verify", target + "^{commit}", cwd=repo) != previous:
            raise ValueError("Rollback target changed")
        result.update(rollback=True, expected_revision=previous)
    return result


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
    fetch = "git fetch --tags origin"
    resolve = 'POST=$(git rev-parse --verify "$TARGET^{commit}")'
    if state.get("expected_base"):
        fetch = ("git fetch --no-tags origin refs/heads/main && "
                 + "test \"$(git rev-parse FETCH_HEAD)\" = " + q(state["expected_base"])
                 + " && git fetch --no-tags origin \"$TARGET\"")
        resolve = 'POST=$(git rev-parse --verify "FETCH_HEAD^{commit}")'
    elif state.get("rollback"):
        fetch = ":"
    expected = state.get("expected_revision", "")
    collision_check = (
        "import os,subprocess,sys;from pathlib import Path;"
        "old=set(subprocess.check_output(['git','ls-files','-z']).split(bytes([0])));"
        "new=subprocess.check_output(['git','ls-tree','-r','--name-only','-z',sys.argv[1]]).split(bytes([0]));"
        "sys.exit(1 if any(p and p not in old and os.path.lexists(os.fsdecode(p)) for p in new) else 0)"
    )
    move = ('git merge --ff-only --no-overwrite-ignore "$POST"' if not state.get("rollback") else
            py + ' -c ' + q(collision_check) + ' "$POST" && '
            'git checkout --detach --no-overwrite-ignore "$POST" && '
            'git update-ref "refs/heads/$BRANCH" "$POST" "$PRE" && '
            'git symbolic-ref HEAD "refs/heads/$BRANCH"')
    ancestry = 'git merge-base --is-ancestor "$PRE" "$POST"' if not state.get("rollback") else ":"
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
# Do not inherit caller-selected Git object/index/worktree locations.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR
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
if ! ( {fetch} ); then
  echo 'ERROR: fetch failed; checkout unchanged; RECOVERY REQUIRED'
  exit 1
fi
# Do not trust names or a moving ref after fetch; resolve one exact object.
{resolve} || {{
  echo 'ERROR: target ref cannot be resolved; RECOVERY REQUIRED'; exit 1;
}}
if test -n {q(expected)} && test "$POST" != {q(expected)}; then
  echo 'ERROR: pinned preview revision changed; RECOVERY REQUIRED'; exit 1
fi
if ! valid_state || test "$(git rev-parse HEAD)" != "$PRE"; then
  echo 'ERROR: checkout changed during fetch; RECOVERY REQUIRED'
  exit 1
fi
if ! ( {ancestry} ); then
  echo 'ERROR: target is not a fast-forward descendant; RECOVERY REQUIRED'
  exit 1
fi
if ! ( {move} ); then
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
    if valid_state && test "$(git rev-parse HEAD)" = "$POST" &&
       git checkout --detach --no-overwrite-ignore "$PRE" &&
       git update-ref "refs/heads/$BRANCH" "$PRE" "$POST" &&
       git symbolic-ref HEAD "refs/heads/$BRANCH"; then
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
if ! {py} -c {q(_BUILD_STAMP)} "$TARGET" "$POST"; then
  echo 'ERROR: generated identity publication failed; RECOVERY REQUIRED'
  exit 1
fi
if ! valid_state || test "$(git rev-parse HEAD)" != "$POST"; then
  echo 'ERROR: checkout changed during identity publication; RECOVERY REQUIRED'
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


def launch(env, target: str, *, expected_revision: str = "", expected_base: str = "", prepare=None) -> dict:
    """Claim a single-flight update and launch an independent transient worker."""
    options = {"expected_revision": expected_revision, "expected_base": expected_base} if expected_revision or expected_base else {}
    try:
        state = preflight(env, target, **options)
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
        second = preflight(env, target, **options)
        if second != state:
            raise ValueError("source or service identity changed during reservation")
        if prepare is not None:
            # Preview backups and provenance must share the worker's atomic ownership.
            prepare(state)
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
            capture_output=True, text=True, timeout=30, check=False,
            env=_safe_git_env())
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
