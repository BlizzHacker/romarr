"""Prove the download clients against real daemons, not fakes.

Run on a host where the daemons are reachable:

    .venv/bin/python scripts/live_proof.py \
        --transmission http://localhost:9091 --transmission-auth user:pass \
        --deluge http://localhost:8112 --deluge-password deluge \
        --rtorrent http://127.0.0.1:5010

Each proof drives the same client class ROMarr uses in production --
built through build_client, exactly as the Settings page would -- through
reachable(), add(), and a listing, against a live daemon. Output is the
evidence block docs/PROOF.md cites; the exit code is the verdict.

The magnet is a syntactically valid infohash that will never resolve to
peers. Accepting it into the queue is the whole protocol conversation:
session handshakes, auth, RPC dialects, label application.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAGNET = ("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
          "&dn=romarr-live-proof")

RESULTS = []


def check(name: str, step: str, ok: bool, detail: str = ""):
    RESULTS.append((name, step, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {step}" + (f" -- {detail}" if detail else ""))


def prove_transmission(url: str, auth: str):
    from urllib.parse import urlsplit
    from romarr.downloaders import build_client

    print("Transmission:", url)
    parts = urlsplit(url)
    user, _, password = (auth or ":").partition(":")
    client = build_client({
        "type": "transmission", "host": parts.hostname, "port": parts.port,
        "username": user, "password": password, "category": "romarr-proof"})
    check("transmission", "reachable() -- session-get behind the 409 handshake",
          client.reachable())
    check("transmission", "add() a magnet with the label applied",
          client.add(MAGNET))
    # Read back through the same RPC to show it actually landed.
    body = client._call("torrent-get", {"fields": ["name", "labels"]})
    torrents = (body.get("arguments") or {}).get("torrents") or []
    ours = [t for t in torrents if "romarr-live-proof" in str(t.get("name", ""))]
    check("transmission", "the torrent is in the live queue with our label",
          bool(ours) and any("romarr-proof" in (t.get("labels") or [])
                             for t in ours),
          f"{len(torrents)} torrent(s) in the daemon")


def prove_deluge(url: str, password: str):
    from urllib.parse import urlsplit
    from romarr.downloaders import build_client

    print("Deluge:", url)
    parts = urlsplit(url)
    client = build_client({
        "type": "deluge", "host": parts.hostname, "port": parts.port,
        "password": password, "category": "romarr-proof"})
    check("deluge", "reachable() -- auth.login + web.connect to the daemon",
          client.reachable())
    check("deluge", "add() a magnet (label best-effort)", client.add(MAGNET))
    body = client._call("core.get_torrents_status", [{}, ["name"]])
    names = [t.get("name", "") for t in (body.get("result") or {}).values()]
    check("deluge", "the torrent is in the live daemon",
          any("romarr-live-proof" in n for n in names),
          f"{len(names)} torrent(s) in the daemon")


def prove_rtorrent(url: str):
    from romarr.downloaders import Rtorrent, RtorrentConfig

    print("rTorrent:", url)
    client = Rtorrent(RtorrentConfig(base_url=url, category="romarr-proof"))
    check("rtorrent", "reachable() -- system.client_version over XML-RPC",
          client.reachable())
    check("rtorrent", "add() via load.start with d.custom1 label",
          client.add(MAGNET))
    rows = client._server().d.multicall2("", "main", "d.name=", "d.custom1=")
    check("rtorrent", "the torrent is in the live client with our label",
          any("romarr-proof" in str(r[1]) for r in rows if len(r) > 1),
          f"{len(rows)} torrent(s) in the client")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transmission")
    ap.add_argument("--transmission-auth", default="")
    ap.add_argument("--deluge")
    ap.add_argument("--deluge-password", default="deluge")
    ap.add_argument("--rtorrent")
    args = ap.parse_args()

    print("ROMarr live client proof --",
          datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if args.transmission:
        prove_transmission(args.transmission, args.transmission_auth)
    if args.deluge:
        prove_deluge(args.deluge, args.deluge_password)
    if args.rtorrent:
        prove_rtorrent(args.rtorrent)

    failed = [r for r in RESULTS if not r[2]]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} proofs passed")
    sys.exit(1 if failed or not RESULTS else 0)


if __name__ == "__main__":
    main()
