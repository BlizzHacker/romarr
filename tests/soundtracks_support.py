"""Fakes for the soundtrack tests: a loopback RomM, hand-written source pages, zip builders.

Nothing here touches the network beyond 127.0.0.1, and every page below is
written by hand to the shape the real sites use -- not saved from them.
"""

from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The "login" the fake RomM accepts. Tests assert this exact string never
#: shows up in logs, job records or API replies.
TOKEN = "SECRET-bearer-8f3a1c9d"
PASSWORD = "hunter2-library-password"

ZOPHAR_SEARCH = """
<table>
<tr class="regularrow odd"><td class="name"><a href="/music/snes-spc/chrono-trigger">Chrono Trigger</a></td>
<td class="console">SNES (SPC)</td><td class="year">1995</td></tr>
<tr class="regularrow even"><td class="name"><a href="/music/gameboy-gbs/chrono-trigger-hack">Chrono Trigger &amp; Friends</a></td>
<td class="console">Game Boy (GBS)</td><td class="year"></td></tr>
<tr class="regularrow odd"><td class="name"><a href="/music/snes-spc/chrono-trigger">Chrono Trigger</a></td>
<td class="console">SNES (SPC)</td><td class="year">1995</td></tr>
<tr class="regularrow odd"><td class="name"><a href="/music/snes-spc/zelda">Zelda</a></td>
<td class="console">SNES (SPC)</td><td class="year">1992</td></tr>
</table>"""

ZOPHAR_PAGE = """
<div id="mass_download">
 <a href="https://fi.zophar.net/soundfiles/snes-spc/chrono-trigger/Chrono%20Trigger%20(mp3).zip"><p>Download All Tracks As MP3</p></a>
 <a href="/soundfiles/snes-spc/chrono-trigger/Chrono%20Trigger%20(spc).zip"><p>Download Original Format</p></a>
</div>
<tr class="regularrow trackrow"></tr><tr class="regularrow trackrow"></tr><tr class="regularrow trackrow"></tr>"""

ARCHIVE_SEARCH = json.dumps({"response": {"docs": [
    {"identifier": "chrono-trigger-ost", "title": "Chrono Trigger Original Soundtrack", "item_size": 123456},
    {"identifier": "unrelated-podcast", "title": "Cooking Hour", "item_size": 5}]}})

ARCHIVE_META = json.dumps({"files": [
    {"name": "disc1/01 Prologue.mp3", "source": "original"},
    {"name": "disc1/02 Theme.flac", "source": "original"},
    {"name": "disc1/02 Theme.mp3", "source": "derivative"},
    {"name": "cover.jpg", "source": "original"},
    {"name": "notes.txt", "source": "original"}]})


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeRomm:
    """Just enough of RomM's soundtrack endpoints, on a loopback port.

    `tracks` maps rom id -> {file_id: file_name}. `requests` records every call
    as (method, path, headers dict, body bytes) so tests can check wire shape.
    """

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.tracks: dict[int, dict[int, str]] = {42: {900: "Mine Already.mp3"}}
        self.rom = {"fs_name": "Chrono Trigger.sfc", "has_multiple_files": False}
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.next_id = 1000
        self.refuse: set[str] = set()          # upload names answered with 409
        self.fail_delete: set[int] = set()      # file ids whose DELETE answers 500 (echoing the credential)
        self.issued_scopes: list[str] = []
        #: (rom id, file id) -> stored bytes; a track without an entry has its own name as content.
        self.blobs: dict[tuple[int, int], bytes] = {}
        #: rom id -> game name, for the library listing the duplicate scan reads
        self.names: dict[int, str] = {42: "Chrono Trigger"}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.path == "/api/token":
                    form = dict(p.split("=", 1) for p in body.decode().split("&"))
                    outer.issued_scopes.append(form.get("scope", "").replace("+", " "))
                    return self._reply(200, json.dumps({"access_token": outer.token}).encode())
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    return self._reply(401, b'{"detail":"bad token"}')
                if self.path.startswith("/api/roms?") and method == "GET":
                    off = int(re.search(r"offset=(\d+)", self.path).group(1))
                    items = [{"id": rid, "name": outer.names.get(rid, f"Game {rid}"), "platform_display_name": "SNES",
                              "files": [{"id": fid, "file_name": n, "category": "soundtrack",
                                         "file_size_bytes": len(outer.content(rid, fid))} for fid, n in t.items()]}
                             for rid, t in sorted(outer.tracks.items())]
                    return self._reply(200, json.dumps({"items": items[off:off + 500]}).encode())
                m = re.fullmatch(r"/api/roms/(\d+)/files/(\d+)/content/(.+)", self.path)
                if m and method == "GET":
                    rid, fid = int(m.group(1)), int(m.group(2))
                    if fid not in outer.tracks.get(rid, {}):
                        return self._reply(404, b"{}")
                    return self._reply(200, outer.content(rid, fid), "application/octet-stream")
                m = re.fullmatch(r"/api/roms/(\d+)", self.path)
                if m and method == "GET":
                    return self._reply(200, json.dumps(outer.rom).encode())
                m = re.fullmatch(r"/api/roms/(\d+)/soundtracks/metadata", self.path)
                if m and method == "GET":
                    t = outer.tracks.get(int(m.group(1)), {})
                    return self._reply(200, json.dumps([{"file_name": v, "file_id": k} for k, v in t.items()]).encode())
                m = re.fullmatch(r"/api/roms/(\d+)/soundtracks", self.path)
                if m and method == "POST":
                    name = self.headers.get("x-upload-filename", "")
                    if name in outer.refuse:
                        return self._reply(409, b'{"detail":"unacceptable"}')
                    outer.next_id += 1
                    outer.tracks.setdefault(int(m.group(1)), {})[outer.next_id] = name
                    head, _, rest = body.partition(b"\r\n\r\n")
                    outer.blobs[(int(m.group(1)), outer.next_id)] = rest.rsplit(b"\r\n--", 1)[0]
                    return self._reply(201, b"{}")
                m = re.fullmatch(r"/api/roms/(\d+)/soundtracks/(\d+)", self.path)
                if m and method == "DELETE":
                    rid, fid = int(m.group(1)), int(m.group(2))
                    if fid in outer.fail_delete:
                        return self._reply(500, ("boom: Authorization: Bearer " + outer.token).encode(), "text/plain")
                    if fid not in outer.tracks.get(rid, {}):
                        return self._reply(404, b"{}")
                    del outer.tracks[rid][fid]
                    return self._reply(200, b"{}")
                self._reply(404, b"{}")

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def content(self, rom_id: int, file_id: int) -> bytes:
        return self.blobs.get((rom_id, file_id), self.tracks[rom_id][file_id].encode())

    def seed(self, rom_id: int, files: dict[str, bytes], name: str | None = None) -> None:
        """Give a ROM stored soundtrack files, as if already in RomM."""
        self.tracks[rom_id] = {}
        if name:
            self.names[rom_id] = name
        for n, data in files.items():
            self.next_id += 1
            self.tracks[rom_id][self.next_id] = n
            self.blobs[(rom_id, self.next_id)] = data

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def uploads(self):
        return [r for r in self.requests if r[0] == "POST" and r[1].endswith("/soundtracks")]
