"""Searching the plugin catalogue, installing your own, submitting one."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from romarr.catalogue import (
    DEFAULT_ALLOWED_HOSTS, Submission, check_source, facets, search,
    submission_link)

ITEMS = [
    {"slug": "nointro-archive", "name": "No-Intro Archive",
     "author": "blizz", "description": "Cartridge dumps from Archive.org",
     "capabilities": ["search", "importer"], "platforms": ["snes", "nes"],
     "installed": True},
    {"slug": "redump-mirror", "name": "Redump Mirror", "author": "someone",
     "description": "Disc images", "capabilities": ["search"],
     "platforms": ["psx"], "installed": False},
    {"slug": "cover-art", "name": "Cover Art", "author": "blizz",
     "description": "Box art and screenshots for the archive",
     "capabilities": ["metadata"], "platforms": [], "installed": False},
]


# --- search ----------------------------------------------------------------

def test_an_exact_slug_wins():
    """What somebody typing `redump-mirror` meant. Burying it under fuzzy
    name matches makes search feel broken even though it worked."""
    assert search(ITEMS, "redump-mirror")[0]["slug"] == "redump-mirror"


def test_a_partial_slug_matches_before_a_description():
    got = search(ITEMS, "archive")
    assert got[0]["slug"] == "nointro-archive", [g["slug"] for g in got]
    assert any(g["slug"] == "cover-art" for g in got), "description still counts"


def test_search_matches_the_author():
    assert {g["slug"] for g in search(ITEMS, "blizz")} == {"nointro-archive",
                                                          "cover-art"}


def test_search_is_case_insensitive():
    assert search(ITEMS, "REDUMP")[0]["slug"] == "redump-mirror"


def test_an_empty_query_returns_everything():
    assert len(search(ITEMS, "")) == len(ITEMS)


def test_nothing_matching_returns_nothing_rather_than_everything():
    assert search(ITEMS, "zzzz") == []


def test_filtering_by_capability():
    got = search(ITEMS, capability="search")
    assert {g["slug"] for g in got} == {"nointro-archive", "redump-mirror"}


def test_filtering_by_platform():
    assert [g["slug"] for g in search(ITEMS, platform="psx")] == ["redump-mirror"]


def test_filtering_by_installed_state():
    assert [g["slug"] for g in search(ITEMS, installed=True)] == ["nointro-archive"]
    assert len(search(ITEMS, installed=False)) == 2


def test_filters_combine_with_the_query():
    assert search(ITEMS, "archive", capability="metadata")[0]["slug"] == "cover-art"


# --- facets ----------------------------------------------------------------

def test_facets_are_built_from_what_is_actually_there():
    """Not a hardcoded list, so a new capability appears in the filter menu
    the moment a plugin declares one."""
    got = facets(ITEMS)
    assert ("search", 2) in got["capabilities"]
    assert ("metadata", 1) in got["capabilities"]
    assert ("snes", 1) in got["platforms"]


def test_facets_of_an_empty_catalogue_are_empty_not_an_error():
    assert facets([]) == {"capabilities": [], "platforms": []}


# --- install your own ------------------------------------------------------

def test_a_known_forge_over_https_is_allowed():
    for host in DEFAULT_ALLOWED_HOSTS:
        assert check_source(f"https://{host}/me/my-plugin").ok, host


def test_plain_http_is_refused():
    """Not pedantry: http means anybody on the network path chooses what code
    ROMarr executes."""
    got = check_source("http://github.com/me/my-plugin")
    assert not got.ok
    assert "https" in got.reason


def test_an_unknown_host_is_refused_and_says_which_are_allowed():
    """A plugin is code ROMarr runs. "Install from any URL" is remote code
    execution with a text box in front of it."""
    got = check_source("https://evil.example/me/plugin")
    assert not got.ok
    assert "evil.example" in got.reason
    assert "github.com" in got.reason


def test_a_self_hosted_forge_can_be_allowed_deliberately():
    assert check_source("https://git.moveweight.com/me/plugin",
                        allowed_hosts=["git.moveweight.com"]).ok


def test_a_bare_host_is_not_a_repository():
    assert not check_source("https://github.com").ok
    assert not check_source("https://github.com/").ok


@pytest.mark.parametrize("url", ["", None, "not a url", "ftp://github.com/a/b",
                                 "javascript:alert(1)"])
def test_junk_is_refused(url):
    assert not check_source(url).ok


# --- submission ------------------------------------------------------------

def good_submission(**kw):
    return Submission(**{
        "slug": "my-plugin", "name": "My Plugin",
        "repository": "https://github.com/me/my-plugin",
        "author": "me", "description": "Does a thing",
        "capabilities": ["search"], "platforms": ["snes"], **kw})


def test_a_complete_submission_has_no_problems():
    assert good_submission().problems() == []


def test_every_problem_is_reported_at_once():
    """A form that reveals one error per attempt takes five round trips, and
    each one is a chance to give up."""
    problems = Submission().problems()
    assert len(problems) >= 4
    assert any("slug" in p for p in problems)
    assert any("description" in p for p in problems)
    assert any("capability" in p for p in problems)


@pytest.mark.parametrize("slug", ["Bad Slug", "UPPER", "trailing-", "-leading",
                                  "double--hyphen", "with_underscore", ""])
def test_a_bad_slug_is_rejected(slug):
    """It becomes a directory name and a CLI argument, so it is validated
    rather than trusted."""
    assert any("slug" in p for p in good_submission(slug=slug).problems())


def test_a_submission_with_no_capability_is_rejected():
    assert any("capability" in p
               for p in good_submission(capabilities=[]).problems())


def test_a_submission_from_an_unknown_host_is_rejected():
    problems = good_submission(repository="https://evil.example/a/b").problems()
    assert any("repository" in p for p in problems)


# --- the link, which ROMarr prepares and never follows ---------------------

def test_the_link_is_a_prefilled_issue():
    link = submission_link(good_submission())
    assert link.startswith("https://github.com/BlizzHacker/rom-hub/issues/new?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    assert "my-plugin" in query["title"][0]
    assert "plugin-submission" in query["labels"][0]


def test_the_link_carries_the_entry_as_valid_json():
    """Whoever reviews it should be able to paste it into the catalogue rather
    than retype it from prose."""
    link = submission_link(good_submission())
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)["body"][0]
    block = body.split("```json")[1].split("```")[0]
    entry = json.loads(block)
    assert entry["slug"] == "my-plugin"
    assert entry["capabilities"] == ["search"]


def test_the_link_escapes_a_description_that_would_break_the_url():
    link = submission_link(good_submission(
        description="Has & ampersands, #hashes and a\nnewline"))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    assert "ampersands" in query["body"][0]
    assert "newline" in query["body"][0]


def test_submitting_sends_nothing():
    """ROMarr prepares the submission and hands back a link. Publishing under
    somebody's name is their decision, and a tool that posts because they
    clicked a button in a settings page has made it for them -- it also means
    this works with no token configured, which is the normal case.
    """
    import inspect

    from romarr import catalogue

    source = inspect.getsource(catalogue)
    for outbound in ("requests.post", "urlopen", "http.client", "session.post"):
        assert outbound not in source, f"catalogue must not send: {outbound}"
