"""The store packages in packaging/ must describe one release, one image, one port.

Seven stores each carry their own copy of the version, the image tag, the
digest and the port. A release that bumps five of them ships two stores still
installing the previous version, and nobody notices until a user does. These
tests read the files as text (the suite has no YAML dependency) and fail on any
drift, so a bump has to touch every package at once.

The Flatpak launcher is real code that ships to users, so its environment and
port logic are tested here too.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
PORT = "6868"
NAS_COMPOSE = (
    "synology/container-manager/docker-compose.yml",
    "qnap/container-station/docker-compose.yml",
    "asustor/portainer/docker-compose.yml",
    "terramaster/docker-compose.yml",
    "ugreen/docker-compose.yml",
    "openmediavault/docker-compose.yml",
)
TRUENAS_IMAGE = r'repository: ghcr\.io/blizzhacker/romarr\n\s+tag: "[\d.]+@(sha256:[0-9a-f]{64})"'


@functools.lru_cache(maxsize=None)
def read(rel: str) -> str:
    return (PKG / rel).read_text(encoding="utf-8")


def only(pattern: str, text: str) -> str:
    found = set(re.findall(pattern, text, flags=re.MULTILINE))
    assert len(found) == 1, f"expected one value for {pattern!r}, found {found}"
    return found.pop()


def flatpak_version() -> str:
    metainfo = read("flatpak/app.romarr.ROMarr.metainfo.xml")
    return re.findall(r'<release version="([^"]+)"', metainfo)[0]


def winget_dir() -> Path:
    versions = sorted((PKG / "winget/manifests/m/MoveWeight/ROMarr").iterdir())
    assert len(versions) == 1, "keep only the current version's manifests here"
    return versions[0]


def package_versions() -> dict[str, str]:
    truenas = "truenas/ix-dev/community/romarr"
    winget = winget_dir()
    winget_text = "".join(p.read_text(encoding="utf-8") for p in winget.glob("*.yaml"))
    return {
        "flatpak metainfo": flatpak_version(),
        "flatpak tarball": only(r"refs/tags/v([\d.]+)\.tar\.gz", read("flatpak/app.romarr.ROMarr.yml")),
        "truenas app_version": only(r"^app_version: ([\d.]+)$", read(f"{truenas}/app.yaml")),
        "truenas image": only(r'blizzhacker/romarr\n\s+tag: "([\d.]+)@sha256', read(f"{truenas}/ix_values.yaml")),
        "casaos image": only(r"blizzhacker/romarr:([\d.]+)", read("casaos/Apps/ROMarr/docker-compose.yml")),
        "casaos version": only(r'^  version: "([\d.]+)"$', read("casaos/Apps/ROMarr/docker-compose.yml")),
        "umbrel version": only(r'^version: "([\d.]+)"$', read("umbrel/romarr/umbrel-app.yml")),
        "umbrel image": only(r"blizzhacker/romarr:([\d.]+)@", read("umbrel/romarr/docker-compose.yml")),
        "truenas custom app": only(r"blizzhacker/romarr:([\d.]+)", read("truenas/custom-app.yaml")),
        "winget folder": winget.name,
        "winget PackageVersion": only(r"^PackageVersion: ([\d.]+)$", winget_text),
        "winget zip": only(r"ROMarr-([\d.]+)-win-x64\.zip", winget_text),
        "winget nested exe": only(r"ROMarr-([\d.]+)-win-x64\\ROMarr\.exe", winget_text),
        "homebrew tarball": only(r"refs/tags/v([\d.]+)\.tar\.gz", read("homebrew/Formula/romarr.rb")),
        "synology spk": only(r"^SPK_VERS = ([\d.]+)$", read("synology/spksrc/spk/romarr/Makefile")),
        "synology source": only(r"^PKG_VERS = ([\d.]+)$", read("synology/spksrc/cross/romarr/Makefile")),
        "synology digest": only(r"^romarr-([\d.]+)\.tar\.gz SHA256", read("synology/spksrc/cross/romarr/digests")),
        "qnap qpkg": only(r'^QPKG_VER="([\d.]+)"$', read("qnap/qpkg/romarr/qpkg.cfg")),
        "asustor apk": json.loads(read("asustor/apk/CONTROL/config.json"))["general"]["version"],
        **{f"{rel} image": only(r"image: ghcr\.io/blizzhacker/romarr:([\d.]+)$", read(rel))
           for rel in NAS_COMPOSE},
    }


def test_every_store_package_names_the_same_release():
    versions = package_versions()
    assert len(set(versions.values())) == 1, versions


def test_flatpak_homebrew_and_synology_pin_the_same_tarball_hash():
    flatpak = only(r"sha256: ([0-9a-f]{64})", read("flatpak/app.romarr.ROMarr.yml"))
    brew = only(r'^  sha256 "([0-9a-f]{64})"$', read("homebrew/Formula/romarr.rb"))
    spk = only(r"^romarr-[\d.]+\.tar\.gz SHA256 ([0-9a-f]{64})$",
               read("synology/spksrc/cross/romarr/digests"))
    assert flatpak == brew == spk


def test_digest_pinned_images_agree():
    truenas = only(TRUENAS_IMAGE, read("truenas/ix-dev/community/romarr/ix_values.yaml"))
    umbrel = only(r"blizzhacker/romarr:[\d.]+@(sha256:[0-9a-f]{64})", read("umbrel/romarr/docker-compose.yml"))
    assert truenas == umbrel


@pytest.mark.parametrize("rel, pattern", [
    ("casaos/Apps/ROMarr/docker-compose.yml", r'port_map: "(\d+)"'),
    ("casaos/Apps/ROMarr/docker-compose.yml", r"target: (\d+)\n"),
    ("umbrel/romarr/umbrel-app.yml", r"^port: (\d+)$"),
    ("umbrel/romarr/docker-compose.yml", r"APP_PORT: (\d+)"),
    ("proxmox/json/romarr.json", r'"interface_port": (\d+)'),
    ("proxmox/install/romarr-install.sh", r"ROMARR_PORT=(\d+)"),
    ("proxmox/ct/romarr.sh", r"\$\{IP\}:(\d+)"),
])
def test_packages_use_romarrs_port(rel, pattern):
    assert only(pattern, read(rel)) == PORT


def test_proxmox_json_resources_match_the_ct_script():
    """community-scripts requires this; their site shows the JSON numbers."""
    meta = json.loads(read("proxmox/json/romarr.json"))
    resources = meta["install_methods"][0]["resources"]
    ct = read("proxmox/ct/romarr.sh")

    def var(name: str) -> str:
        return only(rf'^var_{name}="\$\{{var_{name}:-([^}}]+)\}}"$', ct)

    assert str(resources["cpu"]) == var("cpu")
    assert str(resources["ram"]) == var("ram")
    assert str(resources["hdd"]) == var("disk")
    assert resources["version"] == var("version")
    assert "config_path" not in meta, "config_path belongs on the install method"
    assert len(meta["notes"]) <= 3


def test_flatpak_json_and_python_sources_parse():
    module = json.loads(read("flatpak/python3-requests.json"))
    names = {Path(s["url"]).name.split("-")[0] for s in module["sources"]}
    assert {"requests", "urllib3", "idna", "certifi", "charset_normalizer"} <= names
    assert all(len(s["sha256"]) == 64 for s in module["sources"])


# --- the Flatpak launcher -------------------------------------------------

@pytest.fixture()
def launcher(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "romarr_desktop", PKG / "flatpak" / "romarr-desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("ROMARR_PORT", "ROMARR_DATA", "ROM_HUB_HOME", "LIBRARY_PATH",
                 "ROMM_LIBRARY", "ROMARR_LIBRARY", "ROMARR_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("ROMARR_LIBRARY_ROOT", str(tmp_path / "no-code-here"))
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path))
    real_default = module.default_library
    monkeypatch.setattr(module, "default_library",
                        lambda media=tmp_path / "no-media": real_default(media))
    # prepare_environment() writes os.environ directly, as it must in the app;
    # put the process environment back so nothing leaks into later tests.
    saved = dict(os.environ)
    yield module
    os.environ.clear()
    os.environ.update(saved)


def test_launcher_binds_loopback_and_keeps_state_per_user(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "port_is_free", lambda port: True)
    port = launcher.prepare_environment()
    env = launcher.os.environ
    assert port == 6868
    assert env["ROMARR_HOST"] == "127.0.0.1"
    assert env["ROMARR_DATA"] == str(tmp_path / "data" / "romarr" / "romarr.json")
    assert env["LIBRARY_PATH"] == str(tmp_path / "Games" / "ROMs")
    assert (tmp_path / "Games" / "ROMs").is_dir()


def test_launcher_prefers_an_existing_emudeck_library(launcher, tmp_path):
    (tmp_path / "Emulation" / "roms").mkdir(parents=True)
    assert launcher.default_library() == tmp_path / "Emulation" / "roms"


def test_launcher_finds_an_sd_card_library(launcher, tmp_path):
    card = tmp_path / "media" / "deck" / "sdcard" / "Emulation" / "roms"
    card.mkdir(parents=True)
    assert launcher.default_library(tmp_path / "media") == card


def test_launcher_respects_romarr_library(launcher, monkeypatch, tmp_path):
    monkeypatch.setenv("ROMARR_LIBRARY", str(tmp_path / "mine"))
    monkeypatch.setattr(launcher, "port_is_free", lambda port: True)
    launcher.prepare_environment()
    assert launcher.os.environ["LIBRARY_PATH"] == str(tmp_path / "mine")


def test_launcher_ignores_the_compilers_library_path(launcher, monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_PATH", "/opt/homebrew/lib")
    monkeypatch.setattr(launcher, "port_is_free", lambda port: True)
    launcher.prepare_environment()
    assert launcher.os.environ["LIBRARY_PATH"] == str(tmp_path / "Games" / "ROMs")


def test_launcher_survives_a_garbage_port(launcher, monkeypatch):
    monkeypatch.setenv("ROMARR_PORT", "not-a-port")
    monkeypatch.setattr(launcher, "port_is_free", lambda port: True)
    assert launcher.choose_port() == 6868


def test_downloads_never_overwrite(launcher, tmp_path):
    (tmp_path / "game.zip").write_text("x")
    (tmp_path / "game (2).zip").write_text("x")
    assert launcher.unique_path(tmp_path, "game.zip") == tmp_path / "game (3).zip"
    assert launcher.unique_path(tmp_path, "../evil.zip") == tmp_path / "evil.zip"
    assert launcher.unique_path(tmp_path, "") == tmp_path / "download"


def test_launcher_moves_off_a_taken_port(launcher, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        monkeypatch.setenv("ROMARR_PORT", str(taken))
        chosen = launcher.choose_port()
    assert chosen != taken
    assert 0 < chosen < 65536
