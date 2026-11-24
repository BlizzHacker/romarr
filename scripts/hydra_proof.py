#!/usr/bin/env python3
"""Prove a Hydra Launcher source works end to end on a running ROMarr.

Drives ROMarr's own HTTP API the way the Indexers page does, and stops at the
first step that fails, saying which one and why:

1. check the source (the Indexers page's Check / Test button);
2. add the built-in Direct HTTP client when the source's file links need one
   and none is set up (``--no-add-client`` to only report it);
3. check again: at least one kind of link must now have a client;
4. save the source as an indexer (skipped when that URL is already saved);
5. search a game the source lists, and require a release from this source;
6. grab it, and require the queue row to name the client the check promised;
7. run the import sweep until the file is in a library (``--library-path``
   adds a folder library when none exists; without one the proof ends at the
   download).

With no ``--source`` it serves its own tiny source -- one free homebrew file it
writes itself -- from ``--serve-host``, so the proof needs nothing from the
internet. ``tests/test_hydra_setup.py`` runs exactly this against an in-process
ROMarr, which is what keeps CI proving it.

Standard library only, so it runs wherever ROMarr's API is reachable::

    python scripts/hydra_proof.py --romarr http://127.0.0.1:6868 --api-key KEY \\
        --source https://example.org/source.json --game "Some Game" --platform gbc

The API key, when given, is sent as ``X-Api-Key`` and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

#: The self-served proof game: a few bytes that stand in for a homebrew ROM.
FIXTURE_TITLE = "ROMarr Proof Cart (World) (Homebrew)"
FIXTURE_GAME = "ROMarr Proof Cart"
FIXTURE_PLATFORM = "gbc"
FIXTURE_FILE = "romarr-proof-cart.gbc"


class ProofFailed(RuntimeError):
    """One step of the proof did not hold; the message says which and why."""


class Api:
    """ROMarr's HTTP API, JSON in and out."""

    def __init__(self, base: str, api_key: str = "", timeout: int = 120):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def call(self, method: str, path: str, body=None) -> dict:
        """One request; HTTP errors come back as their JSON body when they have one."""
        request = urllib.request.Request(self.base + path, method=method)
        request.add_header("Accept", "application/json")
        if self.api_key:
            request.add_header("X-Api-Key", self.api_key)
        if body is not None:
            request.add_header("Content-Type", "application/json")
            request.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return {"error": f"HTTP {err.code}", **json.loads(raw or b"{}")}
            except ValueError:
                raise ProofFailed(f"{method} {path} answered HTTP {err.code}") from None
        except urllib.error.URLError as err:
            raise ProofFailed(f"ROMarr at {self.base} did not answer: {err.reason}") from None
        return json.loads(raw or b"{}")


def serve_fixture(host: str = "127.0.0.1", port: int = 0):
    """Serve a one-game Hydra source and its file; returns (source URL, server).

    ``host`` is the address ROMarr will use to reach this machine, which in
    Docker is the host's address on the container network, not 127.0.0.1.
    """
    root = tempfile.mkdtemp(prefix="romarr-hydra-proof-")
    with open(os.path.join(root, FIXTURE_FILE), "wb") as handle:
        # A Game Boy Color header-shaped block: enough for an importer to file.
        handle.write(b"\x00" * 0x134 + b"ROMARR PROOF" + b"\x00" * (0x8000 - 0x140))
    with open(os.path.join(root, "robots.txt"), "w", encoding="utf-8") as handle:
        handle.write("User-agent: *\nAllow: /\n")
    httpd = ThreadingHTTPServer(("0.0.0.0" if host not in ("127.0.0.1", "localhost") else host, port),
                                partial(_Quiet, directory=root))
    base = f"http://{host}:{httpd.server_address[1]}"
    source = {"name": "ROMarr proof source", "downloads": [{
        "title": FIXTURE_TITLE, "platform": FIXTURE_PLATFORM, "fileSize": "32 KB",
        "uploadDate": "2026-11-23T00:00:00Z", "uris": [f"{base}/{FIXTURE_FILE}"]}]}
    with open(os.path.join(root, "source.json"), "w", encoding="utf-8") as handle:
        json.dump(source, handle)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"{base}/source.json", httpd


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):   # the proof prints its own report
        pass


