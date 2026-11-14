"""Safe, synthetic Part 2 decision-core tests; no real ROMs or import writes."""
import unittest

from romarr.request_identity import Candidate, Decision, REASON_CODES, decide_request

OK = Candidate("verified", "Sonic the Hedgehog (USA)", "Synthetic Genesis DAT", "genesis")


def check(*choices, game="Sonic the Hedgehog", platform="genesis", **kwargs):
    return decide_request(game, platform, choices or (OK,), **kwargs)


class RequestIdentityPolicyTests(unittest.TestCase):
    def test_correct_title_region_tag(self):
        self.assertEqual(check().code, "MATCH_CONFIRMED")

    def test_import_is_only_action_on_confirmed_match(self):
        self.assertEqual(check().action, "import")

    def test_wrong_title_held(self):
        self.assertEqual(check(Candidate("verified", "Streets of Rage", "DAT", "genesis")).code,
                         "REQUEST_TITLE_MISMATCH")

    def test_wrong_title_even_reviewed_held(self):
        self.assertEqual(check(Candidate("verified", "Streets of Rage", "DAT", "genesis"), reviewed=True).action,
                         "hold")

    def test_sequel_not_prefix(self):
        self.assertEqual(check(Candidate("verified", "Sonic the Hedgehog 2", "DAT", "genesis")).action,
                         "hold")

    def test_similar_name_not_guessed(self):
        self.assertEqual(check(Candidate("verified", "Sonic & Knuckles", "DAT", "genesis")).action,
                         "hold")

    def test_punctuation_only_difference(self):
        self.assertEqual(check(Candidate("verified", "Sonic: the Hedgehog", "DAT", "genesis")).action,
                         "import")

    def test_accents_normalized(self):
        self.assertEqual(check(Candidate("verified", "Pokémon", "DAT", "gb"), game="Pokemon", platform="gb").action,
                         "import")

    def test_unknown_is_hold_not_bad_dump(self):
        d = check(Candidate("unknown"))
        self.assertEqual((d.action, d.code), ("hold", "DAT_UNKNOWN"))

    def test_bad_dump_rejected(self):
        d = check(Candidate("bad-dump"))
        self.assertEqual((d.action, d.code), ("reject", "DAT_FAILED"))

    def test_multiple_valid_games_held(self):
        self.assertEqual(check(OK, Candidate("verified", "Streets of Rage", "DAT", "genesis")).code,
                         "AMBIGUOUS_MATCH")

    def test_duplicate_same_game_still_ambiguous(self):
        self.assertEqual(check(OK, OK).code, "AMBIGUOUS_MATCH")

    def test_missing_dat_platform_held(self):
        self.assertEqual(check(Candidate("verified", "Sonic the Hedgehog", "DAT")).code,
                         "DAT_PLATFORM_UNCONFIRMED")

    def test_wrong_dat_platform_held(self):
        self.assertEqual(check(Candidate("verified", "Sonic the Hedgehog", "DAT", "snes")).code,
                         "DAT_PLATFORM_MISMATCH")

    def test_platform_casefold(self):
        self.assertEqual(check(Candidate("verified", "Sonic the Hedgehog", "DAT", "GENESIS")).action,
                         "import")

    def test_missing_dat_title_held(self):
        self.assertEqual(check(Candidate("verified", "", "DAT", "genesis")).code,
                         "DAT_IDENTITY_MISSING")

    def test_missing_dat_catalog_held(self):
        self.assertEqual(check(Candidate("verified", "Sonic the Hedgehog", "", "genesis")).code,
                         "DAT_IDENTITY_MISSING")

    def test_no_candidates_held(self):
        self.assertEqual(decide_request("Sonic", "genesis", []).code, "DAT_UNKNOWN")

    def test_missing_request_held(self):
        self.assertEqual(check(game="").code, "REQUEST_MISSING")

    def test_missing_platform_held(self):
        self.assertEqual(check(platform="").code, "REQUEST_MISSING")

    def test_incomplete_download_held(self):
        self.assertEqual(check(source_complete=False).code, "SOURCE_INCOMPLETE")

    def test_incomplete_disc_set_held(self):
        self.assertEqual(check(Candidate("verified", "Sonic the Hedgehog", "DAT", "genesis", False)).code,
                         "SOURCE_INCOMPLETE")

    def test_unsafe_source_rejected(self):
        self.assertEqual(check(source_safe=False).code, "SOURCE_UNSAFE")

    def test_safety_limit_rejected(self):
        self.assertEqual(check(safety_limit_hit=True).code, "SAFETY_LIMIT")

    def test_unsupported_source_held(self):
        self.assertEqual(check(source_supported=False).code, "UNSUPPORTED_FORMAT")

    def test_unknown_verification_status_held(self):
        self.assertEqual(check(Candidate("INVALID")).code, "REVIEW_REQUIRED")

    def test_one_bad_among_verified_rejected(self):
        self.assertEqual(check(OK, Candidate("bad-dump")).code, "DAT_FAILED")

    def test_one_unknown_among_verified_held(self):
        self.assertEqual(check(OK, Candidate("unknown")).code, "DAT_UNKNOWN")

    def test_optional_trusted_title_alias_check(self):
        alias = lambda actual, requested: actual == "The Legend of Zelda" and requested == "Zelda"
        self.assertEqual(check(Candidate("verified", "The Legend of Zelda", "DAT", "nes"),
                               game="Zelda", platform="nes", title_equivalent=alias).action, "import")

    def test_alias_callback_not_used_to_override_platform(self):
        self.assertEqual(check(Candidate("verified", "The Legend of Zelda", "DAT", "snes"),
                               game="Zelda", platform="nes", title_equivalent=lambda a,b: True).action, "hold")

    def test_does_not_use_filename_as_identity(self):
        self.assertEqual(check(Candidate("verified", "Streets of Rage", "DAT", "genesis"),
                               game="Sonic the Hedgehog").action, "hold")

    def test_messages_do_not_echo_game_or_paths(self):
        d = decide_request("/private/secret/Game", "genesis", [OK])
        self.assertNotIn("/private/", d.message)

    def test_invalid_candidate_type_fails(self):
        with self.assertRaises(TypeError):
            decide_request("Sonic", "genesis", ["bad-data"])

    def test_all_decisions_use_known_reason_codes(self):
        for d in (check(), check(Candidate("unknown")), check(Candidate("bad-dump")),
                  check(OK, OK), check(source_safe=False)):
            self.assertIn(d.code, REASON_CODES)

    def test_invalid_action_rejected(self):
        with self.assertRaises(ValueError):
            Decision("delete", "MATCH_CONFIRMED", "x")

    def test_invalid_code_rejected(self):
        with self.assertRaises(ValueError):
            Decision("hold", "CUSTOM", "x")


