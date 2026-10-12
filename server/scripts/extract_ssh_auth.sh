#!/usr/bin/env bash
# extract_ssh_auth.sh — Extract SSH auth attempts from /var/log/auth.log
#
# Writes Accepted/Failed sshd entries to a file readable by the Docker
# container via the existing ./data:/app/data volume mount.
#
# Setup (run once as root or with sudo):
#   sudo crontab -e
#   # Add:  * * * * * /home/camera-user/REPOS/remote_surv_camera/server/scripts/extract_ssh_auth.sh
#
# The script must run as root (or a user in the 'adm' group) because
# /var/log/auth.log is owned by syslog:adm with mode 640.

set -euo pipefail

# --- Configuration ---
AUTH_LOG="/var/log/auth.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/../data"
OUTPUT_FILE="${DATA_DIR}/ssh_auth.log"

# Ensure data directory exists
mkdir -p "${DATA_DIR}"

# Extract only sshd Accepted/Failed/Invalid user lines (last 10000 lines for performance)
tail -n 10000 "${AUTH_LOG}" 2>/dev/null \
    | grep -E 'sshd\[.*\]:\s*(Accepted|Failed|Invalid user)' \
    > "${OUTPUT_FILE}.tmp" 2>/dev/null || true

# Atomic replace so the reader never sees a partial file
mv -f "${OUTPUT_FILE}.tmp" "${OUTPUT_FILE}"

# Make readable by the Docker container (runs as root inside)
chmod 644 "${OUTPUT_FILE}"
