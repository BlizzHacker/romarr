import pytest

from romarr.lists import ListEntry, fetch_entries, parse_list


def games(text):
    return [e.game for e in parse_list(text)]


def test_one_title_per_line():
    assert games("Super Metroid\nChrono Trigger\n") == \
        ["Super Metroid", "Chrono Trigger"]


def test_comments_and_blank_lines_are_skipped():
    text = "# the classics\n\nSuper Metroid  # SNES's finest\n\n"
    assert games(text) == ["Super Metroid"]


def test_ranking_numbers_are_stripped():
    """Top-100 articles arrive numbered; nobody should clean 100 lines by
    hand to use one."""
    text = "1. Super Metroid\n42) Chrono Trigger\n#3 Earthbound\n10 - F-Zero"
    assert games(text) == ["Super Metroid", "Chrono Trigger", "Earthbound",
                           "F-Zero"]


def test_a_tab_names_the_line_platform():
    entries = parse_list("Super Metroid\tsnes\nSonic 2\tgenesis")
    assert entries[0] == ListEntry("Super Metroid", "snes")
    assert entries[1] == ListEntry("Sonic 2", "genesis")


def test_a_dash_subtitle_is_not_mistaken_for_a_platform():
    # " - " both separates platforms and joins subtitles. A right side that
    # does not look like a platform name stays part of the title.
    entries = parse_list("Ecco - The Tides of Time\nZelda - snes")
    assert entries[0].game == "Ecco - The Tides of Time"
    assert entries[0].platform == ""
    assert entries[1] == ListEntry("Zelda", "snes")


def test_duplicates_within_a_list_collapse():
    assert games("Super Metroid\n2. Super Metroid\nsuper metroid") == \
        ["Super Metroid"]


def test_paste_lists_read_their_stored_content():
    entries = fetch_entries({"type": "paste", "content": "Super Metroid"})
    assert entries == [ListEntry("Super Metroid")]


def test_url_lists_fetch_and_a_failure_raises():
    """An expired URL must not look like an empty list -- empty looks like
    success."""

    class FakeResponse:
        text = "1. Doom\n2. Quake"

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, timeout):
            assert url == "https://example.org/list.txt"
            return FakeResponse()

    entries = fetch_entries({"type": "url", "url": "https://example.org/list.txt"},
                            session=FakeSession())
    assert [e.game for e in entries] == ["Doom", "Quake"]

    class AngrySession:
        def get(self, url, timeout):
            raise OSError("expired")

    with pytest.raises(OSError):
        fetch_entries({"type": "url", "url": "https://example.org/x"},
                      session=AngrySession())


def test_a_urlless_url_list_is_empty_not_an_error():
    assert fetch_entries({"type": "url", "url": ""}) == []


def test_an_unknown_type_is_refused():
    with pytest.raises(ValueError):
        fetch_entries({"type": "trakt"})
