#!/usr/bin/env bash
# provision-site.sh — AIB V1 operator onboarding one-shot
# Usage: provision-site.sh <domain> <customer_email> [--brief path/to/brief.txt]
set -euo pipefail

usage() {
  echo "Usage: $(basename "$0") <domain> <customer_email> [--brief path/to/brief.txt]" >&2
  exit 1
}

[[ $# -lt 2 ]] && usage
DOMAIN="$1"
CUSTOMER_EMAIL="$2"
BRIEF_PATH=""

shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --brief) [[ $# -lt 2 ]] && { echo "Error: --brief requires a path" >&2; exit 1; }
             BRIEF_PATH="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

if ! [[ "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]]; then
  echo "Error: invalid domain format: $DOMAIN" >&2; exit 1
fi
if ! [[ "$CUSTOMER_EMAIL" =~ [^@]+@[^@]+\.[^@]+ ]]; then
  echo "Error: invalid email format: $CUSTOMER_EMAIL" >&2; exit 1
fi
if [[ -n "$BRIEF_PATH" && ! -f "$BRIEF_PATH" ]]; then
  echo "Error: brief file not found: $BRIEF_PATH" >&2; exit 1
fi

case "$DOMAIN" in
  digitaldisconnections.com|discnxt.com|pittsburgh-geeks.com)
    echo "refuse: protected Workspace domain — $DOMAIN must not be overwritten" >&2
    exit 2 ;;
esac

ENV_FILE="$(dirname "$(realpath "$0")")/.env"
[[ -f "$ENV_FILE" ]] || { echo "Error: .env not found at $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
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
trap 'rm -rf "$TMPDIR_NC"' EXIT

echo "==> [1/5] Initialising site dir on Contabo..."
# SC2029: $DOMAIN intentionally expands locally before ssh
# shellcheck disable=SC2029
ssh "$AIB_SSH_ALIAS" "set -e
  mkdir -p /var/www/$DOMAIN && cd /var/www/$DOMAIN
  [ -d .git ] || git init -q
  [ -f index.html ] || printf '<h1>%s</h1>\n' '$DOMAIN' > index.html
  git add -A
  git -c user.email=ops@digitaldisconnections.com -c user.name='AIB Provisioner' \
      commit -qm 'init' || true"

echo "==> [2/5] Ensuring Caddy block..."
# shellcheck disable=SC2029
ssh "$AIB_SSH_ALIAS" "
  grep -q '^$DOMAIN {' /etc/caddy/Caddyfile 2>/dev/null || \
    printf '\n%s {\n    root * /var/www/%s\n    file_server\n}\n' \
      '$DOMAIN' '$DOMAIN' | sudo tee -a /etc/caddy/Caddyfile >/dev/null
  sudo caddy reload --config /etc/caddy/Caddyfile"

echo "==> [3/5] Setting Namecheap DNS (A + MX)..."
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
  echo "Error: Namecheap setHosts failed:" >&2; cat "$TMPDIR_NC/sethosts.xml" >&2; exit 1
fi

echo "==> [3/5] Setting Namecheap email forwarding..."
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
  echo "Error: Namecheap setEmailForwarding failed:" >&2; cat "$TMPDIR_NC/setfwd.xml" >&2; exit 1
fi

echo "==> [4/5] Recording in database..."
psql "$AIB_DSN" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO customer_sites(customer_email,domain,contabo_path)
   VALUES ('$CUSTOMER_EMAIL','$DOMAIN','/var/www/$DOMAIN')
   ON CONFLICT (customer_email)
   DO UPDATE SET domain=EXCLUDED.domain,
                 contabo_path=EXCLUDED.contabo_path,
                 status='active'"

echo "==> [5/5] Brief pass..."
if [[ -n "$BRIEF_PATH" ]]; then
  REMOTE_BRIEF="/var/www/$DOMAIN/.aib-brief.txt"
  scp "$BRIEF_PATH" "${AIB_SSH_ALIAS}:${REMOTE_BRIEF}"
  # shellcheck disable=SC2029
  ssh "$AIB_SSH_ALIAS" "cd /var/www/$DOMAIN && \
    aider --message-file='$REMOTE_BRIEF' \
          --model '$AIDER_MODEL' --yes --auto-commits --no-pretty
    rm -f '$REMOTE_BRIEF'"
else
  echo "    (no brief supplied — skipping aider pass)"
fi

echo "Provisioned $DOMAIN -> $CUSTOMER_EMAIL. Now send the welcome email manually."
