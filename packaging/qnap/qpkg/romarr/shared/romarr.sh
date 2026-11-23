#!/bin/sh
# Start and stop ROMarr as a Container Station container.
#
# The QPKG ships no Python: it runs ghcr.io/blizzhacker/romarr at the version
# the package is, so the QPKG version and the app version are the same thing.
# Settings live in ROMARR_CONFIG on a share, so uninstalling the QPKG never
# deletes them.
CONF=/etc/config/qpkg.conf
QPKG_NAME="romarr"
QPKG_ROOT=$(/sbin/getcfg "$QPKG_NAME" Install_Path -f "$CONF")
VERSION=$(/sbin/getcfg "$QPKG_NAME" Version -f "$CONF")
IMAGE="ghcr.io/blizzhacker/romarr:${VERSION}"
NAME="romarr"
ENV_FILE="${QPKG_ROOT}/romarr.env"
export QNAP_QPKG="$QPKG_NAME"

docker_bin() {
    for candidate in "$(command -v docker 2>/dev/null)" \
        "$(/sbin/getcfg container-station Install_Path -f "$CONF")/bin/docker" \
        /usr/local/bin/docker; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# KEY=value lines only; nothing in the file is executed, and unknown keys
# are ignored.
read_env() {
    while IFS='=' read -r key value; do
        case "$key" in
            PUID) CFG_PUID=$value ;;
            PGID) CFG_PGID=$value ;;
            TZ) CFG_TZ=$value ;;
            ROMARR_CONFIG) CFG_ROMARR_CONFIG=$value ;;
            ROMARR_ROMS) CFG_ROMARR_ROMS=$value ;;
            ROMARR_DOWNLOADS) CFG_ROMARR_DOWNLOADS=$value ;;
            ROMARR_HOST_PORT) CFG_ROMARR_HOST_PORT=$value ;;
        esac
    done < "$ENV_FILE"
    CFG_PUID=${CFG_PUID:-1000} CFG_PGID=${CFG_PGID:-100} CFG_TZ=${CFG_TZ:-Etc/UTC}
    CFG_ROMARR_HOST_PORT=${CFG_ROMARR_HOST_PORT:-6868}
}

log() { /sbin/log_tool -t 0 -a "ROMarr: $*" 2>/dev/null; echo "ROMarr: $*"; }

start() {
    DOCKER=$(docker_bin) || { log "Container Station's docker was not found"; exit 1; }
    chmod 600 "$ENV_FILE" 2>/dev/null
    read_env
    for dir in "$CFG_ROMARR_ROMS" "$CFG_ROMARR_DOWNLOADS"; do
        if [ ! -d "$dir" ]; then
            log "$dir does not exist; create it or edit ${ENV_FILE}"
            exit 1
        fi
    done
    mkdir -p "$CFG_ROMARR_CONFIG"
    # App Center's Open button follows the port actually published.
    /sbin/setcfg "$QPKG_NAME" Web_Port "$CFG_ROMARR_HOST_PORT" -f "$CONF"
    /sbin/setcfg "$QPKG_NAME" Service_Port "$CFG_ROMARR_HOST_PORT" -f "$CONF"

    # docker start never applies new mounts or environment, so the existing
    # container is reused only when the image and every setting match the
    # fingerprint it was created with. Anything edited gets a new container.
    spec=$(printf '%s|' "$IMAGE" "$CFG_PUID" "$CFG_PGID" "$CFG_TZ" "$CFG_ROMARR_CONFIG" \
        "$CFG_ROMARR_ROMS" "$CFG_ROMARR_DOWNLOADS" "$CFG_ROMARR_HOST_PORT" | cksum | cut -d' ' -f1)
    if [ "$("$DOCKER" inspect -f '{{index .Config.Labels "org.romarr.spec"}}' "$NAME" 2>/dev/null)" = "$spec" ]; then
        "$DOCKER" start "$NAME" >/dev/null && return 0
    fi
    # Have the image before touching the old container: a registry outage
    # then leaves the previous one in place.
    "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || "$DOCKER" pull "$IMAGE" >/dev/null || \
        { log "could not pull $IMAGE"; exit 1; }
    "$DOCKER" rm -f "$NAME" >/dev/null 2>&1
    "$DOCKER" run -d --name "$NAME" --restart unless-stopped \
        --label "com.qnap.qpkg=$QPKG_NAME" --label "org.romarr.spec=$spec" \
        -p "${CFG_ROMARR_HOST_PORT}:6868" \
        -e PUID="$CFG_PUID" -e PGID="$CFG_PGID" -e UMASK=002 \
        -e TZ="$CFG_TZ" -e LIBRARY_PATH=/roms \
        -v "${CFG_ROMARR_CONFIG}:/config" \
        -v "${CFG_ROMARR_ROMS}:/roms" \
        -v "${CFG_ROMARR_DOWNLOADS}:${CFG_ROMARR_DOWNLOADS}" \
        "$IMAGE" >/dev/null || { log "could not start $IMAGE"; exit 1; }
    log "started $IMAGE"
}

stop() {
    DOCKER=$(docker_bin) || exit 0
    "$DOCKER" stop -t 30 "$NAME" >/dev/null 2>&1
    return 0
}

case "$1" in
    start)
        ENABLED=$(/sbin/getcfg "$QPKG_NAME" Enable -u -d FALSE -f "$CONF")
        if [ "$ENABLED" != "TRUE" ]; then
            echo "$QPKG_NAME is disabled."
            exit 1
        fi
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        start
        ;;
    status)
        DOCKER=$(docker_bin) || exit 1
        [ "$("$DOCKER" inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] || exit 1
        ;;
    remove)
        # The container goes; ROMARR_CONFIG on the share stays.
        DOCKER=$(docker_bin) && "$DOCKER" rm -f "$NAME" >/dev/null 2>&1
        exit 0
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|remove}"
        exit 1
        ;;
esac
exit 0
