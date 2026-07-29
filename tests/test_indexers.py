"""Indexer clients: the Torznab parser, and how privacy reaches a Release.

The parser is tested against bodies rather than a network because the shapes
that break it are the irregular ones -- a missing size, a bare infohash, several
category attributes on one item -- and those are exactly what a live indexer
will not reliably produce on demand.
"""

import pytest

from romarr.indexers import (
    Prowlarr, ProwlarrConfig, Torznab, TorznabConfig, build_indexer,
    indexer_categories, redact_indexer, sanitise_for_display,
)


TORZNAB_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <item>
      <title>Super Metroid (USA).smc</title>
      <guid>https://tracker.example/details.php?id=1</guid>
      <link>https://tracker.example/details.php?id=1</link>
      <enclosure url="https://tracker.example/download.php?id=1" type="application/x-bittorrent"/>
      <torznab:attr name="size" value="3145728"/>
      <torznab:attr name="seeders" value="4"/>
      <torznab:attr name="category" value="1000"/>
      <torznab:attr name="category" value="1090"/>
    </item>
    <item>
      <title>Chrono Trigger (USA)</title>
      <guid>abc123</guid>
      <torznab:attr name="infohash" value="ABCDEF0123456789ABCDEF0123456789ABCDEF01"/>
      <torznab:attr name="category" value="1090"/>
    </item>
  </channel>
