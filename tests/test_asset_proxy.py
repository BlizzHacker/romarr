"""Covers from an http:// library must survive an https page (issue #49).

The browser refuses to render http images on an https document (mixed
content), so a RomM reached internally over http://romm:8080 produced a
library shelf with blank posters. The fix rewrites http cover URLs to a
same-origin relay route; these tests pin both halves -- the rewrite and
the relay's refusal to be an open proxy.
"""

from unittest.mock import MagicMock

from romarr.app import ROMarr


# -- the rewrite in Romm.games ------------------------------------------------

def _romm_games(monkeypatch, base_url, cover_field, cover_value):
    """Drive Romm.games() over a stubbed transport and return the covers."""
    from romarr import clients

    romm = clients.Romm(clients.RommConfig(
        base_url=base_url, username="u", password="p"))
    monkeypatch.setattr(romm, "_get", lambda *a, **kw: MagicMock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"items": [{
            "id": 1, "name": "Super Metroid", cover_field: cover_value,
        }]}))
    out = romm.games(limit=10)
    return [g.cover for g in out]


def test_an_http_cover_is_rewritten_to_the_relay(monkeypatch):
    [cover] = _romm_games(
        monkeypatch, "http://romm:8080",
        "path_cover_small", "/assets/romm/resources/roms/323/cover/small.png")
    assert cover.startswith("/api/v1/asset?url=")
    # The original URL survives, percent-encoded, so the relay can fetch it.
    assert "http%3A%2F%2Fromm%3A8080%2Fassets" in cover


def test_an_https_cover_is_left_direct(monkeypatch):
    """No proxy needed when the browser can fetch it anyway -- and no bytes
    through ROMarr that do not have to be."""
    [cover] = _romm_games(
        monkeypatch, "https://romm.example.com",
        "path_cover_small", "https://romm.example.com/assets/x/cover.png")
    assert cover == "https://romm.example.com/assets/x/cover.png"


def test_a_missing_cover_stays_empty(monkeypatch):
    [cover] = _romm_games(monkeypatch, "http://romm:8080",
                          "path_cover_small", "")
    assert cover == ""


# -- the relay's refusal to be an open proxy ----------------------------------

def _svc(tmp_path, library_url="http://romm:8080"):
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    backend = MagicMock()
    backend._config = MagicMock(base_url=library_url)
    svc.game_libraries = [({"name": "romm", "url": library_url}, backend)]
    return svc


def test_the_relay_serves_a_configured_library_origin(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    def fake_get(url, **kw):
        assert url == "http://romm:8080/assets/x/cover.png"
        return MagicMock(status_code=200,
                         headers={"Content-Type": "image/png; charset=b"},
                         content=png,
                         __enter__=lambda s: s, __exit__=lambda *a: None)

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    code, ctype, body = svc.fetch_asset(
        "http://romm:8080/assets/x/cover.png")
    assert code == 200
    assert ctype == "image/png"          # parameter stripped
    assert body == png


def test_the_relay_refuses_a_foreign_origin(tmp_path, monkeypatch):
    """Without the origin rule this endpoint is an SSRF scanner: any session
    holder could walk ROMarr's internal network through it."""
    svc = _svc(tmp_path)
    fetched = []
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: fetched.append(url))
    code, _, body = svc.fetch_asset("http://192.168.0.1/admin")
    assert code == 403
    assert fetched == []                 # never even asked
    assert b"not a configured library" in body


def test_the_relay_refuses_a_non_http_scheme(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    fetched = []
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: fetched.append(url))
    for bad in ("file:///etc/passwd", "ftp://romm/x.png", "", "romm:8080/x"):
        code, _, _ = svc.fetch_asset(bad)
        assert code == 400, bad
    assert fetched == []


def test_the_relay_reports_an_unreachable_library_as_502(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    import requests

    def down(url, **kw):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "get", down)
    code, ctype, body = svc.fetch_asset("http://romm:8080/assets/x.png")
    assert code == 502 and b"unreachable" in body


def test_the_relay_passes_a_library_404_through(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: MagicMock(
        status_code=404, headers={}, content=b"",
        __enter__=lambda s: s, __exit__=lambda *a: None))
    code, _, _ = svc.fetch_asset("http://romm:8080/assets/missing.png")
    assert code == 404


def test_an_https_library_origin_is_also_allowed(tmp_path, monkeypatch):
    """The rule is origin equality, not scheme preference: an https library
    configured with its internal name is relayed too, which matters when the
    internal cert does not match the public name."""
    svc = _svc(tmp_path, library_url="https://romm.internal")
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: MagicMock(
        status_code=200, headers={"Content-Type": "image/webp"},
        content=b"RIFF", __enter__=lambda s: s, __exit__=lambda *a: None))
    code, ctype, _ = svc.fetch_asset("https://romm.internal/assets/x.webp")
    assert code == 200 and ctype == "image/webp"


def test_the_ui_renders_covers_through_the_cover_field(tmp_path):
    """The shelf's poster div reads `g.cover` verbatim, so a relative relay
    URL renders same-origin with zero UI changes -- assert the template
    still binds that one field and nothing grew a second code path."""
    from romarr.ui import page
    html = page()
    assert "background-image:url('${esc(g.cover||'')}')" in html
    assert "background-image:url('${esc(g.cover_url||'')}')" in html
