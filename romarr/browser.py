"""Headless Chromium, for the sites whose download is not a URL.

Most ROM sites hand out a link and `requests` fetches it. A few do not: the
file is behind a form POST carrying a per-render token, or behind a link that
only exists once the page's own JavaScript has run. Vimm's Lair is the case
that made this necessary -- its download is a POST to `dl3.vimm.net` carrying
a `mediaId`, and the GET-shaped URL two catalogues list for it has never
worked, because it was guessed rather than observed.

The honest way to fetch from a site like that is to *be* on the page. This
module opens the real page, finds the real download control, and clicks it.
Every header that goes out -- `Referer` above all -- is one the browser
genuinely produced because the browser genuinely was there. That is the whole
point of the module and the reason it exists rather than a hand-forged header:
a `Referer` claiming a page you never loaded is a lie about where a request
came from, and this is the same request without the lie.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It is not a bot-detection bypass, and none of the following will be added to
it:

  * solving or answering CAPTCHAs
  * defeating a Cloudflare (or any other) challenge -- when one is detected
    the fetch is REFUSED, by name, and reported to the operator
  * spoofing headers to misrepresent where a request came from
  * stealth plugins, fingerprint spoofing, or patching `navigator.webdriver`
  * signing in anywhere the operator has not been able to sign in themselves

A site that cannot be downloaded from without one of those is a site this
backend reports as unavailable, with the reason, and its plugin stays
catalogue-only. That is a complete answer, not a gap to be closed later.
`tests/test_site_downloader.py` asserts the absence of the evasion machinery,
so the guarantee fails the build rather than eroding quietly.

THE DRIVER IS OPTIONAL
----------------------
`playwright` is imported lazily and never at module import, the same way ROM
Hub treats `pyseccomp`: ROMarr starts, runs and downloads over plain HTTP on
an install that has never heard of a browser, and the browser lane reports
itself as not configured rather than raising an ImportError somewhere far
from the cause.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

#: Chromium flags. Both are container survival, not evasion, and it is worth
#: being precise about that because "--no-sandbox" reads like cheating:
#:
#:   * `--no-sandbox` turns off Chromium's own *process* sandbox, which
#:     refuses to start as root inside an unprivileged LXC. It has nothing to
#:     do with what a site can detect.
#:   * `--disable-dev-shm-usage` moves shared memory off /dev/shm, which a
#:     container gives 64MB of and which Chromium exhausts on the first big
#:     page -- the symptom is a tab that dies mid-navigation.
#:
#: Nothing here alters the fingerprint the site sees, and nothing here may.
LAUNCH_ARGS = ("--no-sandbox", "--disable-dev-shm-usage")

#: Text that means the page in front of us is a challenge rather than the
#: page we asked for. Finding any of these ends the fetch: the answer to a
#: challenge is to stop, not to try harder.
CHALLENGE_MARKERS = (
    "cf_chl_opt",                 # Cloudflare's challenge bootstrap object
    "cf-browser-verification",
    "challenge-platform",
    "just a moment...",           # Cloudflare's interstitial title
    "checking your browser",
    "enable javascript and cookies to continue",
    "g-recaptcha",
    "recaptcha/api.js",
    "hcaptcha.com",
    "cf-turnstile",
    "captcha-delivery.com",       # DataDome
)

#: The control to click when the plugin does not name one. Ordered from the
#: most specific honest signal to the least: a form whose action is a download
#: beats a link that merely mentions the word.
DEFAULT_CONTROLS = (
    "form[action*='download'] button, form[action*='download'] input[type=submit]",
    "form#dl_form button, form#dl button",
    "a[href*='download']",
    "button#download, a#download",
)

#: How the plugin names its control, carried on the URL fragment. A fragment
#: is the one part of a URL that never reaches the server, so putting the
#: instruction there cannot leak into someone's access log or change the
#: request the site receives.
CONTROL_FRAGMENT = "romarr-click="


class Unavailable(RuntimeError):
    """The browser lane is not usable here, and why."""


class Refused(RuntimeError):
    """A challenge or a login stands between us and the file.

    Distinct from Unavailable because the fix is different and neither one is
    a bug: Unavailable means install something, Refused means this site is not
    ours to download from.
    """


def _playwright():
    """Import the driver, or say precisely what is missing.

    Deferred rather than top-level so `import romarr.app` costs nothing on an
    install without it -- the import is worth about a second and a hundred
    megabytes of node, and an install that only ever fetches plain URLs should
    pay neither.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise Unavailable(
            "the playwright driver is not installed -- `pip install "
            "playwright` and, for a browser on this host, `playwright "
            "install chromium`"
        ) from err
    return sync_playwright


