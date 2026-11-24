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


def test_api_key_is_sent_as_bearer_and_skips_cookie_login():
    session, requests_sent = _qbit_session()
    qbit = QBittorrent(QbitConfig(base_url="http://qbit:8080", username="admin",
                                  password="pw", api_key="qbt_abc"),
                       session=session)
    assert qbit.reachable()
    assert requests_sent == [("GET", "app/version", "Bearer qbt_abc")]
    assert session.auth is None  # The shared session keeps its own authentication.
    assert qbit.login() is True
    assert not qbit._uses_login


def test_api_key_with_a_line_break_is_refused_and_never_sent():
    import requests
    session = requests.Session()
    bad = QBittorrent(QbitConfig(base_url="http://qbit:8080", api_key="qbt_a\nX-Evil: 1"),
                      session=session)
    assert session.auth is None
    assert bad.login() is False
    ok = QBittorrent(QbitConfig(base_url="http://qbit:8080", api_key="qbt_abc\n"),
                     session=requests.Session())
    assert ok.login() is True


# --- qBittorrent 5.2+ WebUI API keys, end to end through requests ------------
#
# qBittorrent 5.2 reads `Authorization: Bearer <key>` (release-5.2.0
# src/webui/webapplication.cpp, apiKeySessionInitialize), refuses auth/*
# outright when a bearer header is present, and answers a wrong key -- or any
# bearer header on a pre-5.2 build -- with 403 on private endpoints. These run
# real requests Sessions against a transport adapter, so what is asserted is
# the header that actually leaves the process.

KEY = "qbt_" + "k" * 28


def _qbit_session(status: int = 200, body: bytes = b"v5.2.0"):
    """A real requests.Session whose transport answers like qBittorrent.

    Returns the session and the list of (method, path, Authorization) it saw.
    """
    import requests
    seen: list[tuple[str, str, str | None]] = []

    class Adapter(requests.adapters.BaseAdapter):
        """Records each request's Authorization and answers `status`."""

        def send(self, request, **kwargs):
            seen.append((request.method, request.path_url.rsplit("/api/v2/", 1)[-1],
                         request.headers.get("Authorization")))
            response = requests.Response()
            response.status_code, response._content = status, body
            response.request, response.url = request, request.url
            return response

        def close(self):
            pass

    session = requests.Session()
    session.mount("http://", Adapter())
    return session, seen


def _keyed(session, **cfg):
    """A qBittorrent client configured with an API key over `session`."""
    cfg.setdefault("api_key", KEY)
    return QBittorrent(QbitConfig(base_url="http://qbit:8080", **cfg), session=session)


def test_an_api_key_is_sent_as_a_bearer_header_on_every_api_call():
    session, seen = _qbit_session()
    qbit = _keyed(session, username="admin", password="pw")
    assert qbit.reachable() is True
    qbit._post("torrents/add", data={})
    assert seen == [("GET", "app/version", f"Bearer {KEY}"),
                    ("POST", "torrents/add", f"Bearer {KEY}")]


def test_an_api_key_never_calls_auth_login():
    """qBittorrent 5.2 answers auth/* with 403 whenever a bearer header is
    present, so a login attempt alongside the key could only ever fail."""
    session, seen = _qbit_session()
    qbit = _keyed(session, username="admin", password="pw")
    assert qbit.login() is True
    qbit.reachable()
    assert not any(path == "auth/login" for _, path, _ in seen)


def test_the_key_survives_a_netrc_entry_for_the_qbittorrent_host(tmp_path, monkeypatch):
    """requests applies ~/.netrc as Basic auth to any request without auth of
    its own -- a plain session header would be silently replaced by it."""
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine qbit login netrcuser password netrcpass\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    session, seen = _qbit_session()
    # The fixture is real: without the key, requests does pick the netrc up.
    session.get("http://qbit:8080/api/v2/app/version")
    assert seen[-1][2].startswith("Basic ")
    assert _keyed(session).reachable() is True
    assert seen[-1][2] == f"Bearer {KEY}"
    # Auth belongs to this client, even when it uses a caller-owned session.
    session.get("http://qbit:8080/api/v2/app/version")
    assert seen[-1][2].startswith("Basic ")


