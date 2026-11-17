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


#: Folders whose whole tree is system territory; never an import or eXoDOS root.
_SYSTEM_TREES = ("/etc", "/proc", "/sys", "/dev", "/boot", "/root", "/usr",
                 "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/run")
#: Folders too broad to be a root themselves (their subfolders are fine).
_TOO_BROAD = ("/", "/var", "/home", "/tmp", "/opt")


def usable_root(root: str) -> bool:
    """Whether a settings-derived folder may serve as a confinement root.

    Library and download folders come from settings an API client can write,
    so they must not widen confinement to the whole disk or a system folder.
    """
    if not str(root or "").strip():
        return False
    real = os.path.realpath(str(root))
    if os.name == "nt":
        drive, rest = os.path.splitdrive(real)
        lowered = rest.replace("/", "\\").rstrip("\\").lower()
        return bool(lowered) and not lowered.startswith(("\\windows", "\\program files", "\\programdata"))
    if real in _TOO_BROAD:
        return False
    return not any(real == tree or real.startswith(tree + "/") for tree in _SYSTEM_TREES)


def mapped(path: str, mappings) -> str:
    """Translate a download client's path with remote path mappings, quietly.

    Longest matching remote prefix wins; an unmapped path is returned as-is.
    """
    text = str(path)
    best = None
    for entry in mappings or []:
        if not isinstance(entry, dict):
            continue
        remote = str(entry.get("remote", "")).rstrip("/\\")
        local = str(entry.get("local", "")).rstrip("/\\")
        if remote and local and (text == remote or text.startswith((remote + "/", remote + "\\"))):
            if best is None or len(remote) > len(best[0]):
                best = (remote, local)
    if best is None:
        return text
    rest = text[len(best[0]):].lstrip("/\\")
    return os.path.join(best[1], rest) if rest else best[1]

