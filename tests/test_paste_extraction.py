"""What people actually paste, and what the sync says when it fails."""

from romarr.connect import extract_value


def test_the_whole_json_document_is_accepted():
    """Nobody selects the characters between two quotes -- they select all
    and copy. Both live attempts pasted the entire document."""
    assert extract_value(
        "npsso", '{"npsso":"TOKEN123","expires_in":5179159}') == "TOKEN123"
    assert extract_value(
        "ea_token", '{"access_token":"EA1","token_type":"Bearer"}') == "EA1"
    assert extract_value(
        "epic_code", '{"redirectUrl":"x","authorizationCode":"EP1"}') == "EP1"
    assert extract_value(
        "gog_username", '{"country":"US","username":"wade"}') == "wade"


def test_a_bare_value_still_works():
    assert extract_value("npsso", "TOKEN123") == "TOKEN123"
    assert extract_value("itchio_key", "  KEY  ") == "KEY"


def test_a_url_carrying_the_value_works():
    """Epic's code lands in the address bar when the redirect completes."""
    assert extract_value(
        "epic_code", "https://epic/cb?code=EP2&state=x") == "EP2"


def test_a_partial_selection_that_grabbed_a_quote_is_tolerated():
    assert extract_value("npsso", '"TOKEN123",') == "TOKEN123"


def test_broken_json_falls_back_to_the_raw_text():
    """Half a document is still better treated as a value than refused."""
    assert extract_value("npsso", '{"npsso":"TOKEN') .startswith("{")


def test_nothing_in_nothing_out():
    assert extract_value("npsso", "") == ""
    assert extract_value("npsso", "   ") == ""