def split_control(url: str) -> tuple[str, str]:
    """Separate a page URL from the control the plugin wants clicked.

    Returns (url, control). The control rides in the fragment because that is
    the only part of a URL guaranteed never to be sent: `#romarr-click=` is an
    instruction to this module, and it must not become part of the request the
    site sees.
    """
    head, sep, fragment = url.partition("#")
    if sep and fragment.startswith(CONTROL_FRAGMENT):
        return head, fragment[len(CONTROL_FRAGMENT):]
    return url, ""


def _open(pw, endpoint: str):
    """Get hold of a browser, from whichever of the three places it lives.

    The endpoint's scheme picks the shape, and the difference is not cosmetic
    -- it decides whether the downloaded bytes can get back here at all:

      * blank -- launch Chromium on this machine. Everything is local.
      * ws:// -- a `playwright run-server` on the browser's host. The driver
        runs THERE, owns the finished download, and streams it back over the
        same socket. This is the shape to use for a browser in another
        container, because it needs no shared filesystem.
      * http:// -- a bare `chromium --headless --remote-debugging-port`.
        Playwright's driver runs HERE and tells that Chromium to save into a
        path on this machine; a Chromium on another host duly creates that
        path on its own disk and the file never arrives. It works when the
        two share a directory, and `remote_dir` is how that is declared.
    """
    if not endpoint:
        return pw.chromium.launch(headless=True, args=list(LAUNCH_ARGS))
    if endpoint.startswith(("ws://", "wss://")):
        return pw.chromium.connect(endpoint)
    return pw.chromium.connect_over_cdp(endpoint)


def availability(endpoint: str = "") -> tuple[bool, str]:
    """Whether a browser can be driven from here, and why not when it cannot.

    Called by the status page and by `reachable()`, so it answers rather than
    raises: "browser mode is not configured" is a thing to display next to a
    fix, and an exception three layers down is not.
    """
    try:
        sync_playwright = _playwright()
    except Unavailable as err:
        return False, str(err)
    try:
        with sync_playwright() as pw:
            browser = _open(pw, endpoint)
            version = browser.version
            browser.close()
            return True, (f"connected to chromium {version}" if endpoint
                          else f"chromium {version}")
    except Exception as err:
        # Anything from "no browser binary" to "that host is not listening".
        # The class name plus the message is what tells those apart.
        return False, f"{type(err).__name__}: {err}"


def _challenge_in(text: str) -> str:
    lowered = (text or "").lower()
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _refuse_if_challenged(page, response) -> None:
    """Stop if what loaded is a challenge rather than the page.

    Read from the rendered content, not the status code: Cloudflare's managed
    challenge is served as a 403 on some sites and a 200 on others, so the
    status alone both misses it and cries wolf.
    """
    try:
        body = page.content()
    except Exception:
        body = ""
    marker = _challenge_in(body)
    if marker:
        raise Refused(
            f"the site answered with a bot challenge ({marker}); ROMarr does "
            "not defeat challenges, so this site cannot be downloaded from"
        )
    status = getattr(response, "status", 200) if response is not None else 200
    if status in (401, 403):
        raise Refused(
            f"the page itself answered HTTP {status} -- it is behind a login "
            "or a block, not a download control"
        )


