"""Every way ROMarr can be pointed at an indexer, not just Prowlarr.

Radarr's Add Indexer dialog offers Newznab, Torznab and a handful of direct
trackers. ROMarr offered Prowlarr, Torznab and Newznab, which meant an
operator running Jackett had to know that Jackett speaks Torznab and hand-build
the `/api/v2.0/indexers/all/results/torznab/` URL themselves -- and an operator
running Bitmagnet or NZBHydra2 had nothing telling them it would work at all.

Four of the products below are Torznab or Newznab underneath. That is the
point: they are definitions over two protocol clients, not six new clients.
Two -- a plain RSS feed and TorrentPotato -- genuinely are different, and get
their own parsers.
"""

from __future__ import annotations

import pytest

from romarr import indexers
from romarr.indexers import (
    INDEXER_TYPES, TorrentPotato, TorrentPotatoConfig, TorrentRss,
    TorrentRssConfig, build_indexer, driver_for)


# --- the registry ----------------------------------------------------------

EXPECTED = ("prowlarr", "jackett", "nzbhydra2", "cardigann", "bitmagnet",
            "torznab", "newznab", "torrentrss", "torrentpotato")


@pytest.mark.parametrize("kind", EXPECTED)
def test_every_advertised_indexer_type_exists(kind):
    assert kind in INDEXER_TYPES, kind


@pytest.mark.parametrize("kind", EXPECTED)
def test_every_type_declares_a_driver_and_a_protocol(kind):
    spec = INDEXER_TYPES[kind]
    assert spec.get("driver") in ("prowlarr", "torznab", "rss", "potato"), kind
    assert spec.get("protocol") in ("torrent", "usenet", "any"), kind
    assert spec.get("label"), kind
    assert spec.get("fields"), kind


@pytest.mark.parametrize("kind", EXPECTED)
def test_every_type_can_be_redacted_without_leaking_a_secret(kind):
    """A new type whose secret field is not declared would put a live API key
    into the settings page."""
    cfg = {"type": kind, "name": "x", "api_key": "SECRET", "url": "http://h"}
    out = indexers.redact_indexer(cfg)
    assert "SECRET" not in str(out), kind


# --- the Torznab family ----------------------------------------------------

@pytest.mark.parametrize("kind,protocol", [
    ("jackett", "torrent"),
    ("cardigann", "torrent"),
    ("bitmagnet", "torrent"),
    ("nzbhydra2", "usenet"),
    ("torznab", "torrent"),
    ("newznab", "usenet"),
])
def test_the_torznab_family_builds_a_working_client(kind, protocol):
    client = build_indexer({"type": kind, "url": "http://host/api",
                            "api_key": "k", "name": kind})
    assert client is not None, kind
    assert client._config.protocol == protocol, kind


def test_prowlarr_is_still_driven_separately():
    """It aggregates many indexers rather than being one, so the service holds
    it apart. build_indexer returning a client for it would search it twice."""
    assert build_indexer({"type": "prowlarr", "url": "http://h", "api_key": "k"}) is None


def test_an_indexer_with_no_url_builds_nothing():
    assert build_indexer({"type": "jackett", "api_key": "k"}) is None


def test_jackett_tells_you_the_url_shape_that_actually_works():
    """The single most common Jackett misconfiguration is pointing ROMarr at
    the Jackett homepage. The field help has to carry the real path."""
    fields = {f["name"]: f for f in INDEXER_TYPES["jackett"]["fields"]}
    assert "torznab" in fields["url"].get("help", "").lower()
    assert "9117" in str(fields["url"].get("default", ""))


def test_bitmagnet_needs_no_api_key():
    """It indexes the DHT itself, so there is no third party to authenticate
    to. Demanding a key would make a working setup look misconfigured."""
    fields = {f["name"]: f for f in INDEXER_TYPES["bitmagnet"]["fields"]}
    assert not fields["api_key"].get("required", False)
    client = build_indexer({"type": "bitmagnet", "url": "http://h:3333/torznab"})
    assert client is not None


def test_nzbhydra2_defaults_to_its_own_port():
    fields = {f["name"]: f for f in INDEXER_TYPES["nzbhydra2"]["fields"]}
    assert "5076" in str(fields["url"].get("default", ""))


# --- a plain RSS feed ------------------------------------------------------

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item>
  <title>Chrono Trigger (USA) [!]</title>
  <link>magnet:?xt=urn:btih:aaaabbbbccccddddeeeeffff00001111&amp;dn=ct</link>
  <enclosure url="magnet:?xt=urn:btih:aaaabbbbccccddddeeeeffff00001111"
             length="3145728" type="application/x-bittorrent"/>
