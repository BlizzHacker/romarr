"""Refusing to reuse a collection torrent says why, and what to do."""

from __future__ import annotations

import pytest

from romarr.retained_collection import validate_owners

OWNER = {"client": "q", "download_id": "qbit-tag:romarr-collection-abc",
         "selected_file": {"hash": "h", "index": 0, "path": "a.bin", "size": 1}}
FILES = [{"index": 0, "name": "a.bin", "size": 1, "priority": 1}]


def test_a_torrent_romarr_did_not_add_explains_how_to_hand_it_over():
    with pytest.raises(ValueError, match="ROMarr did not add it"):
        validate_owners({"hash": "h", "category": "romarr", "tags": "x"}, FILES, [], "q", "romarr")


def test_a_wrong_category_names_both_categories():
    with pytest.raises(ValueError, match="category 'games'"):
        validate_owners({"hash": "h", "category": "games", "tags": "romarr-collection-abc"},
                        FILES, [OWNER], "q", "romarr")


def test_changed_tags_say_so():
    with pytest.raises(ValueError, match="tags no longer match"):
        validate_owners({"hash": "h", "category": "romarr", "tags": "other"},
                        FILES, [OWNER], "q", "romarr")


def test_a_matching_torrent_passes():
    validate_owners({"hash": "h", "category": "romarr", "tags": "romarr-collection-abc"},
                    FILES, [OWNER], "q", "romarr")
