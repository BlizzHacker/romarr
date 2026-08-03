"""Every module is reachable from the running service.

This file exists because `romarr/metadata.py` shipped with 249 lines, 27
green tests, and nothing in `app.py` importing it. It was dead code with a
certificate -- the exact "configuration that tests green and does nothing"
failure this project has found three times in other people's software,
committed in its own.

Unit tests prove a module is correct. They cannot prove anybody calls it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "romarr"

#: Modules that are legitimately not imported by the service.
#:
#: Kept explicit and tiny. A growing exemption list is the same failure
#: arriving by a slower route.
STANDALONE = {
    "__init__",
    "__main__",   # the entry point; it imports app, not the other way round
    "app",        # the root of the graph, so it cannot import itself
}


def modules() -> list[str]:
    return sorted(p.stem for p in ROOT.glob("*.py") if p.stem not in STANDALONE)


def imported_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                # `from . import hub` -- the names ARE the modules, and
                # missing this form is how a wired module looks unwired.
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("romarr."):
                    found.add(alias.name.split(".")[1])
    return found


def reachable() -> set[str]:
    """Everything app.py imports, plus what those modules import."""
    seen = imported_names(ROOT / "app.py")
    frontier = list(seen)
    while frontier:
        name = frontier.pop()
        path = ROOT / f"{name}.py"
        if not path.exists():
            continue
        for found in imported_names(path):
            if found not in seen:
                seen.add(found)
                frontier.append(found)
    return seen


@pytest.mark.parametrize("module", modules())
def test_the_service_can_reach_every_module(module):
    assert module in reachable(), (
        f"romarr/{module}.py is never imported by the running service. "
        "Either wire it up or delete it -- a tested module nobody calls is "
        "dead code with a certificate."
    )


def test_the_exemption_list_stays_small():
    """It is the escape hatch, and an escape hatch that grows is the failure
    arriving by a slower route."""
    assert len(STANDALONE) <= 4, sorted(STANDALONE)
