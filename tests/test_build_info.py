"""Build identity must be truthful for tagged, development, and older images."""
from pathlib import Path

from romarr import build_info

SHA = "a41b8bdeb3b52fa42dd12a16df14b84f3f037541"
ROOT = Path(__file__).resolve().parents[1]


def generated(kind="development", ref="refs/heads/main", sha=SHA,
              repository="TenchiMisaki/romarr"):
    return {
        "romarr_build_kind": kind,
        "romarr_source_ref": ref,
        "romarr_source_revision": sha,
        "romarr_source_repository": repository,
    }


def test_missing_build_info_remains_unknown(monkeypatch):
    monkeypatch.setattr(build_info, "_generated", lambda: {})
    info = build_info.get_build_info("0.0.0+unknown")
    assert info["type"] == "unknown"
    assert info["version_known"] is False
    assert not info["source_revision"]


def test_valid_release_contains_exact_tag_and_revision(monkeypatch):
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated("release", "refs/tags/v1.8.1"))
    info = build_info.get_build_info("1.8.1")
    assert info["type"] == "release"
    assert info["version_known"] is True
    assert info["source_revision"] == SHA


def test_development_provenance_does_not_invent_release_version(monkeypatch):
    monkeypatch.setattr(build_info, "_generated", lambda: generated())
    info = build_info.get_build_info("0.0.0+unknown")
    assert info["type"] == "development"
    assert info["source_ref"] == "refs/heads/main"
    assert info["version_known"] is False


def test_pull_request_merge_ref_identifies_exact_source(monkeypatch):
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated(ref="refs/pull/145/merge"))
    assert build_info.get_build_info("0.0.0+unknown")["type"] == "development"


def test_mismatched_release_tag_fails_closed(monkeypatch):
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated("release", "refs/tags/v1.8.0"))
    assert build_info.get_build_info("1.8.1")["type"] == "unknown"


def test_release_with_unknown_version_fails_closed(monkeypatch):
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated("release", "refs/tags/v1.8.1"))
    assert build_info.get_build_info("0.0.0+unknown")["type"] == "unknown"


def test_invalid_identity_never_claims_source(monkeypatch):
    cases = [
        generated(sha="abc1234"),
        generated(repository="romarr"),
        generated(ref="not-a-git-ref"),
        generated(ref="refs/tags/v1.8.1"),
        generated(kind="unknown"),
    ]
    for data in cases:
        monkeypatch.setattr(build_info, "_generated", lambda data=data: data)
        result = build_info.get_build_info("1.8.1")
        assert result["type"] == "unknown"
        assert not result["source_revision"]


def test_known_release_version_does_not_imply_release_build(monkeypatch):
    monkeypatch.setattr(build_info, "_generated", lambda: generated())
    info = build_info.get_build_info("1.8.1")
    assert info["type"] == "development" and info["version_known"]


def test_build_arguments_are_in_both_images_and_every_publish_leg():
    for dockerfile in ("Dockerfile", "Dockerfile.browser"):
        source = (ROOT / dockerfile).read_text()
        for name in ("ROMARR_BUILD_KIND", "ROMARR_SOURCE_REF",
                     "ROMARR_SOURCE_REVISION", "ROMARR_SOURCE_REPOSITORY"):
            assert "ARG " + name in source
        assert "_build_info.py" in source
    workflow = (ROOT / ".github/workflows/docker.yml").read_text()
    for name in ("ROMARR_BUILD_KIND", "ROMARR_SOURCE_REF",
                 "ROMARR_SOURCE_REVISION", "ROMARR_SOURCE_REPOSITORY"):
        assert workflow.count(name + "=") == 4


def test_update_and_status_apis_expose_same_build_metadata():
    source = (ROOT / "romarr/app.py").read_text()
    assert source.count('"build": get_build_info(VERSION),') == 2
