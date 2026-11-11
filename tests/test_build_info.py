"""Build identity must be truthful for tagged, development, and older images."""
import re
from pathlib import Path

from romarr import build_info

SHA = "a41b8bdeb3b52fa42dd12a16df14b84f3f037541"
ROOT = Path(__file__).resolve().parents[1]


def generated(kind="development", ref="refs/heads/main", sha=SHA,
              repository="TenchiMisaki/romarr"):
    """Create synthetic image provenance for isolated test cases."""
    return {
        "romarr_build_kind": kind,
        "romarr_source_ref": ref,
        "romarr_source_revision": sha,
        "romarr_source_repository": repository,
    }


def test_missing_build_info_remains_unknown(monkeypatch):
    """Missing generated metadata must not imply a source revision."""
    monkeypatch.setattr(build_info, "_generated", lambda: {})
    info = build_info.get_build_info("0.0.0+unknown")
    assert info["type"] == "unknown"
    assert info["version_known"] is False
    assert not info["source_revision"]


def test_valid_release_contains_exact_tag_and_revision(monkeypatch):
    """A matching release tag can identify the exact image source."""
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated("release", "refs/tags/v1.8.1"))
    info = build_info.get_build_info("1.8.1")
    assert info["type"] == "release"
    assert info["version_known"] is True
    assert info["source_revision"] == SHA


def test_development_provenance_does_not_invent_release_version(monkeypatch):
    """A development commit does not establish an official release version."""
    monkeypatch.setattr(build_info, "_generated", lambda: generated())
    info = build_info.get_build_info("0.0.0+unknown")
    assert info["type"] == "development"
    assert info["source_ref"] == "refs/heads/main"
    assert info["version_known"] is False


def test_pull_request_merge_ref_identifies_exact_source(monkeypatch):
    """Keep pull-request merge refs distinct from contributor branch heads."""
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated(ref="refs/pull/145/merge"))
    assert build_info.get_build_info("0.0.0+unknown")["type"] == "development"


def test_mismatched_release_tag_fails_closed(monkeypatch):
    """Reject an image release tag that conflicts with its runtime version."""
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated("release", "refs/tags/v1.8.0"))
    assert build_info.get_build_info("1.8.1")["type"] == "unknown"


def test_release_with_unknown_version_fails_closed(monkeypatch):
    """Do not classify unknown-version images as verified releases."""
    monkeypatch.setattr(build_info, "_generated",
                        lambda: generated("release", "refs/tags/v1.8.1"))
    assert build_info.get_build_info("0.0.0+unknown")["type"] == "unknown"


def test_invalid_identity_never_claims_source(monkeypatch):
    """Reject malformed build fields and clear unverified source details."""
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
    """A numeric runtime version alone cannot prove release provenance."""
    monkeypatch.setattr(build_info, "_generated", lambda: generated())
    info = build_info.get_build_info("1.8.1")
    assert info["type"] == "development" and info["version_known"]


def test_build_arguments_are_in_both_images_and_every_publish_leg():
    """Require build arguments in both images and every workflow build leg."""
    for dockerfile in ("Dockerfile", "Dockerfile.browser"):
        source = (ROOT / dockerfile).read_text()
        for name in ("ROMARR_BUILD_KIND", "ROMARR_SOURCE_REF",
                     "ROMARR_SOURCE_REVISION", "ROMARR_SOURCE_REPOSITORY"):
            assert "ARG " + name in source
        assert "_build_info.py" in source
    workflow = (ROOT / ".github/workflows/docker.yml").read_text()
    expected = {
        "ROMARR_BUILD_KIND": "${{ startsWith(github.ref, 'refs/tags/') && 'release' || 'development' }}",
        "ROMARR_SOURCE_REF": "${{ github.ref }}",
        "ROMARR_SOURCE_REVISION": "${{ github.sha }}",
        "ROMARR_SOURCE_REPOSITORY": "${{ github.repository }}",
    }
    steps = re.split(r"(?m)(?=^      - (?:name|id|uses):)", workflow)
    build_roles = set()
    for step in steps:
        if not re.search(r"(?m)^        uses: docker/build-push-action@[^\s]+$", step):
            continue
        marker = re.search(r"(?m)^          build-args: \|[+-]?[ \t]*$", step)
        assert marker is not None, "Docker build step has no build-args"
        arguments = {}
        for line in step[marker.end():].splitlines():
            if not line.strip():
                continue
            indentation = len(line) - len(line.lstrip(" "))
            if indentation <= 10:
                break
            if line.lstrip().startswith("#"):
                continue
            assert indentation == 12, "Unexpected Docker build argument indentation"
            key, separator, value = line.strip().partition("=")
            assert separator and key not in arguments, "Invalid or duplicate build argument"
            arguments[key] = value
        for name, value in expected.items():
            assert arguments.get(name) == value, f"Incorrect {name} in Docker build step"
        dockerfile = ("Dockerfile.browser" if re.search(
            r"(?m)^          file: Dockerfile\.browser[ \t]*$", step
        ) else "Dockerfile")
        role = "publish" if re.search(r"(?m)^          push: ", step) else "smoke"
        build_roles.add((dockerfile, role))
    assert build_roles >= {
        ("Dockerfile", "smoke"), ("Dockerfile", "publish"),
        ("Dockerfile.browser", "smoke"), ("Dockerfile.browser", "publish"),
    }, "Expected standard/browser smoke and publish build steps"


def test_update_and_status_apis_expose_same_build_metadata():
    """Both read-only system APIs must expose the same provenance producer."""
    source = (ROOT / "romarr/app.py").read_text()
    assert source.count('"build": get_build_info(VERSION),') == 2


def test_development_branch_ref_requires_a_name(monkeypatch):
    """Reject branch refs with an empty or malformed branch name."""
    for ref in ("refs/heads/", "refs/heads//", "refs/heads/feature/"):
        monkeypatch.setattr(build_info, "_generated",
                            lambda ref=ref: generated(ref=ref))
        assert build_info.get_build_info("0.0.0+unknown")["type"] == "unknown"


def test_pull_request_ref_requires_a_number_and_expected_suffix(monkeypatch):
    """Reject incomplete or malformed GitHub pull-request refs."""
    for ref in ("refs/pull/", "refs/pull/foo/merge", "refs/pull/145/other"):
        monkeypatch.setattr(build_info, "_generated",
                            lambda ref=ref: generated(ref=ref))
        assert build_info.get_build_info("0.0.0+unknown")["type"] == "unknown"


def test_docker_smoke_enforces_expected_build_identity():
    """Both image smoke checks must verify exact GitHub build provenance."""
    workflow = (ROOT / ".github/workflows/docker.yml").read_text()
    assert workflow.count('docker exec -e EXPECTED_BUILD_TYPE=') == 2
    checks = {
        "type": ("EXPECTED_BUILD_TYPE", None),
        "source_ref": ("EXPECTED_SOURCE_REF", "GITHUB_REF"),
        "source_revision": ("EXPECTED_SOURCE_REVISION", "GITHUB_SHA"),
        "source_repository": ("EXPECTED_SOURCE_REPOSITORY", "GITHUB_REPOSITORY"),
    }
    for field, (expected, github_env) in checks.items():
        assert workflow.count(f'b["{field}"] == os.environ["{expected}"]') == 2
        if github_env is not None:
            assert workflow.count(f'-e {expected}="${github_env}"') == 2
