#!/usr/bin/env bash
# ROMarr Native Desktop & Standalone Launcher for Linux, macOS & SteamOS / Handhelds
# Zero-server setup: extracts or initializes local venv, starts ROMarr on loopback,
# and automatically opens your desktop web browser or system WebView.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Target configuration defaults
PORT="${PORT:-6868}"
DATA_DIR="${DATA_DIR:-}"
LIBRARY_DIR="${LIBRARY_DIR:-}"
VENV_DIR="${VENV_DIR:-}"
NO_BROWSER="${NO_BROWSER:-0}"

# Detect OS
OS_TYPE="$(uname -s)"
case "${OS_TYPE}" in
    Darwin)
        PLATFORM="macos"
        DEFAULT_DATA="${HOME}/Library/Application Support/ROMarr"
        DEFAULT_ROMS="${HOME}/Games/ROMs"
        ;;
    Linux)
        # Check if SteamOS / Steam Deck or handheld
        if [ -f "/etc/os-release" ] && grep -qi "steamos" /etc/os-release 2>/dev/null; then
            PLATFORM="steamos"
            # Prefer SD Card Emulation directory if mounted, else Home Emulation
            if [ -d "/run/media/mmcblk0p1/Emulation/roms" ]; then
                DEFAULT_ROMS="/run/media/mmcblk0p1/Emulation/roms"
            else
                DEFAULT_ROMS="${HOME}/Emulation/roms"
            fi
            DEFAULT_DATA="${HOME}/.local/share/ROMarr"
        elif [ -d "/home/pi/RetroPie" ]; then
            PLATFORM="retropie"
            DEFAULT_DATA="${HOME}/.local/share/ROMarr"
            DEFAULT_ROMS="${HOME}/RetroPie/roms"
        else
            PLATFORM="linux"
            DEFAULT_DATA="${XDG_DATA_HOME:-${HOME}/.local/share}/ROMarr"
            DEFAULT_ROMS="${HOME}/Games/ROMs"
        fi
        ;;
    *)
        echo "Error: Unsupported OS '${OS_TYPE}'. On Windows, please use scripts/start-windows.ps1 or ROMarr.exe." >&2
        exit 1
        ;;
esac

DATA_DIR="${DATA_DIR:-${DEFAULT_DATA}}"
LIBRARY_DIR="${LIBRARY_DIR:-${DEFAULT_ROMS}}"
VENV_DIR="${VENV_DIR:-${REPO_DIR}/.venv-${PLATFORM}}"

mkdir -p "${DATA_DIR}"
mkdir -p "${LIBRARY_DIR}"

# Locate Python 3.11+
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        if "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON_BIN="$(command -v "${candidate}")"
            break
        fi
    fi
done

if [ -z "${PYTHON_BIN}" ]; then
    echo "Error: Python 3.11 or newer is required to run ROMarr standalone." >&2
    echo "Please install Python 3.11+ or run via Docker / flatpak." >&2
    exit 1
fi

# Ensure Virtual Environment
if [ ! -f "${VENV_DIR}/bin/python" ]; then
    echo "Creating standalone environment in ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PY="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"

# Check dependencies
REQ_FILE="${REPO_DIR}/requirements.txt"
MARKER_FILE="${VENV_DIR}/.requirements.installed"

if [ ! -f "${MARKER_FILE}" ] || [ "${REQ_FILE}" -nt "${MARKER_FILE}" ]; then
    echo "Installing ROMarr dependencies into standalone environment..."
    "${VENV_PIP}" install --upgrade pip >/dev/null 2>&1 || true
    "${VENV_PIP}" install -r "${REQ_FILE}"
    touch "${MARKER_FILE}"
fi

# Save environment variables for local non-server run
export ROMARR_DATA_DIR="${DATA_DIR}"
export ROMARR_LIBRARY_DIR="${LIBRARY_DIR}"
export ROMARR_PORT="${PORT}"
export ROMARR_HOST="127.0.0.1"
export ROMARR_DESKTOP_MODE="1"

APP_URL="http://127.0.0.1:${PORT}"

echo "=========================================================="
echo " Starting ROMarr Desktop / Standalone (${PLATFORM})"
echo " Data:    ${DATA_DIR}"
echo " Library: ${LIBRARY_DIR}"
echo " URL:     ${APP_URL}"
echo "=========================================================="

# Start backend in background or foreground
if [ "${NO_BROWSER}" -eq 0 ]; then
    "${VENV_PY}" -m romarr --port "${PORT}" --data-dir "${DATA_DIR}" --library-dir "${LIBRARY_DIR}" &
    SERVER_PID=$!
    trap "kill -TERM ${SERVER_PID} 2>/dev/null || true" EXIT INT TERM

    # Wait for server to become responsive
    echo "Waiting for ROMarr to initialize..."
    for i in {1..30}; do
        if curl -s "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    # Open system browser
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "${APP_URL}" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then
        open "${APP_URL}" >/dev/null 2>&1 &
    else
        echo "Open your browser to: ${APP_URL}"
    fi

    # Wait on backend process
    wait "${SERVER_PID}"
else
    exec "${VENV_PY}" -m romarr --port "${PORT}" --data-dir "${DATA_DIR}" --library-dir "${LIBRARY_DIR}"
fi
