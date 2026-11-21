"""Posting dates, Usenet age limits, NZB links in Torznab feeds and escaped
titles. Behaviour learned from Gamarr and Questarr (GPL-3.0, nothing copied)."""

import datetime as dt

from romarr.indexers import Prowlarr, Torznab, TorznabConfig
from romarr.platforms import by_slug
from romarr.profiles import ReleaseProfile
from romarr.release_guards import is_nzb_link, published_iso, release_age_minutes
from romarr.selection import Release, judge, usenet_age_refusal

FEED = """<?xml version="1.0"?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Ratchet &amp;amp; Clank (USA)</title>
<pubDate>Tue, 17 Nov 2026 10:00:00 +0000</pubDate>
<enclosure url="https://idx.example/get/abc" type="application/x-nzb" length="1000"/>
<torznab:attr name="size" value="1000"/></item>
<item><title>Super Metroid (USA)</title>
<enclosure url="https://idx.example/dl?id=7&amp;file=super.nzb" length="1"/></item>
<item><title>Chrono Trigger (USA)</title>
<enclosure url="https://idx.example/dl/9.torrent" type="application/x-bittorrent" length="1"/>
<torznab:attr name="usenetdate" value="Mon, 16 Nov 2026 08:30:00 +0100"/></item>
</channel></rss>"""


def feed():
    return Torznab(TorznabConfig("https://idx.example/api", "key")).parse(FEED)


def test_an_nzb_in_a_torznab_feed_goes_to_the_usenet_client():
    by_title = {r.title: r for r in feed()}
    assert by_title["Ratchet & Clank (USA)"].protocol == "usenet"
    assert by_title["Super Metroid (USA)"].protocol == "usenet"
    assert by_title["Chrono Trigger (USA)"].protocol == "torrent"


def test_torznab_dates_are_read_from_pubdate_and_usenetdate():
    by_title = {r.title: r for r in feed()}
    assert by_title["Ratchet & Clank (USA)"].published == "2026-11-17T10:00:00Z"
    assert by_title["Chrono Trigger (USA)"].published == "2026-11-16T07:30:00Z"
    assert by_title["Super Metroid (USA)"].published == ""


def test_prowlarr_titles_are_unescaped_and_dated():
    row = {"title": "Ratchet &amp; Clank", "size": 1, "protocol": "usenet",
           "downloadUrl": "https://prowlarr/dl?apikey=x", "publishDate": "2026-11-17T10:00:00Z"}
    release = Prowlarr._to_release(row)
    assert release.title == "Ratchet & Clank"
    assert release.published == "2026-11-17T10:00:00Z"


def test_is_nzb_link():
    assert is_nzb_link("https://x/get/1.NZB")
    assert is_nzb_link("https://x/get?id=1", "application/x-nzb")
    assert not is_nzb_link("magnet:?xt=urn:btih:abc&dn=game.nzb")
    assert not is_nzb_link("https://x/get/1.torrent")


def test_published_iso_and_age():
    assert published_iso("2026-11-17T10:00:00") == "2026-11-17T10:00:00Z"
    assert published_iso("not a date") == ""
    assert published_iso("") == ""
    now = dt.datetime(2026, 11, 17, 11, 0, tzinfo=dt.timezone.utc)
    assert release_age_minutes("2026-11-17T10:00:00Z", now) == 60
    assert release_age_minutes("", now) is None
    assert release_age_minutes("2026-11-18T10:00:00Z", now) == 0


def usenet(published):
    return Release(title="Super Metroid (USA)", size=3 * 1024 * 1024, seeders=0,
                   categories=(1030,), download_url="https://x/1.nzb",
                   protocol="usenet", published=published)


def ago(minutes):
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_usenet_minimum_age_and_retention():
    profile = ReleaseProfile(usenet_minimum_age_minutes=30, usenet_retention_days=10)
    assert "minimum age" in usenet_age_refusal(usenet(ago(5)), profile)
    assert usenet_age_refusal(usenet(ago(120)), profile) == ""
    assert "retention" in usenet_age_refusal(usenet(ago(11 * 1440)), profile)
    # An undated post passes: a missing field must not empty a search.
    assert usenet_age_refusal(usenet(""), profile) == ""
    # Both limits are off by default.
    assert usenet_age_refusal(usenet(ago(1)), ReleaseProfile()) == ""


def test_judge_refuses_a_too_young_usenet_post():
    profile = ReleaseProfile(usenet_minimum_age_minutes=60)
    verdict = judge(usenet(ago(2)), "Super Metroid", by_slug("snes"), profile=profile)
    assert verdict.points < 0 and "minimum age" in verdict.verdict
    fine = judge(usenet(ago(90)), "Super Metroid", by_slug("snes"), profile=profile)
    assert "minimum age" not in fine.verdict


def test_profile_reads_usenet_limits_from_settings_and_platform():
    settings = {"usenet_minimum_age_minutes": "15", "usenet_retention_days": 3000,
                "platform_profiles": {"snes": {"usenet_minimum_age_minutes": 45}}}
    glob = ReleaseProfile.from_settings(settings)
    assert (glob.usenet_minimum_age_minutes, glob.usenet_retention_days) == (15, 3000)
    snes = ReleaseProfile.from_settings(settings, "snes")
    assert snes.usenet_minimum_age_minutes == 45
    assert ReleaseProfile.from_settings({"usenet_retention_days": "junk"}).usenet_retention_days == 0
