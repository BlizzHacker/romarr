"""Screenshot the live ROMarr UI for the README. Run from the repo root."""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://192.168.0.182:7878"
PASSWORD = sys.argv[1]
OUT = "docs/img"

PAGES = [
    ("library", "library.png", 2.5),
    ("search", "interactive-search-live.png", 0),   # search run below
    ("lists", "lists.png", 1.5),
    ("collections", "collections.png", 1.5),
    ("stats", "stats.png", 1.5),
    ("tasks", "tasks.png", 1.5),
    ("logs", "logs.png", 3),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900},
                            device_scale_factor=2)
    page.goto(BASE + "/login")
    page.fill("#password", PASSWORD)
    page.click("#go")
    page.wait_for_url(BASE + "/**")
    time.sleep(2)

    for name, filename, wait in PAGES:
        page.goto(f"{BASE}/#{name}")
        page.evaluate(f"go && go('{name}')")
        time.sleep(wait or 1)
        if name == "search":
            page.fill("#s-name", "Super Metroid")
            page.select_option("#s-plat", "snes")
            page.click("#s-go")
            # A real search against live indexers takes a moment.
            page.wait_for_selector("#s-out table", timeout=90000)
            time.sleep(1)
        page.screenshot(path=f"{OUT}/{filename}")
        print("shot", filename)

    browser.close()
