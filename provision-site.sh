#!/usr/bin/env bash
# provision-site.sh — AIB V1 operator onboarding one-shot
# Usage: provision-site.sh <domain> <customer_email> [--brief path/to/brief.txt]
#
# Audit log: /var/log/discnxt-provision.log  (JSON lines)
#   one line per major action with fields:
#     ts, operator, pid, domain, customer_email, action, exit_code, detail
# Companion logrotate config: /home/discnxt/aib/provision-site.logrotate
set -euo pipefail

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
AUDIT_LOG="${AIB_AUDIT_LOG:-/var/log/discnxt-provision.log}"

# Ensure audit log exists and is writable; fall back to ~/.local/state if not.
_init_audit_log() {
  if [[ ! -e "$AUDIT_LOG" ]]; then
    # Try to create with 644 owned by current user. May need sudo on first run.
    if ! ( umask 022 && : > "$AUDIT_LOG" ) 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo install -m 0644 -o "$USER" -g "$USER" /dev/null "$AUDIT_LOG" 2>/dev/null || true
      fi
    fi
  fi
  if [[ ! -w "$AUDIT_LOG" ]]; then
    AUDIT_LOG="$HOME/.local/state/discnxt-provision.log"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    [[ -e "$AUDIT_LOG" ]] || : > "$AUDIT_LOG"
  fi
}

# JSON-string-escape via python (always available on the workstation).
# Minimal JSON-string escape in pure bash (handles \, ", control chars, newlines).
_json_escape() {
  local s="$1" out="" c i
  for (( i=0; i<${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      '"')  out+='\"' ;;
      '\')  out+='\\' ;;
      $'\n') out+='\n' ;;
      $'\r') out+='\r' ;;
      $'\t') out+='\t' ;;
      *)    out+="$c" ;;
    esac
  done
  printf '"%s"' "$out"
}

log_event() {
  # log_event <action> <exit_code> [<detail>]
  local action="${1:-unknown}"
  local exit_code="${2:-0}"
  local detail="${3:-}"
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
  local op="${USER:-unknown}"
  local dom="${DOMAIN:-}"
  local cem="${CUSTOMER_EMAIL:-}"
  local detail_json; detail_json="$(_json_escape "$detail")"
  printf '{"ts":"%s","operator":"%s","pid":%d,"domain":"%s","customer_email":"%s","action":"%s","exit_code":%s,"detail":%s}\n' \
    "$ts" "$op" "$$" "$dom" "$cem" "$action" "$exit_code" "$detail_json" \
    >> "$AUDIT_LOG" 2>/dev/null || true
}

_init_audit_log

# Always log final exit, success or failure.
trap '_rc=$?; log_event "exit" "$_rc" ""; exit $_rc' EXIT

usage() {
  echo "Usage: $(basename "$0") <domain> <customer_email> [--brief path/to/brief.txt]" >&2
  exit 1
}

log_event "invocation" 0 "argv=$*"

