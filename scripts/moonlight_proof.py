"""Prove the Moonlight-host integration against real Wolf and Sunshine.

Run it on a host that can reach them. Wolf's API is on a UNIX socket, so the
socket half only proves anything when this runs on the Wolf machine itself:

    .venv/bin/python scripts/moonlight_proof.py \
        --wolf 192.168.0.128 --wolf-socket /tmp/sockets/wolf.sock \
        --wolf-api-url http://127.0.0.1:8080 \
        --sunshine 192.168.0.119 --sunshine-auth romarr:proofpass123 \
        --moonlight /root/squashfs-root/AppRun

Everything here drives `MoonlightHost` -- the same class `app.py` builds from
`MOONLIGHT_HOST` -- rather than talking to the hosts directly, because the
point is to prove ROMarr's half of the conversation, not the hosts'.

`--moonlight` is the one that matters most. Point it at a real moonlight-qt
and the script starts a genuine pairing, lets ROMarr notice it, relays the PIN
through `submit_pin`, and then asks the host whether the client actually
paired. Without it the pairing proofs are skipped and say so, because
inventing a client would defeat the purpose.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def check(area: str, step: str, ok: bool, detail: str = ""):
    RESULTS.append((area, step, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}" + (f" -- {detail}" if detail else ""))


def prove_serverinfo(host: str, kind: str, **kwargs):
    """The one call that needs no credential, against a real host."""
    from romarr.playability import MoonlightHost

    print(f"\n{kind} /serverinfo: {host}")
    server = MoonlightHost(host, kind=kind, **kwargs)
    server.refresh(force=True)
    check(kind, "the plain-HTTP probe reaches the host", server.reachable,
          server._problem or server.label)
    if not server.reachable:
        return None

    info = server._info
    check(kind, "hostname came back", bool(info.get("hostname")),
          info.get("hostname", ""))
    check(kind, "appversion came back", bool(info.get("appversion")),
          info.get("appversion", ""))
    check(kind, "HttpsPort came back", info.get("HttpsPort") == "47984",
          info.get("HttpsPort", ""))
    # The two fixture claims this whole probe exists to test.
    check(kind, "PairStatus is 0 over plain HTTP, as the code assumes",
          info.get("PairStatus") == "0", f"PairStatus={info.get('PairStatus')!r}")
    check(kind, "state is SUNSHINE_SERVER_FREE (on Wolf too)",
          info.get("state") == "SUNSHINE_SERVER_FREE",
          f"state={info.get('state')!r}")
    return server


def prove_apps(server, kind: str, expect_platform: str = "ps2"):
    """The app list, and the inference rule applied to real titles."""
    print(f"\n{kind} app list and coverage")
    check(kind, "the app list was read", server._apps_read,
          server._apps_problem or f"{len(server._apps)} app(s)")
    if not server._apps_read:
        return
    print(f"       titles: {server._apps}")
    check(kind, "a single-machine emulator on the host grants its platform",
          server.tier(expect_platform) == "stream",
          f"{expect_platform} -> {server.tier(expect_platform)!r}, "
          f"via {server._coverage.get(expect_platform)!r}")
    # The refusal that matters more than the grant.
    multi = [t for t in server._apps
             if t.lower().startswith(("retroarch", "steam", "emulationstation"))]
    check(kind, "RetroArch/Steam/EmulationStation grant nothing",
          all(slug_src not in multi
              for slug_src in server._coverage.values()),
          f"multi-machine apps seen: {multi}")
    check(kind, "a platform with no emulator on the host gets no route",
          server.tier("nes") is None, f"why: {server.why('nes')[:90]}")


def prove_pin_without_a_client(server, kind: str):
    """Sunshine's `false`, which is the half of its answer that means anything."""
    print(f"\n{kind} PIN relay with nobody waiting")
    out = server.submit_pin("1234")
    check(kind, "a PIN with no client waiting is refused", not out.get("ok"),
          out.get("detail", "")[:140])


