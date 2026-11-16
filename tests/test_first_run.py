"""A fresh install, from the installer's side of the screen.

Issue #8: ROMarr was installed on Unraid, the web UI opened, and then every
page and every save failed with 401 and "Send your key as the X-Api-Key
header". The gate was working exactly as designed. What did not exist was any
way for a browser to get through it -- no sign-in screen, no first-run setup,
and a log line that told the operator to look under Settings, a page that was
itself behind the gate. The install looked healthy and could not be used.

`test_auth_http.py` already proves the gate holds. These prove somebody can
get *in*, which is a different property and the one that was missing. They run
against a real socket and follow the whole journey -- first visit, claim,
subsequent calls, restart -- because every individual piece of this was
already correct in isolation while the install as a whole was unusable.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler
from romarr.auth import SESSION_COOKIE


def serve(service):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def call(url, *, method="GET", body=None, cookie=None, key=None, redirect=True):
    request = urllib.request.Request(url, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()
    if cookie:
        request.add_header("Cookie", cookie)
    if key:
        request.add_header("X-Api-Key", key)

    opener = urllib.request.build_opener()
    if not redirect:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), exc.headers


def session_of(headers) -> str:
    """The session cookie from a Set-Cookie header, ready to send back."""
    raw = headers.get("Set-Cookie", "")
    assert SESSION_COOKIE in raw, f"no session cookie in {raw!r}"
    return raw.split(";")[0]


@pytest.fixture
def fresh(tmp_path):
    """A brand-new install: no password, no operator-supplied key."""
    store = tmp_path / "s.json"

    def boot(**env):
        service = ROMarr({"ROMARR_DATA": str(store), **env})
        url, httpd = serve(service)
        return service, url, httpd

    made: list = []

    def factory(**env):
        service, url, httpd = boot(**env)
        made.append(httpd)
        return service, url

    yield factory
    for httpd in made:
        httpd.shutdown()
        httpd.server_close()


# --- the reported symptom --------------------------------------------------

def test_a_fresh_install_offers_a_way_in_instead_of_a_dead_page(fresh):
    """The exact shape of issue #8, inverted into a requirement."""
    _, url = fresh()
    status, page, _ = call(url + "/")
    assert status == 200
    # Not the app shell. Serving that to somebody who cannot use it is what
    # made the failure so hard to diagnose.
    assert "Set your password" in page
    assert "<title>Set your password" in page


def test_the_first_visitor_can_claim_and_then_everything_works(fresh):
    _, url = fresh()
    status, _, headers = call(url + "/api/v1/setup", method="POST",
                              body={"password": "correct-horse"})
    assert status == 200
    cookie = session_of(headers)

    # These four are the calls the reporter watched fail. Each one is a page
    # of the UI, so a 401 here is a page that renders and cannot load.
    for path in ("/api/v1/config", "/api/v1/system/status",
                 "/api/v1/library", "/api/v1/game"):
        status, _, _ = call(url + path, cookie=cookie)
        assert status == 200, f"{path} still refuses a signed-in browser"

    # And the shell is served now, rather than the login screen.
    status, page, _ = call(url + "/", cookie=cookie)
    assert status == 200 and "<title>ROMarr" in page


def test_the_password_survives_a_restart(tmp_path):
    """A container restart must not strand the operator outside."""
    store = str(tmp_path / "s.json")
    first = ROMarr({"ROMARR_DATA": store})
    url, httpd = serve(first)
    try:
        call(url + "/api/v1/setup", method="POST",
             body={"password": "correct-horse"})
    finally:
        httpd.shutdown()
        httpd.server_close()

    second = ROMarr({"ROMARR_DATA": store})
    url, httpd = serve(second)
    try:
        assert second.claimed is True
        status, page, _ = call(url + "/")
        assert "Sign in" in page and "Set your password" not in page
        status, _, headers = call(url + "/api/v1/login", method="POST",
                                  body={"password": "correct-horse"})
        assert status == 200
        cookie = session_of(headers)
        status, _, _ = call(url + "/api/v1/config", cookie=cookie)
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- the claim is a one-shot -----------------------------------------------

