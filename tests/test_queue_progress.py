"""Progress stays read-only and cannot cross downloader identities."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from romarr.clients import QBittorrent, QbitConfig
from romarr.queue_progress import annotate


def test_actual_qbittorrent_http_progress_filters_malformed_and_bounds_values():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            payload = [{"hash":"a"*40,"tags":"romarr-1, other", "progress":0.42,"eta":90,"dlspeed":2048},
                       {"hash":"b"*40,"progress":2,"eta":8640000,"dlspeed":-1},
                       {"hash":"c"*40,"progress":"NaN"},{"hash":"invalid"},None]
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client=QBittorrent(QbitConfig(base_url=f"http://127.0.0.1:{server.server_port}", category="romarr music"))
        data=client.progress()
        assert data["a"*40]["progress"] == 42
        assert data["a"*40]["eta"] == 90
        assert data["b"*40]["progress"] == 100
        assert data["b"*40]["eta"] is None
        assert data["b"*40]["speed"] == 0
        assert len(data) == 2
        assert seen == ["/api/v2/torrents/info?category=romarr+music"]
    finally: server.shutdown();server.server_close()


def test_same_hash_on_two_clients_and_unmatched_tag_never_leak_progress():
    calls=[]
    class Client:
        configured=True
        def __init__(self,identity,percent):self.romarr_id=identity;self.percent=percent
        def progress(self, *, deadline):
            calls.append(self.romarr_id)
            return {"a"*40:{"tags":["mine"],"progress":self.percent,"eta":12,"speed":100,"dl_state":"downloading"}}
    svc=SimpleNamespace(_lock=threading.RLock(),clients=[Client("A",20),Client("B",90)])
    def row(client,did="",rid="btih:"+"a"*40):return {"state":"grabbed","download_client":client,"download_id":did,"release_id":rid}
    rows=annotate(svc,[row("A"),row("B"),row(""),row("A","qbit-tag:other"),row("A","qbit-tag:mine")])
    assert [r.get("progress") for r in rows]==[20,90,None,None,20]
    assert annotate(svc,[row("A")])[0]["progress"]==20
    assert calls==["A","B"]


def test_client_failure_leaves_progress_unknown_without_changing_queue():
    class Client:
        romarr_id="A";configured=True
        def progress(self, **kwargs):raise RuntimeError("unreachable")
    svc=SimpleNamespace(_lock=threading.RLock(),clients=[Client()])
    row={"state":"grabbed","download_client":"A","release_id":"btih:"+"a"*40}
    assert annotate(svc,[row.copy()])==[row]


def test_client_replacement_invalidates_cached_progress():
    class Client:
        romarr_id="A";configured=True
        def __init__(self,percent):self.percent=percent
        def progress(self,**kwargs):return {"a"*40:{"progress":self.percent}}
    svc=SimpleNamespace(_lock=threading.RLock(),clients=[Client(20)])
    row={"state":"grabbed","download_client":"A","release_id":"btih:"+"a"*40}
    assert annotate(svc,[row.copy()])[0]["progress"]==20
    svc.clients=[Client(90)]
    assert annotate(svc,[row.copy()])[0]["progress"]==90