def prove_live_pairing(server, kind: str, moonlight: str, host: str, pin: str):
    """A real Moonlight client, a real PIN, relayed by ROMarr.

    The only proof that closes the pairing row: moonlight-qt starts the
    handshake and blocks, ROMarr notices (Wolf) or is simply told (Sunshine),
    relays the PIN, and the host's own paired-client list settles it.
    """
    print(f"\n{kind} live pairing with a real Moonlight client")
    before = len(server.paired_clients())
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":0"))
    client = subprocess.Popen(
        [moonlight, "pair", host, "--pin", pin],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        time.sleep(6)                       # let phase 1 reach the host
        secret = ""
        pairing = server.pairing()
        if kind == "wolf":
            check(kind, "ROMarr sees the waiting client",
                  bool(pairing.pending),
                  f"{len(pairing.pending)} pending, detail={pairing.detail[:60]}")
            if pairing.pending:
                secret = pairing.pending[0].get("pair_secret", "")
                url = pairing.pending[0].get("pin_url", "")
                check(kind, "the rebuilt PIN-page URL points at the host",
                      url.startswith(f"http://{server.host}:{server.port}/pin/#"),
                      url[:80])
        else:
            check(kind, "Sunshine is reported as unable to list waiters",
                  not pairing.can_list_pending, pairing.detail[:80])

        out = server.submit_pin(pin, pair_secret=secret)
        check(kind, "the host accepted the PIN submission", out.get("ok"),
              out.get("detail", "")[:140])
        # moonlight-qt's `pair` does not reliably exit once the handshake is
        # done -- it keeps polling the host it just paired with -- so its exit
        # code is not the verdict and is not asserted. The host's own list is,
        # below, which is the same rule `submit_pin` states.
        try:
            client.wait(timeout=45)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=10)
            except subprocess.TimeoutExpired:
                client.kill()

    # The only honest verdict, per the module docstring.
    time.sleep(2)
    after = server.paired_clients()
    check(kind, "the host now lists the client as paired", len(after) > before,
          f"{before} -> {len(after)}: {after}")


def probed(kind: str, host: str, **kwargs):
    """A `MoonlightHost` that has already answered, or has already failed to."""
    from romarr.playability import MoonlightHost

    server = MoonlightHost(host, kind=kind, **kwargs)
    server.refresh(force=True)
    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wolf", default="")
    parser.add_argument("--wolf-socket", default="")
    parser.add_argument("--wolf-api-url", default="")
    parser.add_argument("--sunshine", default="")
    parser.add_argument("--sunshine-auth", default="")
    parser.add_argument("--moonlight", default="",
                        help="path to a real moonlight-qt; enables the "
                             "pairing proofs")
    parser.add_argument("--platform", default="ps2",
                        help="the platform an emulator on the hosts plays")
    args = parser.parse_args()

    print(f"ROMarr Moonlight live proof -- "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    if args.wolf and args.wolf_socket:
        server = prove_serverinfo(args.wolf, "wolf",
                                  socket_path=args.wolf_socket)
        if server:
            prove_apps(server, "wolf", args.platform)
            clients = server.paired_clients()
            check("wolf", "the paired-client list is readable over the socket",
                  isinstance(clients, list), f"{len(clients)} client(s)")

    if args.wolf and args.wolf_api_url:
        # Same host, same assertions, through the nginx proxy Wolf documents.
        server = prove_serverinfo(args.wolf, "wolf",
                                  api_url=args.wolf_api_url)
        if server:
            prove_apps(server, "wolf", args.platform)

    if args.sunshine:
        user, _, password = (args.sunshine_auth or ":").partition(":")
        server = prove_serverinfo(args.sunshine, "sunshine",
                                  username=user, password=password)
        if server:
            prove_apps(server, "sunshine", args.platform)
            prove_pin_without_a_client(server, "sunshine")

    if args.moonlight:
        if args.wolf and args.wolf_socket:
            server = probed("wolf", args.wolf,
                            socket_path=args.wolf_socket)
            prove_live_pairing(server, "wolf", args.moonlight, args.wolf,
                               "1234")
        if args.sunshine:
            user, _, password = (args.sunshine_auth or ":").partition(":")
            server = probed("sunshine", args.sunshine,
                            username=user, password=password)
            prove_live_pairing(server, "sunshine", args.moonlight,
                               args.sunshine, "5678")
    else:
        print("\n(no --moonlight given: the pairing proofs were skipped, and "
              "nothing here claims a client ever paired)")

    failed = [r for r in RESULTS if not r[2]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for area, step, _, detail in failed:
        print(f"  FAIL {area}: {step} -- {detail}")
    return 1 if failed else 0



if __name__ == "__main__":
    sys.exit(main())
