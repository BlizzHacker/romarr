"""The two settings point in opposite directions; keep the setup unambiguous."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_readme_links_to_the_actual_ggrequestz_receiver_documentation():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/FEATURES.md" in readme
    guide = (ROOT / "docs" / "FEATURES.md").read_text(encoding="utf-8")
    assert "REQUEST_WEBHOOK_URL=http://romarr:6868/api/v1/webhook/ggrequestz" in guide
    assert "GGREQUESTZ_URL` goes the other direction" in guide
    assert "request.auto_approve" in guide


def test_the_compose_file_does_not_claim_the_link_setting_delivers_requests():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "is NOT the request-delivery setting" in compose
    assert "REQUEST_WEBHOOK_URL" in compose


def test_unraid_and_standalone_rom_hub_are_covered_in_the_install_guide():
    guide = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "For Unraid or separate stacks" in guide
    assert "rom-hub webhook" in guide
    assert "not installed or needed in this image" in guide
