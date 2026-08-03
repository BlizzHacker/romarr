"""Notifications, and the one thing they say that others cannot.

Eight providers is a table, not eight clients -- almost every one is an HTTP
POST with a differently-shaped body. The tests that matter are the ones about
the *message*: Radarr tells you it grabbed something, and ROMarr can tell you
what it weighed, because the scorer already explained itself.
"""

from __future__ import annotations

import json

import pytest

from romarr import notify
from romarr.notify import (
    NOTIFIERS, Message, Notifier, ON_BAD_DUMP, ON_FAILURE, ON_GRAB, ON_IMPORT,
    failed, grabbed, imported)


@pytest.fixture
def sent(monkeypatch):
    """Capture every outbound request instead of making one."""
    calls = []

    def fake(url, *, data=None, headers=None, method="POST"):
        calls.append({"url": url, "data": data, "headers": headers or {},
                      "method": method})
        return True

    monkeypatch.setattr(notify, "_post", fake)
    return calls


def conn(kind, **kw):
    return {"type": kind, "name": kind, "enable": True,
            "url": f"https://{kind}.test/hook", **kw}


# --- every provider is real ------------------------------------------------

@pytest.mark.parametrize("kind", sorted(NOTIFIERS))
def test_every_provider_sends_something(kind, sent):
    Notifier([conn(kind, token="t", chat_id="1", user="u")]).send(
        Message(ON_GRAB, "Chrono Trigger (USA)"))
    assert len(sent) == 1, f"{kind} sent nothing"
    assert sent[0]["url"], kind


@pytest.mark.parametrize("kind", sorted(NOTIFIERS))
def test_every_provider_is_declared_completely(kind):
    spec = NOTIFIERS[kind]
    assert spec["label"] and callable(spec["send"])
    assert spec["fields"] and spec["help"]


def test_discord_sends_the_rendered_text(sent):
    Notifier([conn("discord")]).send(
        Message(ON_GRAB, "Grabbed X", body="SNES", reasons=("+50 good dump",)))
    payload = json.loads(sent[0]["data"])
    assert "Grabbed X" in payload["content"]
    assert "+50 good dump" in payload["content"]


def test_a_generic_webhook_gets_structure_not_prose(sent):
    """Anything consuming this is a program. Handing it a rendered string to
    parse back into fields is what makes generic webhooks useless."""
    Notifier([conn("webhook")]).send(
        Message(ON_GRAB, "Grabbed X", body="SNES", reasons=("+50 a", "-20 b")))
    payload = json.loads(sent[0]["data"])
    assert payload["event"] == ON_GRAB
    assert payload["title"] == "Grabbed X"
    assert payload["reasons"] == ["+50 a", "-20 b"]


def test_ntfy_puts_the_title_in_the_header_where_it_belongs(sent):
    Notifier([conn("ntfy")]).send(Message(ON_GRAB, "Grabbed X", body="SNES"))
    assert sent[0]["headers"]["Title"] == "Grabbed X"
    assert b"SNES" in sent[0]["data"]


def test_telegram_targets_the_bot_api_not_the_configured_url(sent):
    Notifier([{"type": "telegram", "name": "tg", "token": "123:abc",
               "chat_id": "42"}]).send(Message(ON_GRAB, "X"))
    assert "api.telegram.org/bot123:abc/sendMessage" in sent[0]["url"]
    assert json.loads(sent[0]["data"])["chat_id"] == "42"


def test_gotify_carries_the_token_in_the_query(sent):
    Notifier([conn("gotify", token="tok")]).send(Message(ON_GRAB, "X"))
    assert "token=tok" in sent[0]["url"]


# --- event filtering -------------------------------------------------------

def test_a_connection_with_no_event_list_gets_everything():
    """Somebody who just pasted a Discord webhook wants to be told things."""
    assert Notifier.wants({"type": "discord"}, ON_GRAB)
    assert Notifier.wants({"type": "discord"}, ON_FAILURE)


