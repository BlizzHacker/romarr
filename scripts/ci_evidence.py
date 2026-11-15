#!/usr/bin/env python3
"""Record exact-source pytest evidence without publishing test inputs or logs.

Independent implementation inspired by TenchiMisaki/romarr-ci-controller.
Raw output stays in the private directory; only allowlisted aggregate facts
and anonymous case outcomes enter the public artifact. This is an evidence
quality guard, not a sandbox against malicious code on the same runner.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

REPOSITORY = "BlizzHacker/romarr"
STATES = ("passed", "failed", "error", "skipped")
FILES = {"summary.json", "cases.json"}
STRUCTURAL = [f"tests/test_{name}.py" for name in (
    "wired", "ui_pages", "openapi", "security_claims", "brand", "docker_install", "repo_hygiene")]


def sha(value):
    """Accept complete immutable SHA-1 commit IDs only."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("invalid commit identifier")
    return value


def write(path, value):
    """Atomically replace one JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".partial")
    pending.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def read(path):
    """Read a bounded evidence document."""
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("oversized evidence")
    return json.loads(path.read_text(encoding="utf-8"))


def api(path):
    """Read official repository metadata using a token only in metadata steps."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ROMarr-CI-evidence"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request("https://api.github.com/repos/" + REPOSITORY + "/" + path, headers=headers)
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def live(pr, ref, fetch=api):
    """Resolve official base identity and immutable PR head/base, or one ref."""
    if pr:
        if not re.fullmatch(r"[1-9][0-9]{0,6}", pr):
            raise ValueError("invalid PR")
        data = fetch("pulls/" + pr)
        if (data["base"]["repo"]["full_name"] != REPOSITORY
                or data["number"] != int(pr) or data["state"] != "open"):
            raise ValueError("unexpected source identity")
        return {"head": sha(data["head"]["sha"]), "base": sha(data["base"]["sha"])}
    if not re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9._/-]+", ref) or ".." in ref:
        raise ValueError("invalid source ref")
    commit = sha(fetch("commits/" + ref)["sha"])
    return {"head": commit, "base": commit}


def clean(root, expected):
    """Check Git identity and tracked/untracked cleanliness; failures are dirty."""
    try:
        values = []
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        for args in (("rev-parse", "HEAD"), ("status", "--porcelain", "--untracked-files=all")):
            values.append(subprocess.check_output(["git", "-C", str(root), *args],
                env=env, stderr=subprocess.DEVNULL, text=True, timeout=15).strip())
        return values == [expected, ""]
    except (OSError, subprocess.SubprocessError):
        return False


def test_environment():
    """Keep missing test prerequisites distinct from test failures."""
    return (sys.version_info >= (3, 12) and shutil.which("node") is not None
            and all(importlib.util.find_spec(name) is not None
                    for name in ("pytest", "requests", "rom_hub")))


def test_env():
    """Remove CI credentials and inherited Git overrides before executing tests."""
    denied = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {k: v for k, v in os.environ.items()
            if not any(part in k.upper() for part in denied)
            and not k.startswith(("GIT_", "ACTIONS_"))}


