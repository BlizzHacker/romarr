"""Screenshot a friend's library, and a netplay verdict, from the live UI.

    python docs/shot_friend_library.py <base-url> <api-key>
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE, KEY = sys.argv[1], sys.argv[2]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 1000},
                            device_scale_factor=2)
    page.goto(BASE + "/login")
    page.evaluate("""async ([base, key]) => {
        await fetch(base + '/api/v1/login', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: '', apikey: key, totp: ''})});
    }""", [BASE, KEY])

    page.goto(BASE + "/#peers")
    page.evaluate("go && go('peers')")
    page.wait_for_selector("[data-pbrowse]", timeout=20000)
    page.click("[data-pbrowse]")
    page.wait_for_selector("[data-fplay]", timeout=60000)
    time.sleep(1)
    page.screenshot(path="docs/img/friend-library.png", full_page=False)
    print("captured docs/img/friend-library.png")

    # And a netplay verdict: offer the game both sides were seeded with.
    page.fill("#fs-q", "Proof Cart")
    page.dispatch_event("#fs-q", "change")
    time.sleep(2)
    verdict = page.evaluate("""async () => {
        const r = await fetch('/api/v1/friends/netplay', {method:'POST',
            headers:{'content-type':'application/json'},
            body: JSON.stringify({peer_id: FSHELF.peer,
                                  title:'Proof Cart', platform:'snes'})});
        const body = await r.json();
        document.querySelector('#fs-out').innerHTML = netplayVerdict(body);
        return body.status;
    }""")
    print("netplay verdict rendered:", verdict)
    time.sleep(1)
    page.screenshot(path="docs/img/netplay.png", full_page=False)
    print("captured docs/img/netplay.png")
    browser.close()
