"""qBittorrent's login answer changed shape.

From PR #7 by @m3. qBittorrent 5.x answers a successful `auth/login` with
204 and an empty body; earlier versions answered 200 with the literal string
"Ok.". ROMarr checked only for "Ok.", so against 5.x every login was treated
as rejected -- "qbittorrent login rejected (status 204)" in the log, and no
completed download was ever imported.

The interesting part is that both shapes have to keep working, and a genuine
rejection must not be swept in with them: qBittorrent answers a *failed* login
with 200 and "Fails.", so status alone is not enough either.
"""

from __future__ import annotations

import pytest

from romarr.clients import QBittorrent, QbitConfig


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return {}


class _Session:
    """Stands in for requests.Session, answering login however we like."""

    def __init__(self, response):
        self._response = response
        self.posts: list[str] = []

    def post(self, url, **kwargs):
        self.posts.append(url)
        return self._response

    def get(self, url, **kwargs):
        return _Response(200, "[]")


def client(response):
    session = _Session(response)
    qbit = QBittorrent(QbitConfig(base_url="http://qbit:8080",
                                  username="admin", password="pw"),
                       session=session)
    return qbit, session


def test_qbittorrent_5x_answers_204_and_that_is_a_success():
    """The reported bug: 5.x logs in fine and ROMarr called it a rejection."""
    qbit, _ = client(_Response(204, ""))
    assert qbit.login() is True


def test_the_old_200_ok_answer_still_works():
    qbit, _ = client(_Response(200, "Ok."))
    assert qbit.login() is True


def test_200_ok_with_surrounding_whitespace_still_works():
    qbit, _ = client(_Response(200, "  Ok.\n"))
    assert qbit.login() is True


def test_a_real_rejection_is_still_a_rejection():
    """qBittorrent answers a wrong password with 200 and "Fails.", so a
    status-only check would let a failed login through as success."""
    qbit, _ = client(_Response(200, "Fails."))
    assert qbit.login() is False


def test_banned_ip_is_a_rejection():
    qbit, _ = client(_Response(403, "Your IP has been banned"))
    assert qbit.login() is False


@pytest.mark.parametrize("status", [400, 401, 404, 500, 502])
def test_error_statuses_are_rejections(status):
    qbit, _ = client(_Response(status, ""))
    assert qbit.login() is False


def test_a_rejected_login_is_not_recorded_as_authenticated():
    """Otherwise the next call skips login and fails in a way that looks like
    a different problem entirely."""
    qbit, _ = client(_Response(200, "Fails."))
    qbit.login()
    assert getattr(qbit, "_authed", False) is False


class _ExpiredSession:
    """A live SID expires, then a fresh login makes the same call work."""

    def __init__(self, login_response=None):
        self.login_response = login_response or _Response(200, "Ok.")
        self.gets = 0
        self.posts = 0

    def get(self, url, **kwargs):
        self.gets += 1
        return _Response(403, "Forbidden") if self.gets == 1 else _Response(200, "v5.1.4")

    def post(self, url, **kwargs):
        self.posts += 1
        return self.login_response


def test_an_expired_session_is_reauthenticated_without_restarting_romarr():
    session = _ExpiredSession()
    qbit = QBittorrent(
        QbitConfig(base_url="http://qbit:8090", username="admin", password="pw"),
        session=session,
    )
    qbit._authed = True  # the cached SID was valid when ROMarr started

    assert qbit.reachable() is True
    assert session.posts == 1
    assert session.gets == 2


def test_a_rejected_session_refresh_stays_unhealthy():
    session = _ExpiredSession(_Response(200, "Fails."))
    qbit = QBittorrent(
        QbitConfig(base_url="http://qbit:8090", username="admin", password="wrong"),
        session=session,
    )
    qbit._authed = True

    assert qbit.reachable() is False
    assert session.posts == 1
    assert session.gets == 1


class _ExpiredWriteSession:
    def __init__(self):
        self.paths = []
        self.add_attempts = 0

    def post(self, url, **kwargs):
        self.paths.append(url)
        if url.endswith("/auth/login"):
            return _Response(200, "Ok.")
        self.add_attempts += 1
        if self.add_attempts == 1:
            return _Response(403, "Forbidden")
        return _Response(200, "Ok.")


def test_an_add_is_retried_after_refreshing_an_expired_session():
    session = _ExpiredWriteSession()
    qbit = QBittorrent(
        QbitConfig(base_url="http://qbit:8090", username="admin", password="pw"),
        session=session,
    )
    qbit._authed = True

    assert qbit.add("magnet:?xt=urn:btih:example") is True
    assert session.add_attempts == 2
    assert [path.rsplit('/api/v2/', 1)[-1] for path in session.paths] == [
        "torrents/add", "auth/login", "torrents/add"
    ]
