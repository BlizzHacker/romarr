"""Tests for Open Collective inbound webhook, notifications, UI links, and /opc."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import pathlib
import threading
import urllib.error
import urllib.request

from romarr import notify, ui
from romarr.app import ROMarr, make_handler


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_parse_opencollective_order_and_member_payloads():
    order_payload = {
        "id": 987654,
        "type": "ORDER_PROCESSED",
        "CollectiveId": 1349706,
        "createdAt": "2026-11-16T06:30:00.000Z",
        "data": {
            "collective": {"slug": "romarr", "name": "ROMarr"},
            "fromCollective": {"slug": "backer-one", "name": "Backer One"},
            "order": {
                "id": 42,
                "totalAmount": 1000,
                "currency": "USD",
                "interval": "month",
                "formattedAmount": "$10.00",
                "formattedAmountWithInterval": "$10.00 / month",
                "description": "Monthly contribution to ROMarr",
            },
            "tier": {"name": "Backer"},
        },
    }
    parsed = ROMarr.parse_opencollective_webhook(order_payload)
    assert parsed is not None
    assert parsed["event_type"] == "ORDER_PROCESSED"
    assert parsed["activity_id"] == 987654
    assert parsed["actor"] == "Backer One"
    assert parsed["formatted_amount"] == "$10.00 / month"
    assert parsed["tier"] == "Backer"
    assert parsed["url"] == "https://opencollective.com/romarr"
    assert "Backer One" in parsed["title"]
    assert "$10.00 / month" in parsed["title"]


def test_parse_opencollective_rejects_unrelated_payloads():
    assert ROMarr.parse_opencollective_webhook("not a dict") is None
    assert ROMarr.parse_opencollective_webhook({}) is None
    assert ROMarr.parse_opencollective_webhook({"type": "game_request", "data": {"game_title": "Contra"}}) is None
    assert ROMarr.parse_opencollective_webhook({"type": "ORDER_PROCESSED", "CollectiveId": 9999999, "data": {"collective": {"slug": "other-project"}}}) is not None  # generic OC still parses when direct


def test_handle_opencollective_webhook_records_event_and_notifies(tmp_path, monkeypatch):
    service = ROMarr({
        "ROMARR_DATA": str(tmp_path / "romarr.json"),
        "ROMARR_API_KEY": "testkey",
    })
    delivered = []
    done = threading.Event()

    def fake_send(msg):
        delivered.append(msg)
        done.set()
        return [{"name": "discord", "ok": True}]

    monkeypatch.setattr(service.notifier, "send", fake_send)

    payload = {
        "id": 55501,
        "type": "COLLECTIVE_MEMBER_CREATED",
        "CollectiveId": 1349706,
        "createdAt": "2026-11-16T06:40:00.000Z",
        "data": {
            "collective": {"slug": "romarr", "name": "ROMarr"},
            "member": {
                "role": "BACKER",
                "memberCollective": {"slug": "retro-fan", "name": "Retro Fan"},
                "tier": {"name": "Backer"},
            },
            "order": {
                "totalAmount": 500,
                "currency": "USD",
                "formattedAmount": "$5.00",
            },
        },
    }
    res = service.handle_opencollective_webhook(payload)
    assert res["ok"] is True
    assert res["accepted"] is True
    assert res["event_type"] == "COLLECTIVE_MEMBER_CREATED"
    assert res["actor"] == "Retro Fan"

    # Duplicate activity ID is acknowledged idempotently without re-recording
    dup = service.handle_opencollective_webhook(payload)
    assert dup["ok"] is True
    assert dup.get("duplicate") is True

    oc_events = [e for e in service.store.events if e.kind == "opencollective"]
    assert len(oc_events) == 1
    assert "Retro Fan" in oc_events[0].game

    assert done.wait(2.0)
    assert len(delivered) == 1
    assert delivered[0].event == notify.ON_OPENCOLLECTIVE
    assert "Retro Fan" in delivered[0].title
    assert delivered[0].url == "https://opencollective.com/romarr"


def test_ui_includes_discord_opencollective_and_webhook_bridge_links():
    html = ui.page()
    assert "https://discord.gg/ywNFceuAd" in html
    assert "https://opencollective.com/romarr" in html
    assert "https://romarr.app/opc" in html
    assert "/api/v1/webhook/opencollective" in html
    assert 'id="discord-link"' in html
    assert 'id="opencollective-link"' in html


def test_github_funding_and_website_opc_files_exist():
    funding = ROOT / ".github" / "FUNDING.yml"
    assert funding.is_file()
    funding_text = funding.read_text(encoding="utf-8")
    assert "open_collective: romarr" in funding_text
    assert "https://discord.gg/ywNFceuAd" in funding_text

    opc = ROOT / "website" / "opc" / "index.php"
    assert opc.is_file()
    opc_text = opc.read_text(encoding="utf-8")
    assert "1349706" in opc_text
    assert "https://discord.gg/ywNFceuAd" in opc_text
    assert "https://opencollective.com/romarr" in opc_text

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://discord.gg/ywNFceuAd" in readme
    assert "https://opencollective.com/romarr" in readme
    assert "https://romarr.app/opc" in readme


def test_opencollective_http_endpoint_requires_auth_and_accepts_apikey(tmp_path):
    service = ROMarr({
        "ROMARR_DATA": str(tmp_path / "http.json"),
        "ROMARR_API_KEY": "secret-oc-key",
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(url: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    try:
        unauth_code, _ = post(f"{base}/api/v1/webhook/opencollective", {
            "type": "ORDER_PROCESSED",
            "CollectiveId": 1349706,
        })
        assert unauth_code == 401

        bad_code, bad_body = post(
            f"{base}/api/v1/webhook/opencollective?apikey=secret-oc-key",
            {"type": "game_request", "data": {"game_title": "Super Mario World"}},
        )
        assert bad_code == 422
        assert bad_body["ok"] is False

        ok_code, ok_body = post(
            f"{base}/api/v1/webhook/opencollective?apikey=secret-oc-key",
            {
                "id": 771122,
                "type": "ORDER_PROCESSED",
                "CollectiveId": 1349706,
                "data": {
                    "collective": {"slug": "romarr", "name": "ROMarr"},
                    "fromCollective": {"name": "Alex"},
                    "order": {"formattedAmountWithInterval": "$5.00 / month"},
                },
            },
        )
        assert ok_code == 202
        assert ok_body["ok"] is True
        assert ok_body["actor"] == "Alex"
    finally:
        httpd.shutdown()
        httpd.server_close()
