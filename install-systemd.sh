#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${REPO_DIR}/.env"
UNIT_SRC="${REPO_DIR}/systemd"

[[ $EUID -eq 0 ]] || { echo "ERROR: run as root (sudo $0)"; exit 1; }

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "WARNING: ${ENV_FILE} not found — service will fail to start until it exists."
fi

cp "${UNIT_SRC}/aib-poller.service" "${UNIT_SRC}/aib-poller.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aib-poller.timer
systemctl status aib-poller.timer --no-pager

echo ""
echo "Timer enabled. Manual run: sudo systemctl start aib-poller.service. Logs: journalctl -u aib-poller -f"
