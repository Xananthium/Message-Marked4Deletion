#!/usr/bin/env bash
# backup-postgres.sh — V1 nightly Postgres backup
#
# Dumps the `paperclip` database to /home/discnxt/backups/postgres/.
# Retains the 14 most recent backups; older dumps are deleted.
# Logs to /var/log/discnxt-backup.log (JSON lines), falling back to
# $HOME/.local/state/discnxt-backup.log if /var/log isn't writable.
#
# Safe to run multiple times per day; each run produces a fresh
# timestamped dump.
set -euo pipefail

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-paperclip}"
PGDATABASE="${PGDATABASE:-paperclip}"
# Credential lives in the standard secrets file; fall back to the inline
# token used in the V1 plan if not present (matches the project DSN).
if [[ -z "${PGPASSWORD:-}" ]]; then
  if [[ -f "$HOME/.secrets/paperclip.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$HOME/.secrets/paperclip.env"; set +a
  fi
fi
PGPASSWORD="${PGPASSWORD:-3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730}"
export PGPASSWORD

BACKUP_DIR="${BACKUP_DIR:-/home/discnxt/backups/postgres}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
LOG_FILE="${BACKUP_LOG:-/var/log/discnxt-backup.log}"

mkdir -p "$BACKUP_DIR"

# Init log file (fallback if /var/log not writable)
_init_log() {
  if [[ ! -e "$LOG_FILE" ]]; then
    if ! ( umask 022 && : > "$LOG_FILE" ) 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo install -m 0644 -o "$USER" -g "$USER" /dev/null "$LOG_FILE" 2>/dev/null || true
      fi
    fi
  fi
  if [[ ! -w "$LOG_FILE" ]]; then
    LOG_FILE="$HOME/.local/state/discnxt-backup.log"
    mkdir -p "$(dirname "$LOG_FILE")"
    [[ -e "$LOG_FILE" ]] || : > "$LOG_FILE"
  fi
}

log() {
  local action="$1" exit_code="${2:-0}" detail="${3:-}"
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"ts":"%s","host":"%s","action":"%s","db":"%s","exit_code":%d,"detail":%s}\n' \
    "$ts" "$(hostname -s)" "$action" "$PGDATABASE" "$exit_code" \
    "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    >> "$LOG_FILE"
}

_init_log

# ---------------------------------------------------------------------------
# Step 1: dump
# ---------------------------------------------------------------------------
STAMP="$(date +%Y-%m-%dT%H-%M)"
OUT="$BACKUP_DIR/${PGDATABASE}-${STAMP}.dump"

log "start" 0 "out=$OUT"

if pg_dump -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -Fc -f "$OUT"; then
  size_bytes=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
  log "dump_ok" 0 "size_bytes=$size_bytes path=$OUT"
  echo "OK: $OUT ($size_bytes bytes)"
else
  rc=$?
  log "dump_fail" "$rc" "path=$OUT"
  echo "ERROR: pg_dump failed (rc=$rc)" >&2
  # leave any partial file behind for inspection
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# Step 2: retention — keep newest RETAIN_DAYS dump files, delete rest
# ---------------------------------------------------------------------------
mapfile -t old < <(ls -1t "$BACKUP_DIR"/${PGDATABASE}-*.dump 2>/dev/null | tail -n +"$((RETAIN_DAYS+1))" || true)
for f in "${old[@]:-}"; do
  [[ -z "$f" ]] && continue
  rm -f "$f"
  log "retention_delete" 0 "deleted=$f"
done

log "complete" 0 "kept_count=$(ls -1 "$BACKUP_DIR"/${PGDATABASE}-*.dump 2>/dev/null | wc -l)"
