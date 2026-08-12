#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG
# Author: BlizzHacker
# License: MIT | https://github.com/community-scripts/ProxmoxVED/raw/main/LICENSE
# Source: https://github.com/BlizzHacker/romarr

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
# libarchive-tools provides bsdtar, which reads the .7z and .rar archives the
# disc platforms ship as. Without it every PlayStation, PS2 and Wii import
# fails on a format it cannot open. Debian's GNU tar is not a substitute.
$STD apt-get install -y python3-venv libarchive-tools
msg_ok "Installed Dependencies"

fetch_and_deploy_gh_release "romarr" "BlizzHacker/romarr" "tarball" "latest" "/opt/romarr"

# Refuse to leave an unauthenticated ROMarr on somebody's network.
#
# "latest" is whatever tag exists, and the published release can lag main by a
# long way: v0.7.0 was tagged before authentication existed at all, so a
# release-tracking installer could deploy an API open to anyone who reaches the
# port, on a service that queues downloads and writes to the filesystem. This
# script cannot fall back to main the way ct/romarr.sh does -- the framework
# owns the fetch -- so it stops instead of finishing quietly.
if [[ ! -f /opt/romarr/romarr/auth.py ]]; then
  msg_error "The published release has no romarr/auth.py -- it predates authentication. Install from https://github.com/BlizzHacker/romarr/blob/main/proxmox/ct/romarr.sh instead, which falls back to main."
  exit 1
fi

msg_info "Setting up ROMarr"
cd /opt/romarr
$STD python3 -m venv .venv
$STD /opt/romarr/.venv/bin/pip install --upgrade pip
$STD /opt/romarr/.venv/bin/pip install -r requirements.txt

# ROMarr talks to three services and stores nothing else. Every value here is
# blank on purpose: it starts and serves its UI with none of them reachable,
# and the Settings pages say which are missing, so a first run is never a blank
# failure. Blank rather than a plausible-looking 192.168.1.100 -- an address
# that is wrong but syntactically fine reads as configured, and the operator
# then debugs a connection instead of filling in a field.
#
# These are the same names ct/romarr.sh writes. The two used to disagree: this
# file wrote the legacy ROMM_* aliases, so an operator following the README's
# LIBRARY_* documentation edited variables their install was not reading.
cat <<EOF >/opt/romarr/.env
PROWLARR_URL=
PROWLARR_API_KEY=

QBITTORRENT_URL=
QBITTORRENT_USER=
QBITTORRENT_PASS=

# LIBRARY_KIND is romm, gaseous, retrom or folder. \`folder\` needs no URL and
# no account -- only LIBRARY_PATH.
LIBRARY_KIND=romm
LIBRARY_URL=
LIBRARY_USERNAME=
LIBRARY_PASSWORD=

# Where imported ROMs are filed. This must be a path your library server also
# scans, or the import succeeds and the game never appears.
LIBRARY_PATH=/mnt/roms
ROMARR_DATA=/opt/romarr/romarr.json
ROMARR_BACKENDS_DIR=/opt/romarr/backends
ROMARR_PORT=6868
LOG_LEVEL=INFO
EOF
chmod 600 /opt/romarr/.env
mkdir -p /mnt/roms /opt/romarr/backends
msg_ok "Set up ROMarr"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/romarr.service
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
ReadWritePaths=/opt/romarr /mnt/roms

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now romarr
msg_ok "Created Service"

motd_ssh
customize

msg_info "Cleaning up"
$STD apt-get -y autoremove
$STD apt-get -y autoclean
msg_ok "Cleaned"