</rss>
"""


def torznab(**kw):
    defaults = dict(base_url="https://tracker.example/api", api_key="k",
                    name="Example")
    return Torznab(TorznabConfig(**{**defaults, **kw}))


# --- parsing -----------------------------------------------------------------

def test_parses_titles_sizes_seeders_and_every_category():
    releases = torznab().parse(TORZNAB_FEED)
    assert [r.title for r in releases] == [
        "Super Metroid (USA).smc", "Chrono Trigger (USA)"]

    first = releases[0]
    assert first.size == 3145728
    assert first.seeders == 4
    # Both category attributes are kept: is_game_release only needs one of them
    # to be in range, and discarding the others loses that chance.
    assert first.categories == (1000, 1090)
    assert first.indexer == "Example"


def test_an_enclosure_url_is_preferred_over_a_details_link():
    # <link> is frequently a details page. Handing that to a download client
    # fails in a way that looks like the release was bad.
    first = torznab().parse(TORZNAB_FEED)[0]
    assert first.download_url == "https://tracker.example/download.php?id=1"


def test_a_bare_infohash_becomes_a_magnet_for_a_public_indexer():
    second = torznab().parse(TORZNAB_FEED)[1]
    assert second.download_url.startswith("magnet:?xt=urn:btih:ABCDEF0123456789")
    assert "&tr=" in second.download_url


def test_a_private_indexer_never_rebuilds_a_magnet():
    second = torznab(private=True).parse(TORZNAB_FEED)[1]
    # Nothing to proxy through and no legal rebuild, so it reports honestly.
    assert second.download_url == ""
    assert second.private is True


def test_protocol_and_privacy_come_from_the_configuration():
    usenet = torznab(protocol="usenet", private=True).parse(TORZNAB_FEED)
    assert all(r.protocol == "usenet" and r.private for r in usenet)


def test_unparseable_body_is_empty_not_an_exception():
    # An indexer answering with an error page instead of a feed must not take
    # the whole search down; _search_releases logs and continues.
    assert torznab().parse("<html>not a feed</html>") == []
    assert torznab().parse("") == []


def test_missing_size_and_seeders_default_to_zero():
    feed = """<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
      <item><title>Thing</title><link>https://x/y.torrent</link></item>
    </channel></rss>"""
    only = torznab().parse(feed)[0]
    assert only.size == 0 and only.seeders == 0


def test_newznab_attributes_are_read_the_same_way():
    # Newznab uses a different namespace with the same name/value shape.
    feed = """<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
      <channel><item>
        <title>Some Game</title>
        <link>https://usenet.example/x.nzb</link>
        <newznab:attr name="size" value="1048576"/>
        <newznab:attr name="category" value="4050"/>
      </item></channel></rss>"""
    only = torznab(protocol="usenet").parse(feed)[0]
    assert only.size == 1048576
    assert only.categories == (4050,)


# --- construction ------------------------------------------------------------

def test_build_indexer_ignores_prowlarr_entries():
    # Prowlarr aggregates many indexers rather than being one, so the service
    # holds it separately.
    assert build_indexer({"type": "prowlarr", "url": "http://x:9696"}) is None
    assert build_indexer({"type": "torznab", "url": ""}) is None
    assert build_indexer({"type": "nonsense", "url": "http://x"}) is None


def test_build_indexer_maps_type_to_protocol_and_carries_privacy():
    torrent = build_indexer({"type": "torznab", "url": "http://x/api",
                             "name": "T", "private": True})
    usenet = build_indexer({"type": "newznab", "url": "http://x/api", "name": "N"})
    assert torrent._config.protocol == "torrent" and torrent._config.private
    assert usenet._config.protocol == "usenet" and not usenet._config.private


def test_categories_fall_back_to_the_game_defaults_when_unset():
    built = build_indexer({"type": "torznab", "url": "http://x/api"})
    assert built._config.categories  # never empty, or the query matches nothing
    custom = build_indexer({"type": "torznab", "url": "http://x/api",
                            "categories": "1000, 1090, junk"})
    assert custom._config.categories == (1000, 1090)


def test_indexer_categories_ignores_anything_that_is_not_a_number():
    assert indexer_categories({"categories": "1000,,4050,abc"}) == [1000, 4050]
    assert indexer_categories({}) == []


# --- privacy from prowlarr ---------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Answers the indexer list once and records how often it was asked."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return _FakeResponse(self.payload)


def test_privacy_is_read_from_prowlarr_and_cached():
    session = _FakeSession([
        {"id": 7, "name": "bitGAMER", "privacy": "private"},
        {"id": 8, "name": "Public Thing", "privacy": "public"},
    ])
    client = Prowlarr(ProwlarrConfig(base_url="http://p:9696", api_key="k"), session)

    assert client._private_ids() == {7: True, 8: False}
    client._private_ids()
    assert session.calls == 1, "the privacy map must be fetched once, not per search"


def test_a_result_is_marked_private_by_its_indexer_id():
    privacy = {7: True, 8: False}
    private = Prowlarr._to_release(
        {"title": "x", "indexerId": 7, "downloadUrl": "http://p/1?apikey=k",
         "infoHash": "AB" * 20}, privacy)
    public = Prowlarr._to_release(
        {"title": "x", "indexerId": 8, "infoHash": "AB" * 20}, privacy)

    assert private.private is True
    assert private.download_url == "http://p/1?apikey=k"
    assert public.private is False
    assert public.download_url.startswith("magnet:")


def test_an_unknown_indexer_id_is_treated_as_public():
    # Being wrong in this direction can only refuse a usable private release;
    # the other direction invents a magnet that cannot work.
    release = Prowlarr._to_release({"title": "x", "indexerId": 99}, {7: True})
    assert release.private is False


def test_privacy_failure_does_not_break_search():
    class _Broken:
        def get(self, *a, **kw):
            import requests
            raise requests.ConnectionError("nope")

    client = Prowlarr(ProwlarrConfig(base_url="http://p:9696", api_key="k"), _Broken())
    assert client._private_ids() == {}


# --- credential hygiene ------------------------------------------------------

def test_api_keys_are_stripped_before_display():
    assert "SECRET" not in sanitise_for_display(
        "http://prowlarr/1/download?apikey=SECRET&link=x")
    assert sanitise_for_display("") == ""


def test_a_private_tracker_password_is_never_returned_to_a_browser():
    redacted = redact_indexer({"type": "torznab", "name": "RetroWithin",
                               "api_key": "realkey", "private": True})
    assert redacted["api_key"] != "realkey"
    assert redacted["name"] == "RetroWithin"
