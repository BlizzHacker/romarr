"""The hash index: what netplay is actually judged on.

These exist because the netplay handshake looked like it worked while being
unable to answer anything but "missing". The offer was built from a library
server's game object, which has no SHA1 -- so every offer carried an empty
hash and every answer was a shrug. Nothing failed loudly; it just never
matched.
"""

from dataclasses import dataclass

from romarr.federation import Federation
from romarr.hashes import HashIndex, normalise

SHA_A = "a" * 40
SHA_B = "b" * 40


@dataclass
class Shelf:
    """A library server's game: note what it does NOT have."""

    name: str
    platform: str = ""
    year: int = 0
    verified: bool = False


def test_a_library_game_has_no_hash_which_is_the_whole_problem():
    game = Shelf("Super Mario Kart", "snes")
    assert not getattr(game, "sha1", "")
    offer = Federation("Alice").netplay_offer(game)
    assert offer["sha1"] == ""
    # And an empty hash can only ever produce one answer.
    answer = Federation.netplay_answer(offer, [])
    assert answer["status"] == "missing"


def test_the_index_answers_both_directions(tmp_path):
    index = HashIndex(tmp_path / "h.json")
    index.add(SHA_A, "Super Mario Kart (USA)", "snes", True, "smk.sfc")

    assert index.by_sha1(SHA_A).name == "Super Mario Kart (USA)"
    assert index.by_sha1(SHA_A.upper()) is not None, "hashes compare lowercase"
    assert index.for_game("Super Mario Kart", "snes").sha1 == SHA_A


def test_titles_match_across_the_punctuation_two_sources_disagree_on():
    assert normalise("Super Mario Kart (USA)") == normalise("super mario kart")
    assert normalise("Legend of Zelda, The [!]") == "legend of zelda the"


def test_the_same_title_on_two_machines_does_not_collide(tmp_path):
    index = HashIndex(tmp_path / "h.json")
    index.add(SHA_A, "Aladdin", "snes", True)
    index.add(SHA_B, "Aladdin", "genesis", True)
    assert index.for_game("Aladdin", "snes").sha1 == SHA_A
    assert index.for_game("Aladdin", "genesis").sha1 == SHA_B


def test_a_verified_dump_wins_over_one_that_arrived_first(tmp_path):
    index = HashIndex(tmp_path / "h.json")
    index.add(SHA_A, "Chrono Trigger", "snes", False)
    index.add(SHA_B, "Chrono Trigger", "snes", True)
    assert index.for_game("Chrono Trigger", "snes").sha1 == SHA_B, \
        "an offer should carry the copy this install stands behind"


def test_re_auditing_replaces_a_platform_rather_than_accumulating(tmp_path):
    index = HashIndex(tmp_path / "h.json")
    index.add(SHA_A, "Deleted Game", "snes", True)
    index.add(SHA_B, "Kept Game", "genesis", True)
    index.clear_platform("snes")
    assert index.by_sha1(SHA_A) is None
    assert index.for_game("Deleted Game", "snes") is None
    assert index.by_sha1(SHA_B) is not None, "other platforms are untouched"


def test_the_index_survives_a_restart(tmp_path):
    path = tmp_path / "h.json"
    HashIndex(path).add(SHA_A, "Super Metroid", "snes", True, "sm.sfc")
    HashIndex(path).save()  # a fresh empty one must not clobber on construct

    built = HashIndex(path)
    built.add(SHA_A, "Super Metroid", "snes", True, "sm.sfc")
    built.save()

    reloaded = HashIndex(path)
    reloaded.load()
    assert len(reloaded) == 1
    assert reloaded.for_game("Super Metroid", "snes").sha1 == SHA_A


def test_a_corrupt_index_starts_empty_rather_than_crashing(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("{not json", encoding="utf-8")
    index = HashIndex(path)
    index.load()
    assert len(index) == 0


# -- what the index makes possible -----------------------------------------


def test_netplay_now_reaches_every_verdict_not_just_missing(tmp_path):
    """The four outcomes, judged against real hashes."""
    mine = HashIndex(tmp_path / "mine.json")
    mine.add(SHA_A, "Super Mario Kart", "snes", True)

    ready = Federation.netplay_answer(
        {"title": "Super Mario Kart", "platform": "snes",
         "sha1": SHA_A, "verified": True}, mine.entries())
    assert ready["status"] == "ready"

    unverified = Federation.netplay_answer(
        {"title": "Super Mario Kart", "platform": "snes",
         "sha1": SHA_A, "verified": False}, mine.entries())
    assert unverified["status"] == "unverified"

    mismatch = Federation.netplay_answer(
        {"title": "Super Mario Kart", "platform": "snes",
         "sha1": SHA_B, "verified": True}, mine.entries())
    assert mismatch["status"] == "mismatch", \
        "same title, different bytes -- the failure that reads as lag"

    missing = Federation.netplay_answer(
        {"title": "Earthbound", "platform": "snes",
         "sha1": SHA_B, "verified": True}, mine.entries())
    assert missing["status"] == "missing"


def test_both_sides_derive_the_same_room_without_talking_about_it():
    room = Federation.netplay_room("peer123", SHA_A)
    assert room == Federation.netplay_room("peer123", SHA_A.upper())
    assert room != Federation.netplay_room("peer123", SHA_B), \
        "a different dump is a different room"
    assert room != Federation.netplay_room("other", SHA_A), \
        "a different relationship is a different room"
    assert len(room) == 16


# -- reading hashes from a library server -----------------------------------


class FakeRomm:
    """A RomM where most entries have no hash, which is the normal case."""

    def __init__(self, total=1200, hashed_every=10):
        self.total, self.hashed_every = total, hashed_every
        self.pages = []

    def hashes(self, limit=500, offset=0, timeout=None):
        self.pages.append((limit, offset))
        raw = max(0, min(limit, self.total - offset))
        rows = []
        for i in range(offset, offset + raw):
            if i % self.hashed_every == 0:
                rows.append({"sha1": f"{i:040x}", "name": f"Game {i}",
                             "platform": "snes", "verified": False,
                             "path": f"g{i}.sfc"})
        return rows, raw


def test_seeding_paginates_on_what_the_server_returned(tmp_path):
    """The bug this pins: pagination must not stop on a short FILTERED page.

    Most of a large RomM is catalogued entries with no hash. Filtering those
    makes a full page look short, so paginating on len(rows) stops after the
    first page -- it read 491 dumps out of 166,578 and reported success.
    """
    from romarr.app import ROMarr

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "r.json")})
    svc.romm = svc.game_library = FakeRomm(total=1200, hashed_every=10)

    result = svc.index_hashes_from_library()

    assert result["ok"] is True
    assert result["seen"] == 1200, "every entry was read, not just page one"
    assert result["added"] == 120, "one in ten carried a hash"
    # Three pages: 0, 500, 1000 -- not one.
    assert svc.romm.pages == [(500, 0), (500, 500), (500, 1000)]


def test_seeding_says_how_many_entries_had_no_hash(tmp_path):
    from romarr.app import ROMarr

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "r.json")})
    svc.romm = svc.game_library = FakeRomm(total=100, hashed_every=10)
    result = svc.index_hashes_from_library()
    assert "90" in result["detail"], \
        "an operator needs to know the gap is their library, not a failure"
