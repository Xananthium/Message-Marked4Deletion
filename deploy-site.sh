#!/usr/bin/env bash
# deploy-site.sh — V1 rsync deploy: workstation /var/sites/<domain>/public/
#                  → Contabo /var/www/<domain>/
#
# Usage:
#   deploy-site.sh <domain>            # real deploy
#   deploy-site.sh <domain> --dry-run  # show what would change
#
# Idempotent: running twice in a row is a no-op (rsync transfers 0 bytes).
# Caddy is reloaded ONLY if the local Caddyfile hash differs from the remote
# /etc/caddy/Caddyfile hash. Most site edits don't touch Caddy config.
#
# Audit log: /var/log/discnxt-deploy.log  (JSON lines)
# Exits non-zero on any rsync / ssh failure.
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONTABO_HOST="${CONTABO_HOST:-contabo}"
SITES_ROOT="${SITES_ROOT:-/var/sites}"
REMOTE_WWW_ROOT="${REMOTE_WWW_ROOT:-/var/www}"
LOCAL_CADDYFILE="${LOCAL_CADDYFILE:-/home/discnxt/aib/Caddyfile}"   # optional
AUDIT_LOG="${DEPLOY_AUDIT_LOG:-/var/log/discnxt-deploy.log}"

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
_init_audit_log() {
  if [[ ! -e "$AUDIT_LOG" ]]; then
    if ! ( umask 022 && : > "$AUDIT_LOG" ) 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo install -m 0644 -o "$USER" -g "$USER" /dev/null "$AUDIT_LOG" 2>/dev/null || true
      fi
    fi
  fi
  if [[ ! -w "$AUDIT_LOG" ]]; then
    AUDIT_LOG="$HOME/.local/state/discnxt-deploy.log"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    [[ -e "$AUDIT_LOG" ]] || : > "$AUDIT_LOG"
  fi
}

audit() {
  local action="$1" exit_code="${2:-0}" detail="${3:-}"
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local payload
  payload=$(printf '{"ts":"%s","operator":"%s","pid":%d,"domain":"%s","action":"%s","exit_code":%d,"dry_run":%s,"detail":%s}\n' \
    "$ts" "${USER:-unknown}" "$$" "${DOMAIN:-}" "$action" "$exit_code" "${DRY_RUN:-false}" \
    "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')")
  printf '%s' "$payload" >> "$AUDIT_LOG" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
DRY_RUN="false"
DOMAIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "Unknown flag: $1" >&2; exit 64 ;;
    *)  if [[ -z "$DOMAIN" ]]; then DOMAIN="$1"; shift
        else echo "Unexpected arg: $1" >&2; exit 64; fi ;;
  esac
done

if [[ -z "$DOMAIN" ]]; then
  echo "Usage: $0 <domain> [--dry-run]" >&2
  exit 64
fi

