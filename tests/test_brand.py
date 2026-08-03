"""The product is spelled ROMarr. This is the guard that proves it.

The brand has now been got wrong twice in the same place, both times invisibly to
`grep`, because the logo is split across an element boundary so that "arr" can
carry the accent colour:

    <div id="brand">ROM<span>arr</span></div>

Neither half contains the product name. A source-level search for the wrong
spelling comes back clean while every page served shows it in the top-left
corner, which is the one place a user cannot miss. So these tests read the
*rendered* text with tags stripped, and they sweep the shipped files as well --
a rename is not done when the code is right and the README still disagrees.

What is deliberately NOT renamed, because these are identifiers rather than the
brand, and changing them breaks existing installs:

  * the Python package `romarr`, and `python -m romarr`
  * `ROMARR_DATA` / `ROMARR_PORT`
  * `/opt/romarr`, `romarr.service`, `romarr.json`
  * the default download category `romarr`
  * the image name `ghcr.io/blizzhacker/romarr` (OCI requires lowercase)
"""

import io
import pathlib
import re

from romarr.app import ROMarr, VERSION
from romarr.ui import page

BRAND = "ROMarr"

# Every spelling that has ever been wrong, as a whole word. `Romarr` was the
# spelling before this rebrand; `Rommarr` was the one before that.
WRONG = re.compile(r"\b(?:Rommarr|Romarr|RomArr|ROMArr|RomMarr)\b")

REPO = pathlib.Path(__file__).resolve().parents[1]

# Files a user reads. Test sources are excluded on purpose: this file has to be
# able to name the wrong spellings in order to forbid them.
SHIPPED = [
    "README.md",
    "docker-compose.yml",
    "docker-compose-alt.yml",
    ".env.example",
    "Dockerfile",
    "docker/entrypoint.sh",
]


def _text(html: str) -> str:
    """Tags stripped -- what a reader actually sees."""
    return re.sub(r"<[^>]+>", "", html)


def test_the_rendered_brand_is_exactly_romarr():
    brand = re.search(r'<div id="brand">(.*?)</div>', page(), re.S)
    assert brand, "the page no longer has a brand element"
    assert _text(brand.group(1)).strip() == BRAND


def test_no_wrong_spelling_survives_anywhere_a_user_can_read_it():
    rendered = _text(page())
    found = WRONG.findall(rendered)
    assert not found, f"wrong spelling in the rendered UI: {sorted(set(found))}"


def test_the_document_title_is_the_brand():
    assert f"<title>{BRAND}</title>" in page()


def test_every_shipped_file_uses_the_brand():
    problems = {}
    for name in SHIPPED:
        path = REPO / name
        if not path.exists():
            continue
        hits = WRONG.findall(io.open(path, encoding="utf-8").read())
        if hits:
            problems[name] = sorted(set(hits))
    assert not problems, f"wrong spelling in shipped files: {problems}"


def test_the_source_uses_the_brand_in_prose_too():
    """Comments and docstrings are read by the next person to touch this, and a
    half-finished rename is how the old name survives into the next release."""
    problems = {}
    for path in (REPO / "romarr").rglob("*.py"):
        hits = WRONG.findall(io.open(path, encoding="utf-8").read())
        if hits:
            problems[str(path.relative_to(REPO))] = sorted(set(hits))
    assert not problems, f"wrong spelling in source: {problems}"


def test_the_http_server_header_carries_the_brand():
    """It is the one piece of branding a client sees without loading the page."""
    from romarr.app import make_handler
    src = io.open(REPO / "romarr" / "app.py", encoding="utf-8").read()
    assert f'server_version = "{BRAND}"' in src
    assert callable(make_handler)


def test_the_service_class_is_named_for_the_brand():
    assert ROMarr.__name__ == BRAND


def test_the_technical_identifiers_are_deliberately_left_lowercase():
    """The counterpart to the sweep above: renaming these would break every
    existing install, so they must stay exactly as they are."""
    app = io.open(REPO / "romarr" / "app.py", encoding="utf-8").read()
    entry = io.open(REPO / "romarr" / "__main__.py", encoding="utf-8").read()
    assert '"ROMARR_DATA"' in app
    assert '"ROMARR_PORT"' in entry
    assert 'DEFAULT_CATEGORY = "romarr"' in app
    assert VERSION


def test_the_brand_is_not_split_apart_by_a_flex_gap():
    """The one defect the text assertions above structurally cannot see.

    #brand is a flexbox holding two text nodes -- "ROM" and a coloured span
    "arr" -- so a gap between flex children renders the word as "ROM arr". The
    rendered *text* is still "ROMarr", which is why every test above passed
    while a screenshot of the running app showed the brand with a space in it.
    """
    import re as _re
    css = _re.search(r"#brand\{([^}]*)\}", page())
    assert css, "the brand rule is gone"
    gap = _re.search(r"gap:\s*([^;]+)", css.group(1))
    assert gap, "#brand should state its gap explicitly, so this cannot regress"
    assert gap.group(1).strip() in ("0", "0px"), \
        f"a flex gap of {gap.group(1)!r} splits the brand into two words"


def test_contrib_carries_the_brand_correctly():
    """The plugins ship to other people's machines under this name.

    The sweep covered the Python package and the docs but not `contrib/`, so
    the LaunchBox plugin went out with a `Romarr` namespace and PowerShell
    functions called `Invoke-RomarrSync` -- user-visible in Playnite's own
    menu. Extending the guard here is what makes that a test failure rather
    than something noticed in a screenshot.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "contrib"
    if not root.is_dir():
        return

    # `romarr` lower-case is legitimate: package paths, the config filename,
    # a hostname in an example URL. What is wrong is the title-case spelling,
    # which is neither the brand nor an identifier convention here.
    wrong = re.compile(r"\bRomarr\b")
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() in (".png", ".jpg", ".ico"):
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if wrong.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "brand is ROMarr:\n" + "\n".join(offenders)
