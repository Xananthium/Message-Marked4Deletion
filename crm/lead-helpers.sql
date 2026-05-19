-- =============================================================================
-- Discnxt CRM helpers — Jordan's lead pipeline library
-- =============================================================================
-- Each block is standalone. Pick the helper you need and paste into psql, or
-- adapt the placeholders (:'name') and run via:
--   psql "$DSN" -v business_name='Foo' -v slug='cold_email' \
--                -v contact_email='hi@foo.com' -f lead-helpers.sql
--
-- DSN: postgres://paperclip@127.0.0.1:5432/paperclip
-- Cadence: T+0 initial → T+7 followup_1 → T+14 followup_2 → drop @ T+15 (180d DNC)
-- Pricing: $500 new ($50000c) OR $700 migrate ($70000c) + $195 / 3yr ($19500c)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- (a) Add a new lead (cold-email candidate)
-- -----------------------------------------------------------------------------
-- Inserts a lead. Safe on dupe email via lower(contact_email) unique index.
-- Defaults: stage='new', next_action_at=NOW() (operator triggers send today).
-- Variables expected (set via \set or -v):
--   :'business_name', :'contact_email', :'slug' (lead_sources.slug),
--   :'contact_name', :'website_url', :'vertical', :'city', :'state'
-- Any nullable that's blank ('') will be normalized to NULL.
--
-- Example:
--   \set business_name 'Buena Vista Coffee'
--   \set contact_email 'owner@buenavista.example'
--   \set slug 'cold_email'
--   \set contact_name 'Alex Owner'
--   \set website_url 'https://buenavista.example'
--   \set vertical 'coffee_cafe'
--   \set city 'Pittsburgh'
--   \set state 'PA'
--
INSERT INTO leads (
  business_name, contact_name, contact_email, website_url,
  vertical, city, state,
  lead_source_id, stage, next_action_at
)
SELECT
  :'business_name',
  NULLIF(:'contact_name', ''),
  NULLIF(:'contact_email', ''),
  NULLIF(:'website_url', ''),
  NULLIF(:'vertical', ''),
  NULLIF(:'city', ''),
  NULLIF(:'state', ''),
  ls.id,
  'new',
  NOW()
FROM lead_sources ls
WHERE ls.slug = :'slug'
ON CONFLICT (lower(contact_email)) DO NOTHING
RETURNING id, business_name, contact_email, next_action_at;


-- -----------------------------------------------------------------------------
-- (b1) Log an INITIAL outbound email + advance stage to 'contacted'
-- -----------------------------------------------------------------------------
-- Variables: :'lead_id', :'subject', :'body', :'agent_id' (Jordan UUID or NULL)
WITH ins AS (
  INSERT INTO contact_attempts
    (lead_id, channel, direction, attempt_kind, subject, body,
     outcome, outcome_at, by_agent_id)
  VALUES
    (:'lead_id', 'email', 'outbound', 'initial',
     :'subject', :'body',
     'sent', NOW(), NULLIF(:'agent_id', '')::uuid)
  RETURNING lead_id
)
UPDATE leads
   SET stage = 'contacted',
       next_action_at = NOW() + INTERVAL '7 days',
       updated_at = NOW()
 WHERE id = (SELECT lead_id FROM ins)
RETURNING id, stage, next_action_at;


-- -----------------------------------------------------------------------------
-- (b2) Log followup_1 (T+7 one-line bump). Bumps next_action_at +7 days.
-- -----------------------------------------------------------------------------
WITH ins AS (
  INSERT INTO contact_attempts
    (lead_id, channel, direction, attempt_kind, subject, body,
     outcome, outcome_at, by_agent_id)
  VALUES
    (:'lead_id', 'email', 'outbound', 'followup_1',
     :'subject', :'body',
     'sent', NOW(), NULLIF(:'agent_id', '')::uuid)
  RETURNING lead_id
)
UPDATE leads
   SET next_action_at = NOW() + INTERVAL '7 days',
       updated_at = NOW()
 WHERE id = (SELECT lead_id FROM ins)
RETURNING id, stage, next_action_at;


-- -----------------------------------------------------------------------------
-- (b3) Log followup_2 (T+14 last-touch teardown). No more outbound after this.
-- -----------------------------------------------------------------------------
WITH ins AS (
  INSERT INTO contact_attempts
    (lead_id, channel, direction, attempt_kind, subject, body,
     outcome, outcome_at, by_agent_id)
  VALUES
    (:'lead_id', 'email', 'outbound', 'followup_2',
     :'subject', :'body',
     'sent', NOW(), NULLIF(:'agent_id', '')::uuid)
  RETURNING lead_id
)
UPDATE leads
   SET next_action_at = NULL,
       updated_at = NOW()
 WHERE id = (SELECT lead_id FROM ins)
