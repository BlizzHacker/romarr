#!/usr/bin/env bash
#
# Update an existing ROMarr install, in place.
#
# Run inside the container:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/BlizzHacker/romarr/main/proxmox/ct/update.sh)"
#
# Or from the Proxmox host:
#   pct exec <ctid> -- bash -c "$(curl -fsSL .../update.sh)"

set -euo pipefail

REPO="${REPO:-BlizzHacker/romarr}"
ROOT="${ROOT:-/opt/romarr}"

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; CL=$'\033[m'
msg() { echo -e " ${GN}✔${CL} $1"; }
info() { echo -e " ${YW}➜${CL} $1"; }
die() { echo -e " ${RD}✘ $1${CL}" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -d "$ROOT" ]] || die "No ROMarr installation at ${ROOT}."

# romarr.json holds the request history, the release profile, the API key and
# the password hash. .env holds every credential. Losing either turns an update
# into a reinstall, so they are copied out before anything is unpacked over the
# top and put back afterwards.
STAMP=$(date +%s)
BACKUP="/opt/romarr-backup-${STAMP}"
mkdir -p "$BACKUP"
for keep in romarr.json .env; do
  [[ -f "${ROOT}/${keep}" ]] && cp -a "${ROOT}/${keep}" "${BACKUP}/"
done
# The drop-in backends directory is operator-owned; it is not ours to replace.
[[ -d "${ROOT}/backends" ]] && cp -a "${ROOT}/backends" "${BACKUP}/"
msg "Backed up state to ${BACKUP}"

info "Fetching latest"
url=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
      | grep -o '"tarball_url": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)
if [[ -z "$url" ]]; then
  info "no published release; using main"
  url="https://github.com/${REPO}/archive/refs/heads/main.tar.gz"
fi
curl -fsSL "$url" -o /tmp/romarr-update.tar.gz \
  || die "Download failed from ${REPO}."

systemctl stop romarr 2>/dev/null || true

# Unpack into a staging directory first. Extracting straight over a running
# install leaves a half-updated tree if the archive is truncated, and there is
# then nothing to roll back to.
STAGE=$(mktemp -d)
tar -xzf /tmp/romarr-update.tar.gz -C "$STAGE" --strip-components=1 \
  || die "Archive did not unpack; the old install is untouched."
rm -f /tmp/romarr-update.tar.gz

cp -a "$STAGE"/. "$ROOT"/
rm -rf "$STAGE"

for keep in romarr.json .env; do
  [[ -f "${BACKUP}/${keep}" ]] && cp -a "${BACKUP}/${keep}" "${ROOT}/"
done
[[ -d "${BACKUP}/backends" ]] && cp -a "${BACKUP}/backends" "${ROOT}/"
msg "Unpacked"

info "Updating dependencies"
"${ROOT}/.venv/bin/pip" install --quiet --upgrade -r "${ROOT}/requirements.txt" \
  || die "Dependency update failed. State is in ${BACKUP}."
msg "Dependencies updated"

systemctl start romarr

PORT=$(grep -oP '(?<=^ROMARR_PORT=)\d+' "${ROOT}/.env" 2>/dev/null || echo 6868)
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/api/health" 2>/dev/null; then
    msg "ROMarr is up on port ${PORT}"
    exit 0
  fi
  sleep 2
done

die "ROMarr did not come back up. State is in ${BACKUP}.
   Look at:  journalctl -u romarr -n 50 --no-pager"
