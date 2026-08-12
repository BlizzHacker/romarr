"""IGDB as a real provider, not a field on a settings page.

This install had valid IGDB credentials stored and enabled, and Discover and
the Calendar both told the operator that no provider was configured and that
they should go and register for RAWG. Nothing was broken about the
credentials; the two code paths simply only knew the word "rawg". These tests
pin the provider that fixes that, and -- as much -- the copy, because "no
provider is configured" printed at somebody who configured one is the failure
the whole thing was.

Nothing here carries a real credential. `cid`/`sec` below are literal
placeholders and the network is never touched.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from romarr import metadata as m

IGDB = {"type": "igdb", "client_id": "cid", "token": "sec", "enable": True}


class Reply:
    """The two things `urlopen`'s result is used for."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def igdb(monkeypatch):
    """A fake IGDB, recording every call and answering whatever is queued.

    The token cache is process-wide on purpose -- a token per request would
    be two round trips per cover -- so it has to be emptied around each test
    or the second test in a file silently never exchanges anything.
    """
    monkeypatch.setattr(m, "_IGDB_TOKENS", {})
    monkeypatch.setattr(m, "_IGDB_NEXT", [0.0])
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)

    calls: list[dict] = []
    state = {"token": {"access_token": "bearer-1", "expires_in": 5184000},
             "rows": [], "fail": None}

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        body = (request.data or b"").decode()
        calls.append({"url": url, "body": body,
                      "headers": dict(request.headers)})
        if url.startswith(m.IGDB_TOKEN_URL):
            if isinstance(state["token"], Exception):
                raise state["token"]
            return Reply(state["token"])
        if state["fail"] is not None:
            failure, state["fail"] = state["fail"], None
            raise failure
        rows = state["rows"]
        return Reply(rows.pop(0) if isinstance(rows, list) and rows
                     and isinstance(rows[0], list) else rows)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return type("Fake", (), {"calls": calls, "state": state})()


def http_error(code):
    return urllib.error.HTTPError("https://api.igdb.com/v4/games", code,
                                  "no", {}, io.BytesIO(b""))


def api_calls(igdb):
    return [c for c in igdb.calls if c["url"].startswith(m.IGDB_API)]


# --- the cover URL, which is the classic IGDB trap --------------------------

def test_a_cover_url_is_given_a_protocol_and_a_usable_size():
    """IGDB answers `//images.igdb.com/.../t_thumb/co2lbd.jpg`: no scheme, and
    a 90-pixel thumbnail whatever the row is for. Miss either half and the
    grid draws broken images."""
    assert m.igdb_cover("//images.igdb.com/igdb/image/upload/t_thumb/co2lbd.jpg") \
        == "https://images.igdb.com/igdb/image/upload/t_cover_big/co2lbd.jpg"


def test_any_size_token_is_swapped_not_just_the_default_one():
    """Matched by shape rather than by the literal `t_thumb`, so a row that
    arrives at some other size is still resized instead of passed through."""
    assert m.igdb_cover(
        "//images.igdb.com/igdb/image/upload/t_screenshot_med/x.jpg") \
        == "https://images.igdb.com/igdb/image/upload/t_cover_big/x.jpg"


def test_no_cover_stays_no_cover():
    assert m.igdb_cover("") == "" and m.igdb_cover(None) == ""


def test_an_already_absolute_url_keeps_its_scheme():
    assert m.igdb_cover("https://images.igdb.com/a/t_thumb/b.jpg").startswith(
        "https://")


# --- the token, which must not be fetched per request -----------------------

def test_the_token_is_exchanged_once_and_reused(igdb):
    igdb.state["rows"] = [{"id": 1, "name": "Chrono Trigger"}]
    for _ in range(3):
        m.igdb_query(IGDB, "games", "fields name; limit 1;")
    exchanges = [c for c in igdb.calls if c["url"].startswith(m.IGDB_TOKEN_URL)]
    assert len(exchanges) == 1, "a token per request is two round trips a cover"
    assert len(api_calls(igdb)) == 3


def test_the_exchange_is_client_credentials(igdb):
    igdb.state["rows"] = []
    m.igdb_query(IGDB, "games", "fields name;")
    exchange = igdb.calls[0]["url"]
    assert "grant_type=client_credentials" in exchange
    assert "client_id=cid" in exchange


