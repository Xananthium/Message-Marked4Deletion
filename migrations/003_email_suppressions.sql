-- DIS-579: email_suppressions table in discnxt_ops
-- Applied 2026-05-25 by Reed

CREATE TABLE IF NOT EXISTS email_suppressions (
    id              SERIAL PRIMARY KEY,
    address_or_domain TEXT NOT NULL,
    scope           TEXT NOT NULL CHECK (scope IN ('domain', 'address')),
    reason          TEXT NOT NULL,
    status_code     TEXT,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_suppressions_lookup
    ON email_suppressions (address_or_domain, scope);

CREATE INDEX IF NOT EXISTS idx_suppressions_expires
    ON email_suppressions (expires_at)
    WHERE expires_at IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON email_suppressions TO paperclip;
GRANT USAGE, SELECT ON SEQUENCE email_suppressions_id_seq TO paperclip;
