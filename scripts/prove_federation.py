"""Prove two ROMarr instances can be friends, against real servers.

Not a unit test with fakes -- two Federation states exchanging a real
invitation, then the projection filtered by a real policy over a shelf read
from a REAL second RomM. This is the evidence behind "confirm they can be
friends".

    python scripts/prove_federation.py <romm2-url> [user] [pass]
"""

import sys

sys.path.insert(0, ".")

from romarr.clients import Romm, RommConfig          # noqa: E402
from romarr.federation import Federation             # noqa: E402

FAILURES = []


def check(step, ok, detail=""):
    if not ok:
        FAILURES.append(step)
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}" + (f" -- {detail}" if detail else ""))


def main():
    url = sys.argv[1]
    user = sys.argv[2] if len(sys.argv) > 2 else ""
    password = sys.argv[3] if len(sys.argv) > 3 else ""

    print("ROMarr federation proof")
    print(f"second RomM: {url}")

    # --- the second server is real and reachable ---------------------------
    romm2 = Romm(RommConfig(base_url=url, username=user, password=password))
    reachable = romm2.reachable()
    check("the second RomM answers", reachable)

    shelf2 = []
    if reachable:
        try:
            shelf2 = romm2.games(limit=50)
        except Exception as exc:                     # noqa: BLE001
            check("read its library", False, exc.__class__.__name__)
        else:
            check("read its library", True, f"{len(shelf2)} game(s)")

    # --- two instances become friends --------------------------------------
    alice = Federation("Alice's ROMarr", "http://192.168.0.182:7878")
    bob = Federation("Bob's ROMarr", url)

    invite = alice.invite(name="Alice's ROMarr")
    check("Alice mints an invitation", bool(invite.secret))

    bob_side = bob.redeem(invite.blob())
    check("Bob redeems it out of band", bob_side.confirmed)

    peer = alice.accept(invite.peer_id, invite.secret, name="Bob's ROMarr",
                        url=url)
    check("Alice accepts, held UNCONFIRMED until she says so",
          not peer.confirmed)
    check("an unconfirmed peer cannot act",
          alice.authenticate(peer.peer_id, peer.token) is None)

    alice.confirm(peer.peer_id)
    check("after confirmation the peer authenticates",
          alice.authenticate(peer.peer_id, peer.token) is peer)
    check("the invitation is single-use",
          _raises(lambda: alice.accept(invite.peer_id, invite.secret)))

    # --- sharing is a separate decision ------------------------------------
    check("a fresh peer sees nothing", alice.project(peer, shelf2) == [])

    peer.scope = "all"
    shared = alice.project(peer, shelf2)
    check("with scope=all the shelf is shared",
          len(shared) == len(shelf2), f"{len(shared)} row(s)")
    if shared:
        leaks = [k for row in shared for k in row
                 if k not in {"title", "platform", "year", "verified", "origin"}]
        check("the projection leaks no paths, ids or credentials", not leaks)

    # --- revocation is one-sided and immediate -----------------------------
    alice.revoke(peer.peer_id)
    check("revoking is immediate and needs no cooperation",
          alice.authenticate(peer.peer_id, peer.token) is None)
    check("...and Bob still holds his own side",
          list(bob.peers.values())[0].confirmed)

    print(f"\n{'ALL PROOFS PASSED' if not FAILURES else str(len(FAILURES)) + ' FAILED'}")
    sys.exit(1 if FAILURES else 0)


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    main()