[[ $# -lt 2 ]] && { log_event "usage_error" 1 "too few args"; usage; }
DOMAIN="$1"
CUSTOMER_EMAIL="$2"
BRIEF_PATH=""

shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --brief) [[ $# -lt 2 ]] && { echo "Error: --brief requires a path" >&2; log_event "usage_error" 1 "--brief without path"; exit 1; }
             BRIEF_PATH="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; log_event "usage_error" 1 "unknown option $1"; usage ;;
  esac
done

if ! [[ "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]]; then
  echo "Error: invalid domain format: $DOMAIN" >&2; log_event "validation_error" 1 "invalid domain $DOMAIN"; exit 1
fi
if ! [[ "$CUSTOMER_EMAIL" =~ [^@]+@[^@]+\.[^@]+ ]]; then
  echo "Error: invalid email format: $CUSTOMER_EMAIL" >&2; log_event "validation_error" 1 "invalid email $CUSTOMER_EMAIL"; exit 1
fi
if [[ -n "$BRIEF_PATH" && ! -f "$BRIEF_PATH" ]]; then
  echo "Error: brief file not found: $BRIEF_PATH" >&2; log_event "validation_error" 1 "brief not found $BRIEF_PATH"; exit 1
fi

# Audit logging (legacy syslog tap kept for backwards compatibility)
logger -t aib-provision "operator=$(whoami) domain=$DOMAIN customer_email=$CUSTOMER_EMAIL brief=${BRIEF_PATH:-none}"
log_event "args_validated" 0 "brief=${BRIEF_PATH:-none}"

case "$DOMAIN" in
  digitaldisconnections.com|discnxt.com|pittsburgh-geeks.com)
    echo "refuse: protected Workspace domain — $DOMAIN must not be overwritten" >&2
    log_event "refused_protected_domain" 2 "$DOMAIN"
    exit 2 ;;
esac

ENV_FILE="$(dirname "$(realpath "$0")")/.env"
NAMECHEAP_SECRETS="$HOME/.secrets/namecheap.env.enc.env"
[[ -f "$ENV_FILE" ]] || { echo "Error: .env not found at $ENV_FILE" >&2; log_event "config_error" 1 ".env missing"; exit 1; }
[[ -f "$NAMECHEAP_SECRETS" ]] || { echo "Error: Namecheap secrets not found at $NAMECHEAP_SECRETS" >&2; log_event "config_error" 1 "namecheap secrets missing"; exit 1; }
# shellcheck source=/dev/null
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
# shellcheck source=/dev/null
source <("$HOME/.secrets/bin/secret-source" "$NAMECHEAP_SECRETS")
set +a

: "${AIB_SSH_ALIAS:?AIB_SSH_ALIAS missing in .env}"
: "${CONTABO_IP:?CONTABO_IP missing in .env}"
: "${NAMECHEAP_API_USER:?NAMECHEAP_API_USER missing in .env}"
: "${NAMECHEAP_API_KEY:?NAMECHEAP_API_KEY missing in .env}"
: "${NAMECHEAP_USERNAME:?NAMECHEAP_USERNAME missing in .env}"
: "${NAMECHEAP_CLIENT_IP:?NAMECHEAP_CLIENT_IP missing in .env}"
: "${AIB_DSN:?AIB_DSN missing in .env}"
: "${AIB_OPERATOR_EMAIL:?AIB_OPERATOR_EMAIL missing in .env}"
AIDER_MODEL="${AIDER_MODEL:-ollama_chat/kimi-k2.6:cloud}"

SLD="$(echo "$DOMAIN" | awk -F. '{print $(NF-1)}')"
TLD="$(echo "$DOMAIN" | awk -F. '{print $NF}')"
TMPDIR_NC="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_NC"; _rc=$?; log_event "exit" "$_rc" ""; exit $_rc' EXIT

echo "==> [1/5] Initialising site dir on Contabo..."
log_event "contabo_init_start" 0 "/var/www/$DOMAIN"
# SC2029: $DOMAIN intentionally expands locally before ssh
# shellcheck disable=SC2029
if ssh "$AIB_SSH_ALIAS" "set -e
  mkdir -p /var/www/$DOMAIN && cd /var/www/$DOMAIN
  [ -d .git ] || git init -q
  [ -f index.html ] || printf '<h1>%s</h1>\n' '$DOMAIN' > index.html
  git add -A
  git -c user.email=ops@digitaldisconnections.com -c user.name='AIB Provisioner' \
      commit -qm 'init' || true"; then
  log_event "contabo_init_ok" 0 ""
else
  rc=$?; log_event "contabo_init_failed" "$rc" ""; exit $rc
fi

echo "==> [2/5] Ensuring Caddy block..."
log_event "caddy_block_start" 0 ""
# shellcheck disable=SC2029
if ssh "$AIB_SSH_ALIAS" "
  grep -q '^$DOMAIN {' /etc/caddy/Caddyfile 2>/dev/null || \
    printf '\n%s {\n    root * /var/www/%s\n    file_server\n}\n' \
      '$DOMAIN' '$DOMAIN' | sudo tee -a /etc/caddy/Caddyfile >/dev/null
  sudo caddy reload --config /etc/caddy/Caddyfile"; then
  log_event "caddy_block_ok" 0 ""
else
  rc=$?; log_event "caddy_block_failed" "$rc" ""; exit $rc
fi

echo "==> [3/5] Setting Namecheap DNS (A + MX)..."
log_event "namecheap_dns_start" 0 "SLD=$SLD TLD=$TLD A=$CONTABO_IP"
curl -s "https://api.namecheap.com/xml.response" \
  --data-urlencode "ApiUser=$NAMECHEAP_API_USER" \
  --data-urlencode "ApiKey=$NAMECHEAP_API_KEY" \
  --data-urlencode "UserName=$NAMECHEAP_USERNAME" \
  --data-urlencode "ClientIp=$NAMECHEAP_CLIENT_IP" \
  --data-urlencode "Command=namecheap.domains.dns.setHosts" \
  --data-urlencode "SLD=$SLD" \
  --data-urlencode "TLD=$TLD" \
  --data-urlencode "HostName1=@"  --data-urlencode "RecordType1=A" \
  --data-urlencode "Address1=$CONTABO_IP" --data-urlencode "TTL1=300" \
  --data-urlencode "HostName2=@"  --data-urlencode "RecordType2=MX" \
  --data-urlencode "Address2=mx1-hosting.jellyfish.systems" \
  --data-urlencode "MXPref2=10"   --data-urlencode "TTL2=1800" \
  --data-urlencode "HostName3=@"  --data-urlencode "RecordType3=MX" \
  --data-urlencode "Address3=mx2-hosting.jellyfish.systems" \
  --data-urlencode "MXPref3=20"   --data-urlencode "TTL3=1800" \
  -o "$TMPDIR_NC/sethosts.xml"
if grep -q '<Error ' "$TMPDIR_NC/sethosts.xml"; then
  echo "Error: Namecheap setHosts failed:" >&2; cat "$TMPDIR_NC/sethosts.xml" >&2
  log_event "namecheap_dns_failed" 1 "setHosts error"; exit 1
fi
log_event "namecheap_dns_ok" 0 ""

echo "==> [3/5] Setting Namecheap email forwarding..."
log_event "namecheap_fwd_start" 0 "*@$DOMAIN -> $CUSTOMER_EMAIL"
curl -s "https://api.namecheap.com/xml.response" \
  --data-urlencode "ApiUser=$NAMECHEAP_API_USER" \
  --data-urlencode "ApiKey=$NAMECHEAP_API_KEY" \
  --data-urlencode "UserName=$NAMECHEAP_USERNAME" \
  --data-urlencode "ClientIp=$NAMECHEAP_CLIENT_IP" \
  --data-urlencode "Command=namecheap.domains.dns.setEmailForwarding" \
  --data-urlencode "DomainName=$DOMAIN" \
  --data-urlencode "mailbox1=*" \
  --data-urlencode "ForwardTo1=$CUSTOMER_EMAIL" \
  -o "$TMPDIR_NC/setfwd.xml"
if grep -q '<Error ' "$TMPDIR_NC/setfwd.xml"; then
  echo "Error: Namecheap setEmailForwarding failed:" >&2; cat "$TMPDIR_NC/setfwd.xml" >&2
  log_event "namecheap_fwd_failed" 1 "setEmailForwarding error"; exit 1
fi
log_event "namecheap_fwd_ok" 0 ""

echo "==> [4/5] Recording in database..."
log_event "db_upsert_start" 0 ""
if psql "$AIB_DSN" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO customer_sites(customer_email,domain,contabo_path)
   VALUES ('$CUSTOMER_EMAIL','$DOMAIN','/var/www/$DOMAIN')
   ON CONFLICT (customer_email)
   DO UPDATE SET domain=EXCLUDED.domain,
                 contabo_path=EXCLUDED.contabo_path,
                 status='active'"; then
  log_event "db_upsert_ok" 0 ""
else
  rc=$?; log_event "db_upsert_failed" "$rc" ""; exit $rc
fi

echo "==> [5/5] Brief pass..."
if [[ -n "$BRIEF_PATH" ]]; then
  log_event "brief_pass_start" 0 "$BRIEF_PATH"
  REMOTE_BRIEF="/var/www/$DOMAIN/.aib-brief.txt"
  scp "$BRIEF_PATH" "${AIB_SSH_ALIAS}:${REMOTE_BRIEF}"
  # shellcheck disable=SC2029
  if ssh "$AIB_SSH_ALIAS" "cd /var/www/$DOMAIN && \
    aider --message-file='$REMOTE_BRIEF' \
          --model '$AIDER_MODEL' --yes --auto-commits --no-pretty
    rm -f '$REMOTE_BRIEF'"; then
    log_event "brief_pass_ok" 0 ""
  else
    rc=$?; log_event "brief_pass_failed" "$rc" ""; exit $rc
  fi
else
  echo "    (no brief supplied — skipping aider pass)"
  log_event "brief_pass_skipped" 0 "no brief"
fi

echo "Provisioned $DOMAIN -> $CUSTOMER_EMAIL. Now send the welcome email manually."
logger -t aib-provision "success domain=$DOMAIN"
log_event "provision_success" 0 "$DOMAIN -> $CUSTOMER_EMAIL"
