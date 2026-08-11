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


def test_install_commands_are_commands_not_prose():
    for p in all_projects():
        if p.install:
            assert " " in p.install and "docker" in p.install or \
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
