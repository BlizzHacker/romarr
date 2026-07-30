"""Ask the live fixtures the four questions, using Romarr's own backend code.

tests/test_libraries.py is mocked, which is the right default -- it is fast and
it pins the request shapes a real server was observed to want. What a mock
cannot do is notice that the server changed its mind. This does that.

    docker compose -f contrib/test-backends/docker-compose.yml up -d
    python contrib/test-backends/verify.py
    python contrib/test-backends/verify.py --host 192.168.0.94

A fresh fixture holds no games, so a count of zero is a pass. What is under
test is that every call is accepted and decodes: a changed method, a dropped
filter field or a reframed grpc-web response fails loudly here and is entirely
invisible to the mocks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from romarr.libraries import build_library  # noqa: E402  (after sys.path)

# Ports match docker-compose.yml in this directory, chosen to miss RomM's 8080.
GASEOUS_PORT, RETROM_PORT = 5198, 5101

# Throwaway credentials for a throwaway container. Gaseous ships with no
# account at all -- an empty Users table and no registration endpoint -- so one
# has to be created before it will answer anything. See bootstrap_gaseous.
GASEOUS_USER = "romarr@example.com"
GASEOUS_PASSWORD = "Romarr-Test-1"


def bootstrap_gaseous(base_url: str) -> None:
    """Create the first Gaseous account, if this instance has none.

    Gaseous exposes an unauthenticated POST /api/v1.1/FirstSetup/0 while its
    Users table is empty; the web UI's own first-run wizard uses it. Once a
    user exists the endpoint stops accepting, so calling it unconditionally is
    safe and keeps the fixture reproducible from an empty volume rather than
    needing somebody to click through a wizard.

    Without this, every call 302s to /Identity/Account/Login, which then 404s
    under /api -- a failure that reads as a wrong path rather than no session.
    """
    try:
        r = requests.post(
            f"{base_url}/api/v1.1/FirstSetup/0",
            json={"userName": GASEOUS_USER, "email": GASEOUS_USER,
                  "password": GASEOUS_PASSWORD,
                  "confirmPassword": GASEOUS_PASSWORD},
            timeout=30,
        )
    except requests.RequestException as err:
        print(f"  bootstrap    unreachable ({err.__class__.__name__})")
        return
    # Password must be at least 10 characters, per the wizard's own check.
    # Once a user exists the route is gone entirely, so 404 is the expected
    # answer on every run after the first.
    if r.status_code == 404:
        print("  bootstrap    already set up")
    elif r.ok:
        print(f"  bootstrap    created {GASEOUS_USER}")
    else:
        print(f"  bootstrap    HTTP {r.status_code} {r.text[:120]}")


def check(kind: str, url: str, env: dict[str, str]) -> bool:
    """Run one backend through the whole protocol, reporting each answer."""
    print(f"\n{kind} -- {url}")
    if kind == "gaseous":
        bootstrap_gaseous(url)
    lib = build_library(kind, {"LIBRARY_URL": url, **env})
    ok = True

    def ask(question: str, call):
        nonlocal ok
        try:
            answer = call()
        except Exception as err:  # noqa: BLE001 -- any failure is a finding
            print(f"  {question:<12} FAIL  {err.__class__.__name__}: {err}")
            ok = False
        else:
            print(f"  {question:<12} {answer}")

    ask("configured", lambda: lib.configured)
    ask("reachable", lib.reachable)
    ask("count", lib.count)
    ask("games", lambda: f"{len(lib.games(limit=5))} returned")
    # Optional everywhere in Romarr, so a False here is reported rather than
    # fatal -- but an exception still is.
    ask("rescan", lib.rescan)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost",
                        help="host running the fixtures (default: localhost)")
    args = parser.parse_args()

    backends = (
        ("gaseous", GASEOUS_PORT, {"LIBRARY_USERNAME": GASEOUS_USER,
                                   "LIBRARY_PASSWORD": GASEOUS_PASSWORD}),
        # Retrom needs no credentials on a default install.
        ("retrom", RETROM_PORT, {}),
    )
    results = {kind: check(kind, f"http://{args.host}:{port}", env)
               for kind, port, env in backends}

    print()
    for kind, ok in results.items():
        print(f"{kind}: {'ok' if ok else 'FAILED'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
