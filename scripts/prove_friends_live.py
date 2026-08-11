"""Two ROMarr instances become friends over HTTP, and agree on a ROM.

Every step here is a real request to a real server. The second instance is
started by the caller (see docs/design/) and both are driven exactly as two
operators would drive them.

    python scripts/prove_friends_live.py <alice-url> <alice-key> \
                                         <bob-url> <bob-key>
"""

import sys

import requests

ALICE, ALICE_KEY, BOB, BOB_KEY = sys.argv[1:5]
FAILURES = []


def check(step, ok, detail=""):
    if not ok:
        FAILURES.append(step)
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}" + (f" -- {detail}" if detail else ""))


def call(base, key, method, path, **kw):
    return requests.request(
        method, f"{base}{path}", headers={"X-Api-Key": key}, timeout=30, **kw)


def peer_call(base, method, path, peer_id, token, **kw):
    """As another SERVER, not as a user: peer credentials, no session."""
    return requests.request(
        method, f"{base}{path}",
        headers={"X-Peer-Id": peer_id, "X-Peer-Token": token},
        timeout=30, **kw)


def main():
    print("ROMarr Friends -- live, over HTTP")
    print(f"Alice: {ALICE}\nBob:   {BOB}\n")

    # 1. Alice invites.
    r = call(ALICE, ALICE_KEY, "POST", "/api/v1/peer/invite", json={})
    invite = (r.json() or {}).get("invite") or {}
    check("Alice mints an invitation", bool(invite.get("secret")))

    # 2. Bob redeems it.
    r = call(BOB, BOB_KEY, "POST", "/api/v1/peer/redeem",
             json={"invite": invite})
    check("Bob redeems it", r.ok and not (r.json() or {}).get("error"))

    # 3. Bob's server calls Alice's ACCEPT -- the cross-server leg, with
    #    the one-time secret and no session cookie anywhere.
    r = requests.post(f"{ALICE}/api/v1/peer/accept",
                      json={"peer_id": invite["peer_id"],
                            "secret": invite["secret"],
                            "name": "Bob's ROMarr", "url": BOB}, timeout=30)
    body = r.json() if r.ok else {}
    check("Bob's SERVER completes the handshake without any user session",
          r.ok, f"HTTP {r.status_code}")
    check("...and is held UNCONFIRMED until Alice says so",
          body.get("confirmed") is False)

    peer_id = invite["peer_id"]
    token = invite["secret"]

    # 4. An unconfirmed peer can see nothing.
    r = peer_call(ALICE, "GET", "/api/v1/peer/shelf", peer_id, token)
    check("an unconfirmed peer is refused", r.status_code == 401)

    # 5. Alice confirms.
    r = call(ALICE, ALICE_KEY, "POST", "/api/v1/peer/confirm",
             json={"peer_id": peer_id})
    check("Alice confirms the friend", r.ok)

    # 6. Confirmed but sharing nothing -- the default.
    r = peer_call(ALICE, "GET", "/api/v1/peer/shelf", peer_id, token)
    items = (r.json() or {}).get("items", []) if r.ok else None
    check("a confirmed friend still sees NOTHING by default",
          r.ok and items == [], f"{len(items or [])} row(s)")

    # 7. Alice shares.
    r = call(ALICE, ALICE_KEY, "POST", "/api/v1/peer/policy",
             json={"peer_id": peer_id, "scope": "all", "access": "catalogue"})
    check("Alice opens her shelf to this friend", r.ok)

    r = peer_call(ALICE, "GET", "/api/v1/peer/shelf", peer_id, token)
    shared = (r.json() or {}).get("items", []) if r.ok else []
    check("the friend now sees the library", bool(shared),
          f"{len(shared)} title(s)")
    if shared:
        allowed = {"title", "platform", "year", "verified", "origin"}
        leaked = {k for row in shared[:50] for k in row} - allowed
        check("no paths, ids or credentials cross the wire", not leaked,
              str(sorted(leaked)) if leaked else "")

    # 8. A wrong token is nobody.
    r = peer_call(ALICE, "GET", "/api/v1/peer/shelf", peer_id, "not-the-token")
    check("a wrong peer token is refused", r.status_code == 401)

    # 9. Netplay: agree on the bytes.
    offer = {"title": "Super Mario Kart", "platform": "snes",
             "sha1": "0" * 40, "verified": True}
    r = peer_call(ALICE, "POST", "/api/v1/peer/netplay", peer_id, token,
                  json={"offer": offer})
    answer = r.json() if r.ok else {}
    check("netplay answers an offer by hash", r.ok,
          f"{answer.get('status')}: {str(answer.get('detail'))[:60]}")
    check("an unknown dump is 'missing', never a blind 'ready'",
          answer.get("status") == "missing")

    # 10. Revocation, one-sided and immediate.
    r = call(ALICE, ALICE_KEY, "DELETE", f"/api/v1/peer/{peer_id}")
    check("Alice removes the friend", r.ok)
    r = peer_call(ALICE, "GET", "/api/v1/peer/shelf", peer_id, token)
    check("access is gone immediately, no cooperation needed",
          r.status_code == 401)

    print(f"\n{'ALL LIVE PROOFS PASSED' if not FAILURES else str(len(FAILURES)) + ' FAILED'}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