def test_an_empty_event_list_means_nothing():
    """It says exactly what it says. Reading it as "everything" would make
    muting a connection impossible."""
    assert not Notifier.wants({"type": "discord", "events": []}, ON_GRAB)


def test_only_subscribed_events_are_delivered(sent):
    notifier = Notifier([conn("discord", events=[ON_FAILURE])])
    notifier.send(Message(ON_GRAB, "grabbed"))
    assert sent == []
    notifier.send(Message(ON_FAILURE, "failed"))
    assert len(sent) == 1


def test_a_disabled_connection_is_skipped(sent):
    Notifier([conn("discord", enable=False)]).send(Message(ON_GRAB, "X"))
    assert sent == []


# --- failure is never allowed to matter ------------------------------------

def test_a_provider_that_raises_does_not_break_the_send(monkeypatch):
    def boom(url, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "_post", boom)
    results = Notifier([conn("discord"), conn("slack")]).send(
        Message(ON_GRAB, "X"))
    assert [r["ok"] for r in results] == [False, False]


def test_one_provider_failing_does_not_stop_the_others(monkeypatch):
    calls = []

    def flaky(url, **kw):
        calls.append(url)
        if "discord" in url:
            raise OSError("nope")
        return True

    monkeypatch.setattr(notify, "_post", flaky)
    results = Notifier([conn("discord"), conn("slack")]).send(
        Message(ON_GRAB, "X"))
    assert len(calls) == 2
    assert [r["ok"] for r in results] == [False, True]


def test_an_unknown_provider_type_is_skipped_not_fatal(sent):
    results = Notifier([{"type": "nonsense", "name": "x"}]).send(
        Message(ON_GRAB, "X"))
    assert results == [] and sent == []


def test_a_webhook_url_is_not_written_to_the_log_in_full(caplog, monkeypatch):
    """A Discord or Slack webhook path IS the credential -- anybody holding it
    can post to that channel. Logging a failure must not leak it."""
    def boom(url, **kw):
        raise OSError("refused")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    with caplog.at_level("WARNING"):
        notify._post("https://discord.com/api/webhooks/123/SUPERSECRETTOKEN")
    assert "SUPERSECRETTOKEN" not in caplog.text
    assert "discord.com" in caplog.text


# --- the message other tools cannot send -----------------------------------

def test_a_grab_notification_carries_the_reasons_it_was_chosen():
    """The whole point. Radarr says "Grabbed X"; this says what it weighed,
    which is the difference between trusting the automation and checking it."""
    message = grabbed("Chrono Trigger (USA) [!]", "Super Nintendo",
                      "RetroWithin",
                      ["+50 verified good dump [!]", "+40 region usa"])
    text = message.text()
    assert "Chrono Trigger (USA) [!]" in text
    assert "RetroWithin" in text
    assert "+50 verified good dump [!]" in text
    assert "+40 region usa" in text


def test_an_import_notification_reports_the_dat_verdict():
    from romarr.dat import Match

    good = imported("Super Metroid", "SNES",
                    Match("verified", game="Super Metroid (USA)"))
    assert good.event == ON_IMPORT
    assert "verified" in good.text() and "Super Metroid (USA)" in good.text()


def test_a_bad_dump_is_its_own_event_so_it_can_be_routed_separately():
    """Somebody who wants to be paged for a corrupt import and not for every
    successful one needs these to be different events."""
    from romarr.dat import Match

    message = imported("X", "SNES", Match("bad-dump", detail="checksum differs"))
    assert message.event == ON_BAD_DUMP
    assert "BAD DUMP" in message.text()


def test_an_unverified_import_is_still_an_import():
    from romarr.dat import Match

    message = imported("Homebrew", "SNES", Match("unknown"))
    assert message.event == ON_IMPORT
    assert "not in any loaded DAT" in message.text()


def test_a_failure_carries_the_reason():
    assert "no seeders" in failed("X", "no seeders on a public tracker").text()
