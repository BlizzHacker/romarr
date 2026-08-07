#!/usr/bin/env bash
#
# ROMarr -- Proxmox LXC installer.
#
# Author: BlizzHacker
# License: MIT
# Source: https://github.com/BlizzHacker/romarr
#
# Run on a Proxmox VE host:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/BlizzHacker/romarr/main/proxmox/ct/romarr.sh)"
#
# Self-contained on purpose. This script used to source community-scripts'
# build.func, which then fetched `install/<app>.sh` from *its own* repository.
# That framework is built for scripts that live inside community-scripts, and
# ROMarr does not, so the fetch 404'd and the documented install command could
# never work (issue #2). It also meant every rename on their side -- ProxmoxVE
# to ProxmoxVED had already happened once -- silently broke our installer.
#
# When ROMarr is accepted into community-scripts, their copy lives in their
# repo and uses their framework. This one answers to nobody but us.

set -euo pipefail

APP="ROMarr"
APP_PORT="${APP_PORT:-6868}"
REPO="${REPO:-BlizzHacker/romarr}"

# Container defaults. Every one is overridable from the environment, so an
# unattended install is `CTID=123 DISK=8 bash -c "$(curl ...)"`.
CTID="${CTID:-}"
HOSTNAME_="${HOSTNAME_:-romarr}"
DISK="${DISK:-4}"
CPU="${CPU:-1}"
RAM="${RAM:-512}"
BRIDGE="${BRIDGE:-vmbr0}"
STORAGE="${STORAGE:-}"
OSVERSION="${OSVERSION:-13}"
UNPRIVILEGED="${UNPRIVILEGED:-1}"
ROM_PATH="${ROM_PATH:-/mnt/roms}"

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; CL=$'\033[m'
msg() { echo -e " ${GN}✔${CL} $1"; }
info() { echo -e " ${YW}➜${CL} $1"; }
die() { echo -e " ${RD}✘ $1${CL}" >&2; exit 1; }

# --- checks, each with an answer rather than a stack trace -----------------

command -v pct >/dev/null 2>&1 || die \
  "'pct' not found. Run this on a Proxmox VE host, not inside a container."
[[ $EUID -eq 0 ]] || die "Run as root."

for tool in curl tar pveam; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is required but missing."
done

# --- pick a container id ---------------------------------------------------

if [[ -z "$CTID" ]]; then
  CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo "")
  [[ -n "$CTID" ]] || die "Could not allocate a container id. Set CTID=<id>."
fi
if pct status "$CTID" >/dev/null 2>&1; then
  die "Container $CTID already exists. Set CTID=<free id>."
fi

# --- storage ---------------------------------------------------------------

if [[ -z "$STORAGE" ]]; then
  STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR==2 {print $1}')
  [[ -n "$STORAGE" ]] || die \
    "No storage accepting container root filesystems. Set STORAGE=<name>."
fi

TEMPLATE_STORE=$(pvesm status -content vztmpl 2>/dev/null | awk 'NR==2 {print $1}')
[[ -n "$TEMPLATE_STORE" ]] || die \
  "No storage accepting container templates (content type 'vztmpl')."

# --- template --------------------------------------------------------------

info "Updating template list"
pveam update >/dev/null 2>&1 || true

TEMPLATE=$(pveam available --section system 2>/dev/null \
  | awk -v v="debian-${OSVERSION}-standard" '$2 ~ v {print $2}' | sort -V | tail -1)
[[ -n "$TEMPLATE" ]] || die \
  "No Debian ${OSVERSION} template offered by this host. Set OSVERSION=<n>."

if ! pveam list "$TEMPLATE_STORE" 2>/dev/null | grep -q "$TEMPLATE"; then
  info "Downloading $TEMPLATE"
  pveam download "$TEMPLATE_STORE" "$TEMPLATE" >/dev/null \
    || die "Failed to download template $TEMPLATE."
fi
msg "Template ready: $TEMPLATE"

# --- create ----------------------------------------------------------------

info "Creating LXC $CTID ($HOSTNAME_)"
pct create "$CTID" "${TEMPLATE_STORE}:vztmpl/${TEMPLATE}" \
  --hostname "$HOSTNAME_" \
  --cores "$CPU" \
  --memory "$RAM" \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --unprivileged "$UNPRIVILEGED" \
  --features nesting=1 \
  --onboot 1 \
  --tags "arr;emulation" \
  >/dev/null || die "pct create failed."
msg "Created LXC $CTID"

pct start "$CTID" >/dev/null || die "Container $CTID would not start."

# Wait for the network rather than guessing with a sleep: apt fails in a way
# that looks like a broken mirror when DNS is simply not up yet.
info "Waiting for network"
for _ in $(seq 1 30); do
  if pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 \
  || die "Container $CTID has no working DNS. Check the bridge and DHCP."
msg "Network up"

# --- install ---------------------------------------------------------------

