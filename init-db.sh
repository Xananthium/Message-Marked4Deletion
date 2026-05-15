#!/usr/bin/env bash
set -euo pipefail

SCHEMA_FILE="$(dirname "$(realpath "$0")")/schema.sql"
DB_NAME="agentinabox"

# Create database if it does not exist
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    echo "Creating database ${DB_NAME}..."
    sudo -u postgres createdb "${DB_NAME}"
else
    echo "Database ${DB_NAME} already exists."
fi

# Apply schema idempotently (pipe via stdin so postgres user needs no file access)
echo "Applying schema..."
sudo -u postgres psql -d "${DB_NAME}" < "${SCHEMA_FILE}"
echo "Done."
