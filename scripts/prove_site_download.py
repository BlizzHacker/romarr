"""Prove the ROM-site download backend against a real site, not a fake.

    python scripts/prove_site_download.py            # both modes
    python scripts/prove_site_download.py --direct   # no browser needed

Archive.org is the target because it is already part of this project's
ecosystem, its robots.txt permits everything used here, and the file below is
a 7 KB NASA media zip -- public-domain government imagery, deliberately not
anybody's game. The point being proved is the transport, so the smallest
honest file proves it best.

Both modes fetch THE SAME FILE by two different routes and the proof is that
the SHA-256 matches:

  * direct  -- an ordinary GET of the published file URL.
  * browser -- a real headless Chromium opens Archive.org's own file listing
    for the item and clicks the real link on it. Every header that goes out,
    `Referer` included, is one the browser genuinely produced because the
    browser genuinely was on that page. That is the difference between this
    and forging a header, and it is the only reason the browser mode exists.

Nothing here answers a CAPTCHA, defeats a challenge or signs in anywhere. A
site that would require any of those is reported as unavailable; see
romarr/browser.py.
"""

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ITEM = "dishturntable-197959"
FILENAME = "360494main_turntable_E.zip"
FILE_URL = f"https://archive.org/download/{ITEM}/{FILENAME}"
#: The site's own file listing for the item. A real page with a real link on
#: it, which is what the browser mode is for.
LISTING_URL = f"https://archive.org/download/{ITEM}/"
#: Which link on it. Carried in the fragment because a fragment never reaches
#: the server -- see browser.split_control.
CONTROL = f"#romarr-click=css:a[href$='{FILENAME}']"

RESULTS = []


def check(step: str, ok: bool, detail: str = ""):
    RESULTS.append((step, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}" + (f" -- {detail}" if detail else ""))


def digest(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def prove(mode: str, url: str, root: Path) -> tuple[int, str] | None:
    from romarr.downloaders import build_client

    print(f"\n{mode} mode")
    save = root / mode
    client = build_client({"type": mode, "name": f"proof-{mode}",
                           "save_path": str(save), "delay": 5})
    check("reachable()", client.reachable(), client.detail)
    if not client.reachable():
        return None
    check("add() queues the job", client.add(url, name="NASA turntable"))
    done = client.completed()
    if not done:
        rows = client._read()
        check("completed() produced the file", False,
              "; ".join(r.get("detail", "") for r in rows) or "nothing queued")
        return None
    path = Path(done[0]["content_path"])
    size, sha = digest(path)
    check("completed() produced the file", True,
          f"{path.name} {size} bytes sha256={sha}")
    check("the queue row carries the release title, so it can be imported",
          done[0]["name"] == "NASA turntable", done[0]["name"])
    return size, sha


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct", action="store_true",
                        help="skip the browser mode")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="romarr-site-proof-"))
    print(f"ROMarr ROM-site download proof -- {FILE_URL}")
    print(f"working in {root}")

    direct = prove("direct", FILE_URL, root)
    browser = None
    if not args.direct:
        browser = prove("browser", LISTING_URL + CONTROL, root)

    if direct and browser:
        print()
        check("both routes produced byte-identical files",
              direct == browser, f"{direct[1]} == {browser[1]}")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
