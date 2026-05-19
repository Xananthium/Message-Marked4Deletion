# Billing (DIS-8)

One-time + 3-year-renewal invoicing for AIB customers. Uses the existing
`invoices` table; no subscription / dunning engine.

## Pricing (locked 2026-05-18)

| Item               | Amount | Cadence         |
|--------------------|-------:|-----------------|
| Setup (new site)   | $500   | one-time        |
| Setup (migration)  | $700   | one-time        |
| 3-year term        | $195   | every 3 years   |
| SSL                | free   | always          |
| AI edit agent      | inc.   | always          |

Year-1 total: $695 (new) / $895 (migrate). Years 2-3: $0. Month 36 renewal: $195.

Operator may comp a customer by setting `customers.onboarding_fee_cents=0`
and/or `customers.term_amount_cents=0`. The helpers + renderer respect this:
$0 invoices are valid and render with a "COMPED — no payment required" banner.

## Files

- `invoice-helpers.sql` — five named blocks (a-e): generate setup,
  generate renewal, mark paid, outstanding list, renewal-due query.
- `render-invoice.py <uuid>` — renders an invoice to HTML + TXT under
  `/var/sites/<fqdn>/invoices/<invoice_number>.{html,txt}`.

## Generate a setup invoice

```bash
DSN='postgres://paperclip:...@127.0.0.1:5432/paperclip'

# Migration ($700)
psql "$DSN" \
  -v customer_id="'<customer-uuid>'" \
  -v sku="'setup-migrate'" \
  -v amount_cents=70000 \
  -f /home/discnxt/aib/billing/invoice-helpers.sql
# (block (a) at the top runs; (b)-(e) also re-run but are read-only or
#  guarded — to run only one block, copy/paste that section into psql.)

# New site ($500)
psql "$DSN" \
  -v customer_id="'<customer-uuid>'" \
  -v sku="'setup-new'" \
  -v amount_cents=50000 \
  -f /home/discnxt/aib/billing/invoice-helpers.sql

# Comped ($0)
psql "$DSN" \
  -v customer_id="'<customer-uuid>'" \
  -v sku="'setup-migrate'" \
  -v amount_cents=0 \
  -f /home/discnxt/aib/billing/invoice-helpers.sql
```

Then render:

```bash
python3 /home/discnxt/aib/billing/render-invoice.py <invoice-uuid>
```

## Generate a 3-year renewal invoice

```bash
psql "$DSN" \
  -v customer_id="'<uuid>'" \
  -v amount_cents=19500 \
  -f /home/discnxt/aib/billing/invoice-helpers.sql
# (run block (b) — copy/paste section in practice)
```

Comped renewals: pass `amount_cents=0`. Line items still emit (with 0
amounts) so the audit trail records what would have been billed.

After payment, also update `customers.term_started_at = NOW()` and
`customers.term_expires_at = NOW() + interval '3 years'` (out of scope
for this layer; do it manually for now).

## Mark an invoice paid

Three rails supported in the schema:

- **Stripe** — sets `stripe_invoice_id`:
  ```sql
  -- block (c)
  :invoice_id='<uuid>' :rail='stripe' :external_id='in_1...'
  ```
- **BTCPay** — sets `payment_reference`:
  ```sql
  :invoice_id='<uuid>' :rail='btcpay' :external_id='<btcpay-invoice-id>'
  ```
- **Comped** — only the status flips:
  ```sql
  :invoice_id='<uuid>' :rail='comped' :external_id='COMPED'
  ```

The rail-specific column choice is intentional so `\d invoices` stays
self-documenting; no JSONB metadata column needed.

## Where invoices live

`/var/sites/<fqdn>/invoices/INV-YYYY-NNNN.{html,txt}` — per the two-layer
memory rule (workers→postgres+pgvector, customers→flat files under
`/var/sites/<fqdn>/`).

## Operator email-send pattern (manual)

Until the email service (DIS-138) is active:

1. Generate + render the invoice (steps above).
2. Open `/var/sites/<fqdn>/invoices/INV-YYYY-NNNN.txt`.
3. Paste into a Gmail compose; attach the `.html` if you want.
4. Send from `cass@digitaldisconnections.com`.
5. Flip the invoice from `draft` to `sent`:
   ```sql
   UPDATE invoices SET status='sent' WHERE id='<uuid>';
   ```

## Pending wiring (waits on DIS-86)

- Replace the HTML "[TBD payment link]" placeholder with a real Stripe
  Payment Link or BTCPay invoice URL.
- Automate the `paid_at` flip via webhook.
- Auto-cut renewal invoices 30 days before `term_expires_at` (query (e)
  identifies them; the cron driver is not built).
