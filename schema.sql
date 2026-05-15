-- AIB V1 schema — idempotent
CREATE TABLE IF NOT EXISTS customer_sites (
    customer_email TEXT PRIMARY KEY,
    domain         TEXT NOT NULL UNIQUE,
    contabo_path   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_emails (
    id           BIGSERIAL PRIMARY KEY,
    gmail_msg_id TEXT UNIQUE NOT NULL,
    sender       TEXT,
    subject      TEXT,
    reason       TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);
