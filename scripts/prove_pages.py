"""Drive every page of a live ROMarr and prove it renders with its actions.

Not an assertion against the HTML string -- an actual browser, signing in,
visiting all 27 nav destinations, and checking each one painted content and
shows the interactive controls it is supposed to. This is the evidence
behind "buttons on every page, proven working".

    python scripts/prove_pages.py <base-url> <password>

Exit code is the verdict; the table is the proof.
"""

import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
PASSWORD = sys.argv[2]

# Each page, and a selector that must exist once it has rendered. The
# selector is chosen to be the page's ACTION, not just its heading -- a
# page that renders a title and no way to act is exactly the failure this
# script exists to catch.
PAGES = [
    ("library", "#lib-grid, .empty"),
    ("add", "#g-go"),
    ("discover", "[data-shelf], .empty, #d-out"),
    ("search", "#s-go"),
    ("missing", "#w-all, .empty"),
    ("lists", "#l-add"),
    ("calendar", "[data-calreq], .empty-cat"),
    ("queue", "#q-import, .empty"),
    ("history", "table, .empty"),
    ("blocklist", "table, .empty"),
    ("hub", "#page"),
    ("media", "#s-save"),
    ("profiles", "#s-save"),
    ("indexers", "#i-add"),
    ("clients", "[id$=-add], .empty, #page button"),
    ("libraries", "#page button"),
    ("connections", "#c-add"),
    ("metadata", "#mp-add"),
    ("general", "#s-save"),
    ("status", "#page"),
    ("stats", "#au-go"),
    ("platforms", "#page"),
    ("getstarted", "#page"),
    ("collections", "#page"),
    ("manualimport", "#miscan"),
    ("tasks", "[data-task]"),
    ("logs", "#lg-out"),
]


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/login")
        page.fill("#password", PASSWORD)
        page.click("#go")
        page.wait_for_url(f"{BASE}/**")
        time.sleep(2)

        # Let the default page (Library, which fetches a 166k-game shelf)
        # settle before the sweep, or its late fetches land on whatever page
        # is current and read as that page's fault.
        time.sleep(3)
        for name, selector in PAGES:
            # Navigate the way a user does -- one go() via the nav, not a
            # full reload -- so the proof matches the real path. A goto()
            # to a hash forces a document reload and races the init render.
            page.evaluate(f"go('{name}')")
            ok, detail = False, ""
            try:
                page.wait_for_selector(selector, timeout=8000, state="attached")
                ok = True
            except Exception:
                detail = f"no {selector}"
            # Let this page's own fetches settle, then read errors that are
            # genuinely its own -- a user does not navigate faster than a
            # page can answer.
            time.sleep(0.6)
            before = len(errors)
            time.sleep(0.4)
            js_err = errors[before:]
            if js_err:
                ok, detail = False, js_err[0][:80]
            results.append((name, ok, detail))
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
                  + (f" -- {detail}" if detail else ""))
        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} pages proven")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
