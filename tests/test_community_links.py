"""Verify official Discord and Open Collective links without exposing Open Collective webhook endpoints."""

from __future__ import annotations

import pathlib

from romarr import notify, ui
from romarr.openapi import DESCRIPTIONS


ROOT = pathlib.Path(__file__).resolve().parents[1]
DISCORD_URL = "https://discord.gg/ywNFceuAd"
OPENCOLLECTIVE_URL = "https://opencollective.com/romarr"


def test_in_app_ui_links_official_discord_and_opencollective_without_webhook_relay():
    html = ui.page()
    assert DISCORD_URL in html
    assert OPENCOLLECTIVE_URL in html
    assert 'id="discord-link"' in html
    assert 'id="opencollective-link"' in html
    assert "romarr.app/opc" not in html
    assert "/api/v1/webhook/opencollective" not in html


def test_no_public_opencollective_webhook_endpoint_in_core_or_docs():
    assert "opencollective" not in notify.EVENTS
    assert "/api/v1/webhook/opencollective" not in DESCRIPTIONS
    assert not (ROOT / "website" / "opc").exists()

    for rel in (
        ".github/FUNDING.yml",
        "README.md",
        "docs/OFFICIAL-DISTRIBUTION.md",
        "docs/FEATURES.md",
        "website/index.html",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "/opc" not in text, f"/opc leaked into {rel}"
        assert "/api/v1/webhook/opencollective" not in text

    funding = (ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8")
    assert "open_collective: romarr" in funding
    assert DISCORD_URL in funding

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert DISCORD_URL in readme
    assert OPENCOLLECTIVE_URL in readme
