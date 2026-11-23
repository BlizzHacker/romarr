"""The NAS packages in packaging/ must install the release the stores do, safely.

Synology, QNAP and Asustor get native packages (an spksrc SPK, a QDK QPKG and
an App Central APK); TerraMaster, UGREEN and openmediavault get compose
templates for their Docker apps; and Container Manager, Container Station and
Portainer get compose templates too. None of them is installed by CI on the
vendor's OS -- each OS runs only on that vendor's hardware -- so the vendor's
own build tool and the smoke scripts beside each package are the runtime
proof (.github/workflows/nas-packages.yml). These tests pin what those cannot
see drift in -- ports, the silent-path guard, where state lives, and
that every platform has a guide. Versions are in test_packaging.package_versions(). Read as text; the suite has no YAML parser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
NAS_GUIDE = ROOT / "docs" / "NAS.md"
STORES = ROOT / "docs" / "STORES.md"
PORT = "6868"

COMPOSE_TEMPLATES = (
    "synology/container-manager/docker-compose.yml",
    "qnap/container-station/docker-compose.yml",
    "asustor/portainer/docker-compose.yml",
    "terramaster/docker-compose.yml",
    "ugreen/docker-compose.yml",
    "openmediavault/docker-compose.yml",
)

SPK = PKG / "synology/spksrc/spk/romarr"
SPK_CROSS = PKG / "synology/spksrc/cross/romarr"
QPKG = PKG / "qnap/qpkg/romarr"
APK = PKG / "asustor/apk"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def directives(text: str) -> str:
    """The file with comment lines removed: what the tool actually executes."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def make_var(text: str, name: str) -> str:
    return re.search(rf"^{name}\s*=\s*(.+)$", text, re.M).group(1).strip()


# --- compose templates ----------------------------------------------------


@pytest.mark.parametrize("rel", COMPOSE_TEMPLATES)
def test_compose_templates_publish_romarrs_port_and_library(rel):
    text = directives(read(PKG / rel))
    assert f'"{PORT}:{PORT}"' in text
    assert re.search(r"^\s+LIBRARY_PATH: /roms$", text, re.M)
    assert re.search(r"^\s+target: /roms$", text, re.M)
    assert re.search(r'^\s+UMASK: "002"$', text, re.M)
    assert re.search(r"^\s+PUID: \d+$", text, re.M) and re.search(r"^\s+PGID: \d+$", text, re.M)
    assert not re.search(r"^\s+PUID: 0$", text, re.M), "games would land owned by root"


@pytest.mark.parametrize("rel", COMPOSE_TEMPLATES)
def test_library_and_downloads_cannot_be_created_silently(rel):
    """Docker creates a missing bind source, which files games nowhere.

    The main docker-compose.yml stops on an unset path; these templates are
    pasted into vendor UIs with fixed paths, so the bind itself refuses a
    missing folder (`create_host_path: false`), or the path is required.
    """
    text = directives(read(PKG / rel))
    assert not re.search(r"\$\{[A-Z_]+:-\?", text), "`:-?` supplies a default, it does not stop"
    binds = re.findall(r"- type: bind\n((?:\s{8}.*\n)+)", text + "\n")
    assert len(binds) == 2, f"{rel}: library and downloads must be long-form binds"
    for bind in binds:
        assert "create_host_path: false" in bind


def test_openmediavault_requires_every_path():
    text = directives(read(PKG / "openmediavault/docker-compose.yml"))
    for name in ("ROMARR_CONFIG", "ROMARR_ROMS", "ROMARR_DOWNLOADS"):
        assert re.search(rf"\$\{{{name}:\?[^}}]+\}}", text), name


# --- Synology SPK (SynoCommunity spksrc) ----------------------------------


