"""The play-route answer, as the API and the status page serve it."""

from __future__ import annotations

from romarr.app import ROMarr


def svc(tmp_path, **env):
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), **env})


def test_the_platform_directory_says_how_each_one_plays(tmp_path):
    directory = svc(tmp_path).platform_directory()
    by_slug = {row["slug"]: row for row in directory}

    assert by_slug["psx"]["media"] == "disc"
    assert "local" in by_slug["psx"]["play_routes"]
    assert by_slug["psx"]["plays"] is True

    assert by_slug["snes"]["media"] == "cartridge"
    assert "local" in by_slug["snes"]["play_routes"]


def test_every_platform_in_the_directory_can_be_downloaded(tmp_path):
    for row in svc(tmp_path).platform_directory():
        assert "download" in row["play_routes"], row["slug"]


def test_the_directory_carries_the_extensions_the_importer_uses(tmp_path):
    by_slug = {r["slug"]: r for r in svc(tmp_path).platform_directory()}
    assert by_slug["psx"]["extensions"][0] == ".chd"
    assert ".bin" in by_slug["psx"]["extensions"]


def test_status_counts_the_routes(tmp_path):
    counts = svc(tmp_path).status()["play_routes"]
    assert counts["total"] > 30
    assert counts["local"] > 20
    # Without a stream server, the heavy machines are download-only. Saying so
    # is the point: it is what makes configuring one an obvious win.
    assert counts["download_only"] >= 5


def test_configuring_a_stream_server_is_visible_in_status(tmp_path):
    plain = svc(tmp_path)
    assert plain.stream is None
    assert plain.status()["stream_url"] == ""

    wired = svc(tmp_path / "b", STREAM_SERVER_URL="http://stream.test:8080")
    assert wired.stream is not None
    assert wired.status()["stream_url"] == "http://stream.test:8080"


def test_an_unreachable_stream_server_does_not_break_the_status_page(tmp_path):
    """A LAN service being down is normal and must cost a route, not a page."""
    wired = svc(tmp_path, STREAM_SERVER_URL="http://stream.invalid:9")
    # `service.stream` is the combiner that fronts both stream tiers; the
    # RetroArch client is the thing with a socket on it.
    wired.retroarch.timeout = 0.05
    status = wired.status()
    assert status["play_routes"]["total"] > 30
    assert status["play_routes"]["download_only"] >= 5


def test_a_down_stream_server_is_asked_once_not_once_per_platform(tmp_path):
    """The status page walks every platform. Only *successful* answers are
    cached -- a failure must never be remembered as "cannot play this" -- so
    without a breaker one down server costs one timeout per platform.

    Measured at 69 seconds for a single status page before the breaker
    existed, on an install where the stream server is entirely optional.
    """
    wired = svc(tmp_path, STREAM_SERVER_URL="http://stream.invalid:9")
    attempts = []
    real = wired.retroarch.tier

    def counting(slug):
        attempts.append(slug)
        return real(slug)

    wired.retroarch.timeout = 0.05
    wired.retroarch.tier = counting
    wired.status()

    # Every platform is still asked; the client answers all but the first
    # from its own breaker without touching the network.
    assert len(attempts) > 30
    assert wired.retroarch._down_until > 0
