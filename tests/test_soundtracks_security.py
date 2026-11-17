"""Regression proofs for scoped soundtrack writes, pinned sources and source transport."""
from dataclasses import replace
from pathlib import Path
import json
import os
import socket
import pytest
from romarr import soundtracks as st
from romarr.clients import Romm, RommConfig
from romarr.libraries import Game
from test_soundtracks_api import env, call, wait_done
from soundtracks_support import FakeRomm, TOKEN


def test_http_ambiguous_game_id_requires_explicit_library_and_isolates_undo(env):
    base, service, first, _ = env
    second = FakeRomm()
    try:
        cfg, backend = service.game_libraries[0]
        a = dict(cfg, name="Library A")
        b = dict(cfg, name="Library B", url=second.url)
        other = Romm(RommConfig(base_url=second.url, api_token=TOKEN))
        service.game_libraries = [(a, backend), (b, other)]
        service._library_cache = ([Game(id="42", name="Owned A", platform="snes", source="Library A"),
                                   Game(id="42", name="Owned B", platform="snes", source="Library B")], 0, [])
        assert call(base, "/api/v1/soundtracks/game?id=42")[0] == 400
        code, raw = call(base, "/api/v1/soundtracks/targets")
        assert code == 200
        targets = json.loads(raw)
        ids = {row["name"]: row["id"] for row in targets["games"]}
        assert ids["Owned A"] != ids["Owned B"]
        response = call(base, "/api/v1/soundtracks/download", {"id": ids["Owned A"], "candidate":
                        {"provider": "zophar", "id": "/music/snes-spc/chrono-trigger"}, "format": "mp3"})
        assert response[0] == 200
        assert wait_done(base, ids["Owned A"])["state"] == "done"
        before = dict(second.tracks)
        code, raw = call(base, "/api/v1/soundtracks/undo", {"id": ids["Owned B"]})
        assert code == 200 and json.loads(raw)["removed"] == 0
        assert second.tracks == before
        a_scope, b_scope = (ids[name].split(":")[0] for name in ["Owned A", "Owned B"])
        assert service.soundtrack_store(a_scope).db_path != service.soundtrack_store(b_scope).db_path
        assert call(base, "/api/v1/soundtracks/dups/merge", {"keep": ids["Owned A"], "drop": [ids["Owned B"]]})[0] == 400
    finally:
        second.close()


def test_scoped_journal_refuses_wrong_api_even_for_matching_numeric_ids(tmp_path):
    store = st.Soundtracks(tmp_path / "scoped.db", scope=st.library_origin("https://a.invalid"))
    class WrongApi:
        base = "https://b.invalid"
        def delete_track(self, *args):
            raise AssertionError("wrong server must never receive a deletion")
    with pytest.raises(ValueError, match="different library"):
        store.undo({"id": "7"}, WrongApi())
    with pytest.raises(ValueError, match="different library"):
        store._journal_sent("7", ["01.mp3"], WrongApi())


