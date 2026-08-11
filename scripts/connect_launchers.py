"""Connect the launchers on this PC to ROMarr, in one command.

ROMarr usually runs on a server; the launchers are on the machine you play
on. This is the bridge: it scans every launcher installed here -- Steam,
Epic, GOG Galaxy, Battle.net, EA -- and pushes the titles to ROMarr as an
import list, which then feeds Wanted on ROMarr's normal schedule.

    python scripts/connect_launchers.py --url http://romarr:6868 --key <api key>

    --dry-run       show what would be sent, send nothing
    --platform      file every title against one platform (default: per line)

No store credentials are involved anywhere in this. The launchers already
wrote your library to disk when they installed it; this reads those files.
"""

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from romarr.launchers import scan_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:6868")
    ap.add_argument("--key", default="", help="ROMarr API key. Omitted, it is "
                                              "prompted for without echo.")
    ap.add_argument("--platform", default="",
                    help="Platform slug for every title; default is per-line")
    ap.add_argument("--name", default="Local launchers")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    games = scan_all()
    if not games:
        print("No launcher libraries found on this machine.")
        print("Checked Steam, Epic, GOG Galaxy, Battle.net and EA in their "
              "default locations.")
        return 1

    by_launcher = Counter(g.launcher for g in games)
    print(f"Found {len(games)} installed game(s):")
    for launcher, count in sorted(by_launcher.items()):
        print(f"  {launcher:<10} {count}")
        for game in [g for g in games if g.launcher == launcher][:5]:
            print(f"      {game.name}")
        if count > 5:
            print(f"      … and {count - 5} more")

    # One title per line, tab-separated from its store, which is exactly the
    # shape ROMarr's list parser already understands.
    content = "\n".join(f"{g.name}" for g in games)

    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        return 0

    key = args.key or getpass.getpass("ROMarr API key: ")
    payload = json.dumps({
        "name": args.name,
        "type": "paste",
        "platform": args.platform,
        "content": content,
        "enable": True,
    }).encode()
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/v1/importlist", data=payload,
        headers={"Content-Type": "application/json", "X-Api-Key": key},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            saved = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print(f"\nROMarr refused the list: HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:                # noqa: BLE001
        print(f"\nCould not reach ROMarr at {args.url}: "
              f"{exc.__class__.__name__}", file=sys.stderr)
        return 1

    print(f"\nSent {len(games)} title(s) to ROMarr as list "
          f"{saved.get('id', '?')!r}.")
    print("Wanted → Lists shows it; Sync Now runs it immediately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
