"""RomM 5.4 refuses `scan_library` through the task runner.

`POST /api/tasks/run/scan_library` answers 400 "Task 'scan_library' cannot be
run" on 5.4.0, so every post-import rescan failed and imports only appeared
after RomM's nightly scan. 5.4 added `POST /api/tasks/scan` instead. ROMarr
tries that first, narrowed to the imported platform, and falls back to the
task runner on servers that do not have it.
"""

from __future__ import annotations

import json

import requests

from romarr.clients import Romm, RommConfig


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class Session:
    def __init__(self, answers):
        self.answers = answers
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        self.posts.append(("/" + path, json))
        return self.answers.get("/" + path, Resp(404, text="Not Found"))


def client(answers):
    session = Session(answers)
    romm = Romm(RommConfig(base_url="http://romm", api_token="tok"), session=session)
    return romm, session


def test_romm_54_uses_the_scan_endpoint_for_one_platform():
    romm, session = client({"/api/tasks/scan": Resp(200, {"status": "queued"})})
    assert romm.rescan("snes") is True
    assert session.posts == [
        ("/api/tasks/scan", {"type": "quick", "platform_fs_slugs": ["snes"]})]


def test_no_platform_asks_for_a_quick_scan_of_everything():
    romm, session = client({"/api/tasks/scan": Resp(200, {"status": "queued"})})
    assert romm.rescan() is True
    assert session.posts == [("/api/tasks/scan", {"type": "quick"})]


def test_older_romm_falls_back_to_the_task_runner_and_remembers():
    romm, session = client({"/api/tasks/run/scan": Resp(200, {})})
    assert romm.rescan("nes") is True
    assert [p for p, _ in session.posts] == ["/api/tasks/scan", "/api/tasks/run/scan"]
    session.posts.clear()
    assert romm.rescan("nes") is True
    assert [p for p, _ in session.posts] == ["/api/tasks/run/scan"]


def test_romm_51_task_rename_still_learned_on_the_fallback_path():
    romm, session = client({
        "/api/tasks/scan": Resp(405, text="Method Not Allowed"),
        "/api/tasks/run/scan": Resp(400, text="Task 'scan' not found; available: scan_library"),
        "/api/tasks/run/scan_library": Resp(200, {}),
    })
    assert romm.rescan("n64") is True
    assert session.posts[-1][0] == "/api/tasks/run/scan_library"


def test_permission_refusal_on_the_new_endpoint_is_reported_not_retried():
    romm, session = client({"/api/tasks/scan": Resp(403, text="Forbidden")})
    assert romm.rescan("snes") is False
    assert [p for p, _ in session.posts] == ["/api/tasks/scan"]


class Timers:
    """Stands in for threading.Timer, so a deferred rescan runs when told."""

    def __init__(self):
        self.pending = []

    def __call__(self, seconds, fn):
        timers = self

        class Timer:
            daemon = False

            def start(self):
                timers.pending.append(fn)

        return Timer()

    def fire(self):
        fn = self.pending.pop(0)
        fn()


BUSY = Resp(409, text='{"detail":"Quick Scan is already running"}')


def busy_client(*answers):
    """A client whose /api/tasks/scan answers in turn, and a hand-run timer."""
    romm, session = client({})
    queue = list(answers)
    session.answers = {}
    original = session.post

    def post(url, json=None, **kw):
        session.answers["/api/tasks/scan"] = queue.pop(0)
        return original(url, json=json, **kw)

    session.post = post
    timers = Timers()
    romm._timer = timers
    return romm, session, timers


def test_a_scan_already_running_is_not_a_failure():
    romm, session, timers = busy_client(BUSY)
    assert romm.rescan("snes") is True
    assert [p for p, _ in session.posts] == ["/api/tasks/scan"]
    assert len(timers.pending) == 1


def test_a_refused_platform_is_retried_once_the_running_scan_is_done():
    romm, session, timers = busy_client(BUSY, Resp(202, {}))
    romm.rescan("gba")
    timers.fire()
    assert session.posts[-1] == (
        "/api/tasks/scan", {"type": "quick", "platform_fs_slugs": ["gba"]})
    assert timers.pending == []


def test_a_batch_across_platforms_scans_every_platform_it_touched():
    # SNES starts a scan; GBA and N64 land while it runs and are refused.
    romm, session, timers = busy_client(Resp(202, {}), BUSY, BUSY, Resp(202, {}))
    romm.rescan("snes")
    romm.rescan("gba")
    romm.rescan("n64")
    assert len(timers.pending) == 1  # one retry, not one per refusal
    timers.fire()
    assert session.posts[-1] == (
        "/api/tasks/scan", {"type": "quick", "platform_fs_slugs": ["gba", "n64"]})


def test_the_next_import_carries_the_refused_platforms_with_it():
    romm, session, timers = busy_client(BUSY, Resp(202, {}))
    romm.rescan("gba")
    romm.rescan("snes")
    assert session.posts[-1] == (
        "/api/tasks/scan", {"type": "quick", "platform_fs_slugs": ["gba", "snes"]})
    timers.fire()  # nothing left to scan, so no request
    assert len(session.posts) == 2


def test_a_refused_whole_library_scan_stays_a_whole_library_scan():
    romm, session, timers = busy_client(BUSY, Resp(202, {}))
    romm.rescan()
    romm.rescan("snes")
    assert session.posts[-1] == ("/api/tasks/scan", {"type": "quick"})


def test_retries_give_up_and_leave_it_to_the_scheduled_scan():
    romm, session, timers = busy_client(*[BUSY] * 4)
    romm.DEFERRED_SCAN_ATTEMPTS = 2
    romm.rescan("gba")
    timers.fire()
    timers.fire()
    assert timers.pending == []
    assert romm._deferred_scan == set()
    assert len(session.posts) == 3


def test_a_network_error_does_not_drop_platforms_deferred_earlier():
    romm, session, timers = busy_client(BUSY, Resp(202, {}))
    romm.rescan("gba")
    original = session.post

    def boom(url, json=None, **kw):
        raise requests.ConnectionError("down")

    session.post = boom
    assert romm.rescan("snes") is False
    assert romm._deferred_scan == {"gba"}
    session.post = original
    timers.fire()
    assert session.posts[-1] == (
        "/api/tasks/scan", {"type": "quick", "platform_fs_slugs": ["gba"]})


def test_a_refusal_other_than_409_does_not_drop_platforms_deferred_earlier():
    romm, session, timers = busy_client(BUSY, Resp(500, text="boom"))
    romm.rescan("gba")
    assert romm.rescan("snes") is False
    assert romm._deferred_scan == {"gba"}


def test_a_failed_retry_keeps_the_platform_it_was_retrying():
    romm, session, timers = busy_client(BUSY, Resp(500, text="boom"))
    romm.rescan("gba")
    timers.fire()
    assert romm._deferred_scan == {"gba"}


def test_an_unexpected_error_in_the_retry_thread_is_logged_not_raised():
    romm, session, timers = busy_client(BUSY)
    romm.rescan("gba")

    def broken(*a, **kw):
        raise RuntimeError("token store unreadable")

    romm._headers = broken
    timers.fire()
    assert romm._deferred_scan == {"gba"}
