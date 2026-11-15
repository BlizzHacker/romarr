"""Channel ranking and owner-confirmed PR metadata boundaries."""
import json
import pytest
from types import SimpleNamespace
from romarr import release_controls as controls


@pytest.mark.parametrize("bad", ["", "v2", "1.2.3-preview", "1.2.3-rc", "1.2.3-beta.x", "01.2.3", "1.2.3;echo bad"])
def test_unrecognized_versions_are_not_release_targets(bad):
    assert controls.version_key(bad) is None


def test_semver_stability_and_numeric_preview_order():
    versions = ["v1.10.0-alpha.2", "v1.10.0-alpha.11", "v1.10.0-beta.1", "v1.10.0-rc.1", "v1.10.0", "v1.11.0-alpha.1"]
    assert sorted(reversed(versions), key=controls.version_key) == versions


@pytest.mark.parametrize("channel,expected", [
    ("stable", ["v1.9.0"]),
    ("rc", ["v1.10.0-rc.1", "v1.9.0"]),
    ("beta", ["v1.10.0-rc.1", "v1.10.0-beta.1", "v1.9.0"]),
    ("alpha", ["v1.10.0-rc.1", "v1.10.0-beta.1", "v1.10.0-alpha.1", "v1.9.0"])])
def test_channels_exclude_less_stable_releases(channel, expected, monkeypatch):
    rows = [{"tag_name": t, "prerelease": "-" in t} for t in ["v1.9.0", "v1.10.0-alpha.1", "v1.10.0-beta.1", "v1.10.0-rc.1"]]
    rows += [{"tag_name": "v9.0.0", "draft": True}, {"tag_name": "v8.0.0", "prerelease": True}]
    monkeypatch.setattr(controls, "_get", lambda path: rows)
    assert [r["tag"] for r in controls.releases(channel)] == expected


def metadata(monkeypatch, *, checks=None, draft=False, state="open", repo=controls.REPO):
    row = {"number": 201, "title": "Fixture PR", "state": state, "draft": draft,
           "user": {"login": "fixture"}, "head": {"sha": "a" * 40},
           "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": repo}}}
    checks = checks if checks is not None else [{"name": "test", "status": "completed", "conclusion": "success"}]
    monkeypatch.setattr(controls, "_get", lambda path: row if path.startswith("/pulls/") else
                        {"statuses": [], "state": "pending", "total_count": 0} if "/status?" in path else
                        {"check_runs": checks, "total_count": len(checks)})
    return row


def test_pr_identity_and_ci_are_explicit(monkeypatch):
    metadata(monkeypatch)
    result = controls.inspect_pr(201)
    assert result["head"] == "a" * 40 and result["base"] == "b" * 40
    assert result["checks_passed"] and result["source_ref"] == "refs/pull/201/head"


@pytest.mark.parametrize("checks", [[], [{"name": "CodeRabbit", "status": "completed", "conclusion": "success"}],
    [{"name": "test", "status": "completed", "conclusion": "failure"}],
    [{"name": "test", "status": "in_progress", "conclusion": None}]])
def test_missing_failed_pending_or_bot_only_checks_never_qualify(checks, monkeypatch):
    metadata(monkeypatch, checks=checks)
    assert not controls.inspect_pr(201)["checks_passed"]


@pytest.mark.parametrize("number", [True, 0, -1, 1.2, "201", "1;echo bad", 1000000])
def test_only_bounded_numeric_prs_are_accepted(number):
    with pytest.raises(ValueError): controls.inspect_pr(number)


def test_other_repository_cannot_supply_preview_code(monkeypatch):
    metadata(monkeypatch, repo="other/project")
    with pytest.raises(ValueError): controls.inspect_pr(201)


def test_changed_source_or_missing_consent_blocks_before_source_mutation(monkeypatch):
    metadata(monkeypatch)
    service = SimpleNamespace()
    for payload in [{}, {"confirm": True, "number": 201, "head": "c" * 40, "base": "b" * 40},
                    {"confirm": True, "number": 201, "head": "a" * 40, "base": "b" * 40, "acknowledgement": "yes"}]:
        with pytest.raises(ValueError): controls.apply_pr(service, payload)


