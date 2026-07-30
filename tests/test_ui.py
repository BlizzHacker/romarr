"""The rendered page, as a browser reads it rather than as the source spells it."""

import re

from romarr.ui import page


def _text(html: str) -> str:
    """Tags stripped -- what a reader actually sees."""
    return re.sub(r"<[^>]+>", "", html)


def test_the_brand_reads_romarr():
    """The brand is split across an element boundary so "arr" can carry the
    accent colour: `Rom<span>arr</span>`. That is exactly why the rename from
    Rommarr missed it, and why grepping the source for the old name finds
    nothing -- neither half contains it, only the rendering does.

    So this asserts on rendered text. A test against the source would have
    passed on the broken version.
    """
    html = page()
    brand = re.search(r'<div id="brand">(.*?)</div>', html, re.S)
    assert brand, "the page no longer has a brand element"
    assert _text(brand.group(1)).strip() == "ROMarr"


def test_the_old_name_appears_nowhere_a_user_can_read_it():
    assert "Rommarr" not in _text(page())


def test_the_document_title_is_the_product_name():
    assert re.search(r"<title>ROMarr</title>", page())


def test_the_libraries_page_is_in_the_nav_and_rendered():
    """A feature configured only by hand-editing settings.json is not one most
    people can use."""
    html = page()
    assert 'data-page="libraries"' in html
    assert "RENDER.libraries" in html
    # The generic editor is driven by kind, so the library kind has to be known
    # to the schema cache or Add silently does nothing.
    assert "library: {}" in html
    assert "libraries:'Libraries'" in html


def test_the_status_page_names_each_library_rather_than_saying_romm():
    """With several libraries a hardcoded "RomM" row is both wrong and less
    informative than naming the servers actually configured."""
    html = page()
    assert "h.libraries||[]" in html


def test_a_list_field_round_trips_through_the_form():
    """Platform routing rules are a list. Without list handling the form would
    save the string "n64, snes" as a single platform name that matches nothing.
    """
    html = page()
    assert "f.type === 'list'" in html
    assert "el.dataset.list ? el.value.split(',')" in html
