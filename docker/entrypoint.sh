#!/bin/sh
# Run Romarr as the user who owns the media, not as root.
#
# Romarr writes ROM files into a library directory that another application --
# RomM, Gaseous or Retrom -- has to read. If those files land owned by root, the
# library either cannot read them or has to be run as root too. PUID/PGID is the
# convention every *arr user already has in their compose file, so it is the one
# used here.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Where ROMs are filed, defaulted here rather than in the Dockerfile.
#
# LIBRARY_PATH has an older alias, ROMM_LIBRARY, and the application prefers
# the new name when both are present. An image-level ENV counts as "present",
# so a Dockerfile default would beat the ROMM_LIBRARY an operator actually set
# and file ROMs somewhere they never asked for. Defaulting only when neither
# name is given keeps the alias working.
if [ -z "${LIBRARY_PATH}" ] && [ -z "${ROMM_LIBRARY}" ]; then
    export LIBRARY_PATH=/roms
fi

if [ "$(id -u)" != "0" ]; then
    # Already unprivileged: `docker run --user`, rootless Docker, Kubernetes
    # runAsUser, or a Podman userns. There is nothing to drop to and no
    # authority to chown with, so respect the decision and start.
    echo "Romarr: running as uid $(id -u), PUID/PGID ignored"
    exec "$@"
fi

# /config only. Never the library or the downloads volume: those can be
# multiple terabytes on a NAS, and a recursive chown at every boot would take
# hours and rewrite ownership the operator chose on purpose.
mkdir -p /config
chown "${PUID}:${PGID}" /config

if [ ! -w /config ]; then
    echo "Romarr: /config is not writable by ${PUID}:${PGID} -- history and" >&2
    echo "settings cannot be saved. Check the ownership of the host directory." >&2
    exit 1
fi

echo "Romarr: starting as ${PUID}:${PGID}"
exec su-exec "${PUID}:${PGID}" "$@"
