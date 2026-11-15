"""CI evidence must fail closed on stale source, missing cases and leaked data."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("ci_evidence", Path(__file__).resolve().parents[1] / "scripts/ci_evidence.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
HEAD, BASE = "a" * 40, "b" * 40


@pytest.fixture
def args(tmp_path):
    """Give every test independent raw and public evidence destinations."""
    private = tmp_path / "private"
    private.mkdir()
    return SimpleNamespace(private=private, public=tmp_path / "public", root=tmp_path / "source",
                           scope="full", pr="188", ref="refs/pull/188/merge", expected=HEAD)


def junit(args, body='<testcase name="private-token-example"/>', **totals):
    """Write raw JUnit with deliberately private names and arbitrary totals."""
    counts = dict(tests=1, failures=0, errors=0, skipped=0) | totals
    attrs = " ".join(f'{key}="{value}"' for key, value in counts.items())
    path = args.private / "junit.xml"
    path.write_text(f'<testsuites><testsuite {attrs}>{body}</testsuite></testsuites>')
    return path


def prepared(args, monkeypatch):
    """Provide successful raw facts while retaining real collector and gate."""
    ci.write(args.private / "pin.json", dict(head=HEAD, base=BASE, pr="188", ref=args.ref))
    ci.write(args.private / "run.json", dict(environment="PASS", test="PASS", exit=0))
    monkeypatch.setattr(ci, "live", lambda *a: dict(head=HEAD, base=BASE))
    monkeypatch.setattr(ci, "clean", lambda *a: True)
    monkeypatch.setenv("ARTIFACT_STATUS", "success")
    junit(args)


def test_sanitized_success_contains_no_case_names_or_raw_output(args, monkeypatch):
    prepared(args, monkeypatch)
    ci.finalize(args)
    assert ci.gate(args)
    public = "".join(path.read_text() for path in args.public.iterdir())
    assert "private-token-example" not in public
    assert {p.name for p in args.public.iterdir()} == {"summary.json", "cases.json", "manifest.json"}
    assert ci.read(args.public / "summary.json")["tested_base"] == BASE


@pytest.mark.parametrize("changed", ["head", "base"])
def test_moving_head_or_base_cannot_claim_current_pass(args, monkeypatch, changed):
    prepared(args, monkeypatch)
    monkeypatch.setattr(ci, "live", lambda *a: dict(head=HEAD, base=BASE) | {changed: "c" * 40})
    ci.finalize(args)
    assert ci.read(args.public / "summary.json")["source"] == "STALE_OR_UNVERIFIED"
    assert not ci.gate(args)


def test_metadata_unavailable_is_unverified(args, monkeypatch):
    prepared(args, monkeypatch)
    def unavailable(*a):
        """Represent denied or unavailable postcheck metadata."""
        raise OSError("secret detail")
    monkeypatch.setattr(ci, "live", unavailable)
    ci.finalize(args)
    assert not ci.gate(args)
    assert "secret detail" not in (args.public / "summary.json").read_text()


@pytest.mark.parametrize("totals,body", [
    ({"tests": 2}, '<testcase/>'),
    ({"failures": 1}, '<testcase/>'),
    ({"errors": 1}, '<testcase/>'),
    ({"skipped": 1}, '<testcase/>'),
    ({"tests": -1}, '<testcase/>'),
    ({"tests": 0}, ''),
    ({}, '<testcase><failure/><skipped/></testcase>'),
])
def test_inconsistent_junit_never_passes(args, monkeypatch, totals, body):
    prepared(args, monkeypatch)
    junit(args, body, **totals)
    ci.finalize(args)
    assert ci.read(args.public / "summary.json")["collection"] == "FAIL"
    assert not ci.gate(args)


def test_zero_exit_with_failed_case_is_failure(args, monkeypatch):
    prepared(args, monkeypatch)
    junit(args, '<testcase><failure>private traceback</failure></testcase>', failures=1)
    ci.finalize(args)
    assert ci.read(args.public / "summary.json")["test"] == "FAIL"
    assert not ci.gate(args)


@pytest.mark.parametrize("path", ["summary.json", "cases.json", "manifest.json"])
def test_modified_or_missing_artifact_is_rejected(args, monkeypatch, path):
    prepared(args, monkeypatch)
    ci.finalize(args)
    if path == "manifest.json":
        ci.write(args.public / path, {})
    else:
        (args.public / path).write_text("{}")
    assert not ci.gate(args)


def test_upload_failure_cannot_pass(args, monkeypatch):
    prepared(args, monkeypatch)
    ci.finalize(args)
    monkeypatch.setenv("ARTIFACT_STATUS", "failure")
    assert not ci.gate(args)


def test_missing_runtime_is_environment_failure(args, monkeypatch):
    ci.write(args.private / "pin.json", dict(head=HEAD))
    monkeypatch.setattr(ci, "test_environment", lambda: False)
    ci.run(args)
    assert ci.read(args.private / "run.json") == dict(environment="FAIL", test="NOT_RUN", exit=None)


def test_official_base_identity_and_full_sha_are_required():
    data = dict(number=188, state="open", head=dict(sha=HEAD),
                base=dict(sha=BASE, repo=dict(full_name="fork/romarr")))
    with pytest.raises(ValueError, match="source identity"):
        ci.live("188", "", fetch=lambda _: data)
    data["base"]["repo"]["full_name"] = ci.REPOSITORY
    data["head"]["sha"] = "abc123"
    with pytest.raises(ValueError, match="commit identifier"):
        ci.live("188", "", fetch=lambda _: data)
    with pytest.raises(ValueError, match="invalid PR"):
        ci.live("../../188", "", fetch=lambda _: data)


def test_git_status_checks_real_head_and_untracked_files(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=x@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], check=True)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repo"))
    assert ci.clean(root, head)
    assert not ci.clean(root, HEAD)
    (root / "operator-file").write_text("private")
    assert not ci.clean(root, head)


def test_tokens_are_absent_from_actual_pytest_process(args, monkeypatch):
    args.root.mkdir()
    (args.root / "tests").mkdir()
    (args.root / "tests/test_private_probe.py").write_text(
        "import os\ndef test_probe():\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n"
        "    assert 'GH_TOKEN' not in os.environ\n"
        "    assert 'ACTIONS_RUNTIME_TOKEN' not in os.environ\n"
        "    assert 'PRIVATE_PASSWORD' not in os.environ\n")
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN", "PRIVATE_PASSWORD"):
        monkeypatch.setenv(name, "private-sentinel")
    ci.write(args.private / "pin.json", dict(head=HEAD))
    monkeypatch.setattr(ci, "test_environment", lambda: True)
    monkeypatch.setattr(ci, "clean", lambda *a: True)
    ci.run(args)
    assert ci.read(args.private / "run.json")["test"] == "PASS"
    counts, cases = ci.collect(args.private / "junit.xml")
    assert counts["passed"] == 1 and cases == [{"id": 1, "outcome": "passed"}]


def test_windows_desktop_workflow_sanitizes_native_proof_artifact():
    """Verify .github/workflows/windows.yml uploads only explicit proof artifacts and never browser-profile data (GHSA-8x4v-r6j4-f3j6)."""
    workflow_path = Path(__file__).resolve().parents[1] / ".github/workflows/windows.yml"
    text = workflow_path.read_text(encoding="utf-8")
    assert "romarr-desktop-proof/**" not in text, "Recursive wildcard uploads browser-data profile"
    assert "${{ runner.temp }}/romarr-desktop-proof/smoke-proof.json" in text
    assert "${{ runner.temp }}/romarr-desktop-proof/smoke-ui.png" in text
    assert "${{ runner.temp }}/romarr-desktop-proof/smoke-error.txt" in text
    assert "${{ runner.temp }}/romarr-desktop-proof/config/desktop.log" in text
    upload = text.split("- name: Upload native proof and logs", 1)[1].split("\n      - name:", 1)[0]
    paths = upload.split("path: |", 1)[1].split("if-no-files-found:", 1)[0]
    actual = {line.strip() for line in paths.splitlines() if line.strip()}
    assert actual == {
        "${{ runner.temp }}/romarr-desktop-proof/smoke-proof.json",
        "${{ runner.temp }}/romarr-desktop-proof/smoke-ui.png",
        "${{ runner.temp }}/romarr-desktop-proof/smoke-error.txt",
        "${{ runner.temp }}/romarr-desktop-proof/config/desktop.log",
        "${{ runner.temp }}/romarr-desktop-package/ROMarr-*/BUILD-INFO.json",
    }, "Native evidence uploads must match the complete approved file list"
    assert "Get-Content -LiteralPath $evidence\n" not in text, "Raw smoke-proof JSON should not be dumped to workflow logs"
    assert "Get-ChildItem -LiteralPath $proof -Filter '*error*'" not in text, "Raw error/traceback dumps should not be spewed to workflow logs"

