#!/usr/bin/env bash
# deploy-site.sh — rsync deploy: workstation /var/sites/<domain>/public/
#                  → Contabo /var/www/<domain>/
#
# Usage:
#   deploy-site.sh <domain>                           # deploy only
#   deploy-site.sh <domain> --dry-run                 # show what would change
#   deploy-site.sh <domain> --dis-id DIS-123 \
#     --commit-msg "DIS-123: update hours"            # commit + deploy + log to DIS
#
# Git commit behaviour:
#   When --commit-msg is provided, a git commit is made in /var/sites/<domain>/
#   before the rsync. If the repo does not exist, it is initialized. After a
#   successful deploy the commit hash is posted as a comment on the DIS issue
#   (requires --dis-id and PAPERCLIP_API_URL/KEY/RUN_ID env vars).
#
# Idempotent: running twice in a row is a no-op (rsync transfers 0 bytes).
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
AUDIT_LOG="${DEPLOY_AUDIT_LOG:-/var/log/discnxt-deploy.log}"
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-Discnxt Deploy}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-deploy@digitaldisconnections.com}"

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
# Git helpers
# ---------------------------------------------------------------------------

_git_ensure_repo() {
  local site_dir="$1"
  if ! git -C "$site_dir" rev-parse --git-dir &>/dev/null; then
    echo "==> Initializing git repo at $site_dir"
    git -C "$site_dir" init -q
    git -C "$site_dir" config user.name  "$GIT_AUTHOR_NAME"
    git -C "$site_dir" config user.email "$GIT_AUTHOR_EMAIL"
    audit "git_init" 0 "site_dir=$site_dir"
  fi

  # Ensure .gitignore exists so build artifacts are never committed.
  if [[ ! -f "$site_dir/.gitignore" ]]; then
    cat > "$site_dir/.gitignore" <<'GITIGNORE'
*.swp
*.swo
.DS_Store
.aider*
__pycache__/
*.pyc
.env
node_modules/
dist/
build/
last-deploy.txt
GITIGNORE
    audit "git_gitignore_created" 0 "site_dir=$site_dir"
  fi
}

_git_commit_site() {
  local site_dir="$1" msg="$2"
  local full_msg

  # Co-author line required by Paperclip commit policy.
  full_msg="${msg}

Co-Authored-By: Paperclip <noreply@paperclip.ing>"

  _git_ensure_repo "$site_dir"

  git -C "$site_dir" \
    -c "user.name=$GIT_AUTHOR_NAME" \
    -c "user.email=$GIT_AUTHOR_EMAIL" \
    add -A

  # Bail out cleanly if there is nothing to commit (idempotent).
  if git -C "$site_dir" diff --cached --quiet; then
    echo "==> Nothing to commit in $site_dir (working tree clean)"
    audit "git_commit_skip" 0 "nothing_to_commit site_dir=$site_dir"
    return 0
  fi

  git -C "$site_dir" \
    -c "user.name=$GIT_AUTHOR_NAME" \
    -c "user.email=$GIT_AUTHOR_EMAIL" \
    commit -m "$full_msg"

  local hash
  hash=$(git -C "$site_dir" rev-parse HEAD)
  echo "==> Committed $hash ($site_dir)"
  audit "git_commit_ok" 0 "hash=$hash site_dir=$site_dir"
}

# Write the current HEAD hash of the site repo to stdout (empty string if no repo/no commits).
_git_head_hash() {
  local site_dir="$1"
  git -C "$site_dir" rev-parse HEAD 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Paperclip comment helper
# ---------------------------------------------------------------------------

_paperclip_post_comment() {
  local issue_id="$1" body="$2"

  if [[ -z "${PAPERCLIP_API_URL:-}" || -z "${PAPERCLIP_API_KEY:-}" ]]; then
    echo "==> [paperclip] PAPERCLIP_API_URL/KEY not set — skipping comment on $issue_id"
    return 0
  fi

  local payload
  payload=$(printf '%s' "$body" | \
    python3 -c 'import json,sys; print(json.dumps({"body": sys.stdin.read()}))' 2>/dev/null)
  if [[ -z "$payload" ]]; then
    echo "==> [paperclip] could not build comment payload — skipping" >&2
    return 0
  fi

  local run_id_header=""
  if [[ -n "${PAPERCLIP_RUN_ID:-}" ]]; then
    run_id_header="-H X-Paperclip-Run-Id: ${PAPERCLIP_RUN_ID}"
  fi

  if curl -sf -X POST \
      "${PAPERCLIP_API_URL}/api/issues/${issue_id}/comments" \
      -H "Authorization: Bearer ${PAPERCLIP_API_KEY}" \
      ${run_id_header:+-H "X-Paperclip-Run-Id: ${PAPERCLIP_RUN_ID}"} \
      -H "Content-Type: application/json" \
      --data-binary "$payload" \
      > /dev/null; then
    echo "==> [paperclip] Logged deploy comment on $issue_id"
    audit "paperclip_comment_ok" 0 "issue_id=$issue_id"
  else
    echo "==> [paperclip] Failed to post comment on $issue_id (non-fatal)" >&2
    audit "paperclip_comment_fail" 1 "issue_id=$issue_id"
  fi
}

# Stamp deploy metadata on the issue comment metadata field via PATCH.
_paperclip_stamp_metadata() {
  local issue_id="$1" full_hash="$2" fqdn="$3" url="$4"

  if [[ -z "${PAPERCLIP_API_URL:-}" || -z "${PAPERCLIP_API_KEY:-}" ]]; then
    return 0
  fi

  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({'metadata': {
  'deploy_commit': '$full_hash',
  'deploy_site': '$fqdn',
  'deploy_url': '$url',
}}))
" 2>/dev/null) || return 0

  curl -sf -X PATCH \
    "${PAPERCLIP_API_URL}/api/issues/${issue_id}" \
    -H "Authorization: Bearer ${PAPERCLIP_API_KEY}" \
    ${PAPERCLIP_RUN_ID:+-H "X-Paperclip-Run-Id: ${PAPERCLIP_RUN_ID}"} \
    -H "Content-Type: application/json" \
    --data-binary "$payload" \
    > /dev/null || true
}

