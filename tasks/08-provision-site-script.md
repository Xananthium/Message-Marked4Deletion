---
id: 08
title: Write provision-site.sh end-to-end onboarding script
platform: BACKEND
depends_on: [01, 02]
files_touched: [/home/discnxt/aib/provision-site.sh]
estimate_minutes: 60
estimate_loc: 150
---

## Description
The operator-facing one-shot for onboarding a new customer: creates the Contabo site dir + git repo, appends a Caddy block, sets Namecheap DNS (A + MX + catch-all forward to customer's personal inbox), and inserts the row into `customer_sites`. Must be idempotent where cheap and must refuse to mutate the three Workspace-protected domains.

## Implementation notes
- Usage: `provision-site.sh <domain> <customer_email> [--brief path/to/brief.txt]`. Validate arg count; validate domain matches `^[a-z0-9.-]+\.[a-z]{2,}$`; validate email via simple `[^@]+@[^@]+\.[^@]+` regex. Exit non-zero with `usage` on failure.
- `set -euo pipefail`; source `.env`: `set -a; source /home/discnxt/aib/.env; set +a`. Require `CONTABO_SSH CONTABO_IP NAMECHEAP_API_USER NAMECHEAP_API_KEY NAMECHEAP_USERNAME NAMECHEAP_CLIENT_IP DSN OPERATOR_EMAIL` — exit non-zero if any are unset (`: "${VAR:?missing in .env}"`).
- Protected-domain guard: `case "$DOMAIN" in digitaldisconnections.com|discnxt.com|pittsburgh-geeks.com) echo "refuse: protected Workspace domain"; exit 2 ;; esac`.
- Step 1 — Contabo site dir + git init (idempotent):
  ```
  ssh "$CONTABO_SSH" "set -e; mkdir -p /var/www/$DOMAIN && cd /var/www/$DOMAIN && \
    if [ ! -d .git ]; then git init -q; fi && \
    if [ ! -f index.html ]; then echo '<h1>$DOMAIN</h1>' > index.html; fi && \
    git add -A && \
    git -c user.email=ops@digitaldisconnections.com -c user.name='AIB Provisioner' commit -qm 'init' || true"
  ```
- Step 2 — Caddyfile block (idempotent append): on Contabo, `grep -q \"^$DOMAIN {\" /etc/caddy/Caddyfile || printf '\\n%s {\\n    root * /var/www/%s\\n    file_server\\n}\\n' \"$DOMAIN\" \"$DOMAIN\" | sudo tee -a /etc/caddy/Caddyfile`; then `sudo caddy reload --config /etc/caddy/Caddyfile`.
- Step 3 — Namecheap DNS via `curl` to `https://api.namecheap.com/xml.response`. Split `$DOMAIN` into `SLD` + `TLD` (use `awk -F. '{print $(NF-1)" "$NF}'`). Call `namecheap.domains.dns.setHosts` with: A record `Name=@ Address=$CONTABO_IP TTL=300`, MX records pointing at Namecheap free email-forwarding (`mx1-hosting.jellyfish.systems` / `mx2-hosting.jellyfish.systems` priority 10/20). Then call `namecheap.domains.dns.setEmailForwarding` mapping `*` → `$CUSTOMER_EMAIL`. Treat any `<Errors>` non-empty response as failure (`grep -q '<Errors>.*<Error' response.xml && exit 1`).
- Step 4 — DB insert (idempotent): `psql "$DSN" -v ON_ERROR_STOP=1 -c "INSERT INTO customer_sites(customer_email,domain,contabo_path) VALUES ('$CUSTOMER_EMAIL','$DOMAIN','/var/www/$DOMAIN') ON CONFLICT (customer_email) DO UPDATE SET domain=EXCLUDED.domain, contabo_path=EXCLUDED.contabo_path, status='active'"`.
- Step 5 (optional, only when `--brief` supplied): rsync the site dir down to `mktemp -d`, write `aider --message-file=<brief> --model "$AIDER_MODEL" --yes --auto-commits --no-pretty`, rsync back, ssh caddy reload only if Caddyfile in repo changed (same logic class as poller.py's `caddyfile_changed` but inlined).
- Final echo: `"Provisioned $DOMAIN → $CUSTOMER_EMAIL. Now send the welcome email manually."`.
- Total ≤150 lines.

## Acceptance criteria
- [ ] Script refuses to run against `digitaldisconnections.com`, `discnxt.com`, `pittsburgh-geeks.com` with exit 2
- [ ] Re-running with the same args succeeds (git init / Caddyfile append / DB insert all idempotent)
- [ ] Namecheap call failures (non-empty `<Errors>` in XML response) cause the script to exit non-zero
- [ ] Missing `.env` variables cause exit non-zero with the variable name in the message
- [ ] After success, `psql "$DSN" -c "SELECT * FROM customer_sites WHERE domain=…"` returns one row
- [ ] Script is ≤150 lines and executable
- [ ] `--brief path` triggers the optional initial aider design pass
- [ ] No TODO comments
- [ ] All error paths handled (`set -euo pipefail`, every external call's exit code checked)
- [ ] No placeholder functions or fake data
