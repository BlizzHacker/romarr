"""Manual torrent selection, client ownership and seeding preservation over real HTTP."""
import json
import time

import pytest
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


@pytest.mark.parametrize(('resolved', 'handoff'), [
    (b'torrent', {'metainfo': 'dG9ycmVudA=='}),
    ('magnet:?xt=urn:btih:' + 'a' * 40, {'filename': 'magnet:?xt=urn:btih:' + 'a' * 40}),
])
def test_transmission_http_grab_watch_and_import_preserve_payload(env, tmp_path, resolved, handoff, monkeypatch):
    """Transmission soundtrack polling imports only reported tracks and ZIP members."""
    from unittest.mock import Mock
    import zipfile
    from romarr import soundtracks
    from romarr.downloaders import Transmission, TransmissionConfig
    from test_transmission_completion import HASH, success, torrent

    base, service, romm, _ = env
    existing = set(romm.tracks[42].values())
    client = Transmission(TransmissionConfig('http://fixture.invalid', save_path=str(tmp_path)))
    client._rpc_version = 17
    client.romarr_id = 'transmission-soundtracks'
    client._call = Mock(return_value=success(**{'torrent-added': {'hashString': HASH}}))
    service.clients = [client]
    service.prowlarr = SimpleNamespace(search=lambda _: [Release('Synthetic soundtrack', 123, 1, (),
        'http://prowlarr.invalid/download', 'torrent')], is_prowlarr_download_url=lambda _: True,
        fetch_torrent=lambda _: resolved)
    code, raw = call(base, '/api/v1/soundtracks/torrents?id=42&q=Synthetic')
    release = json.loads(raw)['items'][0]['id']
    assert code == 200
    code, raw = call(base, '/api/v1/soundtracks/torrents/grab', {'id': '42', 'release': release})
    assert code == 200 and json.loads(raw)['ok']
    arguments = client._call.call_args.args[1]
    assert arguments == {**handoff, 'download-dir': str(tmp_path), 'labels': ['romarr-soundtracks']}
    source = tmp_path / 'album'
    source.mkdir()
    disc = source / 'CD1'
    disc.mkdir()
    track = disc / '99 synthetic.mp3'
    payload = b'synthetic fixture audio'
    track.write_bytes(payload)
    with zipfile.ZipFile(source / 'disc.zip', 'w') as archive:
        archive.writestr('CD2/Extra.mp3', payload)
    (source / 'stranger.mp3').write_bytes(payload)
    walk = Mock(side_effect=AssertionError('a scoped torrent must not walk its top folder'))
    monkeypatch.setattr(soundtracks.os, 'walk', walk)
    client._call = Mock(return_value=success(torrents=[torrent(labels=['romarr', 'romarr-soundtracks'],
        downloadDir=str(tmp_path), files=[{'name': 'album/CD1/99 synthetic.mp3'},
                                         {'name': 'album/disc.zip'}])]))
    assert client.completed() == []
    assert 'queued' in service.soundtrack_watch_tick()
    for _ in range(200):
        watch = service.soundtrack_store().watch_for('42')
        if watch['state'] in ('added', 'failed'):
            break
        time.sleep(.025)
    assert watch['state'] == 'added', watch
    assert watch['tag'] == 'transmission:' + HASH and watch['client_id'] == client.romarr_id
    assert track.read_bytes() == payload
    assert set(romm.tracks[42].values()) == existing | {'CD1 - 99 synthetic.mp3', 'CD2 - Extra.mp3'}
    walk.assert_not_called()
    assert {call.args[0] for call in client._call.call_args_list} == {'torrent-get'}
