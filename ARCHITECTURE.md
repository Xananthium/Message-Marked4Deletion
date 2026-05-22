# Agent in a Box — Architecture

Single platform: BACKEND. One Python script + systemd timer. No services, no APIs, no abstractions.

## Files

- `/home/discnxt/aib/poller.py` (~870 lines) — the entire runtime.
- `/etc/systemd/system/aib-poller.service` + `aib-poller.timer` — one-shot + 5-min timer.
- `/home/discnxt/aib/.env.example` — DSN, SA path, mailbox, operator email.
- `/home/discnxt/.secrets/paperclip-poller-api.env` — paperclip DSN, API key, company id.

## Databases

Two databases, on purpose.

| DB | role |
|---|---|
| `agentinabox` (legacy) | `pending_emails` retry table only |
| `paperclip` | `customers`, `domains`, `issues`, `issue_comments` — the system of record |

The poller opens one connection to each. `customer_sites` (legacy) is not touched.

## `customers` + `domains` schema (paperclip db)

The poller reads from:
```sql
customers(id, email, name, business_name, status)
domains(id, customer_id, fqdn, contabo_path, agent_mailbox, status, updated_at)
```

On inbound email, it joins these tables (LEFT JOIN so a customer without a domain still matches),
preferring the domain whose `agent_mailbox` matches the inbound mailbox.

## `pending_emails` schema (agentinabox db)

```sql
pending_emails(
  id BIGSERIAL PK, gmail_msg_id TEXT UNIQUE NOT NULL, sender TEXT, subject TEXT,
  reason TEXT, created_at TIMESTAMPTZ DEFAULT now()
)
```

Used to prevent re-processing of messages that errored or had unknown senders.

## `poller.py` module decomposition

Flat module, no classes except small dataclasses for config/customer. Key functions:

**Gmail helpers**
- `load_config() -> Config` — reads `.env`; returns DSN, SA path, mailbox addr, operator email.
- `gmail_client(sa_path, subject) -> Resource` — google-api-python-client w/ DWD.
- `list_unread(svc, mailbox) -> list[dict]` — `users().messages().list(q='is:unread')`.
- `fetch_message(svc, mailbox, msg_id) -> dict` — parsed `{id, from, subject, body, thread_id, references}`.
- `mark_read(svc, mailbox, msg_id)` / `mark_unread(svc, mailbox, msg_id)` — label mutations.
- `forward_to_operator(svc, mailbox, operator_email, original)` — wraps and forwards unknown-sender mail.
- `parse_sender(raw_from) -> str` — bare email via `email.utils.parseaddr`.

**DB helpers**
- `record_pending(conn, msg, reason)` — INSERT into `pending_emails`.
- `fetch_pending_due(conn) -> list` — SELECT retryable pending entries.
- `clear_pending(conn, gmail_msg_id)` — DELETE after successful retry.

**Routing**
- `match_agent_by_to_header(conn, message) -> str | None` — looks up `agent_mailbox` on domains.
- `match_agent_by_keywords(subject, body) -> tuple` — keyword → agent_id fallback.

**Customer / issue**
- `lookup_customer(pc_conn, sender_email, mailbox) -> Customer | None` — LEFT JOIN query.
- `_internal_customer(sender_email) -> Customer` — synthetic customer for internal senders.
- `find_issue_by_thread(pc_conn, company_id, thread_id) -> str | None` — open issue by gmail thread.
- `append_comment(pc_conn, ...)` — add a follow-up to an existing issue.
- `create_issue_for_email(pc_conn, ...)` — create new `todo` issue with identifier DIS-N.
- `get_identifier(pc_conn, issue_id) -> str` — return `DIS-N` string for ACK reply.

**Main loop**
- `process_message(svc, conn, pc_conn, pc_api, company_id, cfg, msg_id)` — per-message state machine.
- `main()` — opens both DB conns, builds gmail svc, lists unread, loops `process_message`, exits 0.

## Orchestration sequence (per message)

1. `fetch_message` → parse sender, subject, body, thread_id.
2. `lookup_customer(sender)` → if `None`: `forward_to_operator`, `mark_read`, `record_pending(reason='unknown_sender')`, continue.
3. Known sender: `find_issue_by_thread(thread_id)`.
   - **Found** → `append_comment` on existing issue, reply "got your follow-up, added to DIS-N".
   - **Not found** → `create_issue_for_email`, reply "received, tracked as DIS-N".
4. `match_agent_by_to_header` (or keyword fallback) → set `assignee_agent_id` on new issues.
5. `mark_read`, commit both DBs.

No aider, no rsync, no Caddy reload in the poller. The paperclip issue is the system of record;
site-edit work happens via agent execution (or operator manual action) in a separate flow.

## systemd

`aib-poller.timer`: `OnBootSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true`.
`aib-poller.service`: `Type=oneshot`, `User=discnxt`, `WorkingDirectory=/home/discnxt/aib`,
`EnvironmentFile=/home/discnxt/aib/.env` + `paperclip-poller-api.env`,
`ExecStart=/usr/bin/python3 /home/discnxt/aib/poller.py`. No restart (timer drives cadence).

## Error paths (summary table)

| Condition | Action |
|---|---|
| Unknown sender | forward to operator, mark read, pending_emails(reason=unknown_sender) |
| DB error on issue create | mark unread, pending_emails(reason=exception:…) |
| Gmail API error | mark unread (message retried next tick) |
| Any exception | mark unread, log to stderr (journald), pending_emails(reason=exception:repr) |
