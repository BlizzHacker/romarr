"""RomM 5.4 refuses `scan_library` through the task runner.

`POST /api/tasks/run/scan_library` answers 400 "Task 'scan_library' cannot be
run" on 5.4.0, so every post-import rescan failed and imports only appeared
after RomM's nightly scan. 5.4 added `POST /api/tasks/scan` instead. ROMarr
tries that first, narrowed to the imported platform, and falls back to the
task runner on servers that do not have it.
"""

from __future__ import annotations

import json

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


def test_a_scan_already_running_is_not_a_failure():
    romm, session = client({"/api/tasks/scan": Resp(409, text='{"detail":"Quick Scan is already running"}')})
    assert romm.rescan("snes") is True
    assert [p for p, _ in session.posts] == ["/api/tasks/scan"]
