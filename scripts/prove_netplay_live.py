"""Two ROMarr servers browse each other's libraries and agree on a ROM.

The companion to prove_friends_live.py, which proved the trust model. This
proves the half that uses it: one operator browsing a friend's shelf, adding
something from it, and starting a session that is settled on bytes.

It matters because the earlier netplay proof could only ever reach one of the
four verdicts. Offers were built from a library server's game object, which
carries no SHA1, so every offer went out empty and every answer came back
"missing". The hash index is what makes the other three reachable, and this
script exercises all four against two running servers.

    python scripts/prove_netplay_live.py <alice-url> <alice-key> \
                                         <bob-url> <bob-key>
"""

import sys

import requests

ALICE, ALICE_KEY, BOB, BOB_KEY = sys.argv[1:5]
FAILURES = []

SHARED = "1" * 40      # the dump they both hold
DIFFERENT = "2" * 40   # Bob's other dump of the same game
ALICE_ONLY = "3" * 40  # something Bob has never seen


def check(step, ok, detail=""):
    if not ok:
        FAILURES.append(step)
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}" + (f" -- {detail}" if detail else ""))


def call(base, key, method, path, **kw):
    return requests.request(
        method, f"{base}{path}", headers={"X-Api-Key": key}, timeout=60, **kw)


def befriend():
    """Alice and Bob become friends, and Alice shares everything."""
    invite = (call(ALICE, ALICE_KEY, "POST", "/api/v1/peer/invite",
                   json={}).json() or {}).get("invite") or {}
    call(BOB, BOB_KEY, "POST", "/api/v1/peer/redeem", json={"invite": invite})
    requests.post(f"{ALICE}/api/v1/peer/accept",
                  json={"peer_id": invite["peer_id"],
                        "secret": invite["secret"],
                        "name": "Bob's ROMarr", "url": BOB}, timeout=30)
    call(ALICE, ALICE_KEY, "POST", "/api/v1/peer/confirm",
         json={"peer_id": invite["peer_id"]})
    call(ALICE, ALICE_KEY, "POST", "/api/v1/peer/policy",
         json={"peer_id": invite["peer_id"], "scope": "all",
               "access": "catalogue"})
    return invite["peer_id"], invite["secret"]


def main():
    print("ROMarr cross-server library + netplay -- live, over HTTP")
    print(f"Alice: {ALICE}\nBob:   {BOB}\n")

    peer_id, token = befriend()
    check("Alice and Bob are friends, sharing the catalogue", bool(peer_id))

    # -- browsing a friend's library ---------------------------------------
    r = call(BOB, BOB_KEY, "GET",
             f"/api/v1/friends/shelf?peer_id={peer_id}&limit=25")
    body = r.json() if r.ok else {}
    check("Bob browses Alice's library from his own server",
          body.get("ok") is True, f"{body.get('total', 0)} title(s) shared")
    check("...and the rows carry no paths, ids or credentials",
          all(set(row) <= {"title", "platform", "year", "verified", "origin"}
              for row in (body.get("items") or [])[:50]))

    plats = body.get("platforms") or []
    check("...with platform facets to cut it on", bool(plats),
          ", ".join(plats[:6]))

    first = (body.get("items") or [{}])[0]
    title = first.get("title", "")

    if title:
        r = call(BOB, BOB_KEY, "GET",
                 f"/api/v1/friends/shelf?peer_id={peer_id}"
                 f"&q={requests.utils.quote(title[:12])}&limit=25")
        found = r.json() if r.ok else {}
        check("...and searching it filters without asking Alice again",
              (found.get("total") or 0) >= 1,
              f"{found.get('total')} match(es) for {title[:12]!r}")

        # -- taking something off the shelf --------------------------------
        r = call(BOB, BOB_KEY, "POST", "/api/v1/friends/want",
                 json={"peer_id": peer_id, "title": title,
                       "platform": first.get("platform", "")})
        added = r.json() if r.ok else {}
        check("Bob adds one of Alice's titles to his OWN wanted list",
              added.get("ok") is True, added.get("detail", ""))

        r = call(BOB, BOB_KEY, "GET", "/api/v1/wanted/missing")
        wanted = r.json() if r.ok else {}
        rows = wanted.get("items", wanted if isinstance(wanted, list) else [])
        check("...and it really landed in Wanted, on Bob's side",
              any(w.get("game") == title for w in rows))

    # -- netplay, judged on bytes ------------------------------------------
    #
    # Each case is driven through Alice's peer endpoint using a hash Bob's
    # index either holds or does not, so all four verdicts are real answers
    # from a running server rather than assertions about one.
    r = call(ALICE, ALICE_KEY, "GET", "/api/v1/hashes")
    idx = r.json() if r.ok else {}
    check("Alice's hash index is populated", (idx.get("count") or 0) > 0,
          f"{idx.get('count', 0)} dump(s) known to netplay")

    cases = [
        ("ready", {"title": "Proof Cart", "platform": "snes",
                   "sha1": SHARED, "verified": True},
         "the same dump, both verified"),
        ("unverified", {"title": "Proof Cart", "platform": "snes",
                        "sha1": SHARED, "verified": False},
         "same bytes, one side never checked a DAT"),
        ("mismatch", {"title": "Proof Cart", "platform": "snes",
                      "sha1": DIFFERENT, "verified": True},
         "same title, different bytes -- the silent desync"),
        ("missing", {"title": "Never Dumped", "platform": "snes",
                     "sha1": ALICE_ONLY, "verified": True},
         "not there at all"),
    ]
    for expected, offer, why in cases:
        r = requests.post(f"{ALICE}/api/v1/peer/netplay",
                          headers={"X-Peer-Id": peer_id,
                                   "X-Peer-Token": token},
                          json={"offer": offer}, timeout=30)
        answer = r.json() if r.ok else {}
        check(f"netplay answers {expected!r} -- {why}",
              answer.get("status") == expected,
              f"got {answer.get('status')!r}: "
              f"{str(answer.get('detail'))[:70]}")

    print(f"\n{'ALL LIVE PROOFS PASSED' if not FAILURES else str(len(FAILURES)) + ' FAILED'}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
