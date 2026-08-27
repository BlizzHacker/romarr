#!/bin/sh
# Run ROMarr as the user who owns the media, not as root.
#
# ROMarr writes ROM files into a library directory that another application --
# RomM, Gaseous or Retrom -- has to read. If those files land owned by root, the
# library either cannot read them or has to be run as root too. PUID/PGID is the
# convention every *arr user already has in their compose file, so it is the one
# used here.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-002}"

# Media stacks normally share files through a group.  Keep newly-created
# files group-writable (0664) and directories traversable (0775) by default,
# while allowing operators to choose a stricter mask.  This affects only new
# files created by ROMarr; it never rewrites a mounted download's mode.
case "$UMASK" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
    *)
        echo "ROMarr: UMASK must be three or four octal digits (for example 002)." >&2
        exit 1
        ;;
esac
umask "$UMASK"

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
    echo "ROMarr: running as uid $(id -u), PUID/PGID ignored; umask ${UMASK}"
    exec "$@"
fi

# /config only. Never the library or the downloads volume: those can be
# multiple terabytes on a NAS, and a recursive chown at every boot would take
# hours and rewrite ownership the operator chose on purpose.
#
# Recursive within /config, though, and that part is not cosmetic. The chown
# used to cover only the directory, which left a root-owned romarr.json --
# from an install that once ran without PUID, or a backup restored with cp as
# root -- unreadable to ${PUID} while the directory around it stayed writable.
# ROMarr's answer to a state file it cannot read was to start from defaults and
# then save over it: the API key was regenerated, the history emptied, and the
# install came back *unclaimed*, so the next person to reach the port set the
# password. /config holds a JSON file and any installed plugins, so the cost of
# doing this properly is milliseconds.
mkdir -p /config
if ! chown -R "${PUID}:${PGID}" /config 2>/dev/null; then
    echo "ROMarr: cannot change ownership of /config to ${PUID}:${PGID}." >&2
    echo "A read-only mount, or a filesystem that does not carry uids (some" >&2
    echo "SMB/CIFS shares). Mount /config from a directory this container can" >&2
    echo "own, or run the container with --user so no chown is attempted." >&2
    exit 1
fi

# Tested as the user that will actually run, not as root. Root passes `test -w`
# on almost anything, so this check used to be incapable of failing for the
# reason it names.
if ! su-exec "${PUID}:${PGID}" test -w /config; then
    echo "ROMarr: /config is not writable by ${PUID}:${PGID} -- history and" >&2
    echo "settings cannot be saved. Check the ownership of the host directory." >&2
    exit 1
fi

# ROMARR_DATA may point outside /config, in which case nothing above has
# touched it and this is the only thing standing between an unreadable state
# file and a silently reset install.
STATE="${ROMARR_DATA:-/config/romarr.json}"
if [ -e "$STATE" ] && ! su-exec "${PUID}:${PGID}" test -r "$STATE"; then
    echo "ROMarr: ${STATE} exists but ${PUID}:${PGID} cannot read it." >&2
    echo "That file holds the API key, the password hash and the history." >&2
    echo "Fix its ownership on the host -- chown -R ${PUID}:${PGID} on the" >&2
    echo "directory you mounted -- rather than deleting it." >&2
    exit 1
fi

echo "ROMarr: starting as ${PUID}:${PGID} with umask ${UMASK}"
exec su-exec "${PUID}:${PGID}" "$@"
