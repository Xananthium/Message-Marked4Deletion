# Agent in a Box V1 — Architecture

Single platform: BACKEND. One Python script + one shell script + systemd timer + one SQL table. No services, no APIs, no abstractions.

## Files

- `/home/discnxt/aib/poller.py` (~200 lines) — the entire runtime.
- `/home/discnxt/aib/schema.sql` (~30 lines) — `customer_sites` + `pending_emails`.
- `/home/discnxt/aib/provision-site.sh` (~150 lines) — operator onboarding one-shot.
- `/etc/systemd/system/aib-poller.service` + `aib-poller.timer` (~30 lines).
- `/home/discnxt/aib/.env.example` (~20 lines) — DSN, SA path, ssh alias, mailbox, model.

## `customer_sites` schema

```
customer_sites(
  customer_email TEXT PRIMARY KEY,
  domain         TEXT NOT NULL UNIQUE,
  contabo_path   TEXT NOT NULL,   -- e.g. /var/www/customerdomain.com
  status         TEXT NOT NULL DEFAULT 'active',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
pending_emails(
  id BIGSERIAL PK, gmail_msg_id TEXT UNIQUE NOT NULL, sender TEXT, subject TEXT,
  reason TEXT, created_at TIMESTAMPTZ DEFAULT now()
)
```

Lock key = `hashtext(domain)` used with `pg_try_advisory_lock` to serialize per-domain.

## `poller.py` module decomposition

Flat module, no classes except a small dataclass for config. Functions:

- `load_config() -> Config` — reads `.env`; returns DSN, SA path, mailbox addr, ssh alias, model, tmp root.
- `gmail_client(sa_path, subject) -> Resource` — google-api-python-client w/ DWD subject = mailbox.
- `list_unread(svc, mailbox) -> list[dict]` — `users().messages().list(q='is:unread')`.
- `fetch_message(svc, mailbox, msg_id) -> Message` — returns parsed `{id, from, subject, body, thread_id, references}`.
- `parse_sender(raw_from) -> str` — extract bare email via `email.utils.parseaddr`.
- `mark_read(svc, mailbox, msg_id)` / `mark_unread(svc, mailbox, msg_id)` — label mutations.
- `lookup_site(conn, sender_email) -> Site | None` — SELECT on `customer_sites`.
- `try_lock_domain(conn, domain) -> bool` — `pg_try_advisory_lock(hashtext($1))`.
- `unlock_domain(conn, domain)` — paired release.
- `rsync_pull(ssh_alias, contabo_path, local_dir)` — `rsync -a --delete <alias>:path/ local/`.
- `rsync_push(local_dir, ssh_alias, contabo_path)` — reverse + checksum.
- `run_aider(local_dir, body_path, model) -> AiderResult` — subprocess: `aider --message-file=<body> --model ollama_chat/kimi-k2.6:cloud --yes --auto-commits --no-pretty`; capture stdout/stderr/returncode.
- `git_head_sha(local_dir) -> str | None` — `git rev-parse HEAD`; compare pre/post to detect "no diff".
- `caddyfile_changed(local_dir) -> bool` — diff Caddyfile path inside repo if present.
- `ssh_caddy_reload(ssh_alias)` — `ssh <alias> sudo caddy reload`.
- `reply(svc, mailbox, thread_id, references, to_addr, subject, body)` — RFC-2822 via `email.message.EmailMessage`, base64url-encoded raw + `threadId`.
- `forward_to_operator(svc, mailbox, msg)` — wraps original, sends to operator addr from `.env`.
- `record_pending(conn, msg, reason)` — INSERT into `pending_emails`.
- `process_message(svc, conn, cfg, msg)` — the per-message state machine (see sequence below).
- `main()` — opens psycopg conn, builds gmail svc, lists unread, loops `process_message`, exits 0 (timer re-fires).

## Orchestration sequence (per message)

1. `parse_sender(msg.from)` → `sender`.
2. `lookup_site(conn, sender)` → if `None`: `forward_to_operator`, `mark_unread`, `record_pending(reason='unknown_sender')`, continue.
3. `try_lock_domain(conn, site.domain)` → if `False`: leave unread (next tick retries).
4. `mark_read` (claim it) inside try/except so failures can `mark_unread`.
5. mkdtemp under cfg.tmp_root → `local_dir`.
6. `rsync_pull` → record `git_head_sha` pre.
7. Write `body` to `local_dir/.aib-msg.txt`, `run_aider`.
8. `git_head_sha` post: if unchanged or aider rc != 0 → `reply` with "couldn't apply that change; operator notified" + `record_pending(reason='aider_no_diff'|'aider_error')` + `forward_to_operator`.
9. Else `rsync_push`; if `caddyfile_changed` → `ssh_caddy_reload`.
10. `reply` with commit sha (post) + one-line summary (first line of aider stdout) on the original thread.
11. `finally: unlock_domain`, `shutil.rmtree(local_dir)`.

Any uncaught exception → `mark_unread`, log to stderr (journald captures), `record_pending(reason='exception:'+repr)`.

## systemd

`aib-poller.timer`: `OnBootSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true`.
`aib-poller.service`: `Type=oneshot`, `User=discnxt`, `WorkingDirectory=/home/discnxt/aib`, `EnvironmentFile=/home/discnxt/aib/.env`, `ExecStart=/usr/bin/python3 /home/discnxt/aib/poller.py`. No restart (timer drives cadence).

## `provision-site.sh` steps

Args: `<domain> <customer_email>`. Idempotent where cheap.

1. Validate args; require `.env` sourced for `CONTABO_SSH`, `NAMECHEAP_*`, `DSN`, `OPERATOR_EMAIL`.
2. `ssh $CONTABO_SSH "mkdir -p /var/www/$DOMAIN && cd /var/www/$DOMAIN && git init -q && [ -f index.html ] || echo '<h1>$DOMAIN</h1>' > index.html && git add -A && git -c user.email=ops@... commit -qm 'init' || true"`.
3. Append Caddy block for `$DOMAIN { root * /var/www/$DOMAIN; file_server }` to remote Caddyfile if absent; `caddy reload`.
4. Namecheap API (`requests`-via-curl): set A record `@` → Contabo IP, MX → email-forwarding hosts, enable `*@$DOMAIN` → `$customer_email` forward. Preserve existing Workspace MX on protected domains (digitaldisconnections.com, discnxt.com, pittsburgh-geeks.com) — abort if `$DOMAIN` matches.
5. `psql "$DSN" -c "INSERT INTO customer_sites(customer_email,domain,contabo_path) VALUES (...)"`.
6. Optional: initial aider design pass against `/var/www/$DOMAIN` using a brief file the operator passes via `--brief path`.
7. Print next-step hint: send the welcome email manually.

## Error paths (summary table)

| Condition | Action |
|---|---|
| Unknown sender | forward to operator, mark unread, pending_emails(reason=unknown_sender) |
| Domain locked | leave unread, return (next tick) |
| rsync pull fail | mark_unread, pending_emails(reason=rsync_pull), exception bubbles |
| aider rc!=0 or HEAD unchanged | reply apology, forward to operator, pending_emails |
| rsync push fail | mark_unread, pending_emails(reason=rsync_push) |
| caddy reload fail | reply with sha but flag "deploy may be stale", pending_emails |
| Any exception | mark_unread, pending_emails(reason=exception:repr) |

Ready for Task-Creator to break down into granular tasks.
