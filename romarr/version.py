"""Where ROMarr's version comes from.

It used to be a hand-typed constant in ``app.py``, and v1.7.0 shipped with it
still saying ``1.6.0``: nothing failed, so nothing caught it, and the Update
page then compared that stale number against the real latest release and
offered an upgrade to the build that was already running. A number that has to
be retyped by hand on every release is the defect, so it is derived now.

The chain, most authoritative first:

  1. installed distribution metadata -- pip, pipx, a distro package;
  2. ``romarr/_version.py``, written into the image at build time from the
     release tag by the Dockerfile (absent in a plain source checkout);
  3. ``ROMARR_VERSION`` in the environment -- source runs, and images built
     from a tree that carries no tag;
  4. ``UNKNOWN_VERSION``, which is deliberately not a plausible version
     number. A build that cannot say what it is should look like one, rather
     than advertising somebody else's release.

Callers must treat ``UNKNOWN_VERSION`` as "do not compare me against the latest
release": every real tag is newer than it, so the comparison would raise an
update notice that can never be satisfied. ``is_unknown`` is that test.
"""
from __future__ import annotations

import os
import re

# Not a real release and never will be: PEP 440 local segment, sorts below
# everything, and reads as "unknown" to a human on a settings page.
UNKNOWN_VERSION = "0.0.0+unknown"


def _clean(text: object) -> str:
    """Trim, and drop a leading v: a tag says v1.7.0, the release says 1.7.0."""
    value = str(text or "").strip()
    if value[:1] in ("v", "V"):
        value = value[1:].strip()
    return value


def _from_metadata() -> str:
    """The installed distribution's version, for a packaged install."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python 3.7 and older
        return ""
    try:
        return _clean(version("romarr"))
    except PackageNotFoundError:
        return ""
    except Exception:  # pragma: no cover - broken metadata must not stop boot
        return ""


def _from_file() -> str:
    """The version baked into the image at build time, if there is one."""
    try:
        from ._version import __version__
    except Exception:
        return ""
    return _clean(__version__)


def _from_env() -> str:
    return _clean(os.environ.get("ROMARR_VERSION"))


def derive_version() -> str:
    """First source that knows a version wins; unknown if none does."""
    for source in (_from_metadata, _from_file, _from_env):
        found = source()
        if found and found != UNKNOWN_VERSION:
            return found
    return UNKNOWN_VERSION


_RELEASE_RE = re.compile(r"\s*v?(\d+(?:\.\d+)*)", re.IGNORECASE)
_POST_RE = re.compile(r"\.?post\.?(\d+)", re.IGNORECASE)
_PRE_RE = re.compile(r"[.\-_]?(a|b|c|rc|alpha|beta|pre|preview|dev)\.?(\d*)",
                     re.IGNORECASE)
# dev < alpha < beta < release-candidate < final. A collapsed "is it a
# prerelease" flag cannot order these: beta would come out ahead of rc.
_PRE_RANK = {"dev": 1, "a": 2, "alpha": 2, "b": 3, "beta": 3,
             "c": 4, "rc": 4, "pre": 4, "preview": 4}
_RANK_FINAL = 5


def compare_key(value: object) -> tuple:
    """Order two version strings the way a release actually orders them.

    A tuple of ints off ``split(".")`` lies in three ways, and each one has been
    seen in the wild:

      * ``1.7`` vs ``1.7.0`` -- Python calls the longer tuple newer, so the same
        release offers itself as an update. Padding fixes it.
      * ``1.7.0-rc.1`` vs ``1.7.0`` -- both parse to the same numbers, so the
        final release is never offered to a prerelease build.
      * ``1.7.0`` vs ``1.7.0.post1`` -- a post release is a follow-up fix, so it
        must sort *above* the release it patches.

    Anything without a numeric release at all sorts below everything: such a
    build cannot be compared, which is why ``check_update`` refuses an unknown
    version before it ever reaches a comparison.
    """
    core = _clean(value).split("+", 1)[0]
    found = _RELEASE_RE.match(core)
    if not found:
        return ((0, 0, 0, 0), 0, 0, 0)
    numbers = [int(p) for p in found.group(1).split(".")]
    release = tuple((numbers + [0, 0, 0, 0])[:4])
    rest = core[found.end():]
    post = _POST_RE.search(rest)
    pre = _PRE_RE.search(rest)
    post_no = int(post.group(1)) if post else 0
    rank, pre_no = _RANK_FINAL, 0
    if pre:
        rank = _PRE_RANK.get(pre.group(1).lower(), _RANK_FINAL - 1)
        digits = pre.group(2) or ""
        pre_no = int(digits) if digits.isdigit() and digits else 0
    return (release, post_no, rank, pre_no)


def is_unknown(value: object) -> bool:
    """True when this build cannot state a real version of itself.

    Settings come back from JSON, where a key can be missing or null, so this
    takes anything and treats emptiness as unknown rather than raising.
    """
    text = str(value or "").strip()
    if not text:
        return True
    # Any local segment carrying the sentinel counts: 0.0.0+unknown, and also
    # 0.0.0+unknown.dirty from a build off an uncommitted tree.
    return text == UNKNOWN_VERSION or "+unknown" in text


VERSION = derive_version()
