"""The install command in the README has to be one that works.

Issue #2: the documented Proxmox one-liner returned 404. The script itself
resolved fine -- what failed was two hops down. It sourced community-scripts'
`build.func`, which then fetched `install/<app>.sh` from *its own* repository,
and ROMarr does not live there. Their framework is built for scripts inside
their repo, so nothing we could put in ours would satisfy it, and their rename
from ProxmoxVE to ProxmoxVED had already moved the goalposts once.

The installer is now self-contained. These tests pin the properties that made
the old one fail silently: a URL nobody verifies, a helper from a repo we do
not control, and a README command that drifts from the file it names.

Network is only touched in the tests marked `network`; the rest are static and
run offline.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CT = ROOT / "proxmox" / "ct" / "romarr.sh"
UPDATE = ROOT / "proxmox" / "ct" / "update.sh"
README = ROOT / "README.md"


def test_the_installer_exists_where_the_readme_says_it_does():
    """The whole of issue #2 in one assertion, at the level we control."""
    command = re.search(
        r"raw\.githubusercontent\.com/([\w.-]+/[\w.-]+)/([\w.-]+)/(\S+?\.sh)",
        README.read_text(encoding="utf-8"))
    assert command, "README no longer documents a raw installer URL"
    repo, branch, path = command.groups()
    assert repo == "BlizzHacker/romarr", f"README points at {repo}"
    assert branch == "main"
    assert (ROOT / path).is_file(), (
        f"README's install command fetches {path!r}, which is not in the repo. "
        "That is exactly the 404 in issue #2.")


def test_the_installer_does_not_depend_on_someone_elses_repository():
    """A third-party framework fetch is what broke, and it broke silently.

    community-scripts' build.func resolves `install/<app>.sh` against its own
    repo, so no file we ship can satisfy it. Sourcing it again would recreate
    issue #2 with no test failing.
    """
    body = CT.read_text(encoding="utf-8")
    for forbidden in ("build.func", "ProxmoxVED", "ProxmoxVE/",
                      "community-scripts"):
        offenders = [line for line in body.splitlines()
                     if forbidden in line and not line.lstrip().startswith("#")]
        assert not offenders, (
            f"installer depends on {forbidden!r} again:\n  "
            + "\n  ".join(offenders))


def test_every_url_the_installer_uses_is_one_we_control():
    for script in (CT, UPDATE):
        for url in re.findall(r"https://[^\s\"')]+",
                              script.read_text(encoding="utf-8")):
            assert any(host in url for host in (
                "github.com/BlizzHacker/romarr",
                "api.github.com/repos/${REPO}",
                "github.com/${REPO}",
                "raw.githubusercontent.com/${REPO}",
                "raw.githubusercontent.com/BlizzHacker/romarr",
            )), f"{script.name} reaches {url}, which we do not control"


