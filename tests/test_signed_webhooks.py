"""Signed webhooks with retries, and notifications that link back to ROMarr.
Behaviour learned from Questarr (GPL-3.0, nothing copied)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from romarr import notify
from romarr.notify import Message, Notifier, ON_GRAB, failed, grabbed, imported, sign_webhook


@pytest.fixture
def wire(monkeypatch):
    """Answer each outbound request from a script of statuses; run retries now."""
    calls, script = [], []

    def fake(url, *, data=None, headers=None, method="POST"):
        calls.append({"url": url, "data": data, "headers": dict(headers or {})})
        status = script.pop(0) if script else 200
        notify._last.status = status
        return 200 <= status < 300

    monkeypatch.setattr(notify, "_post", fake)
    monkeypatch.setattr(notify, "_later", lambda delay, call: call())
    return calls, script


def hook(**kw):
    return {"type": "webhook", "name": "hook", "url": "https://hook.test/in", **kw}


def test_a_secret_signs_the_raw_body_with_the_timestamp(wire):
    calls, _ = wire
    secret = "s" * 20
    Notifier([hook(secret=secret)]).send(Message(ON_GRAB, "Grabbed Chrono Trigger"))
    sent = calls[0]
    headers = sent["headers"]
    stamp = headers["X-ROMarr-Timestamp"]
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + sent["data"],
                        hashlib.sha256).hexdigest()
    assert headers["X-ROMarr-Signature"] == f"sha256={expected}"
    assert headers["X-ROMarr-Event"] == ON_GRAB
    payload = json.loads(sent["data"])
    assert payload["id"] == headers["X-ROMarr-Delivery"]
    assert payload["title"] == "Grabbed Chrono Trigger"
    assert isinstance(payload["createdAt"], int)


def test_without_a_secret_nothing_is_signed(wire):
    calls, _ = wire
    Notifier([hook()]).send(Message(ON_GRAB, "x"))
    assert "X-ROMarr-Signature" not in calls[0]["headers"]
    assert calls[0]["headers"]["X-ROMarr-Delivery"]


def test_busy_receivers_are_retried_with_the_same_delivery_id(wire):
    calls, script = wire
    script.extend([503, 429, 200])
    Notifier([hook(secret="k" * 16)]).send(Message(ON_GRAB, "x"))
    assert len(calls) == 3
    assert len({c["headers"]["X-ROMarr-Delivery"] for c in calls}) == 1


def test_a_refusal_is_final_and_retries_stop_after_three_attempts(wire):
    calls, script = wire
    script.extend([404])
    Notifier([hook()]).send(Message(ON_GRAB, "x"))
    assert len(calls) == 1
    calls.clear()
    script.extend([500, 500, 500, 500])
    Notifier([hook()]).send(Message(ON_GRAB, "x"))
    assert len(calls) == 3


def test_sign_webhook_is_stable():
    assert sign_webhook("abc", 1, b"{}") == "sha256=" + hmac.new(
        b"abc", b"1.{}", hashlib.sha256).hexdigest()


def test_messages_link_to_their_page_when_the_address_is_known(wire):
    calls, _ = wire
    notifier = Notifier([{"type": "discord", "name": "d", "url": "https://d.test/h"}],
                        base_url="https://romarr.example.com/")
    notifier.send(grabbed("Chrono Trigger", "snes", "idx", ()))
    text = json.loads(calls[0]["data"])["content"]
    assert "https://romarr.example.com/#queue" in text
    assert notifier.link(failed("X", "boom")).url == "https://romarr.example.com/#history"
    assert notifier.link(imported("X", "snes", None)).url.endswith("/#history")


def test_no_link_without_a_usable_address():
    assert Notifier([], base_url="").link(grabbed("X", "snes", "i", ())).url == ""
    assert Notifier([], base_url="romarr.local").link(grabbed("X", "snes", "i", ())).url == ""
    kept = Message(ON_GRAB, "x", url="https://elsewhere/", path="/#queue")
    assert Notifier([], base_url="https://r.test").link(kept).url == "https://elsewhere/"


def test_ntfy_gets_a_click_action(wire):
    calls, _ = wire
    Notifier([{"type": "ntfy", "name": "n", "url": "https://ntfy.sh/t"}],
             base_url="https://r.test").send(grabbed("X", "snes", "i", ()))
    assert calls[0]["headers"]["Click"] == "https://r.test/#queue"


def test_the_webhook_secret_is_a_declared_masked_field():
    from romarr.app import MASKED_ITEM_FIELDS
    assert "secret" in notify.NOTIFIERS["webhook"]["fields"]
    assert "secret" in MASKED_ITEM_FIELDS["connections"]


def test_generic_webhook_carries_request_id_platform_and_assets(wire):
    calls, _ = wire
    msg = imported("Super Mario Bros. Wonder", "switch", None,
                   external_request_id="req-42",
                   assets=["/roms/switch/Super Mario Bros. Wonder.xci"])
    Notifier([hook()]).send(msg)
    payload = json.loads(calls[0]["data"])
    assert payload["platform"] == "switch"
    assert payload["external_request_id"] == "req-42"
    assert payload["assets"] == ["/roms/switch/Super Mario Bros. Wonder.xci"]
