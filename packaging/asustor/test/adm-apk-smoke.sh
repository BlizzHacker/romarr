#!/bin/sh
# Install and run the ROMarr App Central package the way ADM does, against
# real Docker.
#
#   packaging/asustor/test/adm-apk-smoke.sh romarr_1.13.2_any.apk
#
# ADM only runs on Asustor hardware, so this unpacks the .apk apkg-tools
# built (a zip of control.tar.gz and data.tar.gz), puts it where App Central
# would (APKG_PKG_DIR), rewrites /share to scratch folders laid out like
# ADM's, then runs post-install, start-stop.sh start/reload/stop and
# pre-uninstall through the host's Docker. Needs root and a free $PORT.
set -eu

APK=$(realpath "$1")
PORT="${PORT:-16870}"
# The scripts manage a container named romarr. Never touch one this test did
# not create: refuse to run instead.
if docker inspect romarr >/dev/null 2>&1; then
    echo "a container named romarr already exists; stop and rename it first" >&2
    exit 1
fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/adm.XXXXXX")
trap 'docker rm -f romarr >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

PKG="$WORK/AppCentral/romarr"
mkdir -p "$PKG/CONTROL" "$WORK/share/Docker" "$WORK/share/Media" "$WORK/share/Download"
python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$APK" "$WORK/apk"
tar -xzf "$WORK/apk/control.tar.gz" -C "$PKG/CONTROL"
tar -xzf "$WORK/apk/data.tar.gz" -C "$PKG"
grep -q '"architecture": "any"' "$PKG/CONTROL/config.json"
grep -q 'docker-ce' "$PKG/CONTROL/config.json"

# ADM's /share is the host's here.
for f in "$PKG"/CONTROL/*.sh "$PKG/romarr.env.default"; do
    sed -i "s#/share/#$WORK/share/#g" "$f"
done
sed -i "s#^ROMARR_HOST_PORT=.*#ROMARR_HOST_PORT=$PORT#" "$PKG/romarr.env.default"
export APKG_PKG_DIR="$PKG" APKG_PKG_STATUS=install

health() {
    for _ in $(seq 1 40); do
        curl -fsS "http://127.0.0.1:$PORT/api/health" && return 0
        sleep 1
    done
    docker logs romarr; return 1
}

echo "== post-install"
sh "$PKG/CONTROL/post-install.sh"
test -f "$WORK/share/Docker/romarr/romarr.env"
echo "== start"
sh "$PKG/CONTROL/start-stop.sh" start; health; echo
test -d "$WORK/share/Media/Games"
test -s "$WORK/share/Docker/romarr/config/romarr.json"
test "$(docker inspect -f '{{.Config.Image}}' romarr)" = "ghcr.io/blizzhacker/romarr:$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$PKG/CONTROL/config.json")"

echo "== reinstall keeps an edited romarr.env"
echo "# edited" >> "$WORK/share/Docker/romarr/romarr.env"
sh "$PKG/CONTROL/post-install.sh"
grep -q '^# edited$' "$WORK/share/Docker/romarr/romarr.env"

echo "== reload"
sh "$PKG/CONTROL/start-stop.sh" reload; health; echo

echo "== a changed setting gets a new container that has it"
before=$(docker inspect -f '{{.Id}}' romarr)
sed -i "s#^TZ=.*#TZ=America/Chicago#" "$WORK/share/Docker/romarr/romarr.env"
sh "$PKG/CONTROL/start-stop.sh" reload; health; echo
test "$(docker inspect -f '{{.Id}}' romarr)" != "$before"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' romarr | grep -qx "TZ=America/Chicago"

echo "== a mistyped downloads folder refuses to start"
sh "$PKG/CONTROL/start-stop.sh" stop
sed -i "s#^ROMARR_DOWNLOADS=.*#ROMARR_DOWNLOADS=$WORK/share/Dowload#" "$WORK/share/Docker/romarr/romarr.env"
if sh "$PKG/CONTROL/start-stop.sh" start; then echo "started with a missing folder"; exit 1; fi

echo "== pre-uninstall keeps the settings"
sh "$PKG/CONTROL/pre-uninstall.sh"
if docker inspect romarr >/dev/null 2>&1; then echo "container left behind"; exit 1; fi
test -s "$WORK/share/Docker/romarr/config/romarr.json"
echo "ADM APK SMOKE PASSED"