def collect(path):
    """Reconcile declared JUnit totals with every serialized anonymous case."""
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("oversized JUnit")
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    if root.tag not in ("testsuite", "testsuites") or not suites:
        raise ValueError("invalid JUnit structure")
    counts = dict.fromkeys(STATES, 0)
    cases = []
    for suite in suites:
        if suite.tag != "testsuite" or suite.find("testsuite") is not None:
            raise ValueError("invalid nested suite")
        actual = dict.fromkeys(STATES, 0)
        for case in suite.findall("testcase"):
            outcomes = [state for tag, state in (("failure", "failed"), ("error", "error"),
                        ("skipped", "skipped")) if case.find(tag) is not None]
            if len(outcomes) > 1:
                raise ValueError("conflicting outcomes")
            state = outcomes[0] if outcomes else "passed"
            actual[state] += 1
            cases.append({"id": len(cases) + 1, "outcome": state})
        expected = {key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")}
        if (min(expected.values()) < 0 or expected["tests"] != sum(actual.values())
                or expected["failures"] != actual["failed"] or expected["errors"] != actual["error"]
                or expected["skipped"] != actual["skipped"]):
            raise ValueError("JUnit count mismatch")
        for state in STATES:
            counts[state] += actual[state]
    if not cases:
        raise ValueError("no test cases")
    return counts, cases


def pin(args):
    """Save the pre-test official source snapshot after checking exact checkout."""
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("unexpected workflow repository")
    target = live(args.pr, args.ref)
    if target["head"] != sha(args.expected) or not clean(args.root, target["head"]):
        raise ValueError("source changed before tests")
    write(args.private / "pin.json", {**target, "pr": args.pr, "ref": args.ref})


def run(args):
    """Run pytest with bounded time and private output, without metadata tokens."""
    target = read(args.private / "pin.json")
    result = {"environment": "FAIL", "test": "NOT_RUN", "exit": None}
    if test_environment() and clean(args.root, sha(target["head"])):
        result["environment"] = "PASS"
        command = [sys.executable, "-m", "pytest", *(["tests/"] if args.scope == "full" else STRUCTURAL),
                   "-q", "--junitxml=" + str(args.private / "junit.xml")]
        with (args.private / "pytest.log").open("wb") as output:
            worker = subprocess.Popen(command, cwd=args.root, env=test_env(), stdout=output,
                                      stderr=subprocess.STDOUT, start_new_session=True)
            try:
                result["exit"] = worker.wait(timeout=1200)
                result["test"] = "PASS" if result["exit"] == 0 else "FAIL"
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait()
                result["test"] = "TIMEOUT"
    write(args.private / "run.json", result)


def finalize(args):
    """Publish sanitized accounting and independently verify the live source."""
    target = read(args.private / "pin.json")
    args.public.mkdir(parents=True, exist_ok=False)
    summary = {"schema": 1, "repository": REPOSITORY, "tested_head": sha(target["head"]),
               "tested_base": sha(target["base"]), "scope": args.scope,
               "environment": "FAIL", "test": "NOT_RUN", "collection": "FAIL",
               "source": "STALE_OR_UNVERIFIED", "exit": None, "counts": None}
    try:
        result = read(args.private / "run.json")
        if (result["environment"] not in ("PASS", "FAIL")
                or result["test"] not in ("PASS", "FAIL", "NOT_RUN", "TIMEOUT")
                or (result["exit"] is not None and type(result["exit"]) is not int)):
            raise ValueError("invalid run status")
        summary.update({key: result[key] for key in ("environment", "test", "exit")})
        counts, cases = collect(args.private / "junit.xml")
        summary.update(counts=counts, collection="PASS")
        if summary["exit"] == 0 and (counts["failed"] or counts["error"]):
            summary["test"] = "FAIL"
        write(args.public / "cases.json", cases)
    except (OSError, ValueError, KeyError, ET.ParseError):
        pass
    try:
        observed = live(target["pr"], target["ref"])
        if observed == {key: target[key] for key in ("head", "base")} and clean(args.root, target["head"]):
            summary["source"] = "UNCHANGED"
    except (OSError, ValueError, KeyError):
        pass
    write(args.public / "summary.json", summary)
    members = sorted(p.name for p in args.public.iterdir() if p.name in FILES)
    write(args.public / "manifest.json", {name: hashlib.sha256((args.public / name).read_bytes()).hexdigest()
                                        for name in members})


def gate(args):
    """Reject missing uploads, modified evidence, stale source, or partial gates."""
    manifest = read(args.public / "manifest.json")
    if set(manifest) != FILES or {p.name for p in args.public.iterdir()} != FILES | {"manifest.json"}:
        return False
    if any(hashlib.sha256((args.public / name).read_bytes()).hexdigest() != digest
           for name, digest in manifest.items()):
        return False
    summary = read(args.public / "summary.json")
    cases = read(args.public / "cases.json")
    counts = dict.fromkeys(STATES, 0)
    for index, case in enumerate(cases, 1):
        if set(case) != {"id", "outcome"} or case["id"] != index or case["outcome"] not in STATES:
            return False
        counts[case["outcome"]] += 1
    return (bool(cases) and counts == summary["counts"] and not counts["failed"] and not counts["error"]
            and all(summary[key] == "PASS" for key in ("test", "environment", "collection"))
            and summary["source"] == "UNCHANGED" and summary["exit"] == 0
            and os.environ.get("ARTIFACT_STATUS") == "success")


def main():
    """Dispatch one isolated CI evidence step; never expose exception contents."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pin", "run", "finalize", "gate"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--scope", choices=("full", "structural"), default="full")
    parser.add_argument("--pr", default=os.environ.get("CI_EVIDENCE_PR", ""))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    parser.add_argument("--expected", default=os.environ.get("CI_EVIDENCE_HEAD", ""))
    args = parser.parse_args()
    args.private = args.private.resolve()
    args.public = args.public.resolve()
    if args.private == args.public or args.private.is_relative_to(args.public):
        raise ValueError("raw output must remain outside public artifacts")
    if args.mode == "gate":
        raise SystemExit(0 if gate(args) else 1)
    globals()[args.mode](args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ET.ParseError):
        print("CI evidence could not be verified", file=sys.stderr)
        raise SystemExit(2)
