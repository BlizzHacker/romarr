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
    (publisher / ".gitignore").write_text(".venv/\n")
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
    assert "git reset --keep" in script
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
