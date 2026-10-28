"""The version is derived, and a build that cannot state one never claims to be current.

The defect these guard against is not exotic: v1.7.0 shipped with the hand-typed
constant in app.py still saying 1.6.0, so the Update page offered an upgrade to
the build already running. Every test here pins one link of the derivation chain
so the number can never silently drift again.
"""
import re
from pathlib import Path

from romarr import version


def test_metadata_wins_when_installed(monkeypatch):
    monkeypatch.setattr(version, "_from_metadata", lambda: "9.9.9")
    monkeypatch.setattr(version, "_from_file", lambda: "1.0.0")
    monkeypatch.setattr(version, "_from_env", lambda: "2.0.0")
    assert version.derive_version() == "9.9.9"


def test_image_file_used_when_not_a_packaged_install(monkeypatch):
    monkeypatch.setattr(version, "_from_metadata", lambda: "")
    monkeypatch.setattr(version, "_from_file", lambda: "1.7.0")
    monkeypatch.setattr(version, "_from_env", lambda: "")
    assert version.derive_version() == "1.7.0"


def test_environment_used_for_source_checkouts(monkeypatch):
    monkeypatch.setattr(version, "_from_metadata", lambda: "")
    monkeypatch.setattr(version, "_from_file", lambda: "")
    monkeypatch.setenv("ROMARR_VERSION", "1.8.0")
    monkeypatch.setattr(version, "_from_env", lambda: "1.8.0")
    assert version.derive_version() == "1.8.0"


def test_unknown_when_nothing_knows(monkeypatch):
    monkeypatch.setattr(version, "_from_metadata", lambda: "")
    monkeypatch.setattr(version, "_from_file", lambda: "")
    monkeypatch.setattr(version, "_from_env", lambda: "")
    assert version.derive_version() == version.UNKNOWN_VERSION


def test_is_unknown_recognises_the_sentinel_and_emptiness():
    assert version.is_unknown(version.UNKNOWN_VERSION) is True
    assert version.is_unknown("") is True
    assert version.is_unknown(None) is True
    assert version.is_unknown("0.0.0+unknown.dirty") is True
    assert version.is_unknown("1.7.0") is False


def test_app_does_not_hand_type_a_version():
    """Regression guard: nobody may reintroduce `VERSION = "1.2.3"`."""
    app_source = Path(version.__file__).with_name("app.py").read_text()
    hand_typed = re.search(r'^VERSION\s*=\s*[\'"]\d', app_source, re.MULTILINE)
    assert hand_typed is None, (
        "app.py hard-codes its version again: %r. Derive it via version.py."
        % (hand_typed.group(0) if hand_typed else "")
    )
