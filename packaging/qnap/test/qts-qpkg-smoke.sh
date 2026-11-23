#!/bin/sh
# Run the ROMarr QPKG's service script the way QTS does, against real Docker.
#
#   packaging/qnap/test/qts-qpkg-smoke.sh romarr_1.13.2.qpkg
#
# QTS only runs on QNAP hardware, so this unpacks the QPKG QDK built, puts the
# QTS helpers it calls (/sbin/getcfg, /sbin/log_tool) in front of it, points
# romarr.env at scratch folders laid out like /share, and drives
# start/status/restart/stop/remove through the host's Docker. It needs root
# (the container chowns /config) and a free port, passed as $PORT.
set -eu

QPKG=$(realpath "$1")
PORT="${PORT:-16869}"
# The scripts manage a container named romarr. Never touch one this test did
# not create: refuse to run instead.
if docker inspect romarr >/dev/null 2>&1; then
    echo "a container named romarr already exists; stop and rename it first" >&2
    exit 1
fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/qts.XXXXXX")
trap 'docker rm -f romarr >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

# A QPKG is QDK's installer script, then a 20 KB tar holding control.tar.gz,
# then data.tar.gz. Unpack both the way that script does.
mkdir -p "$WORK/root" "$WORK/ctl" "$WORK/sbin" "$WORK/share/Container" "$WORK/share/Multimedia/Games" "$WORK/share/Download"
len=$(grep -a -m1 '^script_len=' "$QPKG" | cut -d= -f2)
dd if="$QPKG" bs="$len" skip=1 2>/dev/null | tar -xO | tar -xz -C "$WORK/ctl"
size=$(grep -a -m1 -o 'f\.seek([0-9]*)' "$QPKG" | tr -dc '0-9')
tail -c +"$((len + 20480 + 1))" "$QPKG" | head -c "$size" | tar -xz -C "$WORK/root"
cp "$WORK/ctl/qpkg.cfg" "$WORK/root/qpkg.cfg"
test -x "$WORK/root/romarr.sh"
version=$(grep -m1 '^QPKG_VER=' "$WORK/root/qpkg.cfg" | cut -d'"' -f2)

cat > "$WORK/sbin/getcfg" <<GETCFG
#!/bin/sh
case "\$1 \$2" in
    "romarr Install_Path") echo "$WORK/root" ;;
    "romarr Version") echo "$version" ;;
    "romarr Enable") echo TRUE ;;
    *) echo "" ;;
esac
GETCFG
printf '#!/bin/sh\nexit 0\n' > "$WORK/sbin/log_tool"
printf '#!/bin/sh\necho "$*" >> %s/setcfg.log\n' "$WORK" > "$WORK/sbin/setcfg"
chmod +x "$WORK/sbin/getcfg" "$WORK/sbin/log_tool" "$WORK/sbin/setcfg"
# The script calls /sbin/getcfg by absolute path; run it with /sbin rewritten.
sed "s#/sbin/#$WORK/sbin/#g" "$WORK/root/romarr.sh" > "$WORK/romarr.sh"
chmod +x "$WORK/romarr.sh"

sed -i -e "s#^ROMARR_CONFIG=.*#ROMARR_CONFIG=$WORK/share/Container/romarr#" \
       -e "s#^ROMARR_ROMS=.*#ROMARR_ROMS=$WORK/share/Multimedia/Games#" \
       -e "s#^ROMARR_DOWNLOADS=.*#ROMARR_DOWNLOADS=$WORK/share/Download#" \
       -e "s#^ROMARR_HOST_PORT=.*#ROMARR_HOST_PORT=$PORT#" "$WORK/root/romarr.env"

health() {
    for _ in $(seq 1 40); do
        curl -fsS "http://127.0.0.1:$PORT/api/health" && return 0
        sleep 1
    done
    docker logs romarr; return 1
}

echo "== start ghcr.io/blizzhacker/romarr:$version"
"$WORK/romarr.sh" start; health; echo
"$WORK/romarr.sh" status
test "$(docker inspect -f '{{.Config.Image}}' romarr)" = "ghcr.io/blizzhacker/romarr:$version"
docker exec romarr sh -c 'ps -o user,args | grep "[p]ython -m romarr"' | grep -q '^1000 '
test -s "$WORK/share/Container/romarr/romarr.json"
docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' romarr | grep -q "$WORK/share/Download"

grep -q "^romarr Web_Port $PORT " "$WORK/setcfg.log"

echo "== restart keeps the container and settings"
before=$(docker inspect -f '{{.Id}}' romarr)
"$WORK/romarr.sh" restart; health; echo
test "$(docker inspect -f '{{.Id}}' romarr)" = "$before"

echo "== a changed setting gets a new container that has it"
sed -i "s#^TZ=.*#TZ=America/Chicago#" "$WORK/root/romarr.env"
"$WORK/romarr.sh" restart; health; echo
test "$(docker inspect -f '{{.Id}}' romarr)" != "$before"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' romarr | grep -qx "TZ=America/Chicago"
test -s "$WORK/share/Container/romarr/romarr.json"
"$WORK/romarr.sh" stop
if "$WORK/romarr.sh" status; then echo "status reported a stopped container as running"; exit 1; fi

echo "== a missing library folder refuses to start"
mv "$WORK/share/Multimedia/Games" "$WORK/share/Multimedia/Games.off"
if "$WORK/romarr.sh" start; then echo "started without a library"; exit 1; fi
mv "$WORK/share/Multimedia/Games.off" "$WORK/share/Multimedia/Games"

echo "== remove keeps the settings on the share"
"$WORK/romarr.sh" remove
if docker inspect romarr >/dev/null 2>&1; then echo "container left behind"; exit 1; fi
test -s "$WORK/share/Container/romarr/romarr.json"
echo "QTS QPKG SMOKE PASSED"