@pytest.mark.parametrize("script", [CT, UPDATE], ids=["install", "update"])
def test_the_scripts_are_valid_bash(script):
    """A syntax error only shows up on somebody else's server otherwise."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    done = subprocess.run([bash, "-n", str(script)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("script", [CT, UPDATE], ids=["install", "update"])
def test_the_scripts_fail_loudly_rather_than_half_way(script):
    body = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in body, (
        "without this a failed step is skipped and the next one reports "
        "success on a broken install")
    assert "die()" in body


def test_the_installer_verifies_the_service_actually_answers():
    """"Installed" has to mean "answering", or the operator finds out later."""
    body = CT.read_text(encoding="utf-8")
    assert "/api/health" in body
    assert "journalctl -u romarr" in body, (
        "a failed start must say where to look")


def test_the_update_path_preserves_state():
    """romarr.json holds the API key and the password hash; .env holds every
    credential. An update that loses them is a reinstall."""
    body = UPDATE.read_text(encoding="utf-8")
    for keep in ("romarr.json", ".env", "backends"):
        assert keep in body, f"update.sh does not preserve {keep}"


def test_the_installer_refuses_to_deploy_a_build_without_authentication():
    """The published release can lag main badly.

    v0.7.0 was tagged before auth.py existed, so installing 'latest' produced
    an open API on a service that queues downloads and writes to disk. The
    installer has to notice that rather than reporting success.
    """
    body = CT.read_text(encoding="utf-8")
    assert "romarr/auth.py" in body, (
        "installer does not check that what it deployed can authenticate")
    assert body.count("romarr/auth.py") >= 2, (
        "it must both fall back and then verify the fallback worked")


def test_the_installer_does_not_preset_a_credential():
    """The first visit claims the install. Baking a password into a public
    script would make every install share it."""
    body = CT.read_text(encoding="utf-8")
    assert "ROMARR_PASSWORD=" not in body
    assert re.search(r"ROMARR_API_KEY=\S", body) is None


@pytest.mark.network
def test_the_documented_command_resolves():
    """The literal URL from the README, fetched.

    Marked `network` so the offline suite stays offline: run with
    `-m network` to include it.
    """
    import urllib.request

    text = README.read_text(encoding="utf-8")
    url = re.search(r"https://raw\.githubusercontent\.com/\S+?\.sh", text)
    assert url, "README no longer documents an installer URL"
    with urllib.request.urlopen(url.group(0), timeout=20) as response:
        assert response.status == 200
        assert b"ROMarr" in response.read()


def test_the_update_refuses_a_build_without_authentication_too():
    """The installer has checked this since v0.7.0. The updater did not.

    An update that silently replaces an authenticated build with an open one is
    worse than an install that does, because the operator has already decided
    this thing is safe to leave running on their network.
    """
    body = UPDATE.read_text(encoding="utf-8")
    assert "romarr/auth.py" in body, (
        "update.sh installs whatever it downloaded without checking that it "
        "can authenticate")


def test_the_update_replaces_the_package_rather_than_merging_over_it():
    """`cp -a` over the top leaves modules upstream deleted still importable.

    Proven on a throwaway container: a file dropped into /opt/romarr/romarr
    before an update was still there afterwards. That is how a version reports
    itself as new while some of it is old.
    """
    body = UPDATE.read_text(encoding="utf-8")
    assert 'rm -rf "${ROOT}/romarr"' in body, (
        "the package directory is merged into, not replaced")


def test_the_update_says_what_it_changed():
    """"Updated" with no version either side is a report nobody can check."""
    body = UPDATE.read_text(encoding="utf-8")
    assert "version_at" in body, "update.sh reads no version at all"
    assert "$BEFORE" in body and "$AFTER" in body, (
        "update.sh does not report the version it moved you from and to")


def test_the_update_can_go_backwards():
    """An update that can only go forwards is a gamble, not an update."""
    body = UPDATE.read_text(encoding="utf-8")
    assert "rolling back" in body.lower(), "update.sh has no rollback path"
    assert 'cp -a "${BACKUP}/romarr"' in body, (
        "nothing puts the previous build back, so the backup is a directory "
        "the operator has to reassemble by hand")


def test_both_proxmox_scripts_agree_on_the_variable_names():
    """These drifted once and it cost an operator an afternoon.

    install/romarr-install.sh wrote the legacy ROMM_* aliases while ct/romarr.sh
    wrote LIBRARY_*, so somebody following the README's documentation edited
    variables their install was not reading.
    """
    community = (ROOT / "proxmox" / "install" / "romarr-install.sh").read_text(
        encoding="utf-8")
    for name in ("LIBRARY_KIND", "LIBRARY_URL", "LIBRARY_PATH"):
        assert name in community, f"{name} missing from the community-scripts installer"
    for legacy in ("ROMM_URL=", "ROMM_LIBRARY=", "ROMM_USERNAME="):
        assert legacy not in community, (
            f"{legacy} is the alias, not the documented name")
