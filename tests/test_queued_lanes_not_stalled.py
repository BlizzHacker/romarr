"""A job waiting its turn in a one-at-a-time lane is not a stalled download."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from romarr.app import ROMarr
from romarr.downloaders import HubConfig, HubDownloader, SiteConfig, SiteDownloader
from romarr.hub import encode_result
from romarr.libraries import LIBRARY_TYPES
from romarr.store import QueueItem


def _hub(n: int) -> str:
    return encode_result({"plugin": "theromdepot", "source_id": str(n), "title": f"Game {n}"})


def _row(title, release_id, minutes_old=240):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_old)).isoformat(timespec="seconds")
    return QueueItem(game=title.split(" (")[0], platform="psx", release=title,
                     indexer="hub:theromdepot", size=300_000_000, seeders=0,
                     state="grabbed", at=stamp, release_id=release_id)


def test_hub_lane_reports_pending_jobs_as_active(tmp_path):
    hub = HubDownloader(HubConfig(save_path=str(tmp_path)))
    assert hub.add(_hub(1), name="Chrono Cross (U) (Disc 1)")
    assert hub.add(_hub(2), name="Chrono Cross (U) (Disc 2)")
    active = hub.active_downloads()
    assert [a["name"] for a in active] == ["Chrono Cross (U) (Disc 1)", "Chrono Cross (U) (Disc 2)"]
    assert all(a["worker_active"] is True for a in active)


def test_site_lane_reports_pending_jobs_as_active(tmp_path):
    site = SiteDownloader(SiteConfig(save_path=str(tmp_path)))
    assert site.add("https://example.org/files/game.zip", name="Game (USA)")
    assert [a["name"] for a in site.active_downloads()] == ["Game (USA)"]


def test_finished_or_failed_jobs_are_not_reported_active(tmp_path):
    hub = HubDownloader(HubConfig(save_path=str(tmp_path)))
    hub.add(_hub(1), name="Done Game")
    hub.add(_hub(2), name="Failed Game")
    hub.add(_hub(3), name="Queued Game")
    rows = hub._read()
    rows[0]["state"] = "done"
    rows[1]["state"] = "failed"
    hub._write(rows)
    assert [a["name"] for a in hub.active_downloads()] == ["Queued Game"]


def test_stall_sweep_leaves_a_queued_hub_job_alone(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.store.settings["stalled_timeout_minutes"] = 180
    hub = HubDownloader(HubConfig(save_path=str(tmp_path / "hub")))
    hub.add(_hub(1), name="Chrono Cross (U) (Disc 1)")
    queued = _row("Chrono Cross (U) (Disc 1)", "hub:theromdepot|chrono cross (u) (disc 1)|1")
    service.queue = [queued]
    service.clients = [hub]

    service.retire_dead_downloads()

    assert queued.state == "grabbed"
    assert not queued.blocklisted
    assert not service.blocklist.contains_id(queued.release_id) \
        if hasattr(service.blocklist, "contains_id") else True
    assert all(e.get("id") != queued.release_id for e in service.store.list_items("blocklist"))


def test_stall_sweep_still_retires_a_job_no_lane_knows(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.store.settings["stalled_timeout_minutes"] = 180
    hub = HubDownloader(HubConfig(save_path=str(tmp_path / "hub")))
    lost = _row("Vanark Astro Trooper (U)", "hub:theromdepot|vanark astro trooper (u)|2")
    service.queue = [lost]
    service.clients = [hub]

    service.retire_dead_downloads()

    assert lost.state == "failed"
    assert lost.detail.startswith("stalled: no import after")


def test_romm_library_offers_a_browser_url():
    fields = {f["name"]: f for f in LIBRARY_TYPES["romm"]["fields"]}
    assert "public_url" in fields
    assert fields["public_url"]["label"] == "Browser URL"
    assert fields["public_url"]["type"] != "secret"


def test_a_debrid_item_finished_in_the_cloud_is_active_while_it_copies(tmp_path, monkeypatch):
    from romarr.downloaders import RealDebrid, RealDebridConfig
    rd = RealDebrid(RealDebridConfig(api_token="t", save_path=str(tmp_path)))
    monkeypatch.setattr(rd, "_finished", lambda: [("Chrono Cross (U)", ["https://x/1"], "")])
    assert rd.active_downloads() == [{"name": "Chrono Cross (U)", "worker_active": True, "progress": 1.0}]
    monkeypatch.setattr(rd, "_finished", lambda: [])
    assert rd.active_downloads() == []


def test_a_library_browser_url_beats_the_global_public_url(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.store.settings["romm_public_url"] = "https://global.example"
    from romarr.clients import Romm
    lib = Romm.__new__(Romm)
    service.game_libraries = [({"name": "RomM", "url": "http://192.168.0.94:8080",
                                "public_url": "https://romm.example"}, lib)]
    assert service._romm_link_base("RomM") == "https://romm.example"
    service.game_libraries = [({"name": "RomM", "url": "http://192.168.0.94:8080"}, lib)]
    assert service._romm_link_base("RomM") == "https://global.example"