def test_immutable_install_reports_guidance_without_launch(monkeypatch):
    metadata(monkeypatch)
    monkeypatch.setattr(controls.selfupdate, "detect", lambda: SimpleNamespace(can_apply=False, detail="Bundled app"))
    result = controls.apply_pr(SimpleNamespace(), {"confirm": True, "number": 201, "head": "a" * 40,
        "base": "b" * 40, "acknowledgement": "APPLY PR #201"})
    assert not result["ok"] and "separately built" in result["guidance"]


def test_release_catalogue_checks_later_pages_for_eligible_stable(monkeypatch):
    """A first page full of prereleases must not hide an older stable publication."""
    calls = []
    def pages(path):
        calls.append(path)
        return ([{"tag_name": "v1.11.0-alpha.%s" % i, "prerelease": True} for i in range(60)]
                if "page=1" in path else [{"tag_name": "v1.10.0", "prerelease": False}])
    monkeypatch.setattr(controls, "_get", pages)
    assert controls.releases("stable")[0]["tag"] == "v1.10.0"
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["failure", "error", "pending"])
def test_failing_or_pending_commit_status_blocks_otherwise_green_checks(monkeypatch, state):
    metadata(monkeypatch)
    original = controls._get
    monkeypatch.setattr(controls, "_get", lambda path:
        {"statuses": [{"state": state}], "state": state, "total_count": 1}
        if "/status?" in path else original(path))
    assert not controls.inspect_pr(201)["checks_passed"]


def test_busy_worker_cannot_remove_existing_preview_marker(tmp_path, monkeypatch):
    """A refused launch must never touch provenance owned by an existing updater."""
    metadata(monkeypatch)
    monkeypatch.setattr(controls.selfupdate, "detect", lambda: SimpleNamespace(can_apply=True))
    monkeypatch.setattr(controls.selfupdate, "queue_busy", lambda queue: False)
    invoked = []
    def busy(*args, prepare=None, **kwargs):
        invoked.append(prepare)
        return {"ok": False, "busy": True}
    monkeypatch.setattr(controls.update_safety, "launch", busy)
    import threading
    service = SimpleNamespace(_lock=threading.Lock(), queue=[])
    result = controls.apply_pr(service, {"confirm": True, "number": 201, "head": "a" * 40,
        "base": "b" * 40, "acknowledgement": "APPLY PR #201"})
    assert result["busy"] and invoked and callable(invoked[0])


def test_transport_errors_never_echo_authentication_values(monkeypatch):
    def denied(path):
        raise controls.requests.exceptions.InvalidHeader("invalid private-fixture-key")
    monkeypatch.setattr(controls, "_read", denied)
    with pytest.raises(ValueError) as caught:
        controls._get("/releases?per_page=60&page=1")
    assert "private-fixture-key" not in str(caught.value)


@pytest.mark.parametrize("value", [[], None, 42, {"head": 1},
    {"head": "a" * 40, "backup_id": 1}, {"number": True, "head": "a" * 40}])
def test_malformed_preview_marker_is_not_active(tmp_path, value):
    root = tmp_path / "repo"; (root / ".git").mkdir(parents=True)
    (root / ".git/romarr-preview.json").write_text(json.dumps(value))
    assert controls.active_preview(SimpleNamespace(repo=str(root))) is None


@pytest.mark.parametrize("path,value", [("/pulls/201", []), ("/pulls/201", {"number": 201, "head": []}),
    ("/check-runs?", []), ("/check-runs?", {"check_runs": [None], "total_count": 1}),
    ("/status?", []), ("/status?", {"statuses": [None], "total_count": 1})])
def test_malformed_metadata_fails_with_validation_error(monkeypatch, path, value):
    metadata(monkeypatch)
    actual = controls._get
    monkeypatch.setattr(controls, "_get", lambda requested: value if path in requested else actual(requested))
    with pytest.raises(ValueError): controls.inspect_pr(201)
