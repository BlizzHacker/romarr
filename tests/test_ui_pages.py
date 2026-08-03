"""Every page in the nav has a renderer, and every renderer is reachable.

The counterpart to `test_wired.py`, one layer up. A nav entry with no
`RENDER.<page>` produces a menu item that clicks to a blank screen -- the UI
version of a module nobody calls, and just as invisible to a passing test
suite.
"""

from __future__ import annotations

import re

import pytest

from romarr.ui import NAV, page


SOURCE = page()


def nav_pages() -> list[str]:
    return [slug for _, items in NAV for slug, _, _ in items]


@pytest.mark.parametrize("slug", nav_pages())
def test_every_nav_entry_has_a_renderer(slug):
    assert f"RENDER.{slug}=" in SOURCE, (
        f"'{slug}' is in the nav but has no RENDER.{slug} -- it clicks to a "
        "blank screen")


@pytest.mark.parametrize("slug", nav_pages())
def test_every_nav_entry_has_a_page_title(slug):
    titles = re.search(r"const titles=\{(.*?)\};", SOURCE, re.S)
    assert titles, "the title map moved"
    assert f"{slug}:" in titles.group(1), f"'{slug}' has no page title"


def test_every_renderer_is_in_the_nav():
    """A renderer nobody can navigate to is dead code with a stylesheet."""
    rendered = set(re.findall(r"RENDER\.([a-z]+)=", SOURCE))
    assert rendered - set(nav_pages()) == set()


def test_the_pages_added_for_the_new_backends_exist():
    """Blocklist, connections, metadata, calendar and manual import all had
    working endpoints and no way to reach them."""
    for slug in ("blocklist", "connections", "metadata", "calendar",
                 "manualimport"):
        assert f"RENDER.{slug}=" in SOURCE, slug


def test_the_catalogue_grid_does_not_collide_with_the_library_grid():
    """`.grid` is the library's 150px poster grid and is defined later in the
    same stylesheet, so it won on equal specificity and rendered the plugin
    catalogue as seven cramped columns. Caught by looking at a screenshot --
    the markup was correct and every test passed.
    """
    assert ".pgrid{" in SOURCE
    assert 'class="pgrid"' in SOURCE
