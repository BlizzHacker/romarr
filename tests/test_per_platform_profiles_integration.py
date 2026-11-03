"""Integration tests for per-system release profiles, min-size filtering, and customizable scoring (issue #113)."""

from __future__ import annotations

import io
import json
import pytest

from romarr.app import ROMarr, make_handler
from romarr.platforms import by_slug
from romarr.selection import Release, best_release, judge

MB = 1024 * 1024
GB = 1024 * 1024 * 1024
NES = by_slug("nes")
SWITCH = by_slug("switch")
PSX = by_slug("psx")


class FakeClient:
    def __init__(self, service, path, body=None, method="GET"):
        self.service = service
        self.path = path
        payload = json.dumps(body or {}).encode()
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(payload))}
        self.status = None
        self.method = method

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        self.status = code
        self.wfile.write(json.dumps(payload).encode())


def request(service, method, path, body=None):
    Handler = make_handler(service)
    client = FakeClient(service, path, body, method=method)
    fn = getattr(Handler, f"_{method.lower()}")
    fn(client)
    raw = client.wfile.getvalue()
    return client.status, (json.loads(raw) if raw else None)


@pytest.fixture()
def service(tmp_path):
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json"),
                       "ROMARR_AUTH": "disabled"})


def test_put_config_persists_and_reloads_platform_profiles(service):
    patch = {
        "min_size_mb": 1,
        "max_size_mb": 2048,
        "platform_profiles": {
            "nes": {
                "min_size_mb": 0.02,
                "max_size_mb": 8,
                "size_preference": "smaller",
            },
            "switch": {
                "min_size_mb": 500,
                "max_size_mb": 32768,
                "region_scores": {"japan": 100, "usa": 40},
            }
        }
    }
    status, updated = request(service, "PUT", "/api/v1/config", patch)
    assert status == 200
    assert updated["platform_profiles"]["nes"]["min_size_mb"] == 0.02
    assert updated["platform_profiles"]["switch"]["min_size_mb"] == 500

    # Ensure profile_for picks up the new config immediately
    nes_p = service.profile_for("nes")
    assert nes_p.min_size_mb == 0.02
    assert nes_p.max_size_mb == 8

    switch_p = service.profile_for("switch")
    assert switch_p.min_size_mb == 500
    assert switch_p.region_scores == {"japan": 100, "usa": 40}


def test_interactive_search_candidates_applies_platform_profiles_per_release(service, monkeypatch):
    service.store.update_settings({
        "platform_profiles": {
            "nes": {"min_size_mb": 0.01, "max_size_mb": 8},
            "switch": {"min_size_mb": 500, "max_size_mb": 32768},
        }
    })
    service.reload_policy()

    # Releases for different systems returned in a single search
    switch_tiny = Release(title="Super Mario Odyssey (Switch) (NSP)", size=5 * MB, seeders=20,
                          categories=(1000,), download_url="magnet:?1", protocol="torrent")
    switch_good = Release(title="Super Mario Odyssey (Switch) (NSP)", size=6 * GB, seeders=20,
                          categories=(1000,), download_url="magnet:?2", protocol="torrent")
    nes_good = Release(title="Super Mario Bros (NES).nes", size=256 * 1024, seeders=20,
                       categories=(1000,), download_url="magnet:?3", protocol="torrent")
    nes_oversized = Release(title="Super Mario Bros (NES).nes", size=50 * MB, seeders=20,
                            categories=(1000,), download_url="magnet:?4", protocol="torrent")

    monkeypatch.setattr(service.prowlarr, "search",
                        lambda game: [switch_tiny, switch_good, nes_good, nes_oversized])
    monkeypatch.setattr(service, "_hub_releases", lambda game, plat: [])

    # Global search (platform="")
    res = service.candidates("super mario", "")
    items_by_title = {}
    for item in res["items"]:
        items_by_title[(item["title"], item["size"])] = item

    # Switch tiny (5MB) must be rejected because Switch min size is 500MB
    tiny_switch_item = items_by_title[("Super Mario Odyssey (Switch) (NSP)", 5 * MB)]
    assert not tiny_switch_item["accepted"]
    assert any("smaller than release profile minimum size" in r for r in tiny_switch_item["reasons"])

    # Switch good (6GB) is accepted
    good_switch_item = items_by_title[("Super Mario Odyssey (Switch) (NSP)", 6 * GB)]
    assert good_switch_item["accepted"]

    # NES good (256KB) is accepted (above NES min_size of 0.01MB)
    good_nes_item = items_by_title[("Super Mario Bros (NES).nes", 256 * 1024)]
    assert good_nes_item["accepted"]

    # NES oversized (50MB) is rejected (above NES max_size of 8MB)
    over_nes_item = items_by_title[("Super Mario Bros (NES).nes", 50 * MB)]
    assert not over_nes_item["accepted"]
    assert any("larger than release profile maximum size" in r for r in over_nes_item["reasons"])


def test_custom_region_scoring_changes_winner(service):
    service.store.update_settings({
        "platform_profiles": {
            "snes": {
                "region_scores": {"japan": 100, "usa": 20}
            }
        }
    })
    service.reload_policy()

    us_dump = Release(title="Chrono Trigger (USA)", size=3 * MB, seeders=10,
                      categories=(1000,), download_url="magnet:?us", protocol="torrent")
    jp_dump = Release(title="Chrono Trigger (Japan)", size=3 * MB, seeders=10,
                      categories=(1000,), download_url="magnet:?jp", protocol="torrent")

    snes_prof = service.profile_for("snes")
    winner = best_release([us_dump, jp_dump], "chrono trigger", by_slug("snes"), profile=snes_prof)
    assert winner is jp_dump
