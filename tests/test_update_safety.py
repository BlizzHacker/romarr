"""Disposable Git/source update safety tests; never start a real ROMarr service."""
from __future__ import annotations

import os
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
    return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                   stderr=subprocess.STDOUT).strip()


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
    # No actual systemd service will be queried by tests.
    old = safety._cmd
    monkeypatch.setattr(safety, "_cmd", lambda *a, **kw:
        "loaded" if a[0] == "systemctl" and "show" in a else
        "active" if a[0] == "systemctl" and "is-active" in a else old(*a, **kw))
    monkeypatch.setenv("ROMARR_UPDATE_HEALTH_URL", "http://127.0.0.1:6868/api/health")
    env = SimpleNamespace(kind="git", repo=str(consumer), python=sys.executable,
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


def test_venv_python_symlink_is_not_resolved_to_system_interpreter(checkout, tmp_path):
    """The updater must use the running venv interpreter, even when symlinked."""
    _pub, _repo, _origin, env = checkout
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(sys.executable)
    env.python = str(venv_python)
    state = safety.preflight(env, "origin/main")
    assert state["python"] == str(venv_python)


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


def test_script_has_no_branch_fallback_or_destructive_reset(checkout):
    _pub, _repo, _origin, env = checkout
    state = safety.preflight(env, "origin/main")
    script = safety.build_script(state, "123", state["gitdir"] / "a.log", state["gitdir"] / "lock")
    assert "checkout main" not in script and "git reset --hard" not in script
    assert "git reset --keep" in script
    assert "git merge --ff-only" in script
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


def _run_worker(checkout, monkeypatch, *, pip="success", restart="success", dirty_on_pip=False):
    publisher, repo, _origin, env = checkout
    req = "placeholder\n" if pip != "none" else None
    new_head = publish(publisher, requirements=req)
    state = safety.preflight(env, "origin/main")
    lock = state["gitdir"] / safety.LOCK_NAME
    lock.mkdir()
    (lock / "run_id").write_text("test-run")
    testbin = repo.parent / "testbin"
    testbin.mkdir()
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
