-- =============================================================================
-- Jordan's weekly KPI snapshot — feeds marketing-kpi-dashboard.py
-- =============================================================================
-- Six result sets returned as TSV-friendly named SELECTs. Run individually
-- (\i this file in psql to see all), or copy a single block into Python via
-- psycopg2 cur.execute().
--
-- Definitions:
--   "this week"  = last 7 days (NOW() - INTERVAL '7 days' .. NOW())
--   reply rate   = positive replies (replied_positive) within 21d of the
--                  matching 'initial' send, on the same lead, divided by
--                  initial sends in window
--   conversion   = leads with stage='won' / total leads (lifetime)
--   funnel       = count of leads grouped by stage (lifetime)
-- =============================================================================


-- (1) Leads added this week, by source
SELECT
  ls.slug          AS source_slug,
  ls.display_name  AS source_name,
  COUNT(*)::int    AS leads_added
FROM leads l
JOIN lead_sources ls ON ls.id = l.lead_source_id
WHERE l.created_at >= NOW() - INTERVAL '7 days'
GROUP BY ls.slug, ls.display_name
ORDER BY leads_added DESC, ls.slug;


-- (2) Outbound emails sent this week
SELECT
  ca.attempt_kind                AS kind,
  COUNT(*)::int                  AS emails_sent
FROM contact_attempts ca
WHERE ca.channel    = 'email'
  AND ca.direction  = 'outbound'
  AND ca.attempted_at >= NOW() - INTERVAL '7 days'
GROUP BY ca.attempt_kind
ORDER BY emails_sent DESC, ca.attempt_kind;


-- (3) Reply rate (this week's cohort of 'initial' sends)
-- Numerator: 'initial' sends in window that received a 'reply' attempt with
--            outcome='replied_positive' on the same lead within 21 days.
-- Denominator: 'initial' sends in window.
WITH initials AS (
  SELECT ca.id, ca.lead_id, ca.attempted_at
    FROM contact_attempts ca
   WHERE ca.channel='email' AND ca.direction='outbound'
     AND ca.attempt_kind='initial'
     AND ca.attempted_at >= NOW() - INTERVAL '7 days'
),
positive_replies AS (
  SELECT DISTINCT i.id AS initial_id
    FROM initials i
    JOIN contact_attempts r
      ON r.lead_id = i.lead_id
     AND r.attempt_kind = 'reply'
     AND r.outcome = 'replied_positive'
     AND r.attempted_at BETWEEN i.attempted_at
                            AND i.attempted_at + INTERVAL '21 days'
)
SELECT
  (SELECT COUNT(*) FROM initials)::int             AS initials_sent,
  (SELECT COUNT(*) FROM positive_replies)::int     AS positive_replies,
  CASE
    WHEN (SELECT COUNT(*) FROM initials) = 0 THEN NULL
    ELSE ROUND(
      (SELECT COUNT(*)::numeric FROM positive_replies)
      / (SELECT COUNT(*)::numeric FROM initials) * 100, 2)
  END                                              AS reply_rate_pct;


-- (4) Conversion rate (lifetime: won / total leads)
SELECT
  COUNT(*) FILTER (WHERE stage='won')::int   AS leads_won,
  COUNT(*)::int                              AS leads_total,
  CASE
    WHEN COUNT(*) = 0 THEN NULL
    ELSE ROUND(
      COUNT(*) FILTER (WHERE stage='won')::numeric
      / COUNT(*)::numeric * 100, 2)
  END                                        AS conversion_rate_pct
FROM leads;


-- (5) Funnel snapshot — leads by stage (lifetime)
SELECT
  stage,
  COUNT(*)::int AS n
FROM leads
GROUP BY stage
ORDER BY
  CASE stage
    WHEN 'new'             THEN 1
    WHEN 'contacted'       THEN 2
    WHEN 'replied'         THEN 3
    WHEN 'demoing'         THEN 4
    WHEN 'negotiating'     THEN 5
    WHEN 'won'             THEN 6
    WHEN 'lost'            THEN 7
    WHEN 'do_not_contact'  THEN 8
    ELSE 9
  END;


-- (6) Single-row summary (one cur.execute() / one fetchone() — easy ingest)
WITH base AS (
  SELECT
    (SELECT COUNT(*) FROM leads
       WHERE created_at >= NOW() - INTERVAL '7 days')::int      AS leads_added_7d,
    (SELECT COUNT(*) FROM contact_attempts
       WHERE channel='email' AND direction='outbound'
         AND attempted_at >= NOW() - INTERVAL '7 days')::int    AS emails_sent_7d,
    (SELECT COUNT(*) FROM leads WHERE stage='won')::int         AS leads_won,
    (SELECT COUNT(*) FROM leads)::int                           AS leads_total,
    (SELECT COUNT(*) FROM leads WHERE stage='new')::int         AS funnel_new,
    (SELECT COUNT(*) FROM leads WHERE stage='contacted')::int   AS funnel_contacted,
    (SELECT COUNT(*) FROM leads WHERE stage='replied')::int     AS funnel_replied,
    (SELECT COUNT(*) FROM leads WHERE stage='won')::int         AS funnel_won,
    (SELECT COUNT(*) FROM leads WHERE stage='lost')::int        AS funnel_lost,
    (SELECT COUNT(*) FROM leads WHERE stage='do_not_contact')::int AS funnel_dnc
)
SELECT
  leads_added_7d,
  emails_sent_7d,
  leads_won,
  leads_total,
  CASE WHEN leads_total = 0 THEN NULL
       ELSE ROUND(leads_won::numeric / leads_total::numeric * 100, 2)
  END                       AS conversion_rate_pct,
  funnel_new,
  funnel_contacted,
  funnel_replied,
  funnel_won,
  funnel_lost,
  funnel_dnc
FROM base;
