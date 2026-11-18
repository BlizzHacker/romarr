"""A stalled download client must not stop soundtrack watches on another client."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from romarr.app import ROMarr
from romarr.downloaders import Transmission


def test_a_stalled_client_does_not_block_a_healthy_one(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    release = threading.Event()
    stalled_entered = threading.Event()
    healthy_polled = threading.Event()

    def stall(**kw):
        stalled_entered.set()
        release.wait(10)
        return []

    stalled = MagicMock(spec=Transmission)
    stalled.romarr_id = "stalled"
    stalled.torrents.side_effect = stall
    healthy = MagicMock(spec=Transmission)
    healthy.romarr_id = "healthy"
    healthy.torrents.side_effect = lambda **kw: (healthy_polled.set(), [])[1]
    service.clients = [stalled, healthy]

    stores = {}
    for scope in ("a", "b", "c"):
        st = MagicMock()
        st.active_watches.return_value = [{"client_id": "stalled"}, {"client_id": "healthy"}]
        stores[scope] = st
    service._soundtrack_libraries = lambda: [(s,) for s in stores]
    service.soundtrack_store = lambda scope: stores[scope]
    try:
        service.soundtrack_watch_tick()
        assert stalled_entered.wait(5) and healthy_polled.wait(5)
        # Three libraries watch the stalled client, yet it holds one slot.
        assert "stalled" in service._soundtrack_watch_running
        # A second tick while it is still stalled must not admit another poll.
        assert service.soundtrack_watch_tick() in ("0 soundtrack client watches queued",
                                                   "1 soundtrack client watches queued")
        assert stalled.torrents.call_count == 1
    finally:
        release.set()