def test_setup_closes_behind_the_first_visitor(fresh):
    """Otherwise it is a standing unauthenticated password reset."""
    _, url = fresh()
    assert call(url + "/api/v1/setup", method="POST",
                body={"password": "correct-horse"})[0] == 200
    status, payload, _ = call(url + "/api/v1/setup", method="POST",
                              body={"password": "attacker-chosen"})
    assert status == 409
    assert "already" in json.loads(payload)["detail"].lower()
    # And the original password still works, i.e. the second call changed
    # nothing rather than merely being reported as refused.
    assert call(url + "/api/v1/login", method="POST",
                body={"password": "correct-horse"})[0] == 200


def test_a_short_password_is_refused_with_a_reason(fresh):
    _, url = fresh()
    status, payload, _ = call(url + "/api/v1/setup", method="POST",
                              body={"password": "short"})
    assert status == 400
    assert "8" in json.loads(payload)["detail"]
    # Still unclaimed, so the operator gets another go.
    assert call(url + "/api/v1/setup", method="POST",
                body={"password": "correct-horse"})[0] == 200


# --- credentials -----------------------------------------------------------

def test_a_wrong_password_gets_nothing(fresh):
    _, url = fresh()
    call(url + "/api/v1/setup", method="POST", body={"password": "correct-horse"})
    status, _, headers = call(url + "/api/v1/login", method="POST",
                              body={"password": "battery-staple"})
    assert status == 401
    assert SESSION_COOKIE not in headers.get("Set-Cookie", "")


def test_no_credential_at_all_gets_nothing(fresh):
    _, url = fresh()
    call(url + "/api/v1/setup", method="POST", body={"password": "correct-horse"})
    assert call(url + "/api/v1/config")[0] == 401


def test_api_clients_keep_working_exactly_as_documented(fresh):
    """The key paths are the contract for scripts; the browser fix must not
    have quietly become the only way in."""
    service, url = fresh()
    call(url + "/api/v1/setup", method="POST", body={"password": "correct-horse"})
    key = service.store.settings["_api_key"]

    assert call(url + "/api/v1/config", key=key)[0] == 200

    request = urllib.request.Request(url + "/api/v1/config")
    request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200

    assert call(f"{url}/api/v1/config?apikey={key}")[0] == 200

    # And a key logs a browser in, which is what the "use an API key instead"
    # link on the sign-in screen posts.
    status, _, headers = call(url + "/api/v1/login", method="POST",
                              body={"apikey": key})
    assert status == 200 and SESSION_COOKIE in headers.get("Set-Cookie", "")


# --- pre-provisioned installs ----------------------------------------------

def test_romarr_password_claims_before_the_first_request(fresh):
    """What a container template should do: no open window at all."""
    service, url = fresh(ROMARR_PASSWORD="from-the-template")
    assert service.claimed is True
    _, page, _ = call(url + "/")
    assert "Sign in" in page
    assert call(url + "/api/v1/login", method="POST",
                body={"password": "from-the-template"})[0] == 200
    # The setup door was never open.
    assert call(url + "/api/v1/setup", method="POST",
                body={"password": "attacker-chosen"})[0] == 409


def test_an_operator_supplied_key_also_counts_as_claimed(fresh):
    """They already hold a credential, so there is nothing to claim."""
    service, url = fresh(ROMARR_API_KEY="key-from-the-unraid-template")
    assert service.claimed is True
    _, page, _ = call(url + "/")
    assert "Sign in" in page
    assert call(url + "/api/v1/config",
                key="key-from-the-unraid-template")[0] == 200
    assert call(url + "/api/v1/setup", method="POST",
                body={"password": "attacker-chosen"})[0] == 409


def test_a_generated_key_does_not_count_as_claimed(fresh):
    """It is not a credential anybody holds -- that was the whole trap. The
    operator was told to find it under Settings, behind the gate it opened."""
    service, url = fresh()
    assert service.claimed is False
    assert service.store.settings["_api_key"]  # one exists, nobody has it
    _, page, _ = call(url + "/")
    assert "Set your password" in page


# --- the deliberate ways out -----------------------------------------------

def test_disabled_serves_the_app_and_never_the_login_screen(fresh):
    _, url = fresh(ROMARR_AUTH="disabled")
    status, page, _ = call(url + "/")
    assert status == 200 and "<title>ROMarr" in page
    assert call(url + "/api/v1/config")[0] == 200


