---
id: 01
title: Create schema.sql with customer_sites + pending_emails and init helper
platform: BACKEND
depends_on: []
files_touched: [/home/discnxt/aib/schema.sql, /home/discnxt/aib/init-db.sh]
estimate_minutes: 20
estimate_loc: 60
---

## Description
Create the Postgres schema for AIB V1: `customer_sites` (one row per onboarded site, PK on `customer_email`, UNIQUE on `domain`) and `pending_emails` (triage queue, UNIQUE on `gmail_msg_id`). Provide a one-shot shell helper that creates the `agentinabox` database if missing and applies the schema idempotently against the local Postgres 18 instance on `:5432`.

## Implementation notes
- `schema.sql` ≤ 30 lines total. Use `CREATE TABLE IF NOT EXISTS` for both tables. Match the architecture column list exactly:
  - `customer_sites(customer_email TEXT PRIMARY KEY, domain TEXT NOT NULL UNIQUE, contabo_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL DEFAULT now())`
  - `pending_emails(id BIGSERIAL PRIMARY KEY, gmail_msg_id TEXT UNIQUE NOT NULL, sender TEXT, subject TEXT, reason TEXT, created_at TIMESTAMPTZ DEFAULT now())`
- `init-db.sh` uses `psql` on the system Postgres. Steps: check DB exists (`psql -tAc "SELECT 1 FROM pg_database WHERE datname='agentinabox'"`); if absent, `createdb agentinabox`; then `psql -d agentinabox -f schema.sql`. Exit non-zero on any failure (`set -euo pipefail`).
- No ORM; raw DDL only. No extensions required (`hashtext` is built-in).

## Acceptance criteria
- [ ] `schema.sql` is ≤30 lines and creates both tables idempotently
- [ ] `init-db.sh` is executable (`chmod +x`) and creates `agentinabox` if missing
- [ ] Running `init-db.sh` twice in a row succeeds (idempotent)
- [ ] `\d customer_sites` and `\d pending_emails` show the exact columns from ARCHITECTURE.md
- [ ] No TODO comments
- [ ] All error paths handled (set -euo pipefail; psql exit codes propagated)
- [ ] No placeholder functions or fake data