</item>
<item>
  <title>Super Metroid (USA)</title>
  <link>https://tracker.example/dl/2.torrent</link>
  <enclosure url="https://tracker.example/dl/2.torrent" length="2097152"
             type="application/x-bittorrent"/>
</item>
</channel></rss>"""


def test_an_rss_feed_yields_releases():
    feed = TorrentRss(TorrentRssConfig(url="http://f/rss", name="Feed"))
    got = feed.parse(RSS)
    assert [r.title for r in got] == ["Chrono Trigger (USA) [!]",
                                      "Super Metroid (USA)"]
    assert got[0].size == 3145728
    assert got[0].download_url.startswith("magnet:")
    assert got[1].download_url.endswith(".torrent")


def test_rss_results_carry_a_game_category_so_they_survive_scoring():
    """`is_game_release` rejects anything without a game category, and a plain
    RSS feed carries no categories at all -- so every result from one would be
    thrown away before it was ever scored. The feed is a game feed by
    configuration; that is what the category asserts."""
    feed = TorrentRss(TorrentRssConfig(url="http://f/rss", name="Feed"))
    from romarr.selection import is_game_release
    assert all(is_game_release(r) for r in feed.parse(RSS))


def test_an_rss_feed_marked_private_says_so_on_its_releases():
    feed = TorrentRss(TorrentRssConfig(url="http://f", name="F", private=True))
    assert all(r.private for r in feed.parse(RSS))


def test_a_malformed_feed_is_empty_not_an_exception():
    feed = TorrentRss(TorrentRssConfig(url="http://f", name="F"))
    assert feed.parse("<not xml") == []
    assert feed.parse("") == []


def test_an_rss_item_with_no_usable_link_is_dropped():
    body = ("<rss><channel><item><title>No link</title></item>"
            "<item><title>Fine</title><link>magnet:?xt=urn:btih:ab</link>"
            "</item></channel></rss>")
    feed = TorrentRss(TorrentRssConfig(url="http://f", name="F"))
    assert [r.title for r in feed.parse(body)] == ["Fine"]


# --- TorrentPotato ---------------------------------------------------------

POTATO = {
    "results": [
        {"release_name": "Chrono Trigger (USA)",
         "download_url": "magnet:?xt=urn:btih:abc",
         "details_url": "https://t.example/1",
         "size": 3,                      # TorrentPotato reports MB
         "seeders": 12, "leechers": 3, "freeleech": True},
        {"release_name": "Broken", "size": 1},   # no download_url
    ]
}


def test_torrentpotato_yields_releases():
    client = TorrentPotato(TorrentPotatoConfig(url="http://p", name="Potato"))
    got = client.parse(POTATO)
    assert [r.title for r in got] == ["Chrono Trigger (USA)"]
    assert got[0].seeders == 12


def test_torrentpotato_size_is_megabytes_and_is_converted():
    """The protocol reports size in MB. Taking it as bytes makes a 3 MB SNES
    release look like 3 bytes and it is rejected as "too small to be a ROM" --
    a whole indexer that silently returns nothing usable."""
    client = TorrentPotato(TorrentPotatoConfig(url="http://p", name="Potato"))
    assert client.parse(POTATO)[0].size == 3 * 1024 * 1024


def test_torrentpotato_survives_junk():
    client = TorrentPotato(TorrentPotatoConfig(url="http://p", name="Potato"))
    assert client.parse({}) == []
    assert client.parse({"results": "nonsense"}) == []
    assert client.parse(None) == []


# --- the driver lookup the service uses ------------------------------------

def test_driver_for_maps_each_type_to_its_family():
    assert driver_for("jackett") == "torznab"
    assert driver_for("nzbhydra2") == "torznab"
    assert driver_for("bitmagnet") == "torznab"
    assert driver_for("torrentrss") == "rss"
    assert driver_for("torrentpotato") == "potato"
    assert driver_for("prowlarr") == "prowlarr"
    assert driver_for("nonsense") == ""


def test_build_indexer_covers_every_non_prowlarr_type():
    """A type in the registry that build_indexer cannot construct is dead
    configuration that tests green -- the exact failure the direct-Torznab
    work was introduced to fix."""
    for kind, spec in INDEXER_TYPES.items():
        if spec["driver"] == "prowlarr":
            continue
        client = build_indexer({"type": kind, "url": "http://h/api",
                                "api_key": "k", "name": kind})
        assert client is not None, f"{kind} is configurable but not searchable"
        assert hasattr(client, "search") and hasattr(client, "caps"), kind