def test_selected_album_parent_replacement_cannot_upload_outside_bytes(tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    (album / "01.mp3").write_bytes(b"selected")
    files = st.collect_album(str(album))
    album.rename(tmp_path / "original")
    album.mkdir()
    (album / "01.mp3").write_bytes(b"outside fixture")
    class Api:
        def rom(self, key):return {"has_multiple_files": True}
        def tracks(self, key):return []
        def upload(self, *args):raise AssertionError("changed source must never upload")
    store = st.Soundtracks(tmp_path / "s.db")
    game = {"id": "7", "name": "Fixture"}
    job = store._begin_job(["7"], "Fixture", "album")
    store._run_album(job, game, Api(), "album", files)
    assert job["state"] == "error" and job["made"] == 0
    assert (album / "01.mp3").read_bytes() == b"outside fixture"


def test_selected_album_symlink_swap_is_refused_before_network(tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    track = album / "01.mp3"
    track.write_bytes(b"selected")
    files = st.collect_album(str(album))
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside fixture")
    track.unlink()
    try:track.symlink_to(outside)
    except OSError as exc:pytest.skip(f"Platform does not grant symlink creation: {exc}")
    class Api:
        def rom(self, key):return {"has_multiple_files": True}
        def tracks(self, key):return []
        def upload(self, *args):raise AssertionError("outside bytes must never upload")
    store = st.Soundtracks(tmp_path / "s.db")
    job = store._begin_job(["7"], "Fixture", "album")
    store._run_album(job, {"id": "7", "name": "Fixture"}, Api(), "album", files)
    assert job["state"] == "error" and job["made"] == 0


def test_http_undo_preserves_changed_remote_file(env):
    base, _, romm, _ = env
    assert call(base, "/api/v1/soundtracks/download", {"id": "42", "candidate":
                {"provider": "zophar", "id": "/music/snes-spc/chrono-trigger"}, "format": "mp3"})[0] == 200
    assert wait_done(base)["state"] == "done"
    target = next(key for key in romm.blobs if key[0] == 42)
    romm.blobs[target] = b"replacement owned elsewhere"
    assert call(base, "/api/v1/soundtracks/undo", {"id": "42"})[0] == 400
    assert romm.blobs[target] == b"replacement owned elsewhere"


def test_source_redirect_rejected_before_request_to_unapproved_host(monkeypatch):
    calls = []
    class Response:
        status_code = 302
        headers = {"Location": "https://127.0.0.1/private"}
        def close(self):pass
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def mount(self,*args):pass
        def get(self,url,**kwargs):
            calls.append((url,kwargs))
            assert kwargs["allow_redirects"] is False
            return Response()
    monkeypatch.setattr(st.requests,"Session",Session)
    monkeypatch.setattr(st,"_wait_turn",lambda *args:None)
    with pytest.raises(ValueError,match="outside"):
        with st._source_response("https://archive.org/file.mp3"):pass
    assert len(calls)==1


@pytest.mark.parametrize("address",["127.0.0.1","10.0.0.2","169.254.169.254","::1","::ffff:127.0.0.1"])
def test_source_dns_private_or_mapped_address_refused_before_socket(monkeypatch,address):
    monkeypatch.setattr(st.socket,"getaddrinfo",lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,"",(address,443))])
    monkeypatch.setattr(st.socket,"socket",lambda *args,**kwargs:pytest.fail("must not connect to rejected address"))
    connection=st._PublicHTTPSConnection("archive.org")
    with pytest.raises(ValueError,match="non-public"):
        connection._new_conn()


