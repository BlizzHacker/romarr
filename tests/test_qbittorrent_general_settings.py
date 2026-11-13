"""The General page displays enabled qBittorrent clients, not stale env values."""

from romarr.app import ROMarr
from romarr.ui import page


def _service(tmp_path, **env):
    """Build a test service with isolated settings and no remote requests."""
    return ROMarr(env={
        "ROMARR_DATA": str(tmp_path / "state.json"),
        "LIBRARY_PATH": str(tmp_path),
        **env,
    })


def test_general_urls_follow_ui_configured_client_without_environment(tmp_path):
    """A connection added in Download Clients appears on General."""
    service = _service(tmp_path)
    assert service.safe_settings()["_qbit_urls"] == []
    service.store.put_item("download_clients", {
        "type": "qbittorrent", "name": "Primary", "enable": True,
        "host": "qbit.example", "port": 8443, "use_ssl": True,
        "url_base": "/webui/", "username": "user", "password": "secret",
    })
    service.reload_clients()

    settings = service.safe_settings()
    assert settings["_qbit_urls"] == ["https://qbit.example:8443/webui"]
    assert settings["_qbit_url"] == ""
    assert settings["download_clients"][0]["password"] != "secret"


def test_general_urls_ignore_stale_environment_after_edit(tmp_path):
    """An edited client takes precedence over the first-run environment seed."""
    service = _service(tmp_path, QBITTORRENT_URL="http://old.example:8080")
    original = dict(service.store.list_items("download_clients")[0])
    service.store.put_item("download_clients", {
        **original, "host": "new.example", "port": 9090, "use_ssl": True,
    })
    service.reload_clients()

    settings = service.safe_settings()
    assert settings["_qbit_urls"] == ["https://new.example:9090"]
    assert settings["_qbit_url"] == "http://old.example:8080"


def test_general_shows_all_enabled_qbittorrent_clients_only(tmp_path):
    """Multiple active endpoints are listed without unrelated or disabled ones."""
    service = _service(tmp_path)
    for name, kind, host, enabled in (
        ("First", "qbittorrent", "one.example", True),
        ("Disabled", "qbittorrent", "disabled.example", False),
        ("Transmission", "transmission", "transmission.example", True),
        ("Second", "qbittorrent", "two.example", True),
    ):
        service.store.put_item("download_clients", {
            "type": kind, "name": name, "host": host, "port": 8080,
            "enable": enabled,
        })
    service.reload_clients()

    assert service.safe_settings()["_qbit_urls"] == [
        "http://one.example:8080",
        "http://two.example:8080",
    ]


def test_general_url_never_shows_embedded_userinfo(tmp_path):
    """An accidentally pasted username and password are stripped from the URL."""
    service = _service(tmp_path)
    service.store.put_item("download_clients", {
        "type": "qbittorrent", "name": "Primary", "enable": True,
        "host": "http://alice:embedded-secret@qbit.example",
        "port": 8080,
    })
    service.reload_clients()

    assert service.safe_settings()["_qbit_urls"] == [
        "http://qbit.example:8080",
    ]


def test_deleted_client_disappears_from_general_urls(tmp_path):
    """Removing the last client makes General report none on the next read."""
    service = _service(tmp_path)
    row = service.store.put_item("download_clients", {
        "type": "qbittorrent", "name": "Temporary", "enable": True,
        "host": "temporary.example", "port": 8080,
    })
    service.reload_clients()
    assert service.safe_settings()["_qbit_urls"] == [
        "http://temporary.example:8080",
    ]
    assert service.store.delete_item("download_clients", row["id"]) is True
    service.reload_clients()
    assert service.safe_settings()["_qbit_urls"] == []


def test_general_navigation_fetches_fresh_settings_with_page_guard():
    """General must not render a boot snapshot or overwrite a later page."""
    html = page()
    renderer = html.split("RENDER.general=async()=>{", 1)[1].split(
        "/* ---------------- system", 1)[0]
    assert "const response=await fetch('/api/v1/config')" in renderer
    assert "activePage!=='general'||pageVersion!==opVer" in renderer
    assert "SETTINGS=fresh;" in renderer
    assert renderer.index("await fetch('/api/v1/config')") < renderer.index(
        "settingsPage('General'")


def test_general_fetch_rejects_http_errors_before_parsing():
    """An HTTP 401/500 JSON body is not a valid settings response."""
    html = page()
    renderer = html.split("RENDER.general=async()=>{", 1)[1].split(
        "/* ---------------- system", 1)[0]
    assert "if(!response.ok) throw new Error('Settings request failed')" in renderer
    assert renderer.index("if(!response.ok)") < renderer.index(
        "fresh=await response.json()")
    assert "Could not load settings. Reopen General to retry." in renderer


def test_general_renderer_reads_active_qbittorrent_urls():
    """The rendered JavaScript uses the new client list, not legacy _qbit_url."""
    html = page()
    assert "SETTINGS._qbit_urls.map" in html
    assert "No enabled qBittorrent client" in html
    assert 'href="#clients"' in html
    assert "esc(SETTINGS._qbit_url||'')" not in html
