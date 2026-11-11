"""Build identity must be truthful for tagged, development, and older images."""
import re
from pathlib import Path

import pytest

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


def _check_docker_build_steps(workflow):
    """Check exact provenance in each Docker build, including future steps."""
    expected = {
        "ROMARR_BUILD_KIND": "${{ startsWith(github.ref, 'refs/tags/') && 'release' || 'development' }}",
        "ROMARR_SOURCE_REF": "${{ github.ref }}",
        "ROMARR_SOURCE_REVISION": "${{ github.sha }}",
        "ROMARR_SOURCE_REPOSITORY": "${{ github.repository }}",
    }
    build_roles = set()
    # Every list-item key can start a workflow step (including 'if' or 'uses').
    for step in re.split(r"(?m)(?=^      - [A-Za-z_][\w-]*:)", workflow):
        if not re.search(
            r"""(?m)^      (?:- uses:|  uses:) (?P<quote>['"]?)docker/build-push-action@\S+?(?P=quote)[ \t]*$""",
            step,
        ):
            continue
        marker = re.search(r"(?m)^          build-args: \|[+-]?[ \t]*$", step)
        assert marker is not None, "Docker build step has no build-args"
        arguments = {}
        argument_indent = None
        for line in step[marker.end():].splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent <= 10:
                break
            if line.lstrip().startswith("#"):
                continue
            if argument_indent is None:
                argument_indent = indent
            assert indent == argument_indent, "Unexpected Docker build argument indentation"
            key, sep, value = line.strip().partition("=")
            assert sep and key not in arguments, "Invalid or duplicate build argument"
            arguments[key] = value
        for name, value in expected.items():
            assert arguments.get(name) == value, f"Incorrect {name} in Docker build step"

        file_match = re.search(r"(?m)^          file: (.+?)[ \t]*$", step)
        selected_file = file_match.group(1) if file_match else "Dockerfile"
        selected_file = selected_file.removeprefix("./")
        if selected_file not in {"Dockerfile", "Dockerfile.browser"}:
            continue  # Additional Dockerfiles cannot satisfy the required roles.

        push_match = re.search(r"(?m)^          push: (.+?)[ \t]*$", step)
        push = push_match.group(1) if push_match else ""
        load_match = re.search(r"(?m)^          load: (.+?)[ \t]*$", step)
        load = load_match.group(1) if load_match else ""
        if push in {"true", "${{ github.event_name != 'pull_request' }}"}:
            build_roles.add((selected_file, "publish"))
        if load == "true":
            build_roles.add((selected_file, "smoke"))
    assert build_roles >= {
        ("Dockerfile", "smoke"), ("Dockerfile", "publish"),
        ("Dockerfile.browser", "smoke"), ("Dockerfile.browser", "publish"),
    }, "Expected standard/browser smoke and publish build steps"


def test_build_arguments_are_in_both_images_and_every_publish_leg():
    """Require exact provenance in all workflow builds and each image."""
    for dockerfile in ("Dockerfile", "Dockerfile.browser"):
        source = (ROOT / dockerfile).read_text()
        for name in ("ROMARR_BUILD_KIND", "ROMARR_SOURCE_REF",
                     "ROMARR_SOURCE_REVISION", "ROMARR_SOURCE_REPOSITORY"):
            assert "ARG " + name in source
        assert "_build_info.py" in source
    _check_docker_build_steps((ROOT / ".github/workflows/docker.yml").read_text())


