"""Screenshot the Friends page, including the invitation panel.

Signs in with the API key rather than the password, so this can run from a
host that holds the key but not the operator's password.

    python docs/shot_friends.py <base-url> <api-key>
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE, KEY = sys.argv[1], sys.argv[2]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900},
                            device_scale_factor=2)
    page.goto(BASE + "/login")
    page.evaluate("""async ([base, key]) => {
        await fetch(base + '/api/v1/login', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: '', apikey: key, totp: ''})});
    }""", [BASE, KEY])

    page.goto(BASE + "/#peers")
    page.evaluate("go && go('peers')")
    time.sleep(2)

    # Open the invitation panel -- the part that renders a textarea.
    page.click("#p-invite")
    page.wait_for_selector("#p-copy", timeout=15000)

    # The panel is showing a live single-use secret. The screenshot goes to a
    # public repo, so redact it in the DOM before the shutter: the shape of
    # the blob is what the image is for, and it is still the real page.
    page.evaluate("""() => {
        const t = document.querySelector('#p-out textarea');
        const blob = JSON.parse(t.value);
        blob.secret = '<one-time secret -- redacted for this screenshot>';
        t.value = JSON.stringify(blob);
    }""")
    time.sleep(1)

    page.screenshot(path="docs/img/friends.png", full_page=True)
    print("captured docs/img/friends.png")
    browser.close()