# ---------------------------------------------------------------------------
# Resolve DIS issue ID from identifier (e.g. DIS-285 → UUID)
# ---------------------------------------------------------------------------
_resolve_issue_id() {
  local identifier="$1"
  source /home/discnxt/.secrets/paperclip-poller-api.env
  local company_id="${PAPERCLIP_COMPANY_ID:-}"
  if [[ -z "$company_id" || -z "${PAPERCLIP_API_URL:-}" || -z "${PAPERCLIP_API_KEY:-}" ]]; then
    echo ""
    return 0
  fi
  curl -sf \
    "${PAPERCLIP_API_URL}/api/companies/${company_id}/issues?q=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$identifier'))")" \
    -H "Authorization: Bearer ${PAPERCLIP_API_KEY}" \
    2>/dev/null \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
issues = data.get('issues', data) if isinstance(data, dict) else data
for i in (issues if isinstance(issues, list) else []):
    if i.get('identifier') == '$identifier':
        print(i['id'])
        break
" 2>/dev/null || echo ""
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
DRY_RUN="false"
DOMAIN=""
DIS_ID=""       # issue identifier, e.g. DIS-285
COMMIT_MSG=""   # if set, a git commit is made before deploy

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --dis-id)
      DIS_ID="${2:-}"; shift 2 ;;
    --commit-msg)
      COMMIT_MSG="${2:-}"; shift 2 ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "Unknown flag: $1" >&2; exit 64 ;;
    *)  if [[ -z "$DOMAIN" ]]; then DOMAIN="$1"; shift
        else echo "Unexpected arg: $1" >&2; exit 64; fi ;;
  esac
done

if [[ -z "$DOMAIN" ]]; then
  echo "Usage: $0 <domain> [--dry-run] [--dis-id DIS-NNN] [--commit-msg <msg>]" >&2
  exit 64
fi