info "Installing dependencies (this takes a minute)"
pct exec "$CTID" -- bash -c "
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # libarchive-tools provides bsdtar, which reads the .7z and .rar archives the
  # disc platforms ship as. Without it every PlayStation, PS2 and Wii import
  # fails on a format it cannot open. Debian's GNU tar is not a substitute.
  apt-get install -y -qq python3-venv python3-pip curl ca-certificates \
    libarchive-tools >/dev/null
" || die "Dependency install failed inside container $CTID."
msg "Dependencies installed"

# Latest release, falling back to main. A repo with no published release is a
# normal state for a fork or a fresh clone, and 'no releases' should not read
# as 'install is broken'.
info "Fetching $APP"
pct exec "$CTID" -- bash -c "
  set -e
  mkdir -p /opt/romarr
  url=\$(curl -fsSL https://api.github.com/repos/${REPO}/releases/latest 2>/dev/null \
        | grep -o '\"tarball_url\": *\"[^\"]*\"' | head -1 | cut -d'\"' -f4 || true)
  if [ -z \"\$url\" ]; then
    echo 'no published release; using main'
    url=https://github.com/${REPO}/archive/refs/heads/main.tar.gz
  fi
  curl -fsSL \"\$url\" -o /tmp/romarr.tar.gz
  tar -xzf /tmp/romarr.tar.gz -C /opt/romarr --strip-components=1
  rm -f /tmp/romarr.tar.gz

  # Refuse to leave an unauthenticated ROMarr on somebody's network.
  #
  # The published release can lag main by a long way -- v0.7.0 was tagged
  # before authentication existed at all, so installing 'latest' produced an
  # install whose API was open to anyone who could reach the port, on a service
  # that queues downloads and writes to the filesystem. An installer that
  # quietly does that is worse than one that fails.
  if [ ! -f /opt/romarr/romarr/auth.py ]; then
    echo 'release predates authentication; using main instead'
    rm -rf /opt/romarr/romarr
    curl -fsSL https://github.com/${REPO}/archive/refs/heads/main.tar.gz \
      -o /tmp/romarr.tar.gz
    tar -xzf /tmp/romarr.tar.gz -C /opt/romarr --strip-components=1
    rm -f /tmp/romarr.tar.gz
  fi
  test -f /opt/romarr/romarr/auth.py
" || die "Could not download an authenticated build of $APP from ${REPO}."
msg "Fetched $APP"

info "Creating virtualenv"
pct exec "$CTID" -- bash -c "
  set -e
  cd /opt/romarr
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
" || die "Python environment setup failed."
msg "Virtualenv ready"

# ROMarr starts and serves its UI with none of these reachable, and the
# Settings pages say which are missing -- a first run is never a blank failure.
# Authentication is deliberately not pre-set: the first visit to the web UI
# asks for a password, which is how the install gets claimed.
pct exec "$CTID" -- bash -c "
cat >/opt/romarr/.env <<'ENVEOF'
PROWLARR_URL=
PROWLARR_API_KEY=

QBITTORRENT_URL=
QBITTORRENT_USER=
QBITTORRENT_PASS=

LIBRARY_KIND=romm
LIBRARY_URL=
LIBRARY_USERNAME=
LIBRARY_PASSWORD=

# Where imported ROMs are filed. This must be a path your library server also
# scans, or the import succeeds and the game never appears.
LIBRARY_PATH=${ROM_PATH}
ROMARR_DATA=/opt/romarr/romarr.json
ROMARR_BACKENDS_DIR=/opt/romarr/backends
ROMARR_PORT=${APP_PORT}
LOG_LEVEL=INFO
ENVEOF
chmod 600 /opt/romarr/.env
mkdir -p ${ROM_PATH} /opt/romarr/backends
"

info "Creating service"
pct exec "$CTID" -- bash -c "
cat >/etc/systemd/system/romarr.service <<'SVCEOF'
[Unit]
Description=ROMarr - the *arr for games
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/romarr
EnvironmentFile=/opt/romarr/.env
ExecStart=/opt/romarr/.venv/bin/python -m romarr
Restart=on-failure
RestartSec=10

# It reads indexers and writes ROMs into one directory; nothing else on the
# filesystem is any of its business.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/romarr ${ROM_PATH}

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable -q --now romarr
"
msg "Service created"

# --- prove it actually came up ---------------------------------------------

info "Waiting for $APP to answer"
UP=0
for _ in $(seq 1 30); do
  if pct exec "$CTID" -- curl -fsS -o /dev/null \
       "http://127.0.0.1:${APP_PORT}/api/health" 2>/dev/null; then
    UP=1
    break
  fi
  sleep 2
done

IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')

if [[ "$UP" -ne 1 ]]; then
  echo
  die "$APP was installed but is not answering on port ${APP_PORT}.
   Look at:  pct exec ${CTID} -- journalctl -u romarr -n 50 --no-pager"
fi

echo
msg "${APP} is up"
echo -e " ${GN}➜${CL} http://${IP}:${APP_PORT}"
echo -e " ${YW}The first visit asks you to set a password.${CL}"
echo
echo -e " Update later:  pct exec ${CTID} -- bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/${REPO}/main/proxmox/ct/update.sh)\""