def test_an_expired_token_is_refreshed_once_and_the_query_retried(igdb):
    """Sixty days outlives most processes but not all of them, and Twitch can
    revoke early. The 401 has to be a refresh, not an empty shelf."""
    igdb.state["fail"] = http_error(401)
    igdb.state["rows"] = [{"id": 1, "name": "Portal 2"}]
    rows = m.igdb_query(IGDB, "games", "fields name;")
    assert [r["name"] for r in rows] == ["Portal 2"]
    exchanges = [c for c in igdb.calls if c["url"].startswith(m.IGDB_TOKEN_URL)]
    assert len(exchanges) == 2, "the second exchange is the forced refresh"


def test_a_second_401_is_a_failure_rather_than_a_loop(igdb, monkeypatch):
    seen = []

    def always_401(request, timeout=None):
        if request.full_url.startswith(m.IGDB_TOKEN_URL):
            return Reply({"access_token": "b", "expires_in": 100})
        seen.append(1)
        raise http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", always_401)
    assert m.igdb_query(IGDB, "games", "fields name;") == []
    assert len(seen) == 2


def test_a_stored_bearer_token_still_works_when_the_exchange_is_refused(igdb):
    """Configs written before ROMarr did the exchange hold a bearer token in
    this field, because that is what the old help text asked for. Telling
    those operators their working setup is broken would be a regression."""
    igdb.state["token"] = urllib.error.URLError("nope")
    igdb.state["rows"] = [{"id": 1, "name": "Super Metroid"}]
    rows = m.igdb_query(IGDB, "games", "fields name;")
    assert [r["name"] for r in rows] == ["Super Metroid"]
    assert api_calls(igdb)[0]["headers"]["Authorization"] == "Bearer sec"


def test_no_credential_means_no_request_at_all(igdb):
    assert m.igdb_query({"type": "igdb", "client_id": "cid"}, "games", "x") == []
    assert m.igdb_query({"type": "igdb", "token": "sec"}, "games", "x") == []
    assert igdb.calls == []


# --- the credential must not leak ------------------------------------------

def test_the_secret_is_never_written_to_the_log(igdb, caplog):
    igdb.state["fail"] = http_error(500)
    with caplog.at_level("DEBUG"):
        m.igdb_query(IGDB, "games", "fields name;")
    assert "sec" not in caplog.text.replace("seconds", "")


def test_the_token_cache_is_keyed_by_a_digest_not_by_the_credential(igdb):
    igdb.state["rows"] = []
    m.igdb_query(IGDB, "games", "fields name;")
    assert list(m._IGDB_TOKENS), "something should have been cached"
    assert not any("sec" in key or "cid" in key for key in m._IGDB_TOKENS), \
        "a stray repr() of this dict must not carry the credential"


# --- the rate limit ---------------------------------------------------------

def test_requests_are_spaced_to_igdbs_four_a_second(igdb, monkeypatch):
    slept: list[float] = []
    clock = [1000.0]

    def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(m.time, "sleep", fake_sleep)
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    igdb.state["rows"] = []
    for _ in range(4):
        m.igdb_query(IGDB, "games", "fields name;")
    # The first goes straight through; the next three each wait out a quarter
    # of a second, because nothing else on this clock consumed any of it.
    assert len(slept) == 3
    assert all(abs(s - 0.25) < 1e-9 for s in slept)
    assert clock[0] - 1000.0 == pytest.approx(0.75)


# --- the query language -----------------------------------------------------

def test_the_body_is_apicalypse_and_not_json(igdb):
    """Posting JSON here earns a 400 that reads like a field error and sends
    people looking in entirely the wrong place."""
    igdb.state["rows"] = []
    m.discover([IGDB], shelf="upcoming")
    body = api_calls(igdb)[0]["body"]
    assert body.startswith("fields ") and body.rstrip().endswith(";")
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)


def test_upcoming_asks_for_games_dated_after_now_in_date_order(igdb):
    igdb.state["rows"] = []
    m.discover([IGDB], shelf="upcoming")
    body = api_calls(igdb)[0]["body"]
    assert "where first_release_date > " in body
    assert "sort first_release_date asc" in body


