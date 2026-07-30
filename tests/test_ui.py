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
    assert _text(brand.group(1)).strip() == "Romarr"


def test_the_old_name_appears_nowhere_a_user_can_read_it():
    assert "Rommarr" not in _text(page())


def test_the_document_title_is_the_product_name():
    assert re.search(r"<title>Romarr</title>", page())
