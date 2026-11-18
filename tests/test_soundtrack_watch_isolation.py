"""A stalled download client must not stop soundtrack watches on another client."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from romarr.app import ROMarr
from romarr.downloaders import Transmission


def test_a_stalled_client_does_not_block_a_healthy_one(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    release = threading.Event()
    healthy_polled = threading.Event()

    stalled = MagicMock(spec=Transmission)
    stalled.romarr_id = "stalled"
    stalled.torrents.side_effect = lambda **kw: (release.wait(10), [])[1]
    healthy = MagicMock(spec=Transmission)
    healthy.romarr_id = "healthy"
    healthy.torrents.side_effect = lambda **kw: (healthy_polled.set(), [])[1]
    service.clients = [stalled, healthy]

    store = MagicMock()
    store.active_watches.return_value = [{"client_id": "stalled"}, {"client_id": "healthy"}]
    service._soundtrack_libraries = lambda: [("main",)]
    service.soundtrack_store = lambda scope: store
    try:
        service.soundtrack_watch_tick()
        assert healthy_polled.wait(5), "the healthy client was never polled"
        # A second tick must not start another poll of the still-stalled client.
        service.soundtrack_watch_tick()
        time.sleep(0.2)
        assert stalled.torrents.call_count == 1
    finally:
        release.set()