def _step(report: list, name: str, ok: bool, detail: str) -> None:
    report.append({"step": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        raise ProofFailed(f"{name}: {detail}")


def _check(api: Api, source: str, name: str) -> dict:
    return api.call("POST", "/api/v1/indexer/test",
                    {"type": "hydra", "url": source, "name": name, "timeout": 120})


def _add_direct_client(api: Api, delay, save_path: str = "") -> str:
    schema = api.call("GET", "/api/v1/downloadclient/schema").get("types", {})
    fields = (schema.get("direct") or {}).get("fields") or []
    item = {"type": "direct", **{f["name"]: f.get("default") for f in fields}}
    if delay is not None:
        item["delay"] = delay
    if save_path:
        item["save_path"] = save_path
    saved = api.call("POST", "/api/v1/downloadclient", item)
    if saved.get("error"):
        raise ProofFailed(f"adding Direct HTTP failed: {saved['error']}")
    return saved.get("id", "")


def _wait_for_import(api: Api, game: str, deadline: float) -> dict:
    """Run the import sweep until the grabbed game is imported or fails."""
    while time.monotonic() < deadline:
        api.call("POST", "/api/import")
        for row in api.call("GET", "/api/queue") or []:
            if row.get("game") == game and row.get("state") in ("imported", "import-failed", "failed"):
                return row
        history = api.call("GET", "/api/v1/history?limit=20").get("items", [])
        for event in history:
            if event.get("kind") == "imported" and game.lower() in str(event.get("game", "")).lower():
                return {"state": "imported", **event}
        time.sleep(2)
    return {}


def run(romarr: str, *, api_key: str = "", source: str = "", name: str = "",
        game: str = "", platform: str = "", add_client: bool = True,
        library_path: str = "", download_path: str = "", direct_delay=None,
        timeout: int = 180, serve_host: str = "127.0.0.1") -> list[dict]:
    """Run every step; returns the report, raises ProofFailed at the first failure."""
    api, report, server = Api(romarr, api_key), [], None
    if not source:
        source, server = serve_fixture(serve_host)
        game, platform = game or FIXTURE_GAME, platform or FIXTURE_PLATFORM
        name = name or "ROMarr proof source"
    if not game:
        raise ProofFailed("--game is required with --source: name a game the source lists")
    try:
        first = _check(api, source, name)
        if not first.get("summary"):
            _step(report, "check the source", False, first.get("message", "no answer"))
        _step(report, "check the source", True, first["message"].replace("\n", " | "))

        routes = first.get("routes") or []
        needs_direct = any(not r["ready"] and r["kind"] in ("direct", "hoster") for r in routes)
        if not first.get("ok") and needs_direct and add_client:
            client_id = _add_direct_client(api, direct_delay, download_path)
            _step(report, "add Direct HTTP", True, f"client {client_id or '(saved)'}")
        again = _check(api, source, name)
        _step(report, "check again", bool(again.get("ok")),
              again.get("message", "").replace("\n", " | "))
        ready = {r["kind"]: r["client"] for r in again.get("routes") or [] if r["ready"]}

        saved = api.call("GET", "/api/v1/indexer").get("items", [])
        if any(i.get("url") == source for i in saved):
            _step(report, "save the source", True, "already saved")
        else:
            made = api.call("POST", "/api/v1/indexer", {
                "type": "hydra", "name": name or again.get("summary", {}).get("name")
                or "Hydra source", "url": source, "enable": True, "priority": 25,
                "timeout": 120})
            _step(report, "save the source", not made.get("error"),
                  made.get("error") or f"indexer {made.get('id', '')}")
        indexer_name = next((i.get("name") for i in api.call("GET", "/api/v1/indexer")["items"]
                             if i.get("url") == source), name)

        query = urllib.parse.urlencode({"game": game, "platform": platform})
        found = api.call("GET", f"/api/v1/release?{query}")
        mine = [r for r in found.get("items", [])
                if r.get("indexer") == indexer_name and r.get("grabbable")]
        _step(report, "search", bool(mine),
              f"{len(mine)} grabbable release(s) from {indexer_name} for {game!r}"
              if mine else found.get("error") or f"no release from {indexer_name} for {game!r}")
        pick = mine[0]

        grabbed = api.call("POST", "/api/v1/release/grab",
                           {"id": pick["id"], "platform": pick.get("platform") or platform})
        _step(report, "grab", bool(grabbed.get("ok")),
              f"{pick['title']} ({pick['protocol']})" if grabbed.get("ok")
              else grabbed.get("error", "refused"))
        clients = {c.get("id"): c for c in api.call("GET", "/api/v1/downloadclient").get("items", [])}
        row = next((q for q in api.call("GET", "/api/queue") or []
                    if q.get("release") == pick["title"]), {})
        owner = clients.get(row.get("download_client"), {})
        expected = ready.get(pick["protocol"], "")
        label = owner.get("name") or owner.get("type") or row.get("download_client") or "?"
        _step(report, "routed to the promised client",
              bool(owner) and (not expected or label in expected or expected in label),
              f"{pick['protocol']} link went to {label}; the check promised {expected or 'a client'}")

        libraries = api.call("GET", "/api/v1/library").get("items", [])
        if not libraries and library_path:
            made = api.call("POST", "/api/v1/library", {"type": "folder", "name": "Proof library",
                                                        "enable": True, "path": library_path})
            libraries = [] if made.get("error") else [made]
        if not libraries:
            _step(report, "import", True, "skipped: no library configured (downloaded only)")
            return report
        done = _wait_for_import(api, row.get("game") or game, time.monotonic() + timeout)
        where = ", ".join(done.get("imported_paths") or []) or str(done.get("detail") or "")
        _step(report, "import", done.get("state") == "imported",
              f"{done.get('state') or 'still waiting'}{': ' + where if where else ''}")
        return report
    finally:
        if server is not None:
            server.shutdown()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--romarr", default=os.environ.get("ROMARR_URL", "http://127.0.0.1:6868"))
    parser.add_argument("--api-key", default=os.environ.get("ROMARR_API_KEY", ""))
    parser.add_argument("--source", default="", help="a Hydra source .json URL; "
                        "omitted, a one-game source is served from this machine")
    parser.add_argument("--name", default="", help="the indexer name to save it under")
    parser.add_argument("--game", default="", help="a game the source lists")
    parser.add_argument("--platform", default="", help="its ROMarr platform slug, e.g. gbc")
    parser.add_argument("--serve-host", default="127.0.0.1",
                        help="the address ROMarr reaches this machine on (self-served source)")
    parser.add_argument("--library-path", default="",
                        help="add a folder library here when none exists")
    parser.add_argument("--download-path", default="",
                        help="where an added Direct HTTP client saves files "
                             "(default: its own default, /downloads/sites)")
    parser.add_argument("--no-add-client", action="store_true",
                        help="report a missing client instead of adding Direct HTTP")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--json", action="store_true", help="print the report as JSON too")
    args = parser.parse_args(argv)
    try:
        report = run(args.romarr, api_key=args.api_key, source=args.source, name=args.name,
                     game=args.game, platform=args.platform, add_client=not args.no_add_client,
                     library_path=args.library_path, download_path=args.download_path,
                     timeout=args.timeout,
                     serve_host=args.serve_host)
    except ProofFailed as err:
        print(f"Hydra proof failed at {err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    print("Hydra proof passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
