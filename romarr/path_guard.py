"""Keep request-supplied filesystem paths inside folders the operator configured."""
from __future__ import annotations

import os


def confined(value: str, roots) -> str:
    """Return `value` with symlinks resolved, if it lies inside one of `roots`.

    Raises ValueError otherwise. The returned string is the resolved path, so
    callers use what was checked rather than the original spelling.
    """
    real = os.path.realpath(str(value))
    for root in roots:
        if not str(root or "").strip():
            continue
        base = os.path.realpath(str(root))
        if real == base:
            return base
        if real.startswith(base.rstrip(os.sep) + os.sep):
            return real
    raise ValueError("path is outside the allowed folders")


def env_roots(env, name: str) -> list[str]:
    """Folders listed in one environment variable, separated by os.pathsep."""
    return [part for part in str(env.get(name, "") or "").split(os.pathsep) if part.strip()]
