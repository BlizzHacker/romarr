"""ROMarr installs a set of proven source plugins out of the box."""
from romarr import preinstalled_plugins as pp


def test_every_default_plugin_is_preinstalled_once_and_failures_do_not_stop_the_rest(monkeypatch, tmp_path):
    calls = []

    def fake(slug, repo, ref, *, home=None, env=None):
        calls.append((slug, repo, ref))
        return "failed: offline" if slug == "homebrew" else "installed"

    monkeypatch.setattr(pp, "preinstall", fake)
    out = pp.preinstall_defaults(home=tmp_path)
    assert [c[0] for c in calls] == [s for s, _, _ in pp.DEFAULT_PLUGINS]
    assert out["homebrew"].startswith("failed") and out["archive-org"] == "installed"


def test_the_defaults_include_archive_org_pinned_to_a_release():
    by_slug = {s: (r, ref) for s, r, ref in pp.DEFAULT_PLUGINS}
    assert "archive-org" in by_slug
    for slug, (repo, ref) in by_slug.items():
        assert repo == f"https://github.com/BlizzHacker/rom-hub-{slug}"
        assert ref.startswith("v")


def test_the_dockerfile_bundles_the_same_defaults_at_the_same_refs():
    from pathlib import Path
    docker = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    for slug, _, ref in pp.DEFAULT_PLUGINS:
        assert f"{slug}:{ref}" in docker