def test_docker_build_step_validator_rejects_misconfigured_steps():
    """Catch invalid arguments, omitted roles, and differently ordered YAML steps."""
    workflow = (ROOT / ".github/workflows/docker.yml").read_text()
    extra = """      - if: ${{ always() }}
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile.browser
          push: true
          build-args: |
            ROMARR_BUILD_KIND=${{ startsWith(github.ref, 'refs/tags/') && 'release' || 'development' }}
            ROMARR_SOURCE_REF=${{ github.ref }}
            ROMARR_SOURCE_REVISION=${{ github.sha }}
            ROMARR_SOURCE_REPOSITORY=${{ github.repository }}
"""
    anchor = "\n  image-browser:\n"
    assert workflow.count(anchor) == 1

    def with_extra(addition):
        return workflow.replace(anchor, "\n" + addition + anchor, 1)

    _check_docker_build_steps(with_extra(extra))  # Extra valid build step is permitted.
    first_uses = extra.replace(
        "      - if: ${{ always() }}\n        uses: docker/build-push-action@v6",
        "      - uses: docker/build-push-action@v6\n        if: ${{ always() }}",
    )
    assert first_uses != extra

    # Valid extra build steps may quote the action or indent block arguments deeper.
    for step in (extra, first_uses):
        for quote in ("'", '"'):
            quoted = step.replace(
                "uses: docker/build-push-action@v6",
                f"uses: {quote}docker/build-push-action@v6{quote}",
            )
            _check_docker_build_steps(with_extra(quoted))
            with pytest.raises(AssertionError):
                _check_docker_build_steps(with_extra(quoted.replace(
                    "ROMARR_SOURCE_REVISION=${{ github.sha }}",
                    "ROMARR_SOURCE_REVISION=${{ github.repository }}",
                )))
    deeper = extra.replace("\n            ROMARR_", "\n              ROMARR_")
    _check_docker_build_steps(with_extra(deeper))

    def fixture_step(dockerfile, publish):
        """Create an independent standard or browser build step."""
        return (
            extra.replace("      - if: ${{ always() }}", "      - name: Fixture", 1)
            .replace("          file: Dockerfile.browser",
                     f"          file: {dockerfile}", 1)
            .replace("          push: true",
                     "          push: true" if publish else "          load: true", 1)
        )

    fixture = "".join(
        fixture_step(dockerfile, publish)
        for dockerfile in ("Dockerfile", "Dockerfile.browser")
        for publish in (False, True)
    )
    _check_docker_build_steps(fixture)
    # Whitespace-neutral YAML comments must not make this test fail.
    commented = fixture.replace(
        "          context: .\n",
        "          context: .\n          # Valid workflow comment\n",
        1,
    )
    assert commented != fixture
    _check_docker_build_steps(commented)

    standard_publish = fixture_step("Dockerfile", True)
    assert fixture.count(standard_publish) == 1
    bad_dockerfile = fixture.replace(
        standard_publish,
        standard_publish.replace(
            "          file: Dockerfile\n", "          file: Dockerfile.extra\n", 1
        ), 1,
    )
    assert bad_dockerfile != fixture

    bad_cases = {
        "wrong revision": workflow.replace(
            "ROMARR_SOURCE_REVISION=${{ github.sha }}",
            "ROMARR_SOURCE_REVISION=${{ github.repository }}", 1
        ),
        "missing argument": workflow.replace(
            "            ROMARR_SOURCE_REVISION=${{ github.sha }}\n", "", 1
        ),
        "duplicate argument": workflow.replace(
            "            ROMARR_SOURCE_REVISION=${{ github.sha }}",
            "            ROMARR_SOURCE_REVISION=${{ github.sha }}\n"
            "            ROMARR_SOURCE_REVISION=${{ github.sha }}", 1,
        ),
        "build step beginning with if": with_extra(extra.replace(
            "ROMARR_SOURCE_REVISION=${{ github.sha }}",
            "ROMARR_SOURCE_REVISION=${{ github.repository }}",
        )),
        "build step beginning with uses": with_extra(first_uses.replace(
            "ROMARR_SOURCE_REVISION=${{ github.sha }}",
            "ROMARR_SOURCE_REVISION=${{ github.repository }}",
        )),
        "non-standard Dockerfile": bad_dockerfile,
        "push disabled": workflow.replace(
            "push: ${{ github.event_name != 'pull_request' }}", "push: false", 1
        ),
    }
    for label, changed in bad_cases.items():
        reference = fixture if label == "non-standard Dockerfile" else workflow
        assert changed != reference, f"Mutation not applied: {label}"
        with pytest.raises(AssertionError):
            _check_docker_build_steps(changed)


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
