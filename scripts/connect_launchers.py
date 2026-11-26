"""Connect the launchers on this PC to ROMarr, in one command.

Installations are reported for the device running this script. ROMarr shows
the snapshot in Wanted > Lists. Use --platform for the older Wanted workflow.

    python scripts/connect_launchers.py --url https://romarr.example

Store credentials and filesystem paths stay on this computer. The ROMarr API
key is prompted for without echo when omitted.
"""

import argparse
import getpass
import json
import platform
import re
import socket
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
                    help="Send titles to Wanted using this platform slug")
    ap.add_argument("--name", default=socket.gethostname(), help="Name shown in Lists")
    ap.add_argument("--device-id", default="", help="Stable identifier for this device")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    games = scan_all()
    if not games:
        print("No launcher libraries found on this machine.")
        print("Checked Steam, Epic, GOG Galaxy, Battle.net, EA and Amazon in their "
              "default locations.")
        if args.platform or args.dry_run:
            return 1

    by_launcher = Counter(g.launcher for g in games)
    print(f"Found {len(games)} launcher game(s):")
    for launcher, count in sorted(by_launcher.items()):
        print(f"  {launcher:<10} {count}")
        for game in [g for g in games if g.launcher == launcher][:5]:
            print(f"      {game.name}")
        if count > 5:
            print(f"      … and {count - 5} more")

    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        return 0

    key = args.key or getpass.getpass("ROMarr API key: ")
    if args.platform:
        endpoint = "/api/v1/importlist"
        data = {"name": args.name, "type": "paste", "platform": args.platform,
                "content": "\n".join(game.name for game in games), "enable": True}
    else:
        endpoint = "/api/v1/lists/device"
        device_id = args.device_id or re.sub(r"[^a-z0-9_.-]", "-", socket.gethostname().lower())
        os_name = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), "other")
        data = {"name": args.name, "device_id": device_id, "os": os_name, "games": [
            {"name": game.name, "provider": game.launcher, "store_id": game.store_id,
             "installed": game.installed} for game in games
        ]}
    request = urllib.request.Request(
        args.url.rstrip("/") + endpoint, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    request.add_unredirected_header("X-Api-Key", key)
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

    if args.platform:
        print(f"\nSent {len(games)} title(s) to import list {saved.get('id', '?')!r}.")
    else:
        print(f"\nReported {saved['count']} game(s) for {args.name}.")
    print("See Wanted > Lists in ROMarr.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
