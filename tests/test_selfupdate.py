"""Self-update: pulling the latest ROMarr and ROM Hub plugins from GitHub.

The invariants worth defending:
  * an update NEVER starts while the queue is busy (the mid-import restart
    the old design refused to risk);
  * a container install gets honest host-side guidance, never a fake success;
  * the detached script is fail-stop, ff-only, and records the pre-update
    commit for rollback;
  * plugin updates go through the Hub's own install path, per-plugin isolated.
"""

from romarr import selfupdate


# -- environment detection ----------------------------------------------------

def test_a_git_checkout_is_detected_as_self_updatable(tmp_path, monkeypatch):
    repo = tmp_path / "romarr"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(selfupdate.shutil, "which",
                        lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(selfupdate, "_git",
                        lambda git, cwd, *a: {"rev-parse": "abc1234"}.get(a[-1], "main")
                        if a else "")
    env = selfupdate.detect(root=repo)
    assert env.kind == "git" and env.can_apply
    assert env.repo == str(repo)


def test_a_container_is_detected_and_refuses_self_update(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate, "_container_marker", lambda: True)
    env = selfupdate.detect(root=tmp_path)
    assert env.kind == "docker"
    assert not env.can_apply


def test_an_unknown_shape_refuses_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate, "_container_marker", lambda: False)
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
    env = selfupdate.detect(root=tmp_path)
    assert env.kind == "none"
    assert not env.can_apply
    assert "cannot tell" in env.detail


def test_docker_guidance_names_the_host_commands():
    out = selfupdate.docker_guidance()
    assert out["ok"] is False
    joined = " ".join(out["guidance"])
    assert "docker pull" in joined
    assert "compose" in joined


# -- channel targeting --------------------------------------------------------

def test_stable_targets_the_release_tag():
    env = selfupdate.Environment(kind="git", branch="main")
    target, why = selfupdate.resolve_target(env, "stable", "1.2.0")
    assert target == "refs/tags/v1.2.0"


def test_stable_without_a_known_tag_refuses():
    env = selfupdate.Environment(kind="git", branch="main")
    target, why = selfupdate.resolve_target(env, "stable", "")
    assert target == "" and "UpdateCheck" in why


def test_nightly_targets_main():
    env = selfupdate.Environment(kind="git", branch="main")
    target, _ = selfupdate.resolve_target(env, "nightly", "")
    assert target == "origin/main"


# -- the busy guard -----------------------------------------------------------

class _Item:
    def __init__(self, state):
        self.state = state


def test_a_busy_queue_defers_the_update():
    assert selfupdate.queue_busy([_Item("imported"), _Item("grabbed")]) is True
    assert selfupdate.queue_busy([_Item("importing")]) is True


def test_an_idle_queue_allows_it():
    assert selfupdate.queue_busy([_Item("imported"), _Item("failed")]) is False
    assert selfupdate.queue_busy([]) is False


def test_an_unreadable_queue_defers_rather_than_assuming_idle():
    class Boom:
        def __iter__(self):
            raise RuntimeError("queue on fire")

    assert selfupdate.queue_busy(Boom()) is True


class _AgedItem(_Item):
    def __init__(self, state, at):
        super().__init__(state)
        self.at = at


def test_a_stalled_grab_does_not_starve_the_update_forever():
    """The live failure this locks down: nine mislabeled grabs from public
    trackers cycled grabbed/stalled/failed/regrabbed for days, every cycle
    minting a fresh `grabbed` row, and every daily UpdateCheck deferred
    behind a queue that would never empty. A `grabbed` row older than the
    stall timeout is dead weight, not work in flight."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=3)).isoformat(timespec="seconds")
    fresh = now.isoformat(timespec="seconds")

    # A grabbed row three days old, stall timeout 180 minutes: not busy.
    assert selfupdate.queue_busy([_AgedItem("grabbed", old)],
                                 stall_minutes=180) is False
    # A genuinely fresh grab still defers the update.
    assert selfupdate.queue_busy([_AgedItem("grabbed", fresh)],
                                 stall_minutes=180) is True
    # Mixed: one fresh row among dead ones is still busy.
    assert selfupdate.queue_busy([_AgedItem("grabbed", old),
                                  _AgedItem("grabbed", fresh)],
                                 stall_minutes=180) is True
    # An importing row is always busy, however old -- an import that has run
    # three days is a wedged import, and restarting mid-write is worse.
    assert selfupdate.queue_busy([_AgedItem("importing", old)],
                                 stall_minutes=180) is True


def test_a_row_with_no_readable_timestamp_still_counts_as_busy():
    """When in doubt, defer: an undatable grabbed row is treated as live."""
    assert selfupdate.queue_busy([_AgedItem("grabbed", "")],
                                 stall_minutes=180) is True
    assert selfupdate.queue_busy([_AgedItem("grabbed", "not a date")],
                                 stall_minutes=180) is True


def test_stall_timeout_zero_means_no_age_check():
    """An operator who disabled the stall sweep gets the old behavior: any
    grabbed row defers, whatever its age."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc)
           - timedelta(days=30)).isoformat(timespec="seconds")
    assert selfupdate.queue_busy([_AgedItem("grabbed", old)],
                                 stall_minutes=0) is True