def test_source_connection_uses_validated_address_not_second_dns_lookup(monkeypatch):
    events=[]
    monkeypatch.setattr(st.socket,"getaddrinfo",lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",443))])
    class Sock:
        def settimeout(self,timeout):pass
        def connect(self,address):events.append(address)
    monkeypatch.setattr(st.socket,"socket",lambda *args,**kwargs:Sock())
    connection=st._PublicHTTPSConnection("archive.org",timeout=10)
    connection._new_conn()
    assert events==[("93.184.216.34",443)] and connection.host=="archive.org"


def test_active_job_count_is_bounded_across_different_games(tmp_path):
    store=st.Soundtracks(tmp_path/"jobs.db")
    for number in range(4):store._begin_job([str(number)],"Fixture","album")
    with pytest.raises(st.JobBusy,match="Four"):
        store._begin_job(["next"],"Fixture","album")


def test_plain_audio_stream_limit_applies_without_zip_metadata(monkeypatch):
    from contextlib import contextmanager
    class Response:
        headers={}
        def iter_content(self,size):yield b"123";yield b"456"
    @contextmanager
    def response(url):yield Response()
    monkeypatch.setattr(st,"_source_response",response)
    monkeypatch.setattr(st,"MAX_ARCHIVE_BYTES",5)
    with pytest.raises(ValueError,match="transfer limit"):
        list(st.stream("https://archive.org/fixture.mp3"))


def test_legacy_unscoped_journal_is_never_auto_adopted(env,tmp_path):
    _,service,_,_=env
    legacy=tmp_path/"romarr-soundtracks.db"
    legacy.write_bytes(b"legacy private state")
    assert service.soundtrack_store().db_path != legacy
    assert legacy.read_bytes()==b"legacy private state"


def test_soundtrack_database_is_private_on_posix(tmp_path):
    store=st.Soundtracks(tmp_path/"private.db")
    if os.name=="posix":
        assert store.db_path.stat().st_mode & 0o777 == 0o600


def test_torrent_watches_do_not_match_a_different_client(tmp_path):
    store=st.Soundtracks(tmp_path/"watches.db")
    store.add_watch("7","fixture-tag","Fixture",client_id="a")
    before=store.active_watches()
    assert store.process_watches([],lambda key:pytest.fail("wrong client must not resolve"),lambda path:path,client_id="b")==[]
    assert store.active_watches()==before


def test_qbittorrent_rejected_add_cannot_leave_a_stale_soundtrack_tag():
    from test_qbittorrent_login import client, _Response
    qbit,_=client(_Response(200,"Fails."))
    qbit._authed=True
    qbit._added.job_id="qbit-tag:old"
    assert qbit.add("magnet:?xt=urn:btih:fixture",category="romarr-soundtracks") is False
    assert qbit.last_job_id==""


def test_status_samples_job_before_journal_count_to_keep_completion_polling(tmp_path,monkeypatch):
    store=st.Soundtracks(tmp_path/"race.db")
    job=store._begin_job(["7"],"Fixture","album")
    class Api:
        def tracks(self,key):return []
    def complete_during_count(key):
        job.update(state="done",finished=1)
        return 0
    monkeypatch.setattr(store,"_journal_count",complete_during_count)
    response=store.status({"id":"7","name":"Fixture"},Api())
    assert response["job"]["state"]=="running" and response["job"]["finished"] is None


def test_disabled_catalogue_sources_stay_disabled(env):
    _,service,_,_=env
    service.store.settings["soundtrack_sources"]=""
    assert service.soundtrack_sources()==()


def test_named_libraries_without_ids_at_same_origin_have_separate_journals(env):
    _,service,_,_=env
    cfg,backend=service.game_libraries[0]
    cfg={key:value for key,value in cfg.items() if key not in ("id","romarr_id")}
    service.game_libraries=[(dict(cfg,name="A"),backend),(dict(cfg,name="B"),backend)]
    scopes=[row[0] for row in service._soundtrack_libraries()]
    assert len(set(scopes))==2


def test_http_game_picker_can_find_titles_beyond_first_bounded_page(env):
    base,service,_,_=env
    games=[Game(id=str(1000+number),name=f"Fixture {number}",platform="snes") for number in range(600)]
    games.append(Game(id="9000",name="Beyond the initial page",platform="snes"))
    service._library_cache=(games,0,[])
    code,raw=call(base,"/api/v1/soundtracks/targets")
    result=json.loads(raw)
    assert code==200 and len(result["games"])==500 and result["total"]==601 and result["truncated"]
    scope=result["libraries"][0]["id"]
    code,raw=call(base,"/api/v1/soundtracks/targets?library="+scope+"&q=Beyond")
    result=json.loads(raw)
    assert code==200 and result["total"]==1 and result["games"][0]["id"]==scope+":9000"


@pytest.mark.parametrize("unavailable",[False,True])
def test_configured_scan_finding_or_failure_blocks_audio_before_romm_upload(env,monkeypatch,unavailable):
    from romarr import app as app_module
    base,service,romm,_=env
    service.store.settings["clamav_enabled"]=True
    scans=[]
    def scan(settings,environment,path):
        assert settings["clamav_enabled"] and Path(path).is_file()
        scans.append(Path(path).read_bytes())
        if unavailable:
            raise app_module.scanners.ScanUnavailable("fixture scanner unavailable")
        return [object()],[]
    monkeypatch.setattr(app_module.scanners,"scan",scan)
    assert call(base,"/api/v1/soundtracks/download",{"id":"42","candidate":
                {"provider":"zophar","id":"/music/snes-spc/chrono-trigger"},"format":"mp3"})[0]==200
    assert wait_done(base)["state"]=="error"
    assert scans and romm.uploads()==[]


def test_torrent_search_exception_does_not_log_credentials(env):
    from types import SimpleNamespace
    import requests
    base,service,_,caplog=env
    secret="fixture-key-that-must-not-be-logged"
    def fail(term):raise requests.ConnectionError("https://fixture.invalid/?apikey="+secret)
    service.prowlarr=SimpleNamespace(search=fail)
    code,raw=call(base,"/api/v1/soundtracks/torrents?id=42&q=Synthetic")
    assert code==200 and secret not in raw and secret not in caplog.text
