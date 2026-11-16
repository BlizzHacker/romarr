"""Disposable Git/source update safety tests; never start a real ROMarr service."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from romarr import update_safety as safety

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX/systemd-only worker")


def git(*args, cwd=None):
    """Run Git within disposable pytest repositories."""
    safe = safety._safe_git_env()
    return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                   stderr=subprocess.STDOUT, env=safe).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """Create a fully local upstream, publisher and consumer checkout."""
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-q", str(origin))
    publisher = tmp_path / "publisher"
    git("init", "-q", "--initial-branch=main", str(publisher))
    git("config", "user.email", "tests@example.invalid", cwd=publisher)
    git("config", "user.name", "Fixture", cwd=publisher)
    (publisher / "example.txt").write_text("original\n")
    (publisher / "romarr").mkdir()
    (publisher / "romarr/version.py").write_text("# Fixture package identity\n")
    (publisher / ".gitignore").write_text(".venv/\nromarr/_version.py\nromarr/_build_info.py\n")
    git("add", ".", cwd=publisher)
    git("commit", "-qm", "first", cwd=publisher)
    git("remote", "add", "origin", str(origin), cwd=publisher)
    git("push", "-q", "-u", "origin", "main", cwd=publisher)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    consumer = tmp_path / "consumer"
    git("clone", "-q", str(origin), str(consumer))
    git("config", "user.email", "tests@example.invalid", cwd=consumer)
    git("config", "user.name", "Fixture", cwd=consumer)
    monkeypatch.setenv("ROMARR_UPDATE_REMOTE_URL", str(origin))
    # Keep real Git lookup; simulated systemd probes do not require a systemd host.
    real_which = safety.shutil.which
    monkeypatch.setattr(safety.shutil, "which", lambda name:
                        f"/usr/bin/{name}" if name in ("systemctl", "systemd-run")
                        else real_which(name))
    # No actual systemd service will be queried by tests.
    old = safety._cmd
    monkeypatch.setattr(safety, "_cmd", lambda *a, **kw:
        "loaded" if a[0] == "systemctl" and "show" in a else
        "active" if a[0] == "systemctl" and "is-active" in a else old(*a, **kw))
    monkeypatch.setenv("ROMARR_UPDATE_HEALTH_URL", "http://127.0.0.1:6868/api/health")
    venv = consumer / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = " + sys.base_prefix + "\n")
    # Copy the host executable into the disposable checkout so pyenv under
    # a protected home directory cannot make the path-only tests fail.
    fixture_python = venv / "bin" / "python-fixture"
    shutil.copy2(sys.executable, fixture_python)
    (venv / "bin" / "python").symlink_to("python-fixture")
    env = SimpleNamespace(kind="git", repo=str(consumer), python=str(venv / "bin" / "python"),
                          branch="main", commit=git("rev-parse", "HEAD", cwd=consumer),
                          service="romarr")
    return publisher, consumer, origin, env


def publish(publisher, content="new\n", *, requirements=None):
    """Advance the disposable remote without moving the consumer."""
    (publisher / "example.txt").write_text(content)
    if requirements is not None:
        (publisher / "requirements.txt").write_text(requirements)
    git("add", ".", cwd=publisher)
    git("commit", "-qm", "next", cwd=publisher)
    git("push", "-q", "origin", "main", cwd=publisher)
    return git("rev-parse", "HEAD", cwd=publisher)


def test_venv_python_symlink_is_not_resolved_to_system_interpreter(checkout):
    """The updater must use the checkout-local venv, even when symlinked."""
    _pub, repo, _origin, env = checkout
    assert (repo / ".venv" / "bin" / "python").is_symlink()
    state = safety.preflight(env, "origin/main")
    assert state["python"] == env.python


def test_home_checkout_is_blocked_before_reservation():
    """ProtectHome hides source checkouts under home and root."""
    with pytest.raises(ValueError, match="ProtectHome"):
        safety._confined_layout(Path("/home/operator/romarr"),
                                Path("/home/operator/romarr/.git"),
                                "/home/operator/romarr/.venv/bin/python")
    with pytest.raises(ValueError, match="ProtectHome"):
        safety._confined_layout(Path("/root/romarr"),
                                Path("/root/romarr/.git"),
                                "/root/romarr/.venv/bin/python")


def test_external_git_metadata_is_blocked_before_reservation(checkout):
    """A linked or separate Git directory cannot receive the worker's log."""
    _pub, repo, _origin, env = checkout
    external = repo.parent / "separate-metadata.git"
    (repo / ".git").rename(external)
    (repo / ".git").write_text("gitdir: " + str(external) + "\n")
    with pytest.raises(ValueError, match="external Git metadata"):
        safety.preflight(env, "origin/main")
    assert not (external / safety.LOCK_NAME).exists()


def test_external_python_environment_is_blocked_before_reservation(checkout):
    """An interpreter outside ReadWritePaths must not reach the worker."""
    _pub, repo, _origin, env = checkout
    env.python = sys.executable
    with pytest.raises(ValueError, match="external Python environment"):
        safety.preflight(env, "origin/main")
    assert not (repo / ".git" / safety.LOCK_NAME).exists()


def test_missing_checkout_local_virtual_environment_is_rejected(checkout):
    """A system Python symlink without venv metadata is not an install target."""
    _pub, repo, _origin, env = checkout
    (repo / ".venv" / "pyvenv.cfg").unlink()
    with pytest.raises(ValueError, match="checkout-local Python virtual environment"):
        safety.preflight(env, "origin/main")


