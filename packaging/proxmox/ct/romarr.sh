#!/usr/bin/env bash
_CS_DEFAULT_URL="https://raw.githubusercontent.com/community-scripts/DevScripts/main"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Copyright (c) 2021-2026 community-scripts ORG
# Author: BlizzHacker
# License: MIT | https://github.com/community-scripts/DevScripts/raw/main/LICENSE
# Source: https://github.com/BlizzHacker/romarr

APP="ROMarr"
var_tags="${var_tags:-arr;games;emulation}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
#var_arm64="${var_arm64:-no}" # unset = ask the user; set yes/no only when verified
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/romarr ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  if check_for_gh_release "romarr" "BlizzHacker/romarr"; then
    msg_info "Stopping Service"
    systemctl stop romarr
    msg_ok "Stopped Service"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "romarr" "BlizzHacker/romarr" "tarball"
    PYTHON_VERSION="3.13" setup_uv

    msg_info "Updating ROMarr"
    $STD uv venv --python 3.13 /opt/romarr/.venv
    $STD uv pip install --python /opt/romarr/.venv/bin/python -r /opt/romarr/requirements.txt
    cat <<EOF >/opt/romarr/romarr/_version.py
__version__ = "$(cat ~/.romarr)"
EOF
    msg_ok "Updated ROMarr"

    msg_info "Starting Service"
    systemctl start romarr
    msg_ok "Started Service"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed Successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:6868${CL}"