def test_new_releases_is_a_recent_window_newest_first(igdb):
    import time as clock

    igdb.state["rows"] = []
    m.discover([IGDB], shelf="new")
    body = api_calls(igdb)[0]["body"]
    assert "sort first_release_date desc" in body
    low = int(body.split("first_release_date > ")[1].split(" ")[0])
    high = int(body.split("first_release_date <= ")[1].split(" ")[0])
    assert 29 * 86400 <= high - low <= 31 * 86400
    assert abs(high - clock.time()) < 60


def test_dated_shelves_exclude_dlc(igdb):
    """Without this the shelf is half expansion packs: "Helldivers 2: Face the
    Unknown" dated next Tuesday is a warbond, not a release."""
    igdb.state["rows"] = []
    for shelf in ("upcoming", "new"):
        igdb.calls.clear()
        m.discover([IGDB], shelf=shelf)
        assert m.IGDB_MAIN_GAMES in api_calls(igdb)[0]["body"]


def test_the_limit_is_clamped_to_what_igdb_accepts(igdb):
    """Over 500 is a 400 rather than a truncation."""
    igdb.state["rows"] = []
    m.discover([IGDB], shelf="upcoming", limit=5000)
    assert f"limit {m.IGDB_MAX_LIMIT};" in api_calls(igdb)[0]["body"]


# --- the popular shelf ------------------------------------------------------

def test_popular_reapplies_the_ranking_igdb_throws_away(igdb):
    """`where id = (...)` answers in IGDB's own order, not the order asked
    for. A Popular shelf silently sorted by internal database id looks
    entirely plausible and means nothing."""
    igdb.state["rows"] = [
        [{"game_id": 1020, "value": 9.0},
         {"game_id": 1942, "value": 8.0},
         {"game_id": 72, "value": 7.0}],
        [{"id": 72, "name": "Portal 2"},
         {"id": 1020, "name": "Grand Theft Auto V"},
         {"id": 1942, "name": "The Witcher 3: Wild Hunt"}],
    ]
    out = m.discover([IGDB], shelf="popular")
    assert [g["title"] for g in out["items"]] == [
        "Grand Theft Auto V", "The Witcher 3: Wild Hunt", "Portal 2"]


def test_popular_reads_the_played_signal(igdb):
    igdb.state["rows"] = [[], []]
    m.discover([IGDB], shelf="popular")
    first = api_calls(igdb)[0]
    assert first["url"].endswith("/popularity_primitives")
    assert f"popularity_type = {m.IGDB_POPULARITY_PLAYED}" in first["body"]


def test_popular_with_nothing_ranked_does_not_ask_for_game_zero(igdb):
    igdb.state["rows"] = [[], []]
    m.discover([IGDB], shelf="popular")
    assert len(api_calls(igdb)) == 1, "no ids means no second call"


# --- the row shape ----------------------------------------------------------

ROW = {"id": 1020, "name": "Grand Theft Auto V",
       "first_release_date": 1379376000, "total_rating": 92.5,
       "cover": {"url": "//images.igdb.com/igdb/image/upload/t_thumb/co2lbd.jpg"},
       "platforms": [{"name": "PlayStation 3"}, {"name": "Xbox 360"}]}


def test_a_shelf_row_carries_what_the_grid_draws(igdb):
    igdb.state["rows"] = [ROW]
    game = m.discover([IGDB], shelf="upcoming")["items"][0]
    assert game["title"] == "Grand Theft Auto V"
    assert game["released"] == "2013-09-17"
    assert game["cover_url"].startswith("https://") and "t_cover_big" in game["cover_url"]
    assert game["platforms"] == ["PlayStation 3", "Xbox 360"]
    assert game["source"] == "IGDB"


def test_the_rating_is_converted_to_the_scale_the_star_means(igdb):
    """IGDB rates out of 100 and RAWG out of 5, and the UI draws one star next
    to whichever number arrives."""
    igdb.state["rows"] = [ROW]
    assert m.discover([IGDB], shelf="upcoming")["items"][0]["rating"] == 4.6


def test_an_undated_row_says_so_rather_than_claiming_1970(igdb):
    igdb.state["rows"] = [{"id": 5, "name": "Untitled"}]
    assert m.discover([IGDB], shelf="upcoming")["items"][0]["released"] == ""