def test_symlinked_external_virtual_environment_is_rejected(checkout):
    """A linked venv cannot bypass checkout-only filesystem allowances."""
    _pub, repo, _origin, env = checkout
    external = repo.parent / "external-venv"
    (repo / ".venv").rename(external)
    (repo / ".venv").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="resolves outside the checkout"):
        safety._confined_layout(repo, repo / ".git", env.python)


@pytest.mark.parametrize("protected", [
    "/home/operator/.pyenv/bin/python", "/root/.pyenv/bin/python",
    "/run/user/1000/python",
])
def test_protecthome_rejects_hidden_interpreter_target_paths(protected):
    """The worker cannot execute targets in paths hidden by ProtectHome."""
    assert safety._hidden_by_protect_home(Path(protected)) is True


def test_resolved_python_target_is_checked_before_claim(checkout, monkeypatch):
    """A protected symlink target blocks the launch despite a local venv path."""
    _pub, repo, _origin, env = checkout
    actual_target = Path(env.python).resolve(strict=True)
    original = safety._hidden_by_protect_home
    monkeypatch.setattr(safety, "_hidden_by_protect_home",
                        lambda p: original(p) or p == actual_target)
    with pytest.raises(ValueError, match="interpreter target is hidden"):
        safety.preflight(env, "origin/main")
    assert not (repo / ".git" / safety.LOCK_NAME).exists()


def test_preflight_requires_exact_clean_git_identity(checkout):
    _pub, repo, origin, env = checkout
    state = safety.preflight(env, "origin/main")
    assert state["repo"] == repo
    assert state["remote"] == str(origin)
    assert state["commit"] == env.commit
    assert state["service"] == "romarr.service"


@pytest.mark.parametrize("content,tracked", [("edit", True), ("new", False)])
def test_dirty_tracked_and_untracked_changes_are_protected(checkout, content, tracked):
    _pub, repo, _origin, env = checkout
    (repo / ("example.txt" if tracked else "operator-note.txt")).write_text(content)
    with pytest.raises(ValueError, match="working tree"):
        safety.preflight(env, "origin/main")


def test_detached_head_is_rejected(checkout):
    _pub, repo, _origin, env = checkout
    git("checkout", "--detach", "-q", cwd=repo)
    with pytest.raises(ValueError, match="branch"):
        safety.preflight(env, "origin/main")


def test_wrong_head_is_rejected(checkout):
    _pub, _repo, _origin, env = checkout
    env.commit = "f" * 40
    with pytest.raises(ValueError, match="HEAD"):
        safety.preflight(env, "origin/main")


def test_wrong_remote_is_rejected(checkout, monkeypatch):
    _pub, _repo, _origin, env = checkout
    monkeypatch.setenv("ROMARR_UPDATE_REMOTE_URL", "https://github.com/BlizzHacker/romarr.git")
    with pytest.raises(ValueError, match="origin"):
        safety.preflight(env, "origin/main")


@pytest.mark.parametrize("target", ["main;id", "--force", "origin/other", "refs/tags/oops", ""])
def test_unsafe_target_is_rejected(checkout, target):
    _pub, _repo, _origin, env = checkout
    with pytest.raises(ValueError, match="target"):
        safety.preflight(env, target)


def test_shell_injection_service_refused(checkout):
    _pub, _repo, _origin, env = checkout
    env.service = "romarr;touch /tmp/oops"
    with pytest.raises(ValueError, match="service"):
        safety.preflight(env, "origin/main")


def test_health_probe_ignores_environment_proxy(checkout):
    """Readiness requests use an explicit opener without ambient HTTP proxies."""
    _pub, _repo, _origin, env = checkout
    state = safety.preflight(env, "origin/main")
    script = safety.build_script(state, "pcheck", state["gitdir"] / "a.log",
                                 state["gitdir"] / "lock")
    assert "urllib.request.ProxyHandler({})" in script
    assert "opener.open(" in script
    assert '"Cache-Control":"no-cache"' in script


def test_fixture_mocks_systemd_binary_discovery_without_hiding_git(checkout):
    """Systemd discovery is synthetic while Git remains a real executable."""
    _pub, _repo, _origin, env = checkout
    assert safety.shutil.which("systemctl") == "/usr/bin/systemctl"
    assert safety.shutil.which("systemd-run") == "/usr/bin/systemd-run"
    assert safety.shutil.which("git")
    assert safety.preflight(env, "origin/main")["branch"] == "main"


def test_script_has_no_branch_fallback_or_destructive_reset(checkout):
    _pub, _repo, _origin, env = checkout
    state = safety.preflight(env, "origin/main")
    script = safety.build_script(state, "123", state["gitdir"] / "a.log", state["gitdir"] / "lock")
    assert "checkout main" not in script and "git reset --hard" not in script
    assert "git checkout --detach --no-overwrite-ignore" in script
    assert 'git update-ref "refs/heads/$BRANCH" "$PRE" "$POST"' in script
    assert 'git merge --ff-only --no-overwrite-ignore "$POST"' in script
    assert "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE" in script
    assert "git merge-base --is-ancestor" in script
    assert "HEALTH: attempt" in script and "consecutive" in script
    shell = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
    assert shell.returncode == 0, shell.stderr


