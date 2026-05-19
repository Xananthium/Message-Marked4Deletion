# `/home/discnxt/aib/crm/` — Discnxt lead-pipeline helpers

The Discnxt CRM lives in Postgres (`paperclip` DB). No SaaS. Three tables run
the show: `lead_sources`, `leads`, `contact_attempts`. Converted leads become
rows in `customers` (linked via `customers.source_lead_id` and
`leads.customer_id`).

This directory holds the **operational** layer on top of that schema —
the SQL Jordan (Marketing Ops) runs every heartbeat to do real work.

The schema itself ships from `/home/discnxt/customers/schema/lead_pipeline.sql`
via DIS-92 and **must not be modified here**.

DSN: `postgres://paperclip:...@127.0.0.1:5432/paperclip`
(creds in `/home/discnxt/.secrets/paperclip-db.env` if you need them).

---

## Files

| File | Purpose |
|---|---|
| `lead-helpers.sql`        | Library of named SQL blocks (add lead, log attempt, convert, drop, disqualify). Paste a block into psql with `\set` variables. |
| `jordan-worklist.sql`     | Single SELECT — the next 20 leads due for action, ordered by `next_action_at ASC`, with a computed `next_step` column. Runs on every Jordan heartbeat. |
| `jordan-kpi-snapshot.sql` | Six queries that feed `/home/discnxt/dashboards/marketing-kpi-dashboard.py` — leads added 7d, emails sent 7d, reply rate, conversion rate, funnel snapshot, single-row summary. |
| `README.md`               | This file. |

---

## Outreach cadence (locked — see `/home/discnxt/strategy/customer-acquisition/outreach.md` L76-81)

| Day | Action | `attempt_kind` |
|---|---|---|
| T+0  | Initial cold email                    | `initial`       |
| T+7  | One-line bump                         | `followup_1`    |
| T+14 | Last-touch with teardown link         | `followup_2`    |
| T+15+ | Drop. Set `do_not_contact_until=T+180`. | — |

3 emails total, then 6-month silence. If a prospect replies to any of the 3
with anything other than "no thanks," **operator takes over the thread by
hand** (helper b4 flips stage to `replied` and `next_action_at=NOW()`).

Deliverability cap: **20/business-day** (worklist query is `LIMIT 20`).

---

## Lead sources (already seeded — do NOT re-seed)

`blog_seo, cold_email, fb_group, gbp, nextdoor, reddit, referral, walk_in`.

Look up a source UUID by slug — that's what the helpers do.

---

## How to use the helpers

Paste the block you want from `lead-helpers.sql` into a psql session,
setting variables first with `\set name 'value'`. Example — add a new
cold-email candidate:

```sql
\set business_name 'Buena Vista Coffee'
\set contact_email 'owner@buenavista.example'
\set slug 'cold_email'
\set contact_name 'Alex Owner'
\set website_url 'https://buenavista.example'
\set vertical 'coffee_cafe'
\set city 'Pittsburgh'
\set state 'PA'
-- then paste helper (a) — Add a new lead
```

After the operator sends the initial cold email today, log it with **helper
(b1)**, which advances `stage='contacted'` and `next_action_at=NOW()+7d`.

On the heartbeat where `next_action_at` is due, the worklist query surfaces
the lead with `next_step='followup_1'`. Operator (or Jordan when un-paused)
sends the bump and logs **helper (b2)**.

Same pattern for **b3** at T+14. At T+15 the worklist shows `next_step='drop'`;
run **helper (c)** with `:'new_stage'='do_not_contact'` to silence for 180d.

---

## Convert a lead -> customer

Use **helper (d)** in `lead-helpers.sql`. Pricing is locked:

| Type | `onboarding_fee_cents` | `term_amount_cents` |
|---|---|---|
| New site             | `50000` ($500) | `19500` ($195 / 3yr) |
| Migrate existing     | `70000` ($700) | `19500` ($195 / 3yr) |

The helper opens a transaction, inserts the customer row (status='active',
lifecycle='onboarding', `term_started_at=NOW()`, `term_expires_at=NOW()+3yr`,
`source_lead_id` set), updates the lead (`stage='won'`, `customer_id` set,
`next_action_at=NULL`), and commits.

---

## KPI dashboard

`/home/discnxt/dashboards/marketing-kpi-dashboard.py` writes
`/home/discnxt/dashboards/marketing-kpi.html` hourly. It already reads
`leads` for the funnel card. The richer pipeline metrics (reply rate,
emails-sent-7d, leads-added-by-source) are in `jordan-kpi-snapshot.sql` —
the snapshot SQL is ready; wiring it into the dashboard is the next
follow-up issue (DIS-11 / new) since the dashboard also has a separate
pre-existing bug (references `customers.monthly_retainer_cents`, which
the schema renamed to `term_amount_cents`).

---

## Quick reference

- Run worklist: `psql "$DSN" -f /home/discnxt/aib/crm/jordan-worklist.sql`
- Run snapshot: `psql "$DSN" -f /home/discnxt/aib/crm/jordan-kpi-snapshot.sql`
- Stage enum: `new, contacted, replied, demoing, negotiating, won, lost, do_not_contact`
- Attempt kinds: `initial, followup_1, followup_2, reply, demo_booked, demo_held, quote_sent, close_attempt, followup_other`
- Outcomes: `sent, delivered, bounced, opened, replied_positive, replied_neutral, replied_negative, no_response, meeting_set`
- Operator user_id literal: `'operator'`
- Jordan agent UUID: `49b01a5f-3df2-4f34-a5f1-d06e0a292851`
- Taylor agent UUID: `891dba11-93bf-4610-8d06-c7cc0f9320f3`
