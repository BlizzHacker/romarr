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


def test_the_system_page_explains_and_builds_the_ggrequestz_webhook():
    html = page()
    assert "GG Requestz page" in html
    assert "reachability only; request delivery is configured below" in html
    assert "REQUEST_WEBHOOK_URL=" in html
    assert "new URL('/api/v1/webhook/ggrequestz'" in html
    assert "target.searchParams.set('apikey',d.api_key)" in html
    assert "base.value=''" in html
    assert "base.oninput" in html
    assert "copy.disabled=true" in html
    assert "request.auto_approve" in html


def test_plugin_install_errors_are_displayed_in_the_hub():
    """A failed install used to refresh the catalogue and erase the API error."""
    html = page()
    assert "let q='', cap='', inst='', actionError=''" in html
    assert "if(!r.ok||result.ok!==true)" in html
    assert "actionError=result.error||result.reason" in html
    assert "esc(actionError)" in html


# -- the inline script must be parseable JavaScript (issue #48) ---------------
#
# The dashboard's whole interactivity is inline <script> blocks in the served
# page. One invalid line continuation -- a backslash right after a CLOSED
# string literal at end-of-line, which is legal Python but a JS SyntaxError --
# killed every click handler on the page while all server-side tests stayed
# green, because nothing ever parsed the emitted JavaScript. These tests parse
# it: a structural check that always runs, and a real `node --check` wherever
# node exists (CI and any dev machine; skipped rather than failed elsewhere).

def _inline_scripts() -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page(), re.S)


def test_no_invalid_js_line_continuations_in_the_inline_scripts():
    """A `'` immediately followed by `\\` at end-of-line is a Python idiom
    that is NOT valid JavaScript: continuations only exist inside a string
    literal. JS allows a bare line break before `+`, so the backslash was
    never needed -- and it made the browser refuse the whole script."""
    offenders = []
    for n, script in enumerate(_inline_scripts()):
        for i, line in enumerate(script.splitlines(), 1):
            stripped = line.rstrip()
            if stripped.endswith("'\\") or stripped.endswith('"\\'):
                offenders.append(f"script {n} line {i}: {stripped[-40:]}")
    assert not offenders, "\n".join(offenders)


def test_the_inline_scripts_pass_node_syntax_check():
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed; the structural test above covers it")
    for n, script in enumerate(_inline_scripts()):
        assert script.strip(), f"script block {n} is empty"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8", newline="\n") as f:
            f.write(script)
            path = f.name
        result = subprocess.run([node, "--check", path],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, \
            f"script block {n} fails to parse:\n{result.stderr[:1500]}"