def test_spk_is_noarch_python_on_romarrs_port():
    spk = read(SPK / "Makefile")
    assert "override ARCH=noarch" in spk
    assert make_var(spk, "SPK_DEPENDS") == '"python312"'
    assert make_var(spk, "SERVICE_PORT") == PORT
    assert make_var(spk, "SERVICE_USER") == "auto", "DSM 7 runs packages as their own user"
    assert (SPK / "src/romarr.png").is_file()


def test_spk_wheels_are_the_flatpak_pins():
    """The wheelhouse is offline: a version that does not exist breaks the install."""
    pins = dict(re.findall(r"^([a-z0-9-]+)==([\d.]+)$", read(SPK / "src/requirements-pure.txt"), re.M))
    flatpak = json.loads(read(PKG / "flatpak/python3-requests.json"))
    shipped = {}
    for source in flatpak["sources"]:
        name, version = Path(source["url"]).name.split("-")[:2]
        shipped[name.replace("_", "-").lower()] = version
    assert pins == {k: shipped[k] for k in pins}
    assert set(pins) == {"requests", "urllib3", "idna", "certifi", "charset-normalizer"}


def test_spk_keeps_state_in_the_package_var_folder():
    """DSM keeps var/ across upgrades and deletes target/."""
    setup = read(SPK / "src/service-setup.sh")
    for line in ('ROMARR_DATA="${SYNOPKG_PKGVAR}/romarr.json"',
                 'ROM_HUB_HOME="${SYNOPKG_PKGVAR}/rom-hub"',
                 'ROMARR_BACKENDS_DIR="${SYNOPKG_PKGVAR}/backends"'):
        assert line in setup
    assert 'LIBRARY_PATH=${SHARE_PATH}' in setup
    assert 'if [ ! -e "${ENV_FILE}" ]' in setup, "an upgrade must not rewrite romarr.env"
    assert "eval" not in directives(setup), "romarr.env is data, not a script"


def test_spk_wizard_asks_for_the_share_it_grants():
    spk = read(SPK / "Makefile")
    key = make_var(spk, "SERVICE_WIZARD_SHARENAME")
    wizard = json.loads(read(SPK / "src/wizard/install_uifile"))
    keys = [s["key"] for step in wizard for item in step["items"] for s in item.get("subitems", [])]
    assert keys == [key]


# --- QNAP QPKG (QDK) ------------------------------------------------------

def qpkg_cfg() -> dict[str, str]:
    return dict(re.findall(r'^(QPKG_\w+|QTS_\w+)="([^"]*)"$', read(QPKG / "qpkg.cfg"), re.M))


def test_qpkg_names_the_release_and_needs_container_station():
    cfg = qpkg_cfg()
    assert cfg["QPKG_WEB_PORT"] == cfg["QPKG_SERVICE_PORT"] == PORT
    assert cfg["QPKG_REQUIRE"].startswith("container-station")
    assert cfg["QPKG_SERVICE_PROGRAM"] == "romarr.sh"
    for icon in ("romarr.gif", "romarr_80.gif", "romarr_gray.gif"):
        assert (QPKG / "icons" / icon).is_file()


@pytest.mark.parametrize("script", [QPKG / "shared/romarr.sh", APK / "CONTROL/start-stop.sh"])
def test_native_wrappers_run_the_image_their_package_version_names(script):
    """A hardcoded tag would leave the package and the app on different versions."""
    text = directives(read(script))
    assert 'IMAGE="ghcr.io/blizzhacker/romarr:${VERSION}"' in text
    assert not re.search(r"blizzhacker/romarr:\d", text)
    assert "eval" not in text, "the env file is data, not a script"
    assert "does not exist" in text, "a missing library folder must refuse to start"
    assert "-e UMASK=002" in text and "-e LIBRARY_PATH=/roms" in text


def test_qpkg_settings_live_on_a_share_that_uninstall_keeps():
    env = read(QPKG / "shared/romarr.env")
    assert re.search(r"^ROMARR_CONFIG=/share/", env, re.M)
    assert re.search(r"^PUID=(?!0$)\d+$", env, re.M)