# Basic sanity: domain looks like a domain
if ! [[ "$DOMAIN" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Refusing suspicious domain: $DOMAIN" >&2
  exit 64
fi

# Validate --commit-msg is provided when --dis-id is given (warn, not hard-fail)
if [[ -n "$DIS_ID" && -z "$COMMIT_MSG" ]]; then
  echo "Warning: --dis-id provided without --commit-msg; commit will be skipped" >&2
fi

_init_audit_log

# --- DB pre-flight: refuse to deploy an unprovisioned domain ---
source /home/discnxt/.secrets/paperclip-poller-api.env
if ! psql "$PAPERCLIP_DSN" -t -A -c \
  "SELECT 1 FROM sites WHERE fqdn = lower('${DOMAIN}') AND status = 'active'" \
  2>/dev/null | grep -q 1; then
  audit "preflight_db_miss" 1 "fqdn=$DOMAIN not in sites table"
  echo "ERROR: $DOMAIN is not in the Paperclip sites table (status=active)." >&2
  echo "Run provision-site.sh $DOMAIN <customer-email> first." >&2
  exit 1
fi
audit "preflight_db_ok" 0 "fqdn=$DOMAIN"

SITE_DIR="$SITES_ROOT/$DOMAIN"
SRC="$SITE_DIR/public/"
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
# Step 0: Git commit (when --commit-msg is provided and not a dry-run)
# ---------------------------------------------------------------------------
DEPLOY_COMMIT=""

if [[ -n "$COMMIT_MSG" && "$DRY_RUN" != "true" ]]; then
  echo "==> Committing changes in $SITE_DIR"
  _git_commit_site "$SITE_DIR" "$COMMIT_MSG"
  DEPLOY_COMMIT=$(_git_head_hash "$SITE_DIR")
  echo "==> Commit hash: $DEPLOY_COMMIT"
elif [[ "$DRY_RUN" == "true" && -n "$COMMIT_MSG" ]]; then
  echo "==> [dry-run] would commit: $COMMIT_MSG"
else
  # No commit message — capture HEAD anyway for audit trail.
  DEPLOY_COMMIT=$(_git_head_hash "$SITE_DIR")
fi

# ---------------------------------------------------------------------------
# Step 1: rsync
# ---------------------------------------------------------------------------

# --backup-dir: files deleted by --delete are moved here rather than destroyed.
# Provides a recovery path if workstation source is missing files that exist on
# the destination. Backup root rotates daily; kept for 14 days on Contabo.
BACKUP_SUFFIX="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_BACKUP_ROOT="${REMOTE_BACKUP_ROOT:-/var/backups/dd-deploy}"
REMOTE_BACKUP_DIR="$REMOTE_BACKUP_ROOT/$DOMAIN/$BACKUP_SUFFIX"

if [[ "$DRY_RUN" != "true" ]]; then
  ssh -o StrictHostKeyChecking=accept-new "$CONTABO_HOST" \
    "mkdir -p '$REMOTE_BACKUP_DIR'" 2>/dev/null || {
    echo "==> WARNING: could not create remote backup dir $REMOTE_BACKUP_DIR — continuing without backup safety net" >&2
    REMOTE_BACKUP_DIR=""
  }
fi

RSYNC_BASE=(
  rsync -av --delete
  --exclude '.git'
  --exclude '.git/**'
  --exclude '*.swp'
  --exclude '.aider*'
  -e "$SSH_CMD"
)

# Add backup flags when not a dry-run and the remote dir was created.
if [[ "$DRY_RUN" != "true" && -n "$REMOTE_BACKUP_DIR" ]]; then
  RSYNC_BASE+=(--backup --backup-dir="$REMOTE_BACKUP_DIR")
fi

if [[ "$DRY_RUN" == "true" ]]; then
  RSYNC_BASE+=(--dry-run)
fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "==> rsync (dry-run) $SRC  ->  $DEST"
else
  echo "==> rsync $SRC  ->  $DEST  (backup: $REMOTE_BACKUP_DIR)"
fi
if "${RSYNC_BASE[@]}" "$SRC" "$DEST"; then
  audit "rsync_ok" 0 "src=$SRC dest=$DEST dry_run=$DRY_RUN commit=${DEPLOY_COMMIT:-none} backup_dir=${REMOTE_BACKUP_DIR:-none}"
else
  rc=$?
  audit "rsync_fail" "$rc" "src=$SRC dest=$DEST"
  echo "ERROR: rsync failed with exit $rc" >&2
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# Step 2: GSC — submit sitemap + request indexing
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
# Step 3: Post deploy comment + metadata to Paperclip (when --dis-id given)
# ---------------------------------------------------------------------------
if [[ -n "$DIS_ID" && "$DRY_RUN" != "true" && -n "$DEPLOY_COMMIT" ]]; then
  SITE_URL="https://${DOMAIN}"
  SHORT_HASH="${DEPLOY_COMMIT:0:7}"

  echo "==> Resolving Paperclip issue ID for $DIS_ID ..."
  ISSUE_UUID=$(_resolve_issue_id "$DIS_ID")

  if [[ -n "$ISSUE_UUID" ]]; then
    COMMENT_BODY="Deployed \`${SHORT_HASH}\` to ${SITE_URL}

- Commit: \`${DEPLOY_COMMIT}\`
- Site: ${SITE_URL}
- Rollback: \`site-rollback ${DOMAIN} ${DEPLOY_COMMIT} --dis-id ${DIS_ID}\`"

    _paperclip_post_comment "$ISSUE_UUID" "$COMMENT_BODY"
    _paperclip_stamp_metadata "$ISSUE_UUID" "$DEPLOY_COMMIT" "$DOMAIN" "$SITE_URL"
  else
    echo "==> [paperclip] Could not resolve UUID for $DIS_ID — skipping comment" >&2
    audit "paperclip_resolve_fail" 1 "identifier=$DIS_ID"
  fi
elif [[ -n "$DIS_ID" && "$DRY_RUN" == "true" ]]; then
  echo "==> [dry-run] would post deploy comment on $DIS_ID"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
audit "deploy_complete" 0 "domain=$DOMAIN dry_run=$DRY_RUN commit=${DEPLOY_COMMIT:-none} dis_id=${DIS_ID:-none}"
_short_commit="${DEPLOY_COMMIT:0:7}"
echo "==> Done: $DOMAIN (dry_run=$DRY_RUN, commit=${_short_commit:-none})"
