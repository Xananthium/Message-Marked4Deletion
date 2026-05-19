-- invoice-helpers.sql
-- DIS-8: One-time invoice generation helpers
-- Pricing (locked 2026-05-18):
--   Setup fee: $500 new site / $700 migration (one-time, activation)
--   Term renewal: $195 every 3 years (hosting $120 + domain $75)
--   SSL: free
--   AI edit agent: included
--
-- Status path: draft -> sent -> paid|overdue|void
-- Invoices stay in 'draft' until payment-rail decision (DIS-86) is made.
--
-- invoice_number scheme: 'INV-YYYY-NNNN'
--   YYYY = 4-digit year of issue
--   NNNN = zero-padded sequential counter, scoped to that year,
--          computed as MAX(existing) + 1 with a row lock to avoid races.
--   Rationale: no bigserial added so the schema stays untouched; the
--   max+1 lookup is wrapped in a single statement using a CTE + lock
--   on the customers row of the target customer so concurrent inserts
--   for the same customer serialize. Cross-customer races are still
--   possible but rare (operator-driven, one at a time today). If
--   collision-on-INSERT ever fires, retry once.

-- =============================================================
-- a) GENERATE SETUP INVOICE
-- =============================================================
-- Params (replace :customer_id, :sku, :amount_cents):
--   :customer_id   uuid    -- target customer
--   :sku           text    -- 'setup-migrate' (70000) or 'setup-new' (50000)
--   :amount_cents  integer -- 70000 or 50000 (or 0 for comped)
--
-- Comped customers ($0): line item still emitted; renderer adds COMPED banner.
--
-- Example (migration, $700):
--   psql ... -v customer_id="'a0d00ddc-1119-42fc-a968-bb908b65092d'" \
--            -v sku="'setup-migrate'" \
--            -v amount_cents=70000 \
--            -f invoice-helpers.sql --section=a

WITH next_num AS (
  SELECT 'INV-' || to_char(now(), 'YYYY') || '-' ||
         lpad((COALESCE(MAX(
           CASE WHEN invoice_number ~ ('^INV-' || to_char(now(), 'YYYY') || '-[0-9]{4}$')
                THEN substring(invoice_number from '[0-9]{4}$')::int
                ELSE 0 END
         ), 0) + 1)::text, 4, '0') AS invoice_number
  FROM invoices
),
cust AS (
  SELECT id, onboarding_fee_cents
  FROM customers
  WHERE id = :customer_id
  FOR UPDATE
)
INSERT INTO invoices (
  customer_id, invoice_number, amount_cents, status,
  line_items, issued_at, due_at, notes
)
SELECT
  cust.id,
  next_num.invoice_number,
  :amount_cents,
  'draft',
  jsonb_build_array(
    jsonb_build_object(
      'sku', :sku,
      'description', CASE :sku
        WHEN 'setup-migrate' THEN 'Site migration + initial deployment'
        WHEN 'setup-new'     THEN 'New site build + initial deployment'
        ELSE 'Setup fee'
      END,
      'amount_cents', :amount_cents
    )
  ),
  NOW(),
  NOW() + interval '14 days',
  CASE WHEN :amount_cents = 0
       THEN 'COMPED — operator-set $0 setup fee'
       ELSE NULL
  END
FROM cust, next_num
RETURNING id, invoice_number, amount_cents, status, due_at;


-- =============================================================
-- b) GENERATE 3-YEAR TERM INVOICE (RENEWAL)
-- =============================================================
-- Params:
--   :customer_id    uuid
--   :amount_cents   integer  -- typically 19500 (hosting 12000 + domain 7500)
--                            -- pass 0 for comped customers
--
-- Drives off customers.term_started_at / term_expires_at. Caller is expected
-- to update those fields separately once invoice is paid (out of scope here).
--
-- Example:
--   psql ... -v customer_id="'<uuid>'" -v amount_cents=19500 ...

