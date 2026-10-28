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