RETURNING id, stage, next_action_at;


-- -----------------------------------------------------------------------------
-- (b4) Log a POSITIVE inbound reply -> stage='replied', operator takes over.
-- -----------------------------------------------------------------------------
-- For replied_neutral / replied_negative, use (b5) drop / (e) disqualify instead.
WITH ins AS (
  INSERT INTO contact_attempts
    (lead_id, channel, direction, attempt_kind, subject, body,
     outcome, outcome_at, by_agent_id)
  VALUES
    (:'lead_id', 'email', 'inbound', 'reply',
     :'subject', :'body',
     'replied_positive', NOW(), NULLIF(:'agent_id', '')::uuid)
  RETURNING lead_id
)
UPDATE leads
   SET stage = 'replied',
       next_action_at = NOW(),
       updated_at = NOW()
 WHERE id = (SELECT lead_id FROM ins)
RETURNING id, stage, next_action_at;


-- -----------------------------------------------------------------------------
-- (b5) Log a NEGATIVE / "no thanks" reply -> drop into do_not_contact for 180d.
-- -----------------------------------------------------------------------------
WITH ins AS (
  INSERT INTO contact_attempts
    (lead_id, channel, direction, attempt_kind, subject, body,
     outcome, outcome_at, by_agent_id)
  VALUES
    (:'lead_id', 'email', 'inbound', 'reply',
     :'subject', :'body',
     'replied_negative', NOW(), NULLIF(:'agent_id', '')::uuid)
  RETURNING lead_id
)
UPDATE leads
   SET stage = 'do_not_contact',
       do_not_contact_until = NOW() + INTERVAL '180 days',
       next_action_at = NULL,
       updated_at = NOW()
 WHERE id = (SELECT lead_id FROM ins)
RETURNING id, stage, do_not_contact_until;


-- -----------------------------------------------------------------------------
-- (c) Mark a lead 'lost' or 'do_not_contact' (drop after T+15 with no reply)
-- -----------------------------------------------------------------------------
-- Variables: :'lead_id', :'new_stage' ('lost' or 'do_not_contact'),
--            :'reason' (text or '')
UPDATE leads
   SET stage = :'new_stage',
       do_not_contact_until = NOW() + INTERVAL '180 days',
       next_action_at = NULL,
       disqualified_reason = NULLIF(:'reason', ''),
       updated_at = NOW()
 WHERE id = :'lead_id'
RETURNING id, stage, do_not_contact_until, disqualified_reason;


-- -----------------------------------------------------------------------------
-- (d) Convert lead -> customer (single transaction)
-- -----------------------------------------------------------------------------
-- Variables:
--   :'lead_id'             — leads.id
--   :'customer_email'      — required
--   :'customer_name'       — text or ''
--   :'business_name'       — text or '' (defaults to leads.business_name)
--   :'onboarding_fee_cents' — 50000 (new site) OR 70000 (migrate existing)
-- Pricing model: term_amount_cents = 19500 ($195 / 3-year term, locked).
BEGIN;

WITH new_cust AS (
  INSERT INTO customers (
    email, name, business_name,
    status, lifecycle_stage,
    onboarding_fee_cents, term_amount_cents,
    source, source_lead_id,
    term_started_at, term_expires_at
  )
  SELECT
    :'customer_email',
    NULLIF(:'customer_name', ''),
    COALESCE(NULLIF(:'business_name', ''), l.business_name),
    'active',
    'onboarding',
    (:'onboarding_fee_cents')::int,
    19500,
    ls.slug,
    l.id,
    NOW(),
    NOW() + INTERVAL '3 years'
  FROM leads l
  JOIN lead_sources ls ON ls.id = l.lead_source_id
  WHERE l.id = :'lead_id'
  RETURNING id, email, source_lead_id
)
UPDATE leads
   SET customer_id = new_cust.id,
       stage = 'won',
       next_action_at = NULL,
       updated_at = NOW()
  FROM new_cust
 WHERE leads.id = new_cust.source_lead_id
RETURNING leads.id AS lead_id, leads.customer_id, leads.stage;

COMMIT;


-- -----------------------------------------------------------------------------
-- (e) Disqualify a lead (bad fit, bounce, no email, etc.)
-- -----------------------------------------------------------------------------
-- Variables: :'lead_id', :'reason' (free text — required, not blank)
UPDATE leads
   SET stage = 'lost',
       disqualified_reason = :'reason',
       next_action_at = NULL,
       updated_at = NOW()
 WHERE id = :'lead_id'
RETURNING id, stage, disqualified_reason;
