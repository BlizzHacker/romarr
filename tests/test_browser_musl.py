"""Issue #35: on Alpine, `pip install playwright` can never work.

The error used to say "pip install playwright", which is exactly the command
that fails there. Name the cause and the way out instead.
"""

import builtins

import pytest

from romarr import browser


def _no_playwright(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_musl_installs_are_told_to_use_the_browser_edition(monkeypatch):
    _no_playwright(monkeypatch)
    monkeypatch.setattr(browser, "_is_musl", lambda: True)
    with pytest.raises(browser.Unavailable) as err:
        browser._playwright()
    text = str(err.value)
    assert "musl" in text and "romarr:browser" in text
    assert "pip install playwright" in text   # named as the thing that fails


def test_glibc_installs_keep_the_ordinary_instruction(monkeypatch):
    _no_playwright(monkeypatch)
    monkeypatch.setattr(browser, "_is_musl", lambda: False)
    with pytest.raises(browser.Unavailable) as err:
        browser._playwright()
    assert "musl" not in str(err.value)
