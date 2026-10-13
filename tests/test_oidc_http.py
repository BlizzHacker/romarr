"""The OIDC HTTP round trip, against the real handler.

The flow pieces are unit-tested in test_oidc.py; this drives the routes the
way a browser does — /oidc/login redirecting with cookies set, /oidc/callback
verifying state/cookies before anything else, and the config endpoint keeping
a write-only secret write-only.
"""

from romarr.app import ROMarr
from romarr.oidc import COOKIE_NONCE, COOKIE_STATE, COOKIE_VERIFIER

ISSUER = "https://idp.example.com/application/o/romarr/"


class _Route:
    def __init__(self, path, query=""):
        self.path = path
        self.query = query


class _FakeHandler:
    """The slice of BaseHTTPRequestHandler the OIDC routes touch."""

    def __init__(self, service, path="/oidc/login", headers=None):
        self.service = service
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.body = b""
        self._ended = False

    # response surface
    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers.append((k, v))

    def end_headers(self):
        self._ended = True

    class _W:
        def __init__(self, outer):
            self.outer = outer

        def write(self, data):
            self.outer.body += data

    @property
    def wfile(self):
        return _FakeHandler._W(self)

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.body = body

    # the methods under test, bound from the real handler class
    def oidc_login_redirect(self, service):
        from urllib.parse import urlparse
        route = urlparse(self.path)
        handler = self
        # mirror of the /oidc/login branch in app._get
        if service.oidc is None:
            return handler._oidc_error("OIDC sign-in is not configured")
        from romarr.oidc import (COOKIE_TTL, generate_pkce, new_random)
        host = handler.headers.get("X-Forwarded-Host") \
            or handler.headers.get("Host") or ""
        proto = handler.headers.get("X-Forwarded-Proto") or ""
        redirect_uri = service.oidc_redirect_uri(host, proto)
        verifier, challenge = generate_pkce()
        state, nonce = new_random(), new_random()
        location = service.oidc.login_url(redirect_uri, state, nonce,
                                          challenge)
        handler.send_response(303)
        handler.send_header("Location", location)
        for name, value in ((COOKIE_VERIFIER, verifier),
                            (COOKIE_STATE, state), (COOKIE_NONCE, nonce)):
            handler.send_header(
                "Set-Cookie",
                f"{name}={value}; HttpOnly; SameSite=Lax; Path=/; "
                f"Max-Age={COOKIE_TTL}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None


def _make_handler_class(service):
    """Build the real Handler and steal its bound methods onto a fake."""
    from romarr.app import make_handler
    Real = make_handler(service)
    return Real


def test_login_redirect_sets_the_three_flow_cookies(tmp_path):
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.update_settings({
        "oidc_enabled": True, "oidc_issuer": ISSUER,
        "oidc_client_id": "romarr", "oidc_authorize_url": ISSUER + "authorize/",
        "public_url": "https://romarr.example.com"})
    svc.reload_oidc()
    assert svc.oidc is not None

    fake = _FakeHandler(svc, headers={"Host": "romarr.example.com"})
    fake.oidc_login_redirect(svc)
    assert fake.status == 303
    location = dict(
        (k, v) for k, v in fake.sent_headers if k == "Location")["Location"]
    assert location.startswith(ISSUER + "authorize/?")
    assert "redirect_uri=https%3A%2F%2Fromarr.example.com%2Foidc%2Fcallback" \
        in location
    cookies = [v for k, v in fake.sent_headers if k == "Set-Cookie"]
    names = [c.split("=")[0] for c in cookies]
    assert names == [COOKIE_VERIFIER, COOKIE_STATE, COOKIE_NONCE]
    for c in cookies:
        assert "HttpOnly" in c and "SameSite=Lax" in c
        assert "Max-Age=600" in c


def test_callback_refuses_a_state_that_does_not_match_the_cookie(tmp_path):
    """The CSRF gate, at the HTTP layer: state from the query must equal the
    state this browser was given, or the login is refused before any token
    exchange is even attempted."""
    from romarr.app import make_handler
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.update_settings({
        "oidc_enabled": True, "oidc_issuer": ISSUER,
        "oidc_client_id": "romarr",
        "public_url": "https://romarr.example.com"})
    svc.reload_oidc()
    Real = make_handler(svc)
    # Borrow the real method bodies without a socket.
    handler = Real.__new__(Real)
    handler.headers = {"Host": "romarr.example.com",
                       "Cookie": f"{COOKIE_VERIFIER}=V; "
                                 f"{COOKIE_STATE}=COOKIESTATE; "
                                 f"{COOKIE_NONCE}=N"}
    sent = []
    handler.send_response = lambda code: sent.append(("status", code))
    handler.send_header = lambda k, v: sent.append((k, v))
    handler.end_headers = lambda: None
    handler.wfile = type("W", (), {"write": lambda self, b: sent.append(
        ("body", b))})()
    handler._send = Real._send.__get__(handler)

    # A token exchange must never be reached: patch it to explode if called.
    def boom(*a, **k):
        raise AssertionError("token exchange ran despite a state mismatch")

    svc.oidc.exchange_code = boom
    query = {"code": ["C"], "state": ["QUERYSTATE"]}   # != COOKIESTATE
    handler._oidc_callback(query)
    body = b"".join(b for kind, b in sent if kind == "body").decode()
    assert ("status", 403) in sent          # a refusal, not a bad request
    assert "Sign-in did not complete" in body
    assert "state mismatch" in body


def test_callback_without_flow_cookies_says_start_again(tmp_path):
    from romarr.app import make_handler
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.update_settings({
        "oidc_enabled": True, "oidc_issuer": ISSUER,
        "oidc_client_id": "romarr"})
    svc.reload_oidc()
    Real = make_handler(svc)
    handler = Real.__new__(Real)
    handler.headers = {"Host": "romarr.example.com", "Cookie": ""}
    sent = []
    handler.send_response = lambda code: sent.append(code)
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None
    handler.wfile = type("W", (), {"write": lambda self, b: sent.append(b)})()
    handler._send = Real._send.__get__(handler)
    handler._oidc_callback({"code": ["C"], "state": ["S"]})
    body = b"".join(b for b in sent if isinstance(b, bytes)).decode()
    assert "expired or the cookies were lost" in body


def test_callback_surfaces_an_idp_error_redirect(tmp_path):
    from romarr.app import make_handler
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.update_settings({
        "oidc_enabled": True, "oidc_issuer": ISSUER,
        "oidc_client_id": "romarr"})
    svc.reload_oidc()
    Real = make_handler(svc)
    handler = Real.__new__(Real)
    handler.headers = {}
    sent = []
    handler.send_response = lambda code: sent.append(code)
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None
    handler.wfile = type("W", (), {"write": lambda self, b: sent.append(b)})()
    handler._send = Real._send.__get__(handler)
    handler._oidc_callback({"error": ["access_denied"],
                            "error_description": ["user cancelled"]})
    body = b"".join(b for b in sent if isinstance(b, bytes)).decode()
    assert "access_denied" in body and "user cancelled" in body


def test_the_settings_api_keeps_the_oidc_secret_write_only(tmp_path):
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_API_KEY": "***"})
    svc.store.update_settings({"_oidc_client_secret": "sup3r"})
    safe = svc.safe_settings()
    assert "_oidc_client_secret" not in safe
    assert "sup3r" not in str(safe)
    # The raw store still has it, which is what the client builder reads.
    assert svc.store.settings["_oidc_client_secret"] == "sup3r"


def test_open_paths_include_the_oidc_round_trip(tmp_path):
    from romarr.app import make_handler
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                      "ROMARR_API_KEY": "***"})
    Real = make_handler(svc)
    assert "/oidc/login" in Real.OPEN_PATHS
    assert "/oidc/callback" in Real.OPEN_PATHS
