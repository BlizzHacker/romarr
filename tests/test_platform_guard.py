"""Fail closed before download handoff if no importable platform was chosen."""
import pytest
from romarr.app import ROMarr
from romarr.selection import Release
from romarr.store import SeerrRequest, Store

class RecordingClient:
    name = 'fixture'
    protocol = 'torrent'
    configured = True
    def __init__(self): self.added = []
    def add(self, url, **kwargs):
        self.added.append(url)
        return True

def service(tmp_path):
    s = ROMarr({'ROMARR_DATA': str(tmp_path/'state.json')})
    client = RecordingClient()
    s.clients = [client]
    return s, client

def release():
    return Release(title='Fixture NSW release', size=1024, seeders=1,
                   categories=(1000,), download_url='magnet:?xt=urn:btih:fixture',
                   protocol='torrent', indexer='Fixture')

def test_blank_platform_is_refused_before_any_handoff_or_state_write(tmp_path):
    s, client = service(tmp_path)
    before = s.store.path.read_bytes()
    result = s.grab(release(), 'Fixture', '', manual=True)
    assert result['ok'] is False
    assert 'platform' in result['error'].lower()
    assert client.added == []
    assert s.queue == []
    assert s.store.path.read_bytes() == before

@pytest.mark.parametrize('platform', [' ', 'not-a-console', None, 42])
def test_invalid_platform_has_no_download_or_queue_side_effects(tmp_path, platform):
    s, client = service(tmp_path)
    before = s.store.path.read_bytes()
    result = s.grab(release(), 'Fixture', platform)
    assert result['ok'] is False
    assert client.added == []
    assert s.queue == []
    assert s.store.path.read_bytes() == before

@pytest.mark.parametrize('manual', [True, False])
def test_valid_platform_still_hands_off_once(tmp_path, manual):
    s, client = service(tmp_path)
    result = s.grab(release(), 'Fixture', 'switch', manual=manual)
    assert result['ok'] is True
    assert len(client.added) == 1
    assert len(s.queue) == 1
    assert s.queue[0].platform == 'switch'
    assert s.queue[0].state == 'grabbed'

def test_cached_any_platform_search_cannot_bypass_the_guard(tmp_path):
    s, client = service(tmp_path)
    s._candidates['fixture-search'] = {'fixture-release': (release(), 'Fixture', '')}
    result = s.grab_candidate('fixture-release')
    assert result['ok'] is False
    assert 'platform' in result['error'].lower()
    assert client.added == []
    assert s.queue == []

@pytest.mark.parametrize('entry', ['grab', '_grab_impl'])
def test_invalid_platform_never_selects_client_or_dispatches_external_request(
        tmp_path, monkeypatch, entry):
    s, client = service(tmp_path)
    s.store.put_seerr_request(SeerrRequest('fixture', 'switch', status='searching'))
    before = s.store.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail('invalid platform reached client selection or request dispatch')

    monkeypatch.setattr('romarr.app.pick_client', forbidden)
    monkeypatch.setattr(s.store, 'mark_seerr_dispatching', forbidden)
    result = getattr(s, entry)(release(), 'Fixture', '', external_request_id='fixture')
    assert result['ok'] is False
    assert client.added == []
    assert s.store.path.read_bytes() == before


def test_platform_alias_is_canonicalized_in_persisted_queue(tmp_path):
    s, client = service(tmp_path)
    result = s.grab(release(), 'Fixture', 'Nintendo Switch', manual=True)
    assert result['ok'] is True
    assert len(client.added) == 1
    assert s.queue[0].platform == 'switch'
    assert Store(s.store.path).queue[0].platform == 'switch'
