"""Start Hub workers before plugin-controlled Python startup hooks can run.

This adapter changes only the Hub broker's subprocess reference;
it does not replace subprocess.Popen globally or claim filesystem isolation.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading

_LOCK = threading.Lock()


class _BrokerSubprocess:
    """Keep broker subprocess behavior, with one recognized runner command."""

    def __init__(self, original, paths):
        self._original = original
        self._bootstrap = (
            "import sys\nsys.path.extend(" + repr(paths) + ")\n"
            "from rom_hub_sdk.runner import main\n"
            "try:\n import pyseccomp\n"
            "except ImportError:\n sys.modules['pyseccomp'] = None\n"
            "main()")

    def __getattr__(self, name):
        return getattr(self._original, name)

    def Popen(self, command, *args, **kwargs):
        """Refuse unknown launch conventions instead of silently skipping hardening."""
        if command != [sys.executable, "-m", "rom_hub_sdk.runner"]:
            raise RuntimeError("unsupported ROM Hub worker command; update ROMarr's adapter")
        # -I ignores ambient PYTHON* settings and excludes the plugin cwd.
        # -S suppresses .pth, sitecustomize and usercustomize startup execution.
        # SDK and optional seccomp imports finish before init adds the plugin
        # directory. A missing binding stays missing; a plugin cannot supply one.
        return self._original.Popen(
            [sys.executable, "-I", "-S", "-X", "utf8", "-c", self._bootstrap], *args, **kwargs)


def install_trusted_startup() -> None:
    """Harden the separately installed Hub without changing its registry or API."""
    host = importlib.import_module("rom_hub.broker.host")
    with _LOCK:
        if isinstance(host.subprocess, _BrokerSubprocess):
            return
        paths = []
        for key in ("purelib", "platlib"):
            value = sysconfig.get_path(key)
            if value and value not in paths:
                paths.append(value)
        # Editable/user-site installs need their resolved trusted source roots;
        # importing .pth hooks would reintroduce pre-confinement execution.
        for name in ("rom_hub", "rom_hub_sdk"):
            package = importlib.import_module(name)
            origin = getattr(package, "__file__", None)
            if not origin:
                raise RuntimeError("ROM Hub package location cannot be verified")
            value = str(Path(origin).resolve(strict=True).parent.parent)
            if value not in paths:
                paths.append(value)
        # The confinement binding may live in user site-packages, which -I
        # excludes; add its resolved directory, never a .pth or startup hook.
        binding = importlib.util.find_spec("pyseccomp")
        if binding is not None and binding.origin:
            value = str(Path(binding.origin).resolve(strict=True).parent)
            if value not in paths:
                paths.append(value)
        if host.subprocess is not subprocess:
            raise RuntimeError("unsupported ROM Hub broker subprocess adapter")
        host.subprocess = _BrokerSubprocess(subprocess, paths)