def test_every_provider_row_is_marked_as_not_owned(igdb):
    """These sit one button away from a grid of games the operator actually
    holds, and the two must not read alike."""
    igdb.state["rows"] = [ROW]
    assert m.discover([IGDB], shelf="upcoming")["items"][0]["owned"] is False
    igdb.state["rows"] = [ROW]
    assert m.calendar([IGDB])["items"][0]["owned"] is False


def test_the_shelf_names_the_provider_that_served_it(igdb):
    igdb.state["rows"] = [ROW]
    out = m.discover([IGDB], shelf="upcoming")
    assert out["provider"] == "igdb" and out["provider_label"] == "IGDB"
    assert out["error"] is None


# --- the calendar -----------------------------------------------------------

def test_the_calendar_converts_its_window_to_unix_seconds(igdb):
    import datetime

    igdb.state["rows"] = []
    out = m.calendar([IGDB], days_back=30, days_ahead=60)
    body = api_calls(igdb)[0]["body"]
    low = int(body.split("first_release_date >= ")[1].split(" ")[0])
    high = int(body.split("first_release_date <= ")[1].split(" ")[0])
    # Against the window the response reports, not against a second reading
    # of the clock -- the two disagree by a day either side of midnight UTC.
    assert datetime.datetime.fromtimestamp(
        low, datetime.timezone.utc).date().isoformat() == out["from"]
    # The far end is inclusive to the second, or a game dated on the last day
    # of the window falls out of it.
    assert datetime.datetime.fromtimestamp(
        high, datetime.timezone.utc).date().isoformat() == out["to"]
    assert high - low == 90 * 86400 + 86399


def test_the_calendar_marks_which_igdb_rows_are_still_upcoming(igdb):
    import datetime

    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
    igdb.state["rows"] = [
        {"id": 1, "name": "Later", "first_release_date": int(soon.timestamp())},
        {"id": 2, "name": "Earlier", "first_release_date": int(past.timestamp())}]
    rows = m.calendar([IGDB])["items"]
    assert {r["title"]: r["upcoming"] for r in rows} == {
        "Later": True, "Earlier": False}


def test_the_upcoming_window_opens_tomorrow_not_today(igdb):
    """What the Calendar's Upcoming view sends. Zero would open the window on
    today, and today's releases are forty games that are already out sitting
    under a heading that says they are not -- which is also the whole limit
    spent before anything actually forthcoming gets a place."""
    import datetime

    igdb.state["rows"] = [
        {"id": 1, "name": "Out this morning",
         "first_release_date": int(datetime.datetime.combine(
             datetime.date.today(), datetime.time(),
             datetime.timezone.utc).timestamp())}]
    out = m.calendar([IGDB], days_back=-1, days_ahead=60)
    assert out["from"] == (datetime.date.today()
                           + datetime.timedelta(days=1)).isoformat()
    low = int(api_calls(igdb)[0]["body"].split(
        "first_release_date >= ")[1].split(" ")[0])
    assert datetime.datetime.fromtimestamp(
        low, datetime.timezone.utc).date() > datetime.date.today()


def test_a_game_out_today_is_not_reported_as_upcoming(igdb):
    import datetime

    igdb.state["rows"] = [
        {"id": 1, "name": "Out this morning",
         "first_release_date": int(datetime.datetime.combine(
             datetime.date.today(), datetime.time(12),
             datetime.timezone.utc).timestamp())}]
    assert m.calendar([IGDB])["items"][0]["upcoming"] is False


# --- the copy, which is what the bug actually was ---------------------------

def test_a_configured_igdb_is_not_told_to_go_and_add_rawg(igdb):
    """The reported failure, verbatim: credentials stored, valid, enabled, and
    a page saying "add RAWG under Settings -> Metadata"."""
    igdb.state["rows"] = []
    for out in (m.discover([IGDB], shelf="upcoming"), m.calendar([IGDB])):
        assert "RAWG" not in out["error"]
        assert "IGDB" in out["error"]
        assert "answered" in out["error"], \
            "it was reached and had nothing -- that is not a config error"


def test_an_empty_credential_names_the_empty_field():
    out = m.discover([{"type": "igdb", "client_id": "cid", "token": ""}])
    assert "client secret" in out["error"]
    assert "RAWG" not in out["error"]