# --- Asustor APK (apkg-tools) ---------------------------------------------

def test_apk_names_the_release_and_depends_on_docker():
    config = json.loads(read(APK / "CONTROL/config.json"))
    general = config["general"]
    assert general["package"] == "romarr"
    assert general["architecture"] == "any"
    assert any(dep.startswith("docker-ce") for dep in general["depends"])
    assert config["adm-desktop"]["app"]["port"] == int(PORT)
    assert config["register"]["port"] == [f"T{PORT}"]
    assert (APK / "CONTROL/icon.png").is_file()


def test_apk_never_overwrites_an_edited_env_file():
    post = read(APK / "CONTROL/post-install.sh")
    assert 'if [ ! -f "$DATA/romarr.env" ]' in post
    assert re.search(r"^ROMARR_CONFIG=/share/Docker/romarr/", read(APK / "romarr.env.default"), re.M)


# --- the image's NAS assumptions -------------------------------------------

def test_the_entrypoint_names_puid_so_fuse_mounts_work():
    """fusermount3 answers "could not determine username" for an unnamed uid.

    su-exec takes numeric ids, so no PUID ever had a passwd entry, and every
    cloud-folder mount in Docker failed with /dev/fuse and SYS_ADMIN granted.
    Measured on 1.13.2 with 1026:100, 99:100, 568:568 and 1000:1000.
    """
    body = read(ROOT / "docker/entrypoint.sh")
    assert 'getent passwd "${PUID}"' in body
    assert 'getent group "${PGID}"' in body
    assert "*[!0-9]*" in body, "ids are written into /etc/passwd; they must be numbers"
    assert body.index('getent passwd "${PUID}"') < body.index('exec su-exec "${PUID}:${PGID}"')
    assert "user_allow_other" in read(ROOT / "Dockerfile")


def test_the_image_smoke_test_mounts_as_a_puid_user():
    workflow = read(ROOT / ".github/workflows/docker.yml")
    assert "--device /dev/fuse --cap-add SYS_ADMIN" in workflow
    assert "-e PUID=1026 -e PGID=100" in workflow


# --- guides and store rows ---------------------------------------------------

PLATFORMS = ("Synology", "QNAP", "Asustor", "TerraMaster", "UGREEN", "ZimaOS",
             "TrueNAS SCALE", "Unraid", "openmediavault", "Umbrel")


def nas_sections() -> dict[str, str]:
    text = read(NAS_GUIDE)
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_platform_has_a_guide_section(platform):
    sections = {title: body for title, body in nas_sections().items() if platform in title}
    assert sections, f"docs/NAS.md has no section for {platform}"
    body = next(iter(sections.values()))
    for topic in ("PUID", PORT, "Updat"):
        assert topic in body, f"{platform} section does not cover {topic}"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_platform_has_a_store_row(platform):
    rows = [line for line in read(STORES).splitlines() if line.startswith("|") and platform in line]
    assert rows, f"docs/STORES.md has no row for {platform}"


def test_the_guide_links_every_package_folder():
    text = read(NAS_GUIDE)
    for folder in ("synology", "qnap", "asustor", "terramaster", "ugreen", "openmediavault",
                   "casaos", "truenas", "umbrel"):
        assert f"packaging/{folder}" in text, folder


def test_fuse_check_names_an_unnamed_uid(monkeypatch):
    """`--user` runs and bare-metal installs get a reason, not fusermount's error."""
    pwd = pytest.importorskip("pwd")

    from romarr import cloud_rclone

    monkeypatch.setattr(cloud_rclone.os.path, "exists", lambda p: p == "/dev/fuse" or False)
    monkeypatch.setattr(cloud_rclone.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cloud_rclone.os, "getuid", lambda: 4242)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    ok, reason = cloud_rclone.fuse_available()
    assert not ok and "uid 4242 has no /etc/passwd entry" in reason
