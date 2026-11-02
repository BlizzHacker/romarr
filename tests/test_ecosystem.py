"""The ecosystem attribution: real projects, real links, no fabrication."""

from romarr.ecosystem import ECOSYSTEM, all_projects, as_dict


def test_every_project_has_a_home():
    """A credit with no link is not a credit. Each project points at a repo
    or a site (some have only one -- ES-DE and Batocera are sites)."""
    for p in all_projects():
        assert p.repo or p.site, f"{p.name} links nowhere"
        for url in (p.repo, p.site):
            if url:
                assert url.startswith("http"), f"{p.name}: {url}"


def test_the_upstreams_that_made_this_possible_are_all_present():
    names = {p.name for p in all_projects()}
    for essential in ("RomM", "Gaseous", "Retrom", "Gameyfin", "GG Requestz",
                      "Prowlarr", "qBittorrent", "No-Intro", "Redump",
                      "EmulatorJS"):
        assert essential in names, f"{essential} missing from the credits"


def test_romarr_marks_itself_and_only_itself():
    selves = [p for p in all_projects() if p.is_self]
    assert len(selves) == 1
    assert selves[0].name == "ROMarr"


#: Every package manager an entry here is allowed to install from.
#:
#: `docker` alone until the browser players arrived: js-dos and Nostalgist.js
#: are npm libraries and have no image to pull, and dropping their install
#: line to satisfy the old shape would have made the page less useful to
#: protect a test. Widened rather than removed -- the point of the check is
#: that the field holds a command somebody can paste, not prose about how to
#: get the thing.
INSTALLERS = ("docker", "npm", "pip", "apt", "brew")


def test_install_commands_are_commands_not_prose():
    for p in all_projects():
        if p.install:
            first = p.install.split(" ")[0]
            assert (" " in p.install and first in INSTALLERS) or \
                p.install.startswith("http"), \
                f"{p.name} install looks wrong: {p.install!r}"


def test_ggrequestz_points_at_the_real_upstream():
    """XTREEMMAK/ggrequestz is the source; BlizzHacker runs a fork. Credit
    goes upstream."""
    gg = next(p for p in all_projects() if p.name == "GG Requestz")
    assert gg.repo == "https://github.com/XTREEMMAK/ggrequestz"


def test_as_dict_round_trips_every_category():
    d = as_dict()
    assert set(d) == set(ECOSYSTEM)
    assert d["Library servers — where your games live"][0]["name"] == "RomM"


def test_the_page_and_route_exist():
    from romarr.ui import page
    p = page()
    assert 'data-page="ecosystem"' in p
    assert "RENDER.ecosystem" in p
    assert "/api/v1/ecosystem" in p
    assert "data-copy" in p, "install commands are copyable"


def test_romm_ecosystem_and_igir_v580_are_present():
    names = {p.name for p in all_projects()}
    for item in (
        "Igir Collection Manager",
        "Argosy Launcher", "Grout", "RomM Playnite Plugin",
        "Tinfoil", "pkgj", "pkgi", "fpkgi", "Kekatsu",
        "RetroArch Cloud Sync",
        "romm-ios-app", "romm-mobile", "RommBrowser", "RomMate",
        "romm-client", "DeckRommSync", "SwitchRomM", "RetroArch Sync",
        "romm-comm", "GG Requestz",
        "Client API Tokens", "Device Sync Protocol", "SSH Sync",
    ):
        assert item in names, f"{item} missing from ecosystem"


def test_portal_fleet_and_mcp_routes_are_wired():
    from romarr.ui import page
    from romarr import hub
    html = page()
    assert 'data-page="portal"' in html
    assert "RENDER.portal=" in html
    assert "/api/v1/hub/fleet" in html
    assert "/api/v1/hub/igir" in html
    assert "/api/v1/mcp" in html
    plan = hub.build_fleet_sync_plan({"device_id": "retropie-arcadepunks", "transport": "ssh"})
    assert plan["ok"] is True
    assert "igir" in plan["igir_command"]
    assert "rsync" in plan["network_sync_command"]
    mcp = hub.mcp_manifest()
    assert "open-eyes" in mcp["mcp_client_config"]["mcpServers"]
