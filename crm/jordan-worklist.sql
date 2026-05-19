-- =============================================================================
-- Jordan's daily worklist — next 20 leads due for action.
-- =============================================================================
-- Filter:  next_action_at <= NOW()
--          (do_not_contact_until IS NULL OR do_not_contact_until <= NOW())
--          stage IN ('new','contacted','replied')
-- Sort:    next_action_at ASC (oldest first)
-- Cap:     20 (matches deliverability cap @ outreach.md L78)
-- Computed `next_step` column: initial / followup_1 / followup_2 / hand_off /
--                              wait (no more touches due).
-- Run via:
--   psql "postgres://paperclip:...@127.0.0.1:5432/paperclip" \
--        -f /home/discnxt/aib/crm/jordan-worklist.sql
-- =============================================================================

SELECT
  l.id                       AS lead_id,
  l.business_name,
  l.contact_email,
  l.vertical,
  l.city,
  l.state,
  l.stage,
  l.next_action_at,
  ls.display_name            AS source,
  last.attempt_kind          AS last_attempt_kind,
  last.attempted_at          AS last_attempted_at,
  CASE
    WHEN l.stage = 'replied'                THEN 'hand_off'      -- operator owns it
    WHEN last.attempt_kind IS NULL          THEN 'initial'
    WHEN last.attempt_kind = 'initial'      THEN 'followup_1'
    WHEN last.attempt_kind = 'followup_1'   THEN 'followup_2'
    WHEN last.attempt_kind = 'followup_2'   THEN 'drop'          -- T+15: mark DNC
    ELSE                                         'wait'
  END                        AS next_step
FROM leads l
JOIN lead_sources ls ON ls.id = l.lead_source_id
LEFT JOIN LATERAL (
  SELECT ca.attempt_kind, ca.attempted_at
    FROM contact_attempts ca
   WHERE ca.lead_id = l.id
     AND ca.direction = 'outbound'
   ORDER BY ca.attempted_at DESC
   LIMIT 1
) last ON TRUE
WHERE l.next_action_at IS NOT NULL
  AND l.next_action_at <= NOW()
  AND (l.do_not_contact_until IS NULL OR l.do_not_contact_until <= NOW())
  AND l.stage IN ('new','contacted','replied')
ORDER BY l.next_action_at ASC
LIMIT 20;