# Additional PR #18 review regressions; these are intentionally separate from
# the 36 owner-run v02 tests and must not be counted as part of that old run.
class ReviewedNormalizationTests(unittest.TestCase):
    def test_revenge_is_not_revision_metadata(self):
        d = decide_request('Sonic', 'genesis', [Candidate('verified','Sonic (Revenge)','DAT','genesis')])
        self.assertEqual(d.code, 'REQUEST_TITLE_MISMATCH')

    def test_revolution_is_not_revision_metadata(self):
        d = decide_request('Sonic', 'genesis', [Candidate('verified','Sonic (Revolution)','DAT','genesis')])
        self.assertEqual(d.code, 'REQUEST_TITLE_MISMATCH')

    def test_revival_is_not_revision_metadata(self):
        d = decide_request('Sonic', 'genesis', [Candidate('verified','Sonic (Revival)','DAT','genesis')])
        self.assertEqual(d.code, 'REQUEST_TITLE_MISMATCH')

    def test_numeric_revision_tag_can_be_ignored(self):
        d = decide_request('Sonic', 'genesis', [Candidate('verified','Sonic (Rev 1)','DAT','genesis')])
        self.assertEqual(d.code, 'MATCH_CONFIRMED')

    def test_single_letter_revision_tag_can_be_ignored(self):
        d = decide_request('Sonic', 'genesis', [Candidate('verified','Sonic (Rev A)','DAT','genesis')])
        self.assertEqual(d.code, 'MATCH_CONFIRMED')

    def test_non_latin_titles_cannot_collapse_to_same_suffix(self):
        d = decide_request('三國志 II', 'snes', [Candidate('verified','信長の野望 II','DAT','snes')])
        self.assertEqual(d.code, 'REQUEST_TITLE_MISMATCH')

    def test_japanese_kana_voice_marks_are_distinct(self):
        d = decide_request('は', 'nes', [Candidate('verified','ば','DAT','nes')])
        self.assertEqual(d.code, 'REQUEST_TITLE_MISMATCH')

    def test_non_latin_identical_titles_are_accepted(self):
        d = decide_request('三國志 II', 'snes', [Candidate('verified','三國志 II','DAT','snes')])
        self.assertEqual(d.code, 'MATCH_CONFIRMED')

    def test_comma_separated_region_tags(self):
        for tag in ("(USA, Europe)", "( usa , europe )"):
            d = decide_request("Super Metroid", "snes", [
                Candidate("verified", "Super Metroid " + tag, "DAT", "snes")])
            self.assertEqual(d.code, "MATCH_CONFIRMED")

    def test_comma_separated_language_tags(self):
        for tag in ("(En,Fr,De)", "( en, fr, de )"):
            d = decide_request("Sonic", "genesis", [
                Candidate("verified", "Sonic " + tag, "DAT", "genesis")])
            self.assertEqual(d.code, "MATCH_CONFIRMED")

    def test_mixed_metadata_and_content_words_preserved(self):
        for tag in ("(USA, Revenge)", "(En, Bonus)"):
            d = decide_request("Sonic", "genesis", [
                Candidate("verified", "Sonic " + tag, "DAT", "genesis")])
            self.assertEqual(d.code, "REQUEST_TITLE_MISMATCH")


    def test_devanagari_vowel_signs_are_distinct(self):
        decision = decide_request("कि", "nes", [
            Candidate("verified", "कु", "DAT", "nes")])
        self.assertEqual((decision.action, decision.code),
                         ("hold", "REQUEST_TITLE_MISMATCH"))

    def test_devanagari_matching_title_can_be_confirmed(self):
        decision = decide_request("कि", "nes", [
            Candidate("verified", "कि", "DAT", "nes")])
        self.assertEqual((decision.action, decision.code),
                         ("import", "MATCH_CONFIRMED"))

    def test_arabic_vowel_signs_are_distinct(self):
        decision = decide_request("قَلْب", "nes", [
            Candidate("verified", "قُلْب", "DAT", "nes")])
        self.assertEqual(decision.code, "REQUEST_TITLE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
