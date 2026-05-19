#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
# Load environment variables from .env (same as systemd)
set -a
source .env
set +a
exec python3 health.py