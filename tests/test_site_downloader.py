"""The ROM-site download backend, both modes.

The sites plugins catalogue serve files over ordinary HTTP, so ROMarr has to
do the fetching itself. Two modes: a plain GET for the sites that publish a
URL, and a real headless Chromium for the few whose download is a form the
page submits. The second exists because Vimm's Lair has no fetchable URL --
its download is a POST carrying a mediaId, and the GET-shaped URL two
catalogues list for it has never worked.

The tests that matter most here are the ones about what the backend REFUSES.
A download backend that quietly grows a CAPTCHA solver, a challenge bypass or
a forged Referer stops being an automated user action and becomes
circumvention, and the difference is invisible in a diff unless something
checks for it.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from romarr import browser
from romarr.downloaders import (
    CLIENT_TYPES, SiteConfig, SiteDownloader, SitePolicy, build_client,
    hand_off, pick_client,
)

ALLOW_ALL = "User-agent: *\nDisallow:\n"


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else body.decode("utf-8", "replace")
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Answers robots.txt from one script and everything else from another."""

    def __init__(self, robots=ALLOW_ALL, responses=(), robots_error=None,
                 robots_status=200):
        self.robots = robots
        self.robots_status = robots_status
        self.robots_error = robots_error
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if url.endswith("/robots.txt"):
            if self.robots_error:
                raise self.robots_error
            return FakeResponse(self.robots_status, self.robots.encode())
        return self.responses.pop(0) if self.responses else FakeResponse()


def make(tmp_path, mode="direct", session=None, driver=None, **cfg):
    policy = SitePolicy(delay=0, session=session or FakeSession(),
                        sleeper=lambda _s: None)
    return SiteDownloader(
        SiteConfig(save_path=str(tmp_path / "dl"), mode=mode, delay=0, **cfg),
        session=session or FakeSession(), policy=policy, driver=driver)


# --- the registry -----------------------------------------------------------

def test_both_modes_are_offered_as_client_types():
    assert CLIENT_TYPES["direct"]["protocol"] == "direct"
    assert CLIENT_TYPES["browser"]["protocol"] == "browser"


def test_the_mode_is_the_protocol_so_releases_route_to_the_right_one():
    """A plugin whose site needs a click must not be handed the plain
    fetcher, and vice versa. Routing by protocol is how every other release
    already picks a client, so this needs no new mechanism."""
    direct = build_client({"type": "direct", "save_path": "/d"})
    browser_client = build_client({"type": "browser", "save_path": "/d"})
    clients = [direct, browser_client]
    assert pick_client("direct", clients) is direct
    assert pick_client("browser", clients) is browser_client


def test_a_blank_browser_host_means_launch_one_here():
    """base_url_for defaults the host to localhost, and an unset host
    assembled into ws://localhost:3000 would point the driver at a machine
    nobody configured -- failing as a refused connection rather than as the
    local launch that was asked for."""
    assert build_client({"type": "browser", "save_path": "/d"})._config.base_url == ""


def test_the_endpoint_scheme_decides_how_the_file_gets_back():
    """Not cosmetic. A `ws://` playwright run-server runs the driver on the
    browser's host and streams the finished file back; a bare `http://`
    debugging port has the driver here, so the browser saves onto its own
    disk and only a shared directory can recover it."""
    ws = build_client({"type": "browser", "save_path": "/d",
                       "host": "10.0.0.5", "port": 3000})
    assert ws._config.base_url == "ws://10.0.0.5:3000"
    cdp = build_client({"type": "browser", "save_path": "/d",
                        "host": "10.0.0.5", "port": 9222, "scheme": "http"})
    assert cdp._config.base_url == "http://10.0.0.5:9222"


def test_an_unconfigured_client_is_inert(tmp_path):
    client = SiteDownloader(SiteConfig(save_path=""))
    assert not client.configured
    assert not client.reachable()
    assert not client.add("https://example.invalid/rom.zip")
    assert client.completed() == []


# --- add --------------------------------------------------------------------

def test_only_http_links_are_accepted(tmp_path):
    client = make(tmp_path)
    assert not client.add("magnet:?xt=urn:btih:abc")
    assert client.add("https://example.invalid/rom.zip")


def test_queueing_the_same_url_twice_is_not_two_downloads(tmp_path):
    client = make(tmp_path)
    assert client.add("https://example.invalid/rom.zip")
    assert client.add("https://example.invalid/rom.zip")
    assert len(client._read()) == 1