def test_two_parallel_launches_have_one_atomic_owner(checkout, monkeypatch):
    _pub, _repo, _origin, env = checkout
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = safety.subprocess.run
    monkeypatch.setattr(safety.subprocess, "run", lambda args, **kwargs:
        fake_run(args, **kwargs) if args[0] == "systemd-run" else original(args, **kwargs))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: safety.launch(env, "origin/main"), range(2)))
    assert sum(x["ok"] for x in results) == 1
    assert sum(x.get("busy", False) for x in results) == 1
    assert len(calls) == 1
    assert calls[0][0] == "systemd-run" and "--collect" in calls[0]


def test_detached_worker_retains_shipped_service_sandbox(checkout, monkeypatch):
    """Independent worker must be confined to the checked-out source directory."""
    _pub, repo, _origin, env = checkout
    calls = []
    real_run = safety.subprocess.run

    def fake_run(args, **kwargs):
        if args[0] == "systemd-run":
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(safety.subprocess, "run", fake_run)
    out = safety.launch(env, "origin/main")
    assert out["ok"] is True
    assert len(calls) == 1
    argv = calls[0]
    assert "--property=Type=exec" in argv
    assert "--property=NoNewPrivileges=yes" in argv
    assert "--property=PrivateTmp=yes" in argv
    assert "--property=ProtectSystem=strict" in argv
    assert "--property=ProtectHome=yes" in argv
    assert f"--property=ReadWritePaths={repo}" in argv
    assert sum(x.startswith("--property=ReadWritePaths=") for x in argv) == 1


def test_ambiguous_checkout_path_is_rejected_before_worker_claim(checkout, monkeypatch):
    """Scoped write allowances must never contain whitespace-separated paths."""
    _pub, repo, _origin, env = checkout
    target = repo.parent / "checkout with spaces"
    repo.rename(target)
    env.repo = str(target)
    env.python = str(target / ".venv" / "bin" / "python")
    with pytest.raises(ValueError, match="unsafe for a scoped systemd"):
        safety.preflight(env, "origin/main")


def test_launch_failure_preserves_ambiguous_claim(checkout, monkeypatch):
    _pub, repo, _origin, env = checkout
    original = safety.subprocess.run
    monkeypatch.setattr(safety.subprocess, "run", lambda args, **kwargs:
        SimpleNamespace(returncode=1, stderr="denied", stdout="")
        if args[0] == "systemd-run" else original(args, **kwargs))
    result = safety.launch(env, "origin/main")
    assert result["ok"] is False
    assert "worker" in result["error"]
    assert (repo / ".git" / safety.LOCK_NAME).exists()


def _run_worker(checkout, monkeypatch, *, pip="success", restart="success", dirty_on_pip=False, fail_status=False):
    publisher, repo, _origin, env = checkout
    req = "placeholder\n" if pip != "none" else None
    new_head = publish(publisher, requirements=req)
    state = safety.preflight(env, "origin/main")
    lock = state["gitdir"] / safety.LOCK_NAME
    lock.mkdir()
    (lock / "run_id").write_text("test-run")
    testbin = repo.parent / "testbin"
    testbin.mkdir()
    if fail_status:
        real_git = shutil.which("git")
        wrapper = testbin / "git"
        wrapper.write_text('#!/bin/sh\nif [ "$1" = status ]; then exit 2; fi\nexec ' + real_git + ' "$@"\n')
        wrapper.chmod(0o755)
    ctl = testbin / "systemctl"
    ctl.write_text("#!/bin/sh\n" +
                   ("if [ \"$1\" = restart ]; then exit 2; fi\n" if restart == "fail" else "") +
                   "exit 0\n")
    ctl.chmod(0o755)
    # A test-only interpreter that handles pip and health without network access.
    fakepy = testbin / "test-python"
    fakepy.write_text("#!/bin/sh\n" +
                      ("if [ \"$1\" = '-m' ]; then touch " + str(repo / "operator-change.txt") + "; exit 12; fi\n"
                       if dirty_on_pip else
                       "if [ \"$1\" = '-m' ]; then exit 12; fi\n" if pip == "fail" else
                       "if [ \"$1\" = '-m' ]; then exit 0; fi\n") +
                      "if [ \"$1\" = '-c' ]; then exit 0; fi\nexit 1\n")
    fakepy.chmod(0o755)
    state["python"] = str(fakepy)
    script = lock / "run.sh"
    log = state["gitdir"] / safety.LOG_NAME
    script.write_text(safety.build_script(state, "test-run", log, lock))
    script.chmod(0o700)
    result = subprocess.run(["/bin/sh", str(script)], cwd=repo,
                            env={**os.environ, "PATH": str(testbin) + ":" + os.environ["PATH"]},
                            capture_output=True, text=True, timeout=20)
    return repo, env, new_head, lock, log.read_text(), result


def test_clean_fast_forward_and_verified_completion(checkout, monkeypatch):
    repo, env, new, lock, log, result = _run_worker(checkout, monkeypatch, pip="none")
    assert result.returncode == 0, log
    assert git("rev-parse", "HEAD", cwd=repo) == new
    assert not lock.exists()
    assert "STATUS: APPLIED" in log
    assert "consecutive=2" in log


def test_dependency_failure_rolls_back_only_clean_code(checkout, monkeypatch):
    repo, env, new, lock, log, result = _run_worker(checkout, monkeypatch, pip="fail")
    assert result.returncode != 0
    assert git("rev-parse", "HEAD", cwd=repo) == env.commit
    assert lock.exists()
    assert "CODE ROLLED BACK" in log and "RECOVERY REQUIRED" in log


