"""Things that must never reach a stranger's screen.

Cheap, whole-repo checks for defects that are invisible in a diff review and
obvious to the first person who lands on the project.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
TEXT = {".py", ".md", ".yml", ".yaml", ".sh", ".toml", ".ini", ".json",
        ".cs", ".psm1", ".txt", ".example"}


def tracked_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT:
            continue
        if SKIP & set(path.relative_to(ROOT).parts):
            continue
        yield path


# `git merge` writes these into the file and a clean `git status` afterwards
# means somebody committed them. The README is the first thing anyone reads,
# so it is the worst possible place for it and exactly where it happens.
MARKERS = ("<<<<<<< ", "=======\n", ">>>>>>> ")


@pytest.mark.parametrize("marker", ["<<<<<<< ", ">>>>>>> "])
def test_no_unresolved_merge_conflicts(marker):
    bad = []
    for path in tracked_text_files():
        if path.name == "test_repo_hygiene.py":
            continue  # it necessarily contains the strings it looks for
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith(marker):
                bad.append(f"{path.relative_to(ROOT)}:{number}")
    assert not bad, "unresolved merge conflict markers:\n  " + "\n  ".join(bad)


def test_the_readme_still_opens_with_what_romarr_is():
    """A reader decides in two lines. They must be the right two."""
    head = README_HEAD = (ROOT / "README.md").read_text(encoding="utf-8")[:400]
    assert head.startswith("# ROMarr")
    assert "arr for games" in head


def test_no_private_third_party_source_was_committed():
    """Epoch is a private repository belonging to somebody else. A local
    clone lives outside this tree on purpose; this fails if one wanders in."""
    strays = [p.relative_to(ROOT) for p in ROOT.rglob("*")
              if p.is_dir() and p.name in {"epoch-mirror", "Epoch"}
              and not SKIP & set(p.relative_to(ROOT).parts)]
    assert not strays, f"third-party source inside the repo: {strays}"
