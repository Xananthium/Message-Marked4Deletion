# AIB V1 build status — 2026-05-15

## Verdict
YELLOW — core pipeline proven end-to-end; one blocking defect (aider PATH in systemd) must be fixed before enabling the timer.

## Verification results
| Check | Result | Notes |
| --- | --- | --- |
| All 8 tasks completed | pass | sqlite3 tasks: all status=completed |
| All 37 blueprints implemented | pass | blueprints table: all status=implemented |
| No TODOs / placeholders in code | pass | grep clean across all .py and .sh files |
| File inventory complete | pass | poller.py, schema.sql, init-db.sh, provision-site.sh, install-systemd.sh, .env.example, requirements.txt, systemd/*.{service,timer} all present |
| Line counts vs budget | pass | poller.py=475 (approved overage); schema.sql=17; provision-site.sh=142; systemd units=15+11 |
| python3 import poller | pass | clean — no import errors |
| bash -n all shell scripts | pass | init-db.sh, provision-site.sh, install-systemd.sh all syntax-clean |
| systemd-analyze verify | pass | both units pass with no warnings |
| agentinabox DB exists | pass | confirmed on :5432 |
| customer_sites columns match ARCHITECTURE.md | pass | customer_email PK, domain UNIQUE, contabo_path, status, created_at all present |
| pending_emails columns match ARCHITECTURE.md | pass | id BIGSERIAL PK, gmail_msg_id UNIQUE, sender, subject, reason, created_at all present |
| init-db.sh idempotent re-run | pass | exits 0; NOTICE skips on existing tables |
| aider --model ollama_chat/kimi-k2.6:cloud --help | pass | model arg accepted; OLLAMA_API_BASE warning is cosmetic only |
| LLM round-trip (aider edits+commits via Ollama cloud) | pass | 'hello' -> 'goodbye' committed in < 30 s |
| Integration test: process_message() mocked end-to-end | pass | aider committed; reply() called with correct SHA; unlock_domain() called; index.html='<h1>Goodbye World</h1>' |
| aider binary findable in systemd PATH | FAIL | aider is at ~/.local/bin/aider; systemd PATH does not include ~/.local/bin — poller.py run_aider() will FileNotFoundError in production |
| provision-site.sh step numbering | warn | echo "[3/5]" printed twice (DNS + email forwarding both labeled 3/5) — cosmetic only |
| CONTABO_SSH missing from .env.example | warn | provision-site.sh requires CONTABO_SSH but .env.example does not document it |
| aider summary line quality | warn | run_aider summary = first non-empty stdout line; currently captures aider warning text ("You can skip this check with --no-gitignore") not the actual change summary — reply body is ugly but not broken |

## Operator TODO
1. Fix aider PATH before enabling timer: either add `Environment=PATH=/home/discnxt/.local/bin:/usr/local/bin:/usr/bin:/bin` to aib-poller.service, or symlink `sudo ln -s ~/.local/bin/aider /usr/local/bin/aider`. This is blocking.
2. Create `/home/discnxt/aib/.env` from `.env.example` — fill in: AIB_DSN (full DSN with host/user if not peer-auth), AIB_SA_PATH, AIB_MAILBOX, AIB_OPERATOR_EMAIL, AIB_SSH_ALIAS, AIB_MODEL, NAMECHEAP_* values, CONTABO_IP, CONTABO_SSH (currently missing from .env.example — add it).
3. Confirm Workspace service account at AIB_SA_PATH has domain-wide delegation for `gmail.modify` and `gmail.send` scopes impersonating `team@digitaldisconnections.com`.
4. Test Contabo SSH alias: `ssh contabo echo ok` must succeed without a passphrase from the discnxt user account (the systemd service has no TTY).
5. Provision the first real customer site: `bash /home/discnxt/aib/provision-site.sh <domain> <customer_email>` — verify the DB row, Caddy block, and DNS all land cleanly.
6. Run `sudo bash /home/discnxt/aib/install-systemd.sh` to install + enable the timer. Confirm `systemctl list-timers | grep aib` shows a next trigger time.
7. Send a test email from the customer address to `team@digitaldisconnections.com`; watch `journalctl -u aib-poller -f` across the next tick.

## Known gaps / deferred
- reply() summary line will include aider warning text ("You can skip this check with --no-gitignore") when OLLAMA_API_BASE is unset — add `Environment=OLLAMA_API_BASE=http://127.0.0.1:11434` to the service file or filter stdout in run_aider().
- provision-site.sh step numbering has a copy-paste error (two "[3/5]" labels) — cosmetic only, no functional impact.
- CONTABO_SSH not in .env.example — add `CONTABO_SSH=contabo` to the example before first real deployment.
- No SPF/DKIM records are set for customer domains by provision-site.sh — Namecheap email forwarding MX is set but deliverability records are not. Decide if this matters for V1.
- DB cleanup from integration test required postgres superuser; discnxt user has no DELETE on customer_sites. For production this is fine (poller only reads/inserts); for future test automation, grant DELETE in a test fixture.