# -- the detached script ------------------------------------------------------

def test_the_script_is_fail_stop_and_ff_only():
    env = selfupdate.Environment(kind="git", repo="/opt/romarr",
                                 python="/opt/romarr/.venv/bin/python",
                                 branch="main", commit="abc1234",
                                 service="romarr")
    script = selfupdate.build_script(env, "refs/tags/v1.2.0",
                                     "/opt/romarr/.romarr-selfupdate.log")
    assert script.startswith("#!/bin/sh")
    assert "set -e" in script                 # fail-stop
    assert "--ff-only" in script              # never create a merge/divergence
    assert "git fetch --tags origin" in script
    assert "refs/tags/v1.2.0" in script
    assert "systemctl restart romarr" in script
    assert "requirements.txt" in script       # deps move with the code
    assert "pre-update commit" in script      # the rollback handle
    # Deps install into the interpreter that RUNS romarr (the venv), and a
    # failed install rolls the checkout back and stops before restarting --
    # coming up on new code with missing imports is worse than staying put.
    assert "/opt/romarr/.venv/bin/python" in script and "-m pip install" in script
    assert "git reset --hard" in script
    assert "NOT restarting" in script


def test_detect_prefers_the_running_interpreter_not_system_python(monkeypatch):
    """A venv install's sys.executable is the venv python; shutil.which
    ('python3') would find the system one, where pip install hits PEP 668 and
    fails. Proved on a live box: the self-update log showed the venv bypassed
    and 'externally-managed-environment'."""
    monkeypatch.setattr(selfupdate, "_container_marker", lambda: False)
    monkeypatch.setattr(selfupdate.sys, "executable",
                        "/opt/romarr/.venv/bin/python")
    monkeypatch.setattr(selfupdate.shutil, "which",
                        lambda name: "/usr/bin/git" if name == "git"
                        else "/usr/bin/python3")
    env = selfupdate.detect(root=selfupdate.Path("/nonexistent"))
    # kind is 'none' (no .git under /nonexistent), but the python it picked
    # is the point: the running venv, not the system python3.
    assert env.python == "/opt/romarr/.venv/bin/python"


def test_launch_refuses_a_non_git_install():
    env = selfupdate.Environment(kind="docker", detail="container")
    out = selfupdate.launch(env, "refs/tags/v1.2.0")
    assert out["ok"] is False


def test_launch_writes_and_starts_the_script(tmp_path, monkeypatch):
    repo = tmp_path / "romarr"
    repo.mkdir()
    env = selfupdate.Environment(kind="git", repo=str(repo),
                                 python="/usr/bin/python3", branch="main",
                                 commit="abc1234", service="romarr")
    started = []
    monkeypatch.setattr(selfupdate.subprocess, "Popen",
                        lambda cmd, **kw: started.append(cmd) or object())
    out = selfupdate.launch(env, "refs/tags/v1.2.0")
    assert out["ok"] is True
    assert started and started[0][0] == "/bin/sh"
    script = repo / selfupdate.SCRIPT_NAME
    assert script.exists() and "ff-only" in script.read_text()
    assert (repo / selfupdate.LOG_NAME).exists()      # truncated for the UI


def test_read_log_returns_the_tail(tmp_path):
    repo = tmp_path / "romarr"
    repo.mkdir()
    (repo / selfupdate.LOG_NAME).write_text(
        "\n".join(f"line {i}" for i in range(60)))
    env = selfupdate.Environment(kind="git", repo=str(repo))
    tail = selfupdate.read_log(env, tail_lines=5)
    assert tail.splitlines()[-1] == "line 59"
    assert len(tail.splitlines()) == 5


# -- plugin updates -----------------------------------------------------------

def test_plugin_sources_reads_the_registry_state(tmp_path, monkeypatch):
    from romarr import hub
    home = tmp_path / "hub"
    home.mkdir()
    (home / "state.json").write_text(
        '{"archive-org": {"source": "https://github.com/x/archive-org", '
        '"ref": null, "commit": "deadbeef"}, '
        '"vimm": {"source": "vimm", "ref": "main", "commit": "cafef00d"}}')
    monkeypatch.setattr(hub, "HOME", home)
    src = hub.plugin_sources()
    assert src["archive-org"]["commit"] == "deadbeef"
    assert src["archive-org"]["ref"] is None
    assert src["vimm"]["ref"] == "main"