def test_the_queue_survives_a_restart(tmp_path):
    """The ledger lives beside the downloads rather than in ROMarr's settings,
    so moving the download directory moves its queue with it."""
    make(tmp_path).add("https://example.invalid/rom.zip")
    assert make(tmp_path)._read()[0]["url"] == "https://example.invalid/rom.zip"


# --- direct mode ------------------------------------------------------------

def test_a_direct_download_lands_and_is_reported_by_its_release_title(tmp_path):
    session = FakeSession(responses=[FakeResponse(200, b"ROM BYTES")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/roms/Contra%20(USA).zip", name="Contra (USA)")
    done = client.completed()
    assert len(done) == 1
    # The importer matches a finished download to its queue row by the release
    # title; a file named by the site would never match.
    assert done[0]["name"] == "Contra (USA)"
    assert pathlib.Path(done[0]["content_path"]).read_bytes() == b"ROM BYTES"
    assert pathlib.Path(done[0]["content_path"]).name == "Contra (USA).zip"


def test_content_disposition_names_the_file_and_cannot_choose_a_path(tmp_path):
    """The name carries the region and revision a DAT check reads, so the
    server's name wins -- but a filename arriving from somebody else's server
    is not allowed to escape the download directory."""
    session = FakeSession(responses=[FakeResponse(
        200, b"x", {"Content-Disposition":
                    'attachment; filename="../../etc/Super Metroid (JU).sfc"'})])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/get?id=7")
    saved = pathlib.Path(client.completed()[0]["content_path"])
    assert saved.name == "Super Metroid (JU).sfc"
    assert saved.parent == tmp_path / "dl"


def test_nothing_half_written_is_ever_reported_as_finished(tmp_path):
    session = FakeSession(responses=[FakeResponse(200, b"partial")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/rom.zip")
    client.completed()
    assert not list((tmp_path / "dl").glob("*.partial"))


def test_a_completed_download_is_reported_on_every_sweep(tmp_path):
    """Reported rather than popped: the importer sweeps repeatedly and decides
    for itself what it has already taken, exactly as it does with a torrent
    that stays in qBittorrent after finishing."""
    session = FakeSession(responses=[FakeResponse(200, b"x")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/rom.zip")
    assert client.completed()
    assert client.completed()


def test_only_one_file_is_fetched_per_sweep(tmp_path):
    """Single concurrent download by default. These are small sites."""
    session = FakeSession(responses=[FakeResponse(200, b"a"),
                                     FakeResponse(200, b"b")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/a.zip")
    client.add("https://example.invalid/b.zip")
    assert len(client.completed()) == 1
    assert len(client.completed()) == 2


# --- being a good guest -----------------------------------------------------

def test_a_disallowed_path_is_never_fetched(tmp_path):
    session = FakeSession(robots="User-agent: *\nDisallow: /roms/download\n")
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/roms/download/1")
    assert client.completed() == []
    assert "robots.txt disallows" in client._read()[0]["detail"]
    assert not any(c["url"].endswith("/1") for c in session.calls)


def test_an_unreadable_robots_txt_is_a_refusal_not_a_free_pass(tmp_path):
    """RFC 9309: a site whose robots.txt cannot be read has not given
    permission, and guessing that it would have is exactly the assumption a
    polite client does not get to make."""
    import requests

    client = make(tmp_path, session=FakeSession(
        robots_error=requests.ConnectionError("boom")))
    client.add("https://example.invalid/rom.zip")
    assert client.completed() == []
    assert "no permission" in client._read()[0]["detail"]


def test_a_missing_robots_txt_is_a_site_saying_it_has_no_rules(tmp_path):
    session = FakeSession(robots_status=404,
                          responses=[FakeResponse(200, b"x")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/rom.zip")
    assert client.completed()


def test_the_sites_own_crawl_delay_wins_when_it_asks_for_longer():
    slept = []
    policy = SitePolicy(delay=5, sleeper=slept.append, session=FakeSession(
        robots="User-agent: *\nDisallow:\nCrawl-delay: 10\n"))
    policy.allowed("https://example.invalid/a")     # loads the rules
    policy.wait("https://example.invalid/a")
    policy.wait("https://example.invalid/a")
    assert slept and slept[0] > 9


def test_the_clock_is_per_host_so_two_sites_do_not_wait_for_each_other():
    slept = []
    policy = SitePolicy(delay=5, sleeper=slept.append, session=FakeSession())
    policy.wait("https://one.invalid/a")
    policy.wait("https://two.invalid/a")
    assert slept == []


def test_a_403_stops_this_site_and_is_not_retried(tmp_path):
    """The whole point of the module is not to keep asking, or to dress the
    request up until it stops being refused."""
    session = FakeSession(responses=[FakeResponse(403, b"")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/a.zip")
    client.add("https://example.invalid/b.zip")
    client.completed()
    assert "403" in client._read()[0]["detail"]
    client.completed()
    rows = client._read()
    assert "answered 403 earlier" in rows[1]["detail"]
    # One GET for the file, one for robots.txt. Nothing was tried twice.
    assert sum(1 for c in session.calls if not c["url"].endswith("robots.txt")) == 1


def test_a_429_is_waited_out_rather_than_hammered(tmp_path):
    slept = []
    session = FakeSession(responses=[
        FakeResponse(429, b"", {"Retry-After": "7"}),
        FakeResponse(200, b"ROM"),
    ])
    policy = SitePolicy(delay=0, session=session, sleeper=slept.append)
    client = SiteDownloader(SiteConfig(save_path=str(tmp_path / "dl"), delay=0),
                            session=session, policy=policy)
    client.add("https://example.invalid/rom.zip")
    assert client.completed()
    assert 7 in slept


def test_a_ridiculous_retry_after_is_capped():
    """A server may say "an hour", and a client that sleeps for an hour inside
    an import sweep looks exactly like one that has hung."""
    from romarr.downloaders import _retry_after

    assert _retry_after("3600", 5) == 300.0
    assert _retry_after("", 5) == 5
    assert _retry_after("not a number", 5) == 5


def test_we_say_who_we_are(tmp_path):
    session = FakeSession(responses=[FakeResponse(200, b"x")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/rom.zip")
    client.completed()
    agents = {c.get("headers", {}).get("User-Agent") for c in session.calls}
    assert agents == {"ROMarr (+https://github.com/BlizzHacker/romarr)"}


# --- the browser lane -------------------------------------------------------

class FakeDriver:
    """Stands in for browser.py so the routing can be tested without Chromium."""

    Refused = browser.Refused
    Unavailable = browser.Unavailable

    def __init__(self, available=(True, "chromium 999"), raises=None):
        self._available = available
        self._raises = raises
        self.calls = []

    def availability(self, cdp_url=""):
        return self._available

    def fetch(self, url, destination, **kw):
        self.calls.append({"url": url, "destination": destination, **kw})
        if self._raises:
            raise self._raises
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "Contra (USA).zip"
        target.write_bytes(b"CLICKED")
        return target


def test_the_browser_lane_fetches_by_clicking_the_real_control(tmp_path):
    driver = FakeDriver()
    client = make(tmp_path, mode="browser", driver=driver,
                  remote_download_dir="/browser/downloads")
    client.add("https://vault.invalid/vault/42#romarr-click=css:form#dl button",
               name="Contra (USA)")
    done = client.completed()
    assert done[0]["name"] == "Contra (USA)"
    assert pathlib.Path(done[0]["content_path"]).read_bytes() == b"CLICKED"
    # The page URL travels whole; the fragment is the browser's instruction and
    # is stripped there, not here, because that is where clicking happens.
    assert driver.calls[0]["url"].endswith("#romarr-click=css:form#dl button")
    assert driver.calls[0]["remote_dir"] == "/browser/downloads"


def test_a_challenge_is_refused_and_the_reason_is_kept(tmp_path):
    """A site that can only be downloaded from by defeating a bot check is a
    site this backend reports as unavailable. That is the finished answer for
    such a site, not a gap to close later."""
    driver = FakeDriver(raises=browser.Refused("the site answered with a bot "
                                               "challenge (cf_chl_opt)"))
    client = make(tmp_path, mode="browser", driver=driver)
    client.add("https://challenged.invalid/rom")
    assert client.completed() == []
    assert "bot challenge" in client._read()[0]["detail"]


def test_without_a_driver_the_browser_lane_says_so_and_direct_still_works(tmp_path):
    missing = FakeDriver(available=(False, "the playwright driver is not installed"))
    browser_client = make(tmp_path, mode="browser", driver=missing)
    assert not browser_client.reachable()
    assert "playwright" in browser_client.detail

    session = FakeSession(responses=[FakeResponse(200, b"x")])
    direct = make(tmp_path, session=session)
    assert direct.reachable()
    direct.add("https://example.invalid/rom.zip")
    assert direct.completed()


def test_robots_txt_applies_to_the_browser_lane_too(tmp_path):
    """It would be absurd for one mode to honour a crawl-delay and the other
    to ignore it: both load pages from the same small servers."""
    driver = FakeDriver()
    client = make(tmp_path, mode="browser", driver=driver,
                  session=FakeSession(robots="User-agent: *\nDisallow: /vault\n"))
    client._policy = SitePolicy(delay=0, sleeper=lambda _s: None,
                                session=FakeSession(
                                    robots="User-agent: *\nDisallow: /vault\n"))
    client.add("https://vault.invalid/vault/42")
    assert client.completed() == []
    assert driver.calls == []


def test_the_control_rides_in_the_fragment_so_it_never_reaches_the_site():
    url, control = browser.split_control(
        "https://vault.invalid/vault/42#romarr-click=css:#dl button")
    assert url == "https://vault.invalid/vault/42"
    assert control == "css:#dl button"
    assert browser.split_control("https://vault.invalid/x#section") == (
        "https://vault.invalid/x#section", "")


def test_the_driver_is_optional_and_ROMarr_starts_without_it(monkeypatch):
    """Matching how ROM Hub treats pyseccomp: an install that only ever
    fetches plain URLs pays nothing for a browser it does not have."""
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kw):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", refuse)
    available, reason = browser.availability()
    assert not available
    assert "playwright" in reason


# --- what this is not allowed to become -------------------------------------

#: Every one of these is a way of pretending to be something you are not, or
#: of getting past a check somebody put up on purpose. None of them may appear
#: in the browser lane, and this test is the guarantee.
EVASION = (
    "playwright_stealth", "puppeteer-extra", "undetected", "stealth",
    "webdriver = ", "webdriver=false", "deleteproperty",
    "2captcha", "anticaptcha", "capsolver", "captcha_solver",
    "flaresolverr", "cf_clearance", "solve_challenge",
    "--disable-blink-features=automationcontrolled",
)


def _code_only(path: pathlib.Path) -> str:
    """The module with its prose removed.

    Comments and docstrings go, ordinary string literals stay. The module
    names several of these tokens in order to say it refuses them, so a naive
    grep would fail on the very sentences that make the promise -- while
    dropping every string would let a call to a solver's URL through, which is
    exactly what this is watching for.
    """
    import ast

    source = path.read_text(encoding="utf-8")
    prose = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [line.split("#")[0] for n, line in enumerate(source.splitlines(), 1)
            if n not in prose]
    return "\n".join(kept).lower()


@pytest.mark.parametrize("module", ["browser", "downloaders"])
def test_neither_half_of_the_backend_contains_evasion_machinery(module):
    import importlib

    code = _code_only(pathlib.Path(
        importlib.import_module(f"romarr.{module}").__file__))
    for token in EVASION:
        assert token not in code, token


def test_the_direct_lane_never_invents_a_referer(tmp_path):
    """The objection this backend answers, stated as a test.

    A `Referer` claiming you were on a page you never loaded is a lie about
    where the request came from. The plain GET has not loaded any page, so it
    sends no such header -- the browser lane exists precisely because that is
    the only honest way to produce one.
    """
    session = FakeSession(responses=[FakeResponse(200, b"x")])
    client = make(tmp_path, session=session)
    client.add("https://example.invalid/rom.zip")
    client.completed()
    for call in session.calls:
        headers = {k.lower() for k in call.get("headers", {})}
        assert "referer" not in headers
        assert "cookie" not in headers


def test_a_challenge_is_detected_from_the_page_not_the_status_code():
    """Cloudflare's managed challenge is a 403 on some sites and a 200 on
    others, so the status alone both misses it and cries wolf."""
    class Page:
        def __init__(self, body):
            self._body = body

        def content(self):
            return self._body

    with pytest.raises(browser.Refused):
        browser._refuse_if_challenged(
            Page("<html>Just a moment...</html>"), type("R", (), {"status": 200}))
    with pytest.raises(browser.Refused):
        browser._refuse_if_challenged(
            Page("<div class='g-recaptcha'></div>"), type("R", (), {"status": 200}))
    browser._refuse_if_challenged(Page("<html>Contra (USA)</html>"),
                                  type("R", (), {"status": 200}))


def test_a_page_behind_a_login_is_refused_rather_than_worked_around():
    class Page:
        def content(self):
            return "<html>Sign in</html>"

    with pytest.raises(browser.Refused, match="behind a login"):
        browser._refuse_if_challenged(Page(), type("R", (), {"status": 403}))


def test_the_user_agent_is_declared_rather_than_disguised():
    """Appended to the browser's real one, not replacing it. Replacing it
    hides that this is Chromium; leaving the token off hides that the visit is
    automated. Both are true, so both are declared."""
    class Probe:
        def new_page(self):
            return type("P", (), {"evaluate": lambda _s, _e: "Mozilla/5.0 Chrome/1"})()

        def close(self):
            pass

    fake = type("B", (), {"new_context": lambda _s: Probe()})()
    assert browser.declared_user_agent(fake, "ROMarr (+url)") == \
        "Mozilla/5.0 Chrome/1 ROMarr (+url)"
    # No token means the browser's own header, which beats a guess.
    assert browser.declared_user_agent(fake, "") is None


def test_the_launch_flags_are_container_survival_not_fingerprinting():
    assert set(browser.LAUNCH_ARGS) == {"--no-sandbox", "--disable-dev-shm-usage"}


# --- wiring -----------------------------------------------------------------

def test_the_release_title_reaches_only_the_clients_that_can_take_it():
    """The importer matches by title, so a client that can carry it should --
    and handing the keyword to one that cannot is a TypeError in the middle of
    a grab."""
    class Old:
        def add(self, url):
            return url

    class New:
        TAKES_NAME = True

        def add(self, url, *, name=""):
            return name

    assert hand_off(Old(), "u", name="Contra") == "u"
    assert hand_off(New(), "u", name="Contra") == "Contra"


def test_the_browser_capability_is_reported_on_its_own_route(tmp_path):
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    report = service.browser_capability()
    assert report["configured"] is False
    assert isinstance(report["available"], bool)
    assert report["reason"]
    # Said out loud in the API, not only in the source.
    assert "captchas" in report["refuses"]


def test_the_new_route_is_documented():
    from romarr.openapi import DESCRIPTIONS

    assert "/api/v1/downloadclient/browser" in DESCRIPTIONS


# --- against a real browser -------------------------------------------------
#
# Skipped where there is no Chromium, which is most CI and the 1GB container
# ROMarr ships in. Kept in the suite anyway because it is the only test that
# proves the claim the whole module rests on: that the Referer is TRUE.

class VaultHandler(BaseHTTPRequestHandler):
    """A page shaped like the case that made this necessary: no fetchable
    URL, a POST carrying an id, and the file only on the far side of it."""

    PAYLOAD = b"ROMARR-PROOF" * 32
    PAGE = (b"<!doctype html><html><body><h1>Contra (USA)</h1>"
            b"<form method='post' action='/dl' id='dl_form'>"
            b"<input type='hidden' name='mediaId' value='42'>"
            b"<button type='submit'>Download</button></form></body></html>")
    seen: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/game/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(self.PAGE)))
            self.end_headers()
            self.wfile.write(self.PAGE)
            return
        self.send_response(404)          # no robots.txt: this site has no rules
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        VaultHandler.seen = {
            "body": self.rfile.read(length).decode(),
            "referer": self.headers.get("Referer", ""),
            "agent": self.headers.get("User-Agent", ""),
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         'attachment; filename="Contra (USA).zip"')
        self.send_header("Content-Length", str(len(self.PAYLOAD)))
        self.end_headers()
        self.wfile.write(self.PAYLOAD)


def test_the_referer_the_browser_sends_is_one_it_actually_earned(tmp_path):
    """THE claim this module rests on.

    A hand-forged `Referer` asserting you were on a page you never loaded is a
    lie about where a request came from. A real browser that really navigated
    to the page and really clicked its download control sends the same header
    truthfully. This drives a live Chromium against a locally served page in
    exactly the shape that has no fetchable URL, and checks that the request
    the server receives is the site's own POST with the site's own page as its
    referrer -- nothing synthesised.
    """
    available, why = browser.availability()
    if not available:
        pytest.skip(f"no browser here: {why}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), VaultHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = f"http://127.0.0.1:{server.server_address[1]}/game/42"
        client = build_client({"type": "browser", "save_path": str(tmp_path / "dl"),
                               "delay": 0})
        assert client.add(page, name="Contra (USA)")
        done = client.completed()
        assert done, json.dumps(client._read())
    finally:
        server.shutdown()

    assert VaultHandler.seen["body"] == "mediaId=42"
    assert VaultHandler.seen["referer"] == page
    # Chromium's own identity, with ROMarr's token appended so an operator
    # reading their access log can tell exactly what visited them.
    assert "Chrome/" in VaultHandler.seen["agent"]
    assert "ROMarr (+https://github.com/BlizzHacker/romarr)" in VaultHandler.seen["agent"]

    saved = pathlib.Path(done[0]["content_path"])
    assert saved.name == "Contra (USA).zip"
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == \
        hashlib.sha256(VaultHandler.PAYLOAD).hexdigest()