def test_login_redirects_to_the_app_once_signed_in(fresh):
    _, url = fresh()
    _, _, headers = call(url + "/api/v1/setup", method="POST",
                         body={"password": "correct-horse"})
    cookie = session_of(headers)
    status, _, headers = call(url + "/login", cookie=cookie, redirect=False)
    assert status == 303
    assert headers.get("Location") == "/"


def test_the_login_screen_is_reachable_without_a_credential(fresh):
    """It is the one page that must answer somebody who has nothing."""
    _, url = fresh()
    call(url + "/api/v1/setup", method="POST", body={"password": "correct-horse"})
    status, page, _ = call(url + "/login")
    assert status == 200 and "Sign in" in page


def test_the_setup_screen_never_contains_the_api_key(fresh):
    """The key exists at this point. Putting it on an unauthenticated page to
    be helpful would hand it to whoever reached the port first."""
    service, url = fresh()
    key = service.store.settings["_api_key"]
    _, page, _ = call(url + "/")
    assert key not in page
    _, page, _ = call(url + "/login")
    assert key not in page


def test_concurrent_setup_claims_allow_only_one_winner(fresh):
    """Two simultaneous first-run claims produce one winner and one 409."""
    service, url = fresh()
    barrier = threading.Barrier(2)
    real_hash = service.auth.hash_password

    def aligned_hash(password):
        assert not service._setup_lock.locked()
        barrier.wait(timeout=5)
        return real_hash(password)

    service.auth.hash_password = aligned_hash

    def attempt(password):
        status, body, headers = call(
            url + "/api/v1/setup", method="POST", body={"password": password}
        )
        return {
            "password": password,
            "status": status,
            "payload": json.loads(body),
            "headers": headers,
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt, "first-claim-alpha"),
            pool.submit(attempt, "second-claim-bravo"),
        ]
        results = [future.result() for future in futures]

    assert sorted(result["status"] for result in results) == [200, 409]
    winner = next(result for result in results if result["status"] == 200)
    loser = next(result for result in results if result["status"] == 409)

    assert service.claimed is True
    assert winner["payload"].get("ok") is True
    assert loser["payload"]["error"] == "already set up"
    assert "already" in loser["payload"]["detail"].lower()

    assert loser["headers"].get("Set-Cookie") is None
    assert SESSION_COOKIE not in (loser["headers"].get("Set-Cookie") or "")

    winner_cookie = session_of(winner["headers"])
    assert call(url + "/api/v1/config", cookie=winner_cookie)[0] == 200
    assert call(url + "/api/v1/login", method="POST",
                body={"password": winner["password"]})[0] == 200
    assert call(url + "/api/v1/login", method="POST",
                body={"password": loser["password"]})[0] == 401


def test_setup_rolls_back_when_persistence_fails(fresh, monkeypatch):
    """A failed save leaves the install unclaimed with no session cookie."""
    service, url = fresh()
    save_calls = []

    def failing_save(*, raise_on_error=False):
        save_calls.append(raise_on_error)
        raise OSError("disk full")

    monkeypatch.setattr(service.store, "save", failing_save)

    status, body, headers = call(
        url + "/api/v1/setup",
        method="POST",
        body={"password": "first-attempt-secret"},
    )
    assert status == 500
    assert save_calls == [True]
    assert "disk full" not in body
    assert headers.get("Set-Cookie") is None
    assert SESSION_COOKIE not in (headers.get("Set-Cookie") or "")
    assert service.claimed is False
    assert service.auth.password_hash == ""
    assert "_password_hash" not in service.store.settings
    assert call(url + "/api/v1/config")[0] == 401
    assert call(url + "/api/v1/login", method="POST",
                body={"password": "first-attempt-secret"})[0] == 401

    monkeypatch.undo()

    status, _, headers = call(
        url + "/api/v1/setup",
        method="POST",
        body={"password": "second-attempt-secret"},
    )
    assert status == 200
    cookie = session_of(headers)
    assert service.claimed is True
    assert call(url + "/api/v1/config", cookie=cookie)[0] == 200
    assert call(url + "/api/v1/login", method="POST",
                body={"password": "second-attempt-secret"})[0] == 200
    assert call(url + "/api/v1/login", method="POST",
                body={"password": "first-attempt-secret"})[0] == 401
    persisted = json.loads(service.store.path.read_text(encoding="utf-8"))
    assert persisted["settings"]["_password_hash"] == service.auth.password_hash
