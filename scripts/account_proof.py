"""Prove the account connectors against YOUR real accounts.

The maintainer cannot hold a Steam key, an Xbox account, a PSN token, an
itch.io key and a public GOG profile on your behalf -- so this is the proof
harness for the rows of docs/PROOF.md that only an account holder can turn
green. One command per service, using the exact fetcher ROMarr's list sync
uses:

    python scripts/account_proof.py steam  --steam-id 7656119... --api-key ...
    python scripts/account_proof.py gog    --username your_public_profile
    python scripts/account_proof.py xbox   --openxbl-key ...
    python scripts/account_proof.py psn    --npsso ...
    python scripts/account_proof.py itchio --api-key ...

Output is the first titles of your library through ROMarr's own code path.
If it prints your games, the connector works against the live service today
-- paste the (redacted) output into an issue titled "account proof: <service>"
and that row of PROOF.md gets your name on it.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from romarr.lists import fetch_entries  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("service",
                    choices=["steam", "gog", "xbox", "psn", "itchio"])
    ap.add_argument("--steam-id", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--username", default="")
    ap.add_argument("--openxbl-key", default="")
    ap.add_argument("--npsso", default="")
    ap.add_argument("--source", default="owned")
    args = ap.parse_args()

    cfg = {
        "steam": {"type": "steam", "steam_id": args.steam_id,
                  "api_key": args.api_key, "source": args.source},
        "gog": {"type": "gog", "gog_username": args.username},
        "xbox": {"type": "xbox", "openxbl_key": args.openxbl_key},
        "psn": {"type": "psn", "npsso": args.npsso},
        "itchio": {"type": "itchio", "itchio_key": args.api_key},
    }[args.service]

    print(f"{args.service} account proof -- "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    try:
        entries = fetch_entries(cfg)
    except Exception as exc:
        print(f"  [FAIL] {exc.__class__.__name__}: {exc}")
        sys.exit(1)
    if not entries:
        print("  [FAIL] the service answered but the library came back "
              "empty -- check the credential and any privacy settings")
        sys.exit(1)
    print(f"  [PASS] {len(entries)} titles through ROMarr's own fetcher")
    for entry in entries[:10]:
        print(f"    - {entry.game}")
    if len(entries) > 10:
        print(f"    … and {len(entries) - 10} more")


if __name__ == "__main__":
    main()