# Basic sanity: domain looks like a domain
if ! [[ "$DOMAIN" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Refusing suspicious domain: $DOMAIN" >&2
  exit 64
fi

_init_audit_log

SRC="$SITES_ROOT/$DOMAIN/public/"
DEST="$CONTABO_HOST:$REMOTE_WWW_ROOT/$DOMAIN/"

if [[ ! -d "$SRC" ]]; then
  audit "src_missing" 2 "no_directory $SRC"
  echo "ERROR: source directory missing: $SRC" >&2
  exit 2
fi

SSH_CMD="ssh -o StrictHostKeyChecking=accept-new"

# Stamp deploy time into the source tree so the live status page reads the
# moment-we-shipped value out of /last-deploy.txt. Skip on dry-run.
if [[ "$DRY_RUN" != "true" ]]; then
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "${SRC}last-deploy.txt"
fi

# ---------------------------------------------------------------------------
# Step 1: rsync
# ---------------------------------------------------------------------------
RSYNC_BASE=(
  rsync -av --delete
  --exclude '.git'
  --exclude '.git/**'
  --exclude '*.swp'
  --exclude '.aider*'
  -e "$SSH_CMD"
)

if [[ "$DRY_RUN" == "true" ]]; then
  RSYNC_BASE+=(--dry-run)
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "==> rsync (dry-run) $SRC  ->  $DEST"
else
  echo "==> rsync $SRC  ->  $DEST"
fi
if "${RSYNC_BASE[@]}" "$SRC" "$DEST"; then
  audit "rsync_ok" 0 "src=$SRC dest=$DEST dry_run=$DRY_RUN"
else
  rc=$?
  audit "rsync_fail" "$rc" "src=$SRC dest=$DEST"
  echo "ERROR: rsync failed with exit $rc" >&2
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# Step 1.5: GSC sitemap + indexing (non-fatal)
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" != "true" ]]; then
  echo "==> Notifying GSC (sitemap + indexing)..."
  if python3 -m lib.gsc sitemap "$DOMAIN" > /dev/null 2>&1; then
    audit "gsc_sitemap_ok" 0 "domain=$DOMAIN"
    python3 -m lib.gsc index "$DOMAIN" > /dev/null 2>&1 && \
      audit "gsc_index_ok" 0 "domain=$DOMAIN" || \
      audit "gsc_index_skipped" 0 "indexing API may not be enabled"
  else
    audit "gsc_skipped" 0 "domain=$DOMAIN not verified in GSC (will retry)"
  fi
fi

# ---------------------------------------------------------------------------
# Step 2: Caddy reload (only if local Caddyfile changed)
# ---------------------------------------------------------------------------
if [[ -f "$LOCAL_CADDYFILE" ]]; then
  LOCAL_HASH=$(sha256sum "$LOCAL_CADDYFILE" | awk '{print $1}')
  REMOTE_HASH=$($SSH_CMD "$CONTABO_HOST" 'sudo sha256sum /etc/caddy/Caddyfile 2>/dev/null | awk "{print \$1}"' || echo "")

  if [[ -z "$REMOTE_HASH" ]]; then
    audit "caddy_hash_unreadable" 0 "could_not_read_remote_caddyfile"
    echo "==> WARN: could not read remote Caddyfile hash; skipping reload check"
  elif [[ "$LOCAL_HASH" == "$REMOTE_HASH" ]]; then
    audit "caddy_unchanged" 0 "hash=$LOCAL_HASH"
    echo "==> Caddyfile unchanged ($LOCAL_HASH); skipping reload"
  else
    echo "==> Caddyfile differs (local=$LOCAL_HASH remote=$REMOTE_HASH); deploying + reloading"
    if [[ "$DRY_RUN" == "true" ]]; then
      echo "==> [dry-run] would scp $LOCAL_CADDYFILE -> $CONTABO_HOST:/etc/caddy/Caddyfile"
      echo "==> [dry-run] would: sudo caddy reload --config /etc/caddy/Caddyfile"
      audit "caddy_reload_dryrun" 0 "would_copy_and_reload"
    else
      scp -o StrictHostKeyChecking=accept-new "$LOCAL_CADDYFILE" "$CONTABO_HOST:/tmp/Caddyfile.new"
      $SSH_CMD "$CONTABO_HOST" \
        'sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile && sudo caddy reload --config /etc/caddy/Caddyfile && rm -f /tmp/Caddyfile.new'
      audit "caddy_reloaded" 0 "new_hash=$LOCAL_HASH"
      echo "==> Caddy reloaded."
    fi
  fi
else
  audit "caddy_no_local" 0 "no_local_caddyfile_to_compare"
fi

# ---------------------------------------------------------------------------
# Step 3: GSC — submit sitemap + request indexing
# ---------------------------------------------------------------------------
GSC_SCRIPT="$(dirname "$(realpath "$0")")/lib/gsc.py"
if [[ "$DRY_RUN" != "true" && -f "$GSC_SCRIPT" ]]; then
  echo "==> Submitting sitemap to GSC..."
  if python3 -m lib.gsc sitemap "$DOMAIN" > /dev/null 2>&1; then
    audit "gsc_sitemap_ok" 0 "$DOMAIN"
    echo "==> GSC sitemap submitted."
  else
    audit "gsc_sitemap_skip" 0 "not yet verified in GSC"
    echo "==> GSC sitemap skipped (domain not yet verified in GSC)"
  fi

  echo "==> Requesting indexing..."
  if python3 -m lib.gsc index "$DOMAIN" > /dev/null 2>&1; then
    audit "gsc_index_ok" 0 "$DOMAIN"
    echo "==> Indexing request sent."
  else
    audit "gsc_index_skip" 0 "indexing API unavailable"
    echo "==> Indexing request skipped."
  fi
elif [[ "$DRY_RUN" == "true" ]]; then
  echo "==> [dry-run] would submit sitemap + request indexing to GSC"
  audit "gsc_dryrun" 0 "would_submit_sitemap_and_index"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
audit "deploy_complete" 0 "domain=$DOMAIN dry_run=$DRY_RUN"
echo "==> Done: $DOMAIN (dry_run=$DRY_RUN)"
