"""Browser player wiring for the authenticated Library Shell."""

from romarr.app import ROMarr
from romarr.portal_player import PLAYER_SHIM
from romarr.ui import JS


def test_proxied_document_installs_romm_player_support_before_the_app():
    """Navigation within the embedded app retains the loader guard."""
    upstream = b'<html><head><script src="/assets/app.js"></script></head></html>'
    result = ROMarr._inject_portal_shim(upstream, "http://library.example")
    shim_position = result.index(PLAYER_SHIM.encode())
    assert shim_position < result.index(b'/assets/app.js')


def test_portal_opens_from_config_and_defers_the_device_inventory():
    """Opening the library should not wait for integration health probes."""
    renderer = JS.split("RENDER.portal=async()=>{", 1)[1].split("\n};", 1)[0]
    assert "/api/v1/library/config" in renderer
    assert "/api/v1/system/status" not in renderer
    assert "await fleetRequest" not in renderer
