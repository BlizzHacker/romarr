"""Library backends defined outside the tree.

The point of the registry is that a driver gets the *same* treatment as a
compiled-in backend -- schema, routing, redaction, construction -- with no
further wiring. Each test here pins one of those, because "it registered"
without "it is routed to" or "its password is masked" would be a backend that
looks supported and leaks or silently never receives an import.
"""

import textwrap

import pytest

from romarr import libraries
from romarr.libraries import (
    LIBRARY_KINDS, LIBRARY_TYPES, build_library_from_config,
    load_backend_plugins, redact_library, register_library_backend,
    registered_backends, route_library,
)


class _Fake:
    name = "Fake"
    BACKGROUND_TIMEOUT = 30

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def configured(self):
        return bool(self.cfg.get("url"))

    def reachable(self):
        return True

    def count(self):
        return 7

    def games(self, limit=60, offset=0, timeout=None):
        return []

    def rescan(self, platform_slug=None):
        return True


@pytest.fixture
def fake_backend():
    """Register a throwaway backend and remove it again.

    Registration mutates module state shared by every other test, so it has to
    be undone -- a leaked kind would show up in another test's schema.
    """
    register_library_backend(
        "faklib", label="FakLib", build=_Fake, default_port=9999,
        fields=[{"name": "token", "label": "Token", "type": "secret",
                 "default": ""}],
    )
    yield "faklib"
    LIBRARY_TYPES.pop("faklib", None)
    libraries._REGISTERED.pop("faklib", None)


def test_registered_kind_appears_in_the_schema(fake_backend):
    # The Libraries page reads LIBRARY_TYPES directly, so landing in that dict
    # is what makes the type selectable at all.
    assert LIBRARY_TYPES["faklib"]["label"] == "FakLib"
    assert LIBRARY_TYPES["faklib"]["external"] is True
    names = {f["name"] for f in LIBRARY_TYPES["faklib"]["fields"]}
    # Its own field, plus the common ones it did not have to restate.
    assert "token" in names
    assert {"name", "enable", "url", "path", "is_default"} <= names
    assert fake_backend in registered_backends()


def test_it_builds_through_the_normal_path(fake_backend):
    lib = build_library_from_config({"type": "faklib", "url": "http://x"})
    assert isinstance(lib, _Fake)
    assert lib.count() == 7


def test_secrets_are_masked_like_any_other_backend(fake_backend):
    safe = redact_library({"type": "faklib", "url": "http://x", "token": "s3cret"})
    assert safe["token"] == libraries.SECRET_PLACEHOLDER
    assert safe["url"] == "http://x"


def test_platform_routing_reaches_a_registered_backend(fake_backend):
    configs = [
        {"type": "romm", "enable": True, "is_default": True},
        {"type": "faklib", "enable": True, "platforms": ["dreamcast"]},
    ]
    assert route_library(configs, "dreamcast")["type"] == "faklib"
    assert route_library(configs, "snes")["type"] == "romm"


def test_a_builtin_cannot_be_hijacked():
    # Otherwise a drop-in file could redefine "romm" and quietly redirect an
    # existing library's imports somewhere else.
    for kind in LIBRARY_KINDS:
        with pytest.raises(ValueError, match="built-in"):
            register_library_backend(kind, label="Evil", build=_Fake)


def test_a_driver_that_raises_on_build_does_not_take_down_the_service(fake_backend):
    def explode(cfg):
        raise RuntimeError("no")

    libraries._REGISTERED["faklib"] = explode
    assert build_library_from_config({"type": "faklib"}) is None


def test_unknown_kind_is_still_none():
    assert build_library_from_config({"type": "nosuchthing"}) is None


def test_drop_in_directory_is_loaded(tmp_path):
    (tmp_path / "mylib.py").write_text(textwrap.dedent("""
        from romarr.libraries import register_library_backend

        class Driver:
            name = "Mine"
            BACKGROUND_TIMEOUT = 5
            def __init__(self, cfg): self.cfg = cfg
            @property
            def configured(self): return True
            def reachable(self): return True
            def count(self): return 1
            def games(self, limit=60, offset=0, timeout=None): return []
            def rescan(self, platform_slug=None): return True

        register_library_backend("mylib", label="Mine", build=Driver)
    """), encoding="utf-8")
    # A file starting with an underscore is a helper, not a driver.
    (tmp_path / "_helper.py").write_text("raise AssertionError('imported')",
                                         encoding="utf-8")
    try:
        assert load_backend_plugins(str(tmp_path)) == ["mylib"]
        assert build_library_from_config({"type": "mylib"}).count() == 1
    finally:
        LIBRARY_TYPES.pop("mylib", None)
        libraries._REGISTERED.pop("mylib", None)


def test_a_broken_drop_in_is_skipped_not_fatal(tmp_path):
    (tmp_path / "bad.py").write_text("import nonexistent_module_xyz", encoding="utf-8")
    (tmp_path / "good.py").write_text(textwrap.dedent("""
        from romarr.libraries import register_library_backend
        register_library_backend("goodlib", label="Good", build=lambda cfg: None)
    """), encoding="utf-8")
    try:
        # The bad file is skipped; the good one still loads. Sorted order puts
        # "bad" first, so this also pins that one failure does not abort the run.
        assert load_backend_plugins(str(tmp_path)) == ["good"]
    finally:
        LIBRARY_TYPES.pop("goodlib", None)
        libraries._REGISTERED.pop("goodlib", None)


def test_a_missing_directory_is_not_an_error():
    assert load_backend_plugins("/nonexistent/path/for/sure") == []