def test_plugin_sources_survives_a_missing_state(tmp_path, monkeypatch):
    from romarr import hub
    monkeypatch.setattr(hub, "HOME", tmp_path / "nothing")
    assert hub.plugin_sources() == {}


def test_update_plugins_isolates_a_single_failure(tmp_path, monkeypatch):
    from romarr import hub
    home = tmp_path / "hub"
    home.mkdir()
    (home / "state.json").write_text(
        '{"good": {"source": "https://x/good", "commit": "1"}, '
        '"bad": {"source": "https://x/bad", "commit": "2"}}')
    monkeypatch.setattr(hub, "HOME", home)

    def fake_cli(*args):
        slug = args[2] if len(args) > 2 else ""
        if "bad" in str(args):
            return {"ok": False, "out": "", "err": "clone failed", "code": 1}
        return {"ok": True, "out": "installed", "err": "", "code": 0}

    monkeypatch.setattr(hub, "_run_cli", fake_cli)
    out = hub.update_plugins()
    assert out["ok"] is False
    assert out["updated"] == ["good"]
    assert "bad" in out["failed"]


def test_update_plugins_with_none_installed(tmp_path, monkeypatch):
    from romarr import hub
    monkeypatch.setattr(hub, "HOME", tmp_path / "nothing")
    out = hub.update_plugins()
    assert out["ok"] is True and "no installed plugins" in out["message"]


# -- service wiring -----------------------------------------------------------

def _svc(tmp_path):
    from romarr.app import ROMarr
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})


def test_apply_update_defers_on_a_busy_queue(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(selfupdate, "detect", lambda root=None:
                        selfupdate.Environment(kind="git", repo="/opt/romarr",
                                               python="python3", branch="main",
                                               commit="abc", service="romarr"))
    from romarr.store import QueueItem
    svc.queue = [QueueItem("g", "snes", "r", 0, "grabbed", "")]
    out = svc.apply_update()
    assert out["ok"] is False and out.get("deferred") is True


def test_apply_update_on_docker_returns_guidance(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(selfupdate, "detect", lambda root=None:
                        selfupdate.Environment(kind="docker"))
    out = svc.apply_update()
    assert out["ok"] is False and "container" in out["error"]


def test_apply_update_launches_when_idle(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.store.settings["_latest_version"] = "9.9.9"
    svc.store.settings["update_channel"] = "stable"
    env = selfupdate.Environment(kind="git", repo=str(tmp_path),
                                 python="python3", branch="main",
                                 commit="abc", service="romarr")
    monkeypatch.setattr(selfupdate, "detect", lambda root=None: env)
    monkeypatch.setattr(selfupdate, "launch",
                        lambda e, target: {"ok": True, "target": target,
                                           "message": "started"})
    svc.queue = []
    out = svc.apply_update()
    assert out["ok"] is True
    assert out["target"] == "refs/tags/v9.9.9"


def test_update_status_reports_the_shape_and_settings(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(selfupdate, "detect", lambda root=None:
                        selfupdate.Environment(kind="git", repo=str(tmp_path),
                                               branch="main", commit="abc1234"))
    st = svc.update_status()
    assert st["install"]["kind"] == "git"
    assert st["settings"]["update_channel"] == "stable"
    assert "running_version" in st and "plugins" in st


def test_check_update_applies_when_auto_update_is_on(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.store.settings["auto_update"] = True
    svc.store.settings["_latest_version"] = ""
    monkeypatch.setattr(svc, "notify", lambda m: [])
    applied = []
    monkeypatch.setattr(svc, "apply_update",
                        lambda: applied.append(1) or
                        {"ok": True, "message": "updating to v99.0.0"})

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v99.0.0", "html_url": "https://x"}

    import requests as _requests
    monkeypatch.setattr(_requests, "get", lambda *a, **kw: FakeResponse())
    out = svc.check_update()
    assert applied and out["ok"] is True
    assert "updating" in out["message"]


def test_check_update_does_not_apply_when_auto_update_is_off(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(svc, "notify", lambda m: [])
    applied = []
    monkeypatch.setattr(svc, "apply_update", lambda: applied.append(1))

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v99.0.0", "html_url": "https://x"}

    import requests as _requests
    monkeypatch.setattr(_requests, "get", lambda *a, **kw: FakeResponse())
    out = svc.check_update()
    assert not applied
    assert out["update_available"] is True


def test_settings_default_to_safe(tmp_path):
    from romarr.store import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["auto_update"] is False
    assert DEFAULT_SETTINGS["auto_update_plugins"] is False
    assert DEFAULT_SETTINGS["update_channel"] == "stable"
    assert DEFAULT_SETTINGS["update_check"] is True