def test_a_switched_off_provider_says_so_rather_than_going_missing():
    out = m.discover([dict(IGDB, enable=False)])
    assert "switched off" in out["error"] and "IGDB" in out["error"]


def test_a_provider_that_cannot_be_reached_is_not_reported_as_empty(monkeypatch):
    monkeypatch.setitem(m.DISCOVER_PROVIDERS, "igdb",
                        lambda cfg, shelf, limit: (_ for _ in ()).throw(
                            OSError("refused")))
    out = m.discover([IGDB], shelf="popular")
    assert "could not be reached" in out["error"]


def test_with_nothing_configured_both_providers_are_offered():
    """Naming one of two is how the operator who picked the other one gets
    told to register for an account they do not need."""
    for out in (m.discover([]), m.calendar([])):
        assert "IGDB" in out["error"] and "RAWG" in out["error"]


# --- wired, not merely written ----------------------------------------------

def test_igdb_is_registered_for_both_browse_paths():
    """The bug was never in the provider -- it was that these two tables only
    ever held one key."""
    assert "igdb" in m.DISCOVER_PROVIDERS and "igdb" in m.CALENDAR_PROVIDERS


def test_every_provider_declares_what_to_get_and_what_is_missing():
    for name, spec in m.PROVIDERS.items():
        assert callable(spec["ready"]), name
        assert spec["offer"] and spec["needs"], name


def test_the_ready_check_matches_the_fields_the_editor_offers():
    assert m.PROVIDERS["igdb"]["ready"](IGDB)
    assert not m.PROVIDERS["igdb"]["ready"]({"client_id": "cid"})
    assert m.PROVIDERS["rawg"]["ready"]({"api_key": "k"})
    assert not m.PROVIDERS["rawg"]["ready"]({})


def test_the_api_serves_an_igdb_shelf_end_to_end(tmp_path, igdb):
    """Through the store and the service, because a provider table nothing
    reads is dead code with a certificate."""
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    service.store.put_item("metadata_providers", dict(IGDB))
    service.reload_metadata()
    igdb.state["rows"] = [ROW]
    out = m.discover(service.store.list_items("metadata_providers"),
                     shelf="upcoming")
    assert out["items"][0]["title"] == "Grand Theft Auto V"
    assert out["provider_label"] == "IGDB"


def test_the_editor_calls_the_secret_what_it_is():
    """`token` is the storage key and has to stay one -- `put_item` replaces
    the whole entry, so renaming it would drop the credential the first time
    somebody saved the form. What it holds is a Twitch client secret, and a
    box labelled "Token" over help text about a secret is how somebody pastes
    the wrong half."""
    assert m.PROVIDERS["igdb"]["labels"]["token"] == "Client Secret"


def test_saving_a_provider_without_resending_its_secret_keeps_it(tmp_path):
    """A caller that renames a provider must not wipe the credential it was
    renaming -- the provider would then report itself as unconfigured, which
    is the confusion this whole change exists to end."""
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    saved = service.store.put_item("metadata_providers", dict(IGDB))
    stored = service.store.get_item("metadata_providers", saved["id"])
    assert stored["token"] == "sec"

    # What the route does with a body carrying only id, type and a new name.
    cfg = {"id": saved["id"], "type": "igdb", "name": "IGDB (main)"}
    for key in ("api_key", "token", "client_id"):
        if cfg.get(key, "") in ("********", ""):
            cfg[key] = stored.get(key, "")
    service.store.put_item("metadata_providers", cfg)
    service.reload_metadata()
    assert m.PROVIDERS["igdb"]["ready"](
        service.store.get_item("metadata_providers", saved["id"]))


def test_identify_goes_through_the_same_token_path(igdb):
    igdb.state["rows"] = [{"id": 1, "name": "Chrono Trigger",
                           "cover": {"url": "//x/t_thumb/y.jpg"}}]
    info = m.Metadata([dict(IGDB)]).identify(filename="Chrono Trigger.smc")
    assert info.title == "Chrono Trigger" and info.source == "IGDB"
    assert info.cover_url == "https://x/t_cover_big/y.jpg"
    assert [c for c in igdb.calls if c["url"].startswith(m.IGDB_TOKEN_URL)], \
        "identify used to require a hand-pasted bearer token"
