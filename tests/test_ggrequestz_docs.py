"""The two settings point in opposite directions; keep the setup unambiguous."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_the_readme_documents_the_actual_ggrequestz_receiver():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "REQUEST_WEBHOOK_URL=http://romarr:6868/api/v1/webhook/ggrequestz" in readme
    assert "GGREQUESTZ_URL` goes the other direction" in readme
    assert "request.auto_approve" in readme


def test_the_compose_file_does_not_claim_the_link_setting_delivers_requests():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "is NOT the request-delivery setting" in compose
    assert "REQUEST_WEBHOOK_URL" in compose


def test_unraid_and_standalone_rom_hub_are_covered_in_the_install_guide():
    guide = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "For Unraid or separate stacks" in guide
    assert "rom-hub webhook" in guide
    assert "not installed or needed in this image" in guide
