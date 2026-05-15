---
id: 04
title: Implement DB helpers in poller.py (lookup, advisory lock, pending_emails)
platform: BACKEND
depends_on: [01, 02]
files_touched: [/home/discnxt/aib/poller.py]
estimate_minutes: 30
estimate_loc: 70
---

## Description
Add psycopg-backed helpers to `poller.py`: site lookup by sender email, per-domain advisory lock acquire/release using `hashtext(domain)`, and an insert into `pending_emails` that is safe to call repeatedly for the same `gmail_msg_id`.

## Implementation notes
- Imports: `import psycopg`, `from dataclasses import dataclass`.
- `@dataclass(frozen=True) class Site: customer_email: str; domain: str; contabo_path: str; status: str`.
- `def lookup_site(conn: psycopg.Connection, sender_email: str) -> Site | None`: `SELECT customer_email, domain, contabo_path, status FROM customer_sites WHERE customer_email = %s AND status = 'active'`. Return `Site(*row)` or `None`. Use `with conn.cursor() as cur`.
- `def try_lock_domain(conn, domain: str) -> bool`: `SELECT pg_try_advisory_lock(hashtext(%s))`; fetchone()[0]. Caller must call `unlock_domain` on the same connection in a `finally`.
- `def unlock_domain(conn, domain: str)`: `SELECT pg_advisory_unlock(hashtext(%s))`. Log a warning (not raise) if result is False.
- `def record_pending(conn, msg: dict, reason: str)`: `INSERT INTO pending_emails (gmail_msg_id, sender, subject, reason) VALUES (%s,%s,%s,%s) ON CONFLICT (gmail_msg_id) DO UPDATE SET reason = EXCLUDED.reason, created_at = now()`. Always `conn.commit()` after.
- All helpers use a single connection passed in by `main()` (no internal connect/close); they must not swallow `psycopg.Error` — let it bubble so the per-message handler can catch and `mark_unread`.
- Connection construction lives in `main()` (next task): `psycopg.connect(cfg.dsn, autocommit=False)`.

## Acceptance criteria
- [ ] `Site` dataclass matches the 4 columns selected
- [ ] `lookup_site` returns `None` for unknown / non-active senders, `Site` otherwise
- [ ] `try_lock_domain` + `unlock_domain` form a balanced pair on the same connection
- [ ] `record_pending` is idempotent on `gmail_msg_id` (ON CONFLICT clause)
- [ ] `record_pending` commits its transaction
- [ ] `psycopg.Error` is not swallowed; bubbles to `process_message`
- [ ] No TODO comments
- [ ] All error paths handled
- [ ] No placeholder functions or fake data
