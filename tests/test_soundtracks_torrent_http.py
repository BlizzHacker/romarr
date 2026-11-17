"""Manual torrent selection, client ownership and seeding preservation over real HTTP."""
import json
import time
from types import SimpleNamespace
from romarr.clients import QBittorrent,QbitConfig
from romarr.selection import Release
from test_soundtracks_api import env,call


def test_http_manual_torrent_search_grab_and_watched_import_preserves_source(env,tmp_path):
    base,service,romm,_=env
    posts=[]
    class Response:
        ok=True
        status_code=200
        text="Ok."
    class Session:
        def post(self,url,**kwargs):
            posts.append((url,kwargs))
            return Response()
    client=QBittorrent(QbitConfig(base_url="http://fixture.invalid",username="fixture",password="fixture"),session=Session())
    client._authed=True
    client.romarr_id="soundtrack-fixture-client"
    service.clients=[client]
    release=Release(title="Synthetic soundtrack",size=123,indexer="Fixture",protocol="torrent",categories=[],
                    download_url="magnet:?xt=urn:btih:fixture",seeders=1)
    service.prowlarr=SimpleNamespace(search=lambda term:[release],is_prowlarr_download_url=lambda url:False)
    code,raw=call(base,"/api/v1/soundtracks/torrents?id=42&q=Synthetic")
    assert code==200
    result=json.loads(raw)
    assert result["items"][0]["title"]=="Synthetic soundtrack"
    code,raw=call(base,"/api/v1/soundtracks/torrents/grab",{"id":"42","release":result["items"][0]["id"]})
    assert code==200 and json.loads(raw)["ok"] is True
    assert posts[0][1]["data"]["category"]=="romarr-soundtracks"
    watch=service.soundtrack_store().active_watches()[0]
    assert watch["client_id"]==client.romarr_id
    source=tmp_path/"synthetic-torrent"
    source.mkdir()
    track=source/"99 synthetic.mp3"
    track.write_bytes(b"synthetic fixture audio, not a real recording")
    original=track.read_bytes()
    client.torrents=lambda:[{"category":"romarr-soundtracks","tags":watch["tag"],"progress":1.0,
                             "content_path":str(source),"state":"uploading","name":"Synthetic soundtrack"}]
    before=time.monotonic()
    assert "queued" in service.soundtrack_watch_tick()
    assert time.monotonic()-before<0.5
    for _ in range(200):
        current=service.soundtrack_store().watch_for("42")
        if current["state"] in ("added","failed"):
            break
        time.sleep(0.025)
    assert current["state"]=="added",current
    assert track.read_bytes()==original
    assert "99 synthetic.mp3" in romm.tracks[42].values()
    assert len(posts)==1                         # no torrent delete, pause or source cleanup
    assert service.soundtrack_store().status({"id":"42","name":"Fixture"},service._soundtrack_target("42")[1])["undoable"]==1