def _click_target(page, control: str):
    """The element the plugin means, or the first ordinary download control.

    A plugin that knows its own site names its control. One that does not gets
    the ladder, which is deliberately short: guessing wildly at what to click
    on somebody's page is how an automated browser ends up pressing "delete".
    """
    if control.startswith("text:"):
        return page.get_by_text(control[5:], exact=False).first
    if control.startswith("css:"):
        return page.locator(control[4:]).first
    if control:
        return page.locator(control).first
    for selector in DEFAULT_CONTROLS:
        found = page.locator(selector).first
        try:
            if found.count():
                return found
        except Exception:
            continue
    raise Refused(
        "no download control found on the page -- the plugin must name one "
        f"with #{CONTROL_FRAGMENT}css:<selector>"
    )


def declared_user_agent(browser, token: str) -> str | None:
    """The browser's real User-Agent with ROMarr's token appended.

    Appended rather than replaced, on purpose. Replacing it hides that this is
    Chromium, which breaks sites that branch on the engine and edges towards
    misrepresenting the client; leaving the token off hides that the visit is
    automated at all. Both are true, so both are declared -- an operator
    reading their access log should be able to tell exactly what visited them.

    Learned from a blank page rather than hard-coded, because a pinned UA
    string becomes a lie about the version the moment Chromium updates.
    None when it cannot be read: the browser's own header is a better answer
    than a guess.
    """
    if not token:
        return None
    try:
        probe = browser.new_context()
        try:
            base = probe.new_page().evaluate("navigator.userAgent")
        finally:
            probe.close()
    except Exception as err:
        log.warning("could not read the browser's User-Agent: %s",
                    type(err).__name__)
        return None
    return f"{base} {token}" if base else None


def fetch(page_url: str, destination: Path, *, control: str = "",
          endpoint: str = "", ua_token: str = "", timeout: int = 120,
          remote_dir: str = "", path_mappings=()) -> Path:  # noqa: C901
    """Open the page, click the real control, keep what the browser downloads.

    `destination` is a directory. The saved file keeps the name the site gave
    it, because that name is metadata -- "Contra (USA).zip" tells the importer
    and the DAT check things a generated name does not.

    Raises Unavailable when there is no browser to drive and Refused when
    there is one and the site is not ours to take from.
    """
    sync_playwright = _playwright()
    url, fragment_control = split_control(page_url)
    control = control or fragment_control
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = _open(pw, endpoint)
        try:
            context = browser.new_context(
                accept_downloads=True,
                # Declared, not disguised: see declared_user_agent.
                user_agent=declared_user_agent(browser, ua_token),
            )
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded",
                                 timeout=timeout * 1000)
            _refuse_if_challenged(page, response)

            target = _click_target(page, control)
            with page.expect_download(timeout=timeout * 1000) as pending:
                target.click()
            download = pending.value
            saved = _keep(download, destination, remote_dir, path_mappings)
            context.close()
            return saved
        finally:
            browser.close()


def _keep(download, destination: Path, remote_dir: str, path_mappings) -> Path:
    """Put the downloaded file where ROMarr can import it.

    `save_as` covers two of the three shapes in _open: a local launch, where
    everything is on one disk, and a `ws://` Playwright server, which streams
    the finished artifact back over its own socket.

    It cannot cover the third. A bare `--remote-debugging-port` Chromium on
    another host was told to save into a path on THIS machine, so it made
    that path on its own disk and the file is over there. That is recoverable
    only if the two share a directory, which is what `remote_dir` declares --
    translated with the same mapping table the download clients already use.
    Without one there is no way to move the bytes, and saying so beats a
    FileNotFoundError raised from inside somebody else's driver.
    """
    name = download.suggested_filename or "download.bin"
    target = destination / name
    try:
        download.save_as(str(target))
        return target
    except Exception as err:
        if not remote_dir:
            raise Unavailable(
                "the browser downloaded the file onto its own host and this "
                "one cannot read it -- give the browser a download directory "
                f"both containers can see ({type(err).__name__})"
            ) from err
    # The browser wrote it somewhere we were told we can also read.
    from .library import map_remote_path

    try:
        reported = download.path()
    except Exception:
        reported = os.path.join(remote_dir, name)
    local = map_remote_path(str(reported), path_mappings)
    if not Path(local).exists():
        raise Unavailable(
            f"the browser reported {reported}, which does not exist here -- "
            "check the remote path mapping for the browser's download "
            "directory"
        )
    shutil.copy2(str(local), str(target))
    return target
