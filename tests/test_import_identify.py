"""Issue #247: a verified import is identified by its hashes, in the background."""
from __future__ import annotations

from types import SimpleNamespace

from romarr.app import ROMarr
from romarr.metadata import GameInfo, Metadata


class NowPool:
    """Runs submitted work immediately, so the test sees the outcome."""

    def submit(self, fn):
        fn()


class FakeMetadata:
    def __init__(self, info=None, boom=False, can=True):
        self.info, self.boom, self.can, self.calls = info, boom, can, []

    def can_identify(self):
        return self.can

    def identify(self, *, verification=None, filename="", platform=""):
        self.calls.append((verification, platform))
        if self.boom:
            raise RuntimeError("provider down")
        return self.info


def svc(tmp_path):
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    s._identify_pool = NowPool()
    return s


def verified(status="verified"):
    rom = SimpleNamespace(name="Super Metroid (USA).sfc", crc="d63ed5f8",
                          md5="21f3e98df4780ee1c667b84e57d88675",
                          sha1="da957f0d63d14cb441d215462904c4fa8519c613")
    return SimpleNamespace(status=status, game="Super Metroid (Japan, USA)", rom=rom)


def test_a_verified_import_is_identified_and_kept(tmp_path):
    s = svc(tmp_path)
    s.metadata = FakeMetadata(GameInfo(title="Super Metroid", summary="Samus", source="screenscraper",
                                       matched_by="dat", cover_url="https://x/c.png"))
    s._identify_after_import(verified(), "snes", "Super Metroid (USA)")
    assert s.metadata.calls and s.metadata.calls[0][1] == "snes"
    assert s.metadata.calls[0][0].rom.sha1.startswith("da957f0d")   # the hashes reach the provider
    identity = s.store.game_meta("snes", "Super Metroid (USA)")["identity"]
    assert identity["title"] == "Super Metroid" and identity["source"] == "screenscraper"
    assert identity["identified_at"]


def test_unverified_or_unidentifiable_imports_ask_nobody(tmp_path):
    s = svc(tmp_path)
    s.metadata = FakeMetadata(GameInfo(title="x"))
    s._identify_after_import(verified("unknown"), "snes", "Game")
    s._identify_after_import(verified(), "snes", "")
    assert s.metadata.calls == []
    s.metadata = FakeMetadata(GameInfo(title="x"), can=False)
    s._identify_after_import(verified(), "snes", "Game")
    assert s.metadata.calls == []


def test_a_failing_provider_never_breaks_the_import(tmp_path):
    s = svc(tmp_path)
    s.metadata = FakeMetadata(boom=True)
    s._identify_after_import(verified(), "snes", "Game")          # must not raise
    assert "identity" not in s.store.game_meta("snes", "Game")
    s.metadata = FakeMetadata(GameInfo())                         # not found: nothing stored
    s._identify_after_import(verified(), "snes", "Game")
    assert s.store.game_meta("snes", "Game") == {}


def test_rating_a_game_keeps_its_identity(tmp_path):
    s = svc(tmp_path)
    s.store.set_game_identity("snes", "Game", {"title": "Game", "rating": 9.1})
    s.store.set_game_meta("snes", "Game", rating=7)
    row = s.store.game_meta("snes", "Game")
    assert row["rating"] == 7 and row["identity"]["rating"] == 9.1
    s.store.set_game_meta("snes", "Game", rating=0)
    assert s.store.game_meta("snes", "Game")["identity"]["title"] == "Game"


def test_can_identify_ignores_artwork_only_providers():
    m = Metadata.__new__(Metadata)
    m._enabled = lambda: [({}, {"artwork": True})]
    assert m.can_identify() is False
    m._enabled = lambda: [({}, {"artwork": True}), ({}, {})]
    assert m.can_identify() is True
