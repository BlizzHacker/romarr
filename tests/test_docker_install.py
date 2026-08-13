"""The Docker install surface, pinned at the level a reader trusts.

None of this can start a container in CI, so each test asserts the property
whose absence produced a real failure, and names the failure. All four were
observed on a live Docker host before they were fixed:

  * `${VAR:-?ERROR}` was documented as fail-fast validation and is not. `:-`
    supplies a default, so the second compose file quietly set the Prowlarr
    API key to the literal string "?ERROR" and connected with it.
  * Placeholder bind-mount paths are not inert. Docker creates a missing
    source, so `/path/to/roms` produced a container that started, reported
    healthy, and filed ROMs into a directory nothing scanned.
  * A `docker run` with no restart policy does not survive a host reboot, and
    the README's command was the one people copy.
  * The entrypoint's writability check ran as root, which passes `test -w` on
    nearly everything, so it could not fail for the reason it printed.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"
README = ROOT / "README.md"
INSTALL = ROOT / "docs" / "INSTALL.md"


def _directives(path: pathlib.Path) -> str:
    """The compose file with its comments removed.

    The comments in that file deliberately quote both mistakes below in order
    to explain them, so a naive substring search finds the explanation and
    fails. What is being pinned is what compose executes.
    """
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


def test_there_is_exactly_one_compose_file():
    """Two files opening with the same sentence gave nobody a way to choose.

    docker-compose-alt.yml existed for env-var validation that did not work.
    The idea is kept in the one file; a second one coming back means the
    choice is unexplained again.
    """
    found = sorted(p.name for p in ROOT.glob("docker-compose*.y*ml"))
    assert found == ["docker-compose.yml"], (
        f"more than one compose file ships: {found}")


def test_the_compose_file_does_not_use_the_fail_fast_form_that_is_not_one():
    """`:-?` reads like `:?` and behaves like the opposite.

    Verified against docker compose on a real host: `${MISSING:-?ERROR}`
    resolves to the literal string "?ERROR" and the container starts with it.
    """
    offenders = re.findall(r"\$\{[A-Z_]+:-\?[^}]*\}", _directives(COMPOSE))
    assert not offenders, (
        f"{offenders} looks like validation and is a default. Use ${{VAR:?msg}}.")


def test_the_two_silently_wrong_paths_have_no_default():
    """Everything else that can be wrong is reported. These two are not.

    Docker creates a missing bind-mount source, so a placeholder produces a
    green install writing ROMs where nothing looks. Compose has to stop.
    """
    body = _directives(COMPOSE)
    for var in ("ROMARR_ROMS", "ROMARR_DOWNLOADS"):
        assert re.search(rf"\$\{{{var}:\?[^}}]+\}}", body), (
            f"{var} is not a required interpolation, so an unedited file "
            "starts and files ROMs into a directory nobody scans")
    assert "/path/to/roms" not in body, (
        "a placeholder path is not inert -- Docker creates it")


def test_the_compose_file_comes_back_after_a_reboot():
    assert re.search(r"^\s*restart:\s*unless-stopped\s*$", COMPOSE.read_text(
        encoding="utf-8"), re.M), "no restart policy"


def test_the_documented_docker_run_comes_back_after_a_reboot():
    """The README command is the one people copy, so it has to be complete."""
    for doc in (README, INSTALL):
        text = doc.read_text(encoding="utf-8")
        for block in re.findall(r"docker run[^`]+", text):
            assert "--restart" in block, (
                f"{doc.name} documents a `docker run` with no restart policy; "
                "it will not survive a host reboot")


def test_the_documented_docker_run_uses_an_absolute_config_path():
    """`-v ./config:/config` needs Docker Engine 23+.

    Older daemons reject a relative bind source as an invalid volume name, and
    the error names volumes rather than the path, so it reads as a bug in the
    image.
    """
    for doc in (README, INSTALL):
        text = doc.read_text(encoding="utf-8")
        for block in re.findall(r"docker run[^`]+", text):
            assert "-v ./" not in block, (
                f"{doc.name} documents a relative bind mount in `docker run`")


def test_the_entrypoint_tests_writability_as_the_user_it_will_run_as():
    """Root passes `test -w` on nearly everything.

    The check printed "/config is not writable by ${PUID}:${PGID}" while
    running as root, so it was incapable of failing for that reason.
    """
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'su-exec "${PUID}:${PGID}" test -w /config' in body, (
        "the writability check does not run as the target user")


def test_the_entrypoint_owns_the_whole_of_config_not_just_the_directory():
    """A root-owned romarr.json under a PUID container was unreadable.

    ROMarr answered that by starting from defaults and saving over it: the API
    key was regenerated, the history emptied, and the install came back
    unclaimed, so the next visitor to the port set the password. Measured on a
    live container before the fix.
    """
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'chown -R "${PUID}:${PGID}" /config' in body, (
        "only the directory is chowned, which leaves a root-owned state file "
        "unreadable inside a writable directory")


def test_the_entrypoint_refuses_rather_than_letting_state_be_overwritten():
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert "ROMARR_DATA" in body, (
        "ROMARR_DATA can point outside /config, where nothing above has "
        "touched it -- that case needs its own check")
    assert 'test -r "$STATE"' in body


def test_the_healthcheck_asks_the_port_romarr_was_told_to_use():
    """A hardcoded 6868 reports unhealthy forever on a moved port."""
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in body
    assert "ROMARR_PORT" in body.split("HEALTHCHECK", 1)[1]


def test_the_runtime_image_opens_alpines_versioned_seccomp_library():
    """Alpine has libseccomp.so.2 but ctypes looks for libseccomp.so.

    The package was installed and the image still raised ``Unable to find
    libseccomp``. The loader fallback is the difference between ROM Hub being
    present and its plugins actually being confined.
    """
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert r'or \"/usr/lib/libseccomp.so.2\"' in body
    assert "from rom_hub.sandbox import install; install()" in body, (
        "the image does not prove that its seccomp filter can actually load")


def test_the_install_guide_exists_and_the_readme_sends_people_to_it():
    assert INSTALL.is_file()
    assert "docs/INSTALL.md" in README.read_text(encoding="utf-8"), (
        "the full install guide is unreachable from the README")
