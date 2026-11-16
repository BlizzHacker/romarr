#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG
# Author: BlizzHacker
# License: MIT | https://github.com/community-scripts/DevScripts/raw/main/LICENSE
# Source: https://github.com/BlizzHacker/romarr

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt install -y libarchive-tools
msg_ok "Installed Dependencies"

fetch_and_deploy_gh_release "romarr" "BlizzHacker/romarr" "tarball"
PYTHON_VERSION="3.13" setup_uv

msg_info "Installing Python Dependencies"
$STD uv venv --python 3.13 /opt/romarr/.venv
$STD uv pip install --python /opt/romarr/.venv/bin/python -r /opt/romarr/requirements.txt
cat <<EOF >/opt/romarr/romarr/_version.py
__version__ = "$(cat ~/.romarr)"
EOF
msg_ok "Installed Python Dependencies"

msg_info "Configuring ROMarr"
mkdir -p /opt/romarr_data/{backends,rom-hub} /mnt/roms
cat <<EOF >/opt/romarr_data/.env
ROMARR_DATA=/opt/romarr_data/romarr.json
ROMARR_BACKENDS_DIR=/opt/romarr_data/backends
ROM_HUB_HOME=/opt/romarr_data/rom-hub
LIBRARY_PATH=/mnt/roms
ROMARR_PORT=6868
LOG_LEVEL=INFO
EOF
chmod 600 /opt/romarr_data/.env
msg_ok "Configured ROMarr"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/romarr.service
[Unit]
Description=ROMarr
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/romarr
EnvironmentFile=/opt/romarr_data/.env
ExecStart=/opt/romarr/.venv/bin/python -m romarr
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now romarr
msg_ok "Created Service"

motd_ssh
customize
cleanup_lxc
