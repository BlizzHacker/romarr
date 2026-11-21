"""Complete editions hold the game and its DLC (behaviour learned from Gamarr)."""

import pytest

from romarr.game_packages import BASE, DLC, UPDATE, release_parts


@pytest.mark.parametrize("title,wanted", [
    ("The Witcher 3 Wild Hunt GOTY Edition", "The Witcher 3 Wild Hunt"),
    ("Fallout.4.Game.of.the.Year.Edition-GRP", "Fallout 4"),
    ("Borderlands 2 Complete Edition", "Borderlands 2"),
    ("Sleeping Dogs Definitive Edition", "Sleeping Dogs"),
    ("Mortal Kombat 11 Ultimate Edition [PS4]", "Mortal Kombat 11"),
    ("Skyrim Legendary Edition", "Skyrim"),
])
def test_complete_editions_are_the_game_plus_dlc(title, wanted):
    assert release_parts(title, wanted) >= {BASE, DLC}


def test_an_edition_named_in_the_request_is_just_the_game():
    assert release_parts("Tomb Raider Definitive Edition", "Tomb Raider Definitive Edition") == {BASE}


def test_an_extra_that_mentions_an_edition_stays_an_extra():
    parts = release_parts("Game Update v2.5 for the Gold Edition", "Game")
    assert BASE not in parts and UPDATE in parts


def test_edition_words_inside_other_words_do_not_count():
    assert release_parts("GoldenEye 007 (USA)", "GoldenEye 007") == {BASE}
    assert release_parts("Gold Rush Edition Wars", "Gold Rush Edition Wars") == {BASE}