WITH next_num AS (
  SELECT 'INV-' || to_char(now(), 'YYYY') || '-' ||
         lpad((COALESCE(MAX(
           CASE WHEN invoice_number ~ ('^INV-' || to_char(now(), 'YYYY') || '-[0-9]{4}$')
                THEN substring(invoice_number from '[0-9]{4}$')::int
                ELSE 0 END
         ), 0) + 1)::text, 4, '0') AS invoice_number
  FROM invoices
),
cust AS (
  SELECT id, term_amount_cents
  FROM customers
  WHERE id = :customer_id
  FOR UPDATE
)
INSERT INTO invoices (
  customer_id, invoice_number, amount_cents, status,
  line_items, issued_at, due_at, notes
)
SELECT
  cust.id,
  next_num.invoice_number,
  :amount_cents,
  'draft',
  jsonb_build_array(
    jsonb_build_object(
      'sku', 'hosting-3yr',
      'description', '3-year hosting (Contabo + Caddy + SSL + AI edit agent)',
      'amount_cents', CASE WHEN :amount_cents = 0 THEN 0 ELSE 12000 END
    ),
    jsonb_build_object(
      'sku', 'domain-3yr',
      'description', '3-year domain registration',
      'amount_cents', CASE WHEN :amount_cents = 0 THEN 0 ELSE 7500 END
    )
  ),
  NOW(),
  NOW() + interval '30 days',
  CASE WHEN :amount_cents = 0
       THEN 'COMPED — operator-set $0 renewal'
       ELSE NULL
  END
FROM cust, next_num
RETURNING id, invoice_number, amount_cents, status, due_at;


-- =============================================================
-- c) MARK INVOICE PAID
-- =============================================================
-- Params:
--   :invoice_id    uuid
--   :rail          text   -- 'stripe' or 'btcpay'
--   :external_id   text   -- Stripe invoice id (in_...) or BTCPay invoice id / txid
--
-- Stripe: writes stripe_invoice_id.
-- BTCPay: writes payment_reference (we keep stripe_invoice_id NULL for BTCPay
--         to preserve the column's semantic meaning).
-- Comped: pass :rail='comped', :external_id='COMPED'; only status flips.

UPDATE invoices
SET status            = 'paid',
    paid_at           = NOW(),
    stripe_invoice_id = CASE WHEN :rail = 'stripe' THEN :external_id ELSE stripe_invoice_id END,
    payment_reference = CASE WHEN :rail = 'btcpay' THEN :external_id ELSE payment_reference END,
    notes             = COALESCE(notes, '') ||
                        CASE WHEN notes IS NULL OR notes = '' THEN '' ELSE E'\n' END ||
                        'Paid via ' || :rail || ' at ' || NOW()::text
WHERE id = :invoice_id
RETURNING id, invoice_number, status, paid_at, stripe_invoice_id, payment_reference;


-- =============================================================
-- d) OUTSTANDING-INVOICE LIST
-- =============================================================
-- All invoices in 'sent' or 'overdue', joined to customer + primary domain.
-- Run as-is, no params.

SELECT
  i.invoice_number,
  i.status,
  i.amount_cents,
  i.issued_at,
  i.due_at,
  CASE WHEN i.due_at < NOW() AND i.status = 'sent' THEN true ELSE false END AS is_overdue,
  c.id            AS customer_id,
  c.email         AS customer_email,
  c.business_name,
  d.fqdn
FROM invoices i
JOIN customers c ON c.id = i.customer_id
LEFT JOIN LATERAL (
  SELECT fqdn FROM domains
  WHERE customer_id = c.id AND status = 'active'
  ORDER BY created_at ASC LIMIT 1
) d ON true
WHERE i.status IN ('sent', 'overdue')
ORDER BY i.due_at ASC NULLS LAST;


-- =============================================================
-- e) RENEWAL DUE QUERY
-- =============================================================
-- Customers whose term expires in the next 30 days AND have no unpaid
-- renewal invoice already on file. "Renewal invoice" detected by the
-- 'hosting-3yr' SKU in line_items.

SELECT
  c.id                AS customer_id,
  c.email,
  c.business_name,
  c.term_amount_cents,
  c.term_started_at,
  c.term_expires_at,
  (c.term_expires_at - NOW())::interval AS time_until_expiry
FROM customers c
WHERE c.term_expires_at IS NOT NULL
  AND c.term_expires_at < NOW() + interval '30 days'
  AND c.status = 'active'
  AND NOT EXISTS (
    SELECT 1
    FROM invoices i
    WHERE i.customer_id = c.id
      AND i.status IN ('draft', 'sent', 'overdue')
      AND i.line_items @> '[{"sku":"hosting-3yr"}]'::jsonb
  )
ORDER BY c.term_expires_at ASC;