def test_modified_worktree_blocks_rollback(checkout, monkeypatch):
    repo, env, new, lock, log, result = _run_worker(checkout, monkeypatch, pip="fail", dirty_on_pip=True)
    assert result.returncode != 0
    assert git("rev-parse", "HEAD", cwd=repo) == new
    assert (repo / "operator-change.txt").exists()
    assert lock.exists()
    assert "ROLLBACK BLOCKED" in log


def test_restart_failure_keeps_recovery_claim(checkout, monkeypatch):
    repo, env, new, lock, log, result = _run_worker(checkout, monkeypatch, pip="none", restart="fail")
    assert result.returncode != 0
    assert git("rev-parse", "HEAD", cwd=repo) == new
    assert lock.exists()
    assert "restart failed; RECOVERY REQUIRED" in log


def test_git_status_probe_failure_is_not_treated_as_clean(checkout, monkeypatch):
    """A failing Git cleanliness probe must stop before moving the checkout."""
    repo, env, _new, lock, log, result = _run_worker(
        checkout, monkeypatch, pip="none", fail_status=True)
    assert result.returncode != 0
    assert git("rev-parse", "HEAD", cwd=repo) == env.commit
    assert lock.exists()
    assert "source identity, branch or worktree changed" in log


@pytest.mark.parametrize("optimized", [False, True])
def test_health_false_verdict_fails_even_with_python_optimization(checkout, optimized):
    """Readiness validation must remain effective when assertions are disabled."""
    import shlex
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        """Return a successful HTTP response with a failed application verdict."""

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":false}')

        def log_message(self, *args):
            pass

    _pub, _repo, _origin, env = checkout
    state = safety.preflight(env, "origin/main")
    script = safety.build_script(state, "probe", state["gitdir"] / "a.log",
                                 state["gitdir"] / "lock")
    line = next(line.strip() for line in script.splitlines() if " -c " in line)
    tokens = shlex.split(line)
    probe = tokens[tokens.index("-c") + 1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        args = [sys.executable] + (["-O"] if optimized else [])
        result = subprocess.run(args + ["-c", probe,
            f"http://127.0.0.1:{server.server_port}/api/health"],
            capture_output=True, timeout=10)
        assert result.returncode != 0
    finally:
        server.shutdown()
        server.server_close()


def test_git_path_environment_cannot_redirect_preflight(checkout, monkeypatch):
    """Read-only Git probes ignore inherited path overrides."""
    _pub, repo, _origin, env = checkout
    monkeypatch.setenv("GIT_DIR", str(repo.parent / "untrusted-metadata"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo.parent / "untrusted-tree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo.parent / "untrusted-index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(repo.parent / "untrusted-objects"))
    observed = safety._cmd("git", "rev-parse", "--show-toplevel", cwd=repo)
    assert observed == str(repo)
    assert safety.preflight(env, "origin/main")["repo"] == repo
    script = safety.build_script(safety.preflight(env, "origin/main"),
                                 "git-path-env", repo / ".git" / "a.log",
                                 repo / ".git" / "claim")
    assert "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE" in script


@pytest.mark.parametrize("ssh_origin_url", [
    "git@github.com:BlizzHacker/romarr.git",
    "deploy@example.invalid:romarr.git",
    "example.invalid:romarr.git",
    "ssh://git@github.com/BlizzHacker/romarr.git",
    "git+ssh://git@github.com/BlizzHacker/romarr.git",
    "ssh+git://git@github.com/BlizzHacker/romarr.git",
])
def test_ssh_remote_is_blocked_before_worker_reservation(
        checkout, monkeypatch, ssh_origin_url):
    """Confined worker must reject URL and SCP-form SSH before its claim."""
    _pub, repo, _origin, env = checkout
    original_cmd = safety._cmd

    def ssh_origin(*args, **kwargs):
        """Return an otherwise official SSH remote for the read-only probe."""
        if len(args) >= 4 and tuple(args[1:4]) == ("remote", "get-url", "origin"):
            return ssh_origin_url
        return original_cmd(*args, **kwargs)

    monkeypatch.setattr(safety, "_cmd", ssh_origin)
    monkeypatch.setenv("ROMARR_UPDATE_REMOTE_URL", ssh_origin_url)
    with pytest.raises(ValueError, match="SSH Git remote"):
        safety.preflight(env, "origin/main")
    assert not (repo / ".git" / safety.LOCK_NAME).exists()


def test_empty_host_uses_local_health_fallback(monkeypatch):
    """An explicitly empty host should behave like an unset host."""
    monkeypatch.delenv("ROMARR_UPDATE_HEALTH_URL", raising=False)
    monkeypatch.setenv("ROMARR_HOST", "")
    monkeypatch.setenv("ROMARR_PORT", "6868")
    monkeypatch.delenv("ROMARR_SSL_CERT", raising=False)
    monkeypatch.delenv("ROMARR_SSL_KEY", raising=False)
    assert safety._health_url() == "http://127.0.0.1:6868/api/health"


def test_ignored_operator_file_is_not_overwritten_by_fast_forward(checkout):
    """The exact worker merge mode refuses an ignored-file/tracked-file collision."""
    publisher, repo, _origin, _env = checkout
    with (publisher / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("operator.txt\n")
    git("add", ".gitignore", cwd=publisher)
    git("commit", "-qm", "ignore local operator file", cwd=publisher)
    git("push", "-q", "origin", "main", cwd=publisher)
    git("fetch", "-q", "origin", cwd=repo)
    git("merge", "--ff-only", "origin/main", cwd=repo)
    (repo / "operator.txt").write_text("owner data\n")
    (publisher / "operator.txt").write_text("upstream data\n")
    git("add", "-f", "operator.txt", cwd=publisher)
    git("commit", "-qm", "upstream now tracks formerly ignored path", cwd=publisher)
    git("push", "-q", "origin", "main", cwd=publisher)
    git("fetch", "-q", "origin", cwd=repo)
    before = git("rev-parse", "HEAD", cwd=repo)
    failed = subprocess.run(["git", "merge", "--ff-only",
                             "--no-overwrite-ignore", "origin/main"],
                            cwd=repo, capture_output=True, text=True,
                            env=safety._safe_git_env())
    assert failed.returncode != 0
    assert git("rev-parse", "HEAD", cwd=repo) == before
    assert (repo / "operator.txt").read_text() == "owner data\n"


def test_ssh_remote_detection_preserves_local_and_https_paths():
    """Explicit relative/absolute origins are not misclassified as SCP remotes."""
    assert safety._is_ssh_remote("deploy@example.invalid:romarr.git")
    assert safety._is_ssh_remote("example.invalid:romarr.git")
    assert not safety._is_ssh_remote("https://github.com/BlizzHacker/romarr.git")
    assert not safety._is_ssh_remote("/tmp/romarr:local")
    assert not safety._is_ssh_remote("./repo:local")



def _preview_fixture(checkout, monkeypatch, *, remove_file=False):
    """Publish a real pull ref and capture launch without running host systemd."""
    import threading
    from romarr import release_controls as controls
    from romarr.store import Store
    publisher, repo, origin, env = checkout
    env.can_apply = True
    if remove_file == "nested":
        (publisher / "payload").mkdir()
        (publisher / "payload/game").write_text("tracked game fixture")
        git("add", "payload/game", cwd=publisher); git("commit", "-qm", "nested fixture", cwd=publisher)
        git("push", "-q", "origin", "main", cwd=publisher)
        git("pull", "-q", "--ff-only", cwd=repo)
        env.commit = git("rev-parse", "HEAD", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=publisher)
    git("checkout", "-qb", "preview-fixture", cwd=publisher)
    if remove_file:
        removed = "payload/game" if remove_file == "nested" else "example.txt"
        ignored = "payload" if remove_file == "nested" else "example.txt"
        git("rm", "-q", removed, cwd=publisher)
        (publisher / ".gitignore").write_text(".venv/\nromarr/_version.py\nromarr/_build_info.py\n" + ignored + "\n")
    else:
        (publisher / "example.txt").write_text("preview fixture\n")
    git("add", ".", cwd=publisher);git("commit", "-qm", "preview", cwd=publisher)
    head = git("rev-parse", "HEAD", cwd=publisher)
    git("push", "-q", "origin", "HEAD:refs/pull/201/head", cwd=publisher)
    info = {"number": 201, "head": head, "base": base, "draft": False, "checks_passed": True,
            "source_ref": "refs/pull/201/head", "build_guidance": "separate preview build"}
    monkeypatch.setattr(controls, "inspect_pr", lambda number: info)
    monkeypatch.setattr(controls.selfupdate, "detect", lambda: env)
    old_run = safety.subprocess.run
    monkeypatch.setattr(safety.subprocess, "run", lambda args, **kwargs:
        SimpleNamespace(returncode=0, stdout="", stderr="") if args[0] == "systemd-run" else old_run(args, **kwargs))
    store = Store(repo.parent / "settings.json");store.update_settings({"auto_update": True})
    original = store.path.read_bytes()
    service = SimpleNamespace(_lock=threading.RLock(), queue=[], store=store)
    return controls, service, info, original


def _execute_preview_worker(repo, env, *, ignored_pip_failure=False):
    """Execute generated worker with real Git; only service/pip/health are fixtures."""
    testbin = repo.parent / "preview-testbin";testbin.mkdir(exist_ok=True)
    ctl = testbin / "systemctl";ctl.write_text("#!/bin/sh\nexit 0\n");ctl.chmod(0o755)
    interpreter = Path(env.python).resolve()
    script = '#!/bin/sh\n'
    if ignored_pip_failure:
        import shlex
        script += 'if [ "$1" = "-m" ]; then printf owner-data > ' + shlex.quote(str(repo / "payload")) + '; exit 12; fi\n'
    else:
        script += 'if [ "$1" = "-m" ]; then exit 0; fi\n'
    script += 'if [ "$1" = "-c" ]; then case "$2" in *urllib.request*) exit 0;; esac; fi\n'
    script += 'exec ' + sys.executable + ' "$@"\n'
    interpreter.write_text(script)
    interpreter.chmod(0o755)
    gitdir = Path(git("rev-parse", "--absolute-git-dir", cwd=repo))
    return subprocess.run(["/bin/sh", str(gitdir / safety.LOCK_NAME / "run.sh")], cwd=repo,
                          env={**os.environ, "PATH": str(testbin)+":"+os.environ["PATH"]},
                          capture_output=True, text=True, timeout=30)


def test_pinned_pr_apply_backup_and_code_restore_use_real_git(checkout, monkeypatch):
    """Apply exact upstream pull ref, retain private config backup, restore known code."""
    import hashlib,json
    controls,service,info,original = _preview_fixture(checkout, monkeypatch)
    _pub,repo,_origin,env = checkout
    result=controls.apply_pr(service,{"confirm":True,"number":201,"head":info["head"],"base":info["base"],"acknowledgement":"APPLY PR #201"})
    assert result["ok"] and git("rev-parse","HEAD",cwd=repo)==info["base"]
    ident=result["backup_id"];backup=repo/".git/romarr-preview-backups"/(ident+".config.json")
    assert backup.read_bytes()==original and backup.stat().st_mode & 0o777 == 0o600
    assert not service.store.settings["auto_update"]
    worker=_execute_preview_worker(repo,env);assert worker.returncode==0,(repo/".git"/safety.LOG_NAME).read_text()
    assert git("rev-parse","HEAD",cwd=repo)==info["head"]
    assert controls.active_preview(env)["head"]==info["head"]
    env.commit=info["head"]
    service.store.update_settings({"update_channel": "beta"})
    assert service.store.settings["update_channel"] == "beta"
    current_settings = service.store.path.read_bytes()
    restored=controls.restore_preview(service,{"confirm":True,"backup_id":ident});assert restored["ok"]
    worker=_execute_preview_worker(repo,env);assert worker.returncode==0,(repo/".git"/safety.LOG_NAME).read_text()
    assert git("rev-parse","HEAD",cwd=repo)==info["base"]
    assert controls.active_preview(env) is None and backup.read_bytes()==original
    assert service.store.path.read_bytes() == current_settings


def test_pinned_pr_worker_rejects_moved_base_before_checkout_changes(checkout, monkeypatch):
    controls,service,info,_original = _preview_fixture(checkout, monkeypatch)
    publisher,repo,_origin,env = checkout
    result=controls.apply_pr(service,{"confirm":True,"number":201,"head":info["head"],"base":info["base"],"acknowledgement":"APPLY PR #201"});assert result["ok"]
    git("checkout","-q","main",cwd=publisher);publish(publisher,"base advanced\n")
    worker=_execute_preview_worker(repo,env)
    assert worker.returncode!=0 and git("rev-parse","HEAD",cwd=repo)==info["base"]
    assert (repo/".git"/safety.LOCK_NAME).exists()


def test_preview_rollback_never_overwrites_ignored_owner_file(checkout, monkeypatch):
    controls,service,info,_original = _preview_fixture(checkout, monkeypatch,remove_file=True)
    _publisher,repo,_origin,env=checkout
    result=controls.apply_pr(service,{"confirm":True,"number":201,"head":info["head"],"base":info["base"],"acknowledgement":"APPLY PR #201"});assert result["ok"]
    assert _execute_preview_worker(repo,env).returncode==0
    (repo/"example.txt").write_text("owner's ignored data\n");env.commit=info["head"]
    result=controls.restore_preview(service,{"confirm":True,"backup_id":result["backup_id"]});assert result["ok"]
    assert _execute_preview_worker(repo,env).returncode!=0
    assert (repo/"example.txt").read_text()=="owner's ignored data\n"
    assert git("rev-parse","HEAD",cwd=repo)==info["head"]


def test_prepare_runs_only_after_atomic_worker_claim(checkout, monkeypatch):
    """A busy caller cannot run its backup/provenance preparation callback."""
    _publisher, repo, _origin, env = checkout
    actual_run = safety.subprocess.run
    monkeypatch.setattr(safety.subprocess, "run", lambda args, **kwargs:
        SimpleNamespace(returncode=0, stdout="", stderr="") if args[0] == "systemd-run" else actual_run(args, **kwargs))
    observations = []
    def prepare(state):
        observations.append((repo / ".git" / safety.LOCK_NAME).is_dir())
    assert safety.launch(env, "origin/main", prepare=prepare)["ok"]
    assert safety.launch(env, "origin/main", prepare=prepare)["busy"]
    assert observations == [True]


def test_busy_preview_retains_existing_marker_backup_and_preferences(checkout, monkeypatch):
    """Repeated clicks during a detached update cannot erase its rollback provenance."""
    controls, service, info, _original = _preview_fixture(checkout, monkeypatch)
    _publisher, repo, _origin, _env = checkout
    body = {"confirm": True, "number": 201, "head": info["head"], "base": info["base"], "acknowledgement": "APPLY PR #201"}
    first = controls.apply_pr(service, body)
    assert first["ok"]
    marker = repo / ".git/romarr-preview.json"
    before = marker.read_bytes()
    backups = sorted((repo / ".git/romarr-preview-backups").iterdir())
    second = controls.apply_pr(service, body)
    assert second["busy"] and marker.read_bytes() == before
    assert sorted((repo / ".git/romarr-preview-backups").iterdir()) == backups
    assert service.store.settings["auto_update"] is False


def test_active_preview_cannot_be_stacked_or_lose_original_rollback(checkout, monkeypatch):
    """An already active preview must be restored before another preview is installed."""
    controls, service, info, _original = _preview_fixture(checkout, monkeypatch)
    _publisher, repo, _origin, env = checkout
    body = {"confirm": True, "number": 201, "head": info["head"], "base": info["base"], "acknowledgement": "APPLY PR #201"}
    assert controls.apply_pr(service, body)["ok"]
    assert _execute_preview_worker(repo, env).returncode == 0
    env.commit = info["head"]
    marker = repo / ".git/romarr-preview.json"
    before = marker.read_bytes()
    second = controls.apply_pr(service, body)
    assert not second["ok"] and "Restore the active preview" in second["error"]
    assert marker.read_bytes() == before and controls.active_preview(env)
    assert not (repo / ".git" / safety.LOCK_NAME).exists()


def test_ambiguous_preview_launch_retains_provenance_and_disables_auto_apply(checkout, monkeypatch):
    """A worker may be running despite a rejected/lost systemd acknowledgement."""
    controls, service, info, _original = _preview_fixture(checkout, monkeypatch)
    _publisher, repo, _origin, _env = checkout
    old_run = safety.subprocess.run
    monkeypatch.setattr(safety.subprocess, "run", lambda args, **kwargs:
        SimpleNamespace(returncode=1, stdout="", stderr="manager acknowledgement unavailable")
        if args[0] == "systemd-run" else old_run(args, **kwargs))
    result = controls.apply_pr(service, {"confirm": True, "number": 201, "head": info["head"],
        "base": info["base"], "acknowledgement": "APPLY PR #201"})
    assert not result["ok"] and result["recovery_required"]
    assert (repo / ".git" / safety.LOCK_NAME).exists()
    assert (repo / ".git/romarr-preview.json").exists()
    assert service.store.settings["auto_update"] is False
    assert (repo / ".git/romarr-preview-backups" / (result["backup_id"] + ".config.json")).exists()


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_preview_restore_preserves_ignored_ancestor_replacements(checkout, monkeypatch, replacement):
    """Rollback must not erase an ignored owner file/link blocking an old directory."""
    controls, service, info, _original = _preview_fixture(checkout, monkeypatch, remove_file="nested")
    _publisher, repo, _origin, env = checkout
    result = controls.apply_pr(service, {"confirm": True, "number": 201, "head": info["head"],
        "base": info["base"], "acknowledgement": "APPLY PR #201"})
    assert result["ok"] and _execute_preview_worker(repo, env).returncode == 0
    outside = repo.parent / "owner-outside"
    outside.mkdir(); (outside / "private.txt").write_text("owner bytes")
    blocked = repo / "payload"
    if replacement == "file":
        blocked.write_text("owner ignored ancestor")
    else:
        blocked.symlink_to(outside, target_is_directory=True)
    env.commit = info["head"]
    restore = controls.restore_preview(service, {"confirm": True, "backup_id": result["backup_id"]})
    assert restore["ok"]
    assert _execute_preview_worker(repo, env).returncode != 0
    assert git("rev-parse", "HEAD", cwd=repo) == info["head"]
    assert (outside / "private.txt").read_text() == "owner bytes"
    if replacement == "file":
        assert blocked.read_text() == "owner ignored ancestor"
    else:
        assert blocked.is_symlink() and blocked.resolve() == outside


def test_dependency_failure_rollback_preserves_new_ignored_ancestor(checkout, monkeypatch):
    """Even automatic code recovery after failed pip may not erase owner data."""
    controls, service, info, _original = _preview_fixture(checkout, monkeypatch, remove_file="nested")
    publisher, repo, _origin, env = checkout
    (publisher / "requirements.txt").write_text("fixture-requirement\n")
    git("add", "requirements.txt", cwd=publisher); git("commit", "-qm", "dependency fixture", cwd=publisher)
    info["head"] = git("rev-parse", "HEAD", cwd=publisher)
    git("push", "-q", "origin", "HEAD:refs/pull/201/head", cwd=publisher)
    result = controls.apply_pr(service, {"confirm": True, "number": 201, "head": info["head"],
        "base": info["base"], "acknowledgement": "APPLY PR #201"})
    assert result["ok"]
    worker = _execute_preview_worker(repo, env, ignored_pip_failure=True)
    assert worker.returncode != 0
    assert (repo / "payload").read_text() == "owner-data"
    assert git("rev-parse", "HEAD", cwd=repo) == info["head"]
    assert "ROLLBACK BLOCKED" in (repo / ".git" / safety.LOG_NAME).read_text()


@pytest.mark.parametrize("tag", ["v1.10.1", "v1.11.0-beta.2", "v1.11.0-rc.1"])
def test_real_release_worker_publishes_exact_version_and_revision(checkout, monkeypatch, tag):
    """Moving the source must not leave the ignored version files on the old release."""
    import json
    publisher,repo,origin,env=checkout
    (repo/"romarr/_version.py").write_text("__version__ = '1.9.0'\n")
    new=publish(publisher)
    git("tag",tag,new,cwd=publisher);git("push","-q","origin",tag,cwd=publisher)
    old_run=safety.subprocess.run
    monkeypatch.setattr(safety.subprocess,"run",lambda args,**kwargs:
        SimpleNamespace(returncode=0,stdout="",stderr="") if args[0]=="systemd-run" else old_run(args,**kwargs))
    result=safety.launch(env,"refs/tags/"+tag);assert result["ok"]
    worker=_execute_preview_worker(repo,env)
    assert worker.returncode==0,(repo/".git"/safety.LOG_NAME).read_text()
    version={};exec((repo/"romarr/_version.py").read_text(),version)
    assert version["__version__"]==tag[1:]
    info={};exec((repo/"romarr/_build_info.py").read_text(),info)
    assert info["romarr_build_kind"]=="release" and info["romarr_source_revision"]==new
    assert info["romarr_source_ref"]=="refs/tags/"+tag
    assert git("status","--porcelain",cwd=repo)==""


def test_preview_restore_reinstates_previous_tagged_build_identity(checkout, monkeypatch):
    controls,service,info,original=_preview_fixture(checkout,monkeypatch)
    publisher,repo,origin,env=checkout
    git("tag","v1.10.0",info["base"],cwd=repo)
    result=controls.apply_pr(service,{"confirm":True,"number":201,"head":info["head"],"base":info["base"],"acknowledgement":"APPLY PR #201"})
    assert result["ok"]
    worker=_execute_preview_worker(repo,env);assert worker.returncode==0
    generated={};exec((repo/"romarr/_build_info.py").read_text(),generated)
    assert generated["romarr_build_kind"]=="development" and generated["romarr_source_ref"]=="refs/pull/201/head"
    version={};exec((repo/"romarr/_version.py").read_text(),version)
    assert version["__version__"]=="0.0.0+unknown"
    env.commit=info["head"]
    assert controls.restore_preview(service,{"confirm":True,"backup_id":result["backup_id"]})["ok"]
    worker=_execute_preview_worker(repo,env);assert worker.returncode==0
    version={};exec((repo/"romarr/_version.py").read_text(),version)
    generated={};exec((repo/"romarr/_build_info.py").read_text(),generated)
    assert version["__version__"]=="1.10.0"
    assert generated["romarr_source_revision"]==info["base"]
    assert generated["romarr_source_ref"]=="refs/tags/v1.10.0"


def test_build_identity_publication_refuses_linked_metadata(checkout, monkeypatch):
    publisher,repo,origin,env=checkout
    owner=repo.parent/"owner-data";owner.write_text("preserve owner bytes")
    (repo/"romarr/_version.py").symlink_to(owner)
    publish(publisher)
    old_run=safety.subprocess.run
    monkeypatch.setattr(safety.subprocess,"run",lambda args,**kwargs:
        SimpleNamespace(returncode=0,stdout="",stderr="") if args[0]=="systemd-run" else old_run(args,**kwargs))
    assert safety.launch(env,"origin/main")["ok"]
    worker=_execute_preview_worker(repo,env)
    assert worker.returncode!=0
    assert owner.read_text()=="preserve owner bytes"
    assert (repo/"romarr/_version.py").is_symlink()
    assert (repo/".git"/safety.LOCK_NAME).exists()


def test_internal_gitfile_preview_retains_provenance_and_real_rollback(checkout, monkeypatch):
    """An ignored in-checkout Git directory must use the same marker as preparation."""
    controls, service, info, _original = _preview_fixture(checkout, monkeypatch)
    _publisher, repo, _origin, env = checkout
    gitdir = repo / ".romarr-git"
    (repo / ".git").rename(gitdir)
    (repo / ".git").write_text("gitdir: .romarr-git\n")
    with (gitdir / "info/exclude").open("a") as output:
        output.write("\n.romarr-git/\n")
    monkeypatch.setattr(controls, "__file__", str(repo / "romarr/release_controls.py"))
    body = {"confirm": True, "number": 201, "head": info["head"], "base": info["base"], "acknowledgement": "APPLY PR #201"}
    result = controls.apply_pr(service, body)
    assert result["ok"]
    assert (gitdir / "romarr-preview.json").is_file()
    worker = _execute_preview_worker(repo, env)
    assert worker.returncode == 0, (gitdir / safety.LOG_NAME).read_text()
    env.commit = info["head"]
    assert controls.active_preview(env)["backup_id"] == result["backup_id"]
    assert controls.preview_provenance()["romarr_source_revision"] == info["head"]
    restored = controls.restore_preview(service, {"confirm": True, "backup_id": result["backup_id"]})
    assert restored["ok"]
    worker = _execute_preview_worker(repo, env)
    assert worker.returncode == 0, (gitdir / safety.LOG_NAME).read_text()
    assert git("rev-parse", "HEAD", cwd=repo) == info["base"]
    assert controls.active_preview(env) is None


def test_external_gitfile_metadata_cannot_claim_active_preview(checkout):
    """Reading provenance must retain the updater's checkout-only metadata confinement."""
    import json
    from romarr import release_controls as controls
    _publisher, repo, _origin, env = checkout
    external = repo.parent / "external-git-metadata"
    (repo / ".git").rename(external)
    (repo / ".git").write_text("gitdir: " + str(external) + "\n")
    (external / "romarr-preview.json").write_text(json.dumps({
        "number": 201, "head": env.commit, "backup_id": "a" * 32, "previous_commit": env.commit}))
    assert controls.active_preview(env) is None


def test_tracked_ignore_rules_allow_source_build_identity_publication(tmp_path):
    """Actual project ignore rules, without a workstation's private excludes, must suffice."""
    project=Path(__file__).resolve().parents[1]
    repo=tmp_path/"clean-ignore-check";repo.mkdir()
    git("init","-q",str(repo))
    (repo/".gitignore").write_bytes((project/".gitignore").read_bytes())
    for name in ("romarr/_version.py","romarr/_build_info.py"):
        subprocess.run(["git","check-ignore","-q",name],cwd=repo,env=safety._safe_git_env(),check=True)