def test_a_pasted_key_with_surrounding_whitespace_still_works():
    session, seen = _qbit_session()
    _keyed(session, api_key=f"  {KEY}\n").reachable()
    assert seen[-1][2] == f"Bearer {KEY}"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_a_blank_key_is_treated_as_no_key(key):
    qbit = QBittorrent(QbitConfig(base_url="http://qbit:8080", username="admin",
                                  password="pw", api_key=key))
    assert qbit._uses_login


def test_a_rejected_key_says_so_on_the_test_line():
    session, seen = _qbit_session(status=403, body=b"Forbidden")
    qbit = _keyed(session, username="admin", password="pw")
    assert qbit.reachable() is False
    assert "rejected the API key" in qbit.detail
    assert "5.2" in qbit.detail
    assert KEY not in qbit.detail
    # A 403 with a key is final: no silent fall back to a cookie login.
    assert [path for _, path, _ in seen] == ["app/version"]


def test_a_malformed_key_says_so_on_the_test_line():
    qbit = _keyed(_qbit_session()[0], api_key="qbt_a\nX-Evil: 1")
    assert qbit.reachable() is False
    assert "line break" in qbit.detail


def test_a_rejected_password_is_named_too():
    qbit, _ = client(_Response(200, "Fails."))
    assert qbit.reachable() is False
    assert qbit.detail == "qBittorrent rejected the username or password"


def test_a_successful_test_reports_the_version_and_the_auth_mode():
    qbit = _keyed(_qbit_session(body=b"v5.2.0")[0])
    assert qbit.reachable() is True
    assert qbit.detail == "qBittorrent v5.2.0, API key"


def test_without_a_key_username_and_password_login_is_unchanged():
    """Pre-5.2 qBittorrent has no API keys; a client left without one keeps
    the cookie login and sends no Authorization header at all."""
    session, seen = _qbit_session(status=204, body=b"")
    qbit = QBittorrent(QbitConfig(base_url="http://qbit:8080",
                                  username="admin", password="pw"), session=session)
    assert qbit.login() is True
    assert session.auth is None
    assert seen == [("POST", "auth/login", None)]


def test_neither_password_nor_key_appears_in_any_repr():
    import requests
    cfg = QbitConfig(base_url="http://qbit:8080", username="admin",
                     password="hunter2", api_key=KEY)
    assert "hunter2" not in repr(cfg)
    assert KEY not in repr(cfg)
    session = requests.Session()
    QBittorrent(cfg, session=session)
    assert session.auth is None


def test_the_api_key_is_a_secret_field_redacted_like_the_password():
    from romarr.downloaders import CLIENT_TYPES, SECRET_PLACEHOLDER, redact
    field = next(f for f in CLIENT_TYPES["qbittorrent"]["fields"] if f["name"] == "api_key")
    assert field["type"] == "secret"
    assert redact({"type": "qbittorrent", "api_key": KEY})["api_key"] == SECRET_PLACEHOLDER


def test_a_backup_without_secrets_drops_the_key():
    from romarr.ops import make_backup
    backup = make_backup({"download_clients": [{"type": "qbittorrent", "api_key": KEY}]})
    assert KEY not in str(backup)


def test_a_stored_client_with_a_key_builds_a_keyed_client():
    from romarr.downloaders import build_client
    qbit = build_client({"type": "qbittorrent", "host": "qbit", "port": 8080,
                         "api_key": KEY})
    assert qbit._has_key and not qbit._uses_login


def test_the_environment_key_seeds_the_client_and_never_reaches_the_browser(tmp_path):
    import json
    from romarr.app import ROMarr
    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "r.json"),
                          "QBITTORRENT_URL": "http://qbit:8080",
                          "QBITTORRENT_API_KEY": KEY})
    stored = [c for c in service.store.list_items("download_clients")
              if c.get("type") == "qbittorrent"]
    assert stored and stored[0]["api_key"] == KEY
    assert service.qbit._has_key
    assert KEY not in json.dumps(service.safe_settings())
