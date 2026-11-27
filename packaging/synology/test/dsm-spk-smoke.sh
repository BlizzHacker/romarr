#!/bin/sh
# Install, start, upgrade and remove a ROMarr .spk the way DSM 7 does, in a
# container laid out like a DiskStation (/var/packages, /volume1, a package
# user, SynoCommunity's Python at /var/packages/python312/target).
#
#   packaging/synology/test/dsm-spk-smoke.sh romarr_noarch-dsm7_1.14.1-1.spk
#
# DSM itself only runs on Synology hardware (its licence forbids anything
# else), so this replays Package Center's sequence: preinst, extract
# package.tgz into target/, postinst, start-stop-status start/status/stop as
# the package user, then the upgrade scripts with var/ kept, then uninstall.
set -eu

SPK=$(realpath "$1")
IMAGE="${PYTHON_IMAGE:-python:3.12-slim}"

docker run --rm -i -v "$SPK:/pkg.spk:ro" "$IMAGE" sh -eu <<'INNER'
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null && apt-get -qq install -y curl procps >/dev/null 2>&1

# DSM 7 layout.
P=/var/packages/romarr
mkdir -p /var/packages/python312/target/bin "$P/target" "$P/var" "$P/etc" "$P/shares" /volume1/games /tmp/spk
ln -s /usr/local/bin/python3.12 /var/packages/python312/target/bin/python3
ln -s /usr/local/bin/pip3.12 /var/packages/python312/target/bin/pip3
# DSM creates the package user and, from conf/resource data-share, the share
# with rw for that user, linked under shares/.
groupadd -g 65536 synocommunity
useradd -u 1027 -g synocommunity -M -d "$P/var" sc-romarr
chown sc-romarr:synocommunity /volume1/games
ln -s /volume1/games "$P/shares/games"

cd /tmp/spk && tar -xf /pkg.spk
export SYNOPKG_PKGNAME=romarr SYNOPKG_PKGDEST="$P/target" SYNOPKG_PKGVAR="$P/var" \
       SYNOPKG_PKGINST_TEMP_DIR="$P/target" SYNOPKG_DSM_VERSION_MAJOR=7 \
       SYNOPKG_DSM_VERSION_MINOR=2 SYNOPKG_DSM_VERSION_BUILD=72806 \
       SYNOPKG_PKG_STATUS=INSTALL SYNOPKG_TEMP_LOGFILE=/tmp/spk/install.log
# The wizard's answer, which DSM passes to the install scripts only.
WIZARD="wizard_games_share=games"
grep -q '^install_dep_packages="python312"' INFO
grep -q '"run-as": "package"' conf/privilege

step() { echo "== $*"; }
step preinst;  env $WIZARD sh scripts/preinst
step extract;  tar -xzf package.tgz -C "$P/target"
step postinst; env $WIZARD sh scripts/postinst || { cat /tmp/spk/install.log; exit 1; }
chown -R sc-romarr:synocommunity "$P/target" "$P/var"
"$P/target/env/bin/python3" -c "import requests; print('requests', requests.__version__)"
grep -q '^LIBRARY_PATH=/volume1/games$' "$P/var/romarr.env"

# Package Center runs start-stop-status as the package user with the SYNOPKG_
# variables only; the wizard answers exist during installation alone.
svc() { su sc-romarr -s /bin/sh -c "$(env | grep -E '^SYNOPKG_' | sed 's/^/export /'); sh /tmp/spk/scripts/start-stop-status $1"; }
health() {
    for _ in $(seq 1 30); do
        curl -fsS http://127.0.0.1:6868/api/health && return 0
        sleep 1
    done
    cat "$P/var/romarr.log"; return 1
}

step start; svc start; health; echo
svc status
pgrep -u sc-romarr -f -- '-m romarr' >/dev/null
test -s "$P/var/romarr.json"
grep -q 'listening on http://0.0.0.0:6868, library=/volume1/games' "$P/var/romarr.log"
before=$(sha256sum "$P/var/romarr.json")

step stop; svc stop
if svc status; then echo "still running after stop"; exit 1; fi

step upgrade
export SYNOPKG_PKG_STATUS=UPGRADE
sh scripts/preupgrade
sh scripts/preuninst
sh scripts/postuninst
rm -rf "$P/target" && mkdir -p "$P/target" && tar -xzf package.tgz -C "$P/target"
sh scripts/postinst
sh scripts/postupgrade
chown -R sc-romarr:synocommunity "$P/target" "$P/var"
test "$(sha256sum "$P/var/romarr.json")" = "$before"
svc start; health; echo; svc stop

# The upgrade's postinst read the share back from installer-variables.
grep -q '^LIBRARY_PATH=/volume1/games$' "$P/var/romarr.env"

step uninstall
export SYNOPKG_PKG_STATUS=UNINSTALL
sh scripts/preuninst
sh scripts/postuninst
echo "DSM SPK SMOKE PASSED"
INNER
