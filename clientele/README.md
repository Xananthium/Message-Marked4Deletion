# clientele — Pittsburgh Geeks Prospect Pipeline

**Goal:** Build a ranked list of Pittsburgh small businesses that need website help.
Marketing Pod (Researcher + Writer) consumes this data to draft per-recipient outreach
emails sent by Mercer via `team@digitaldisconnections.com` under the Pittsburgh Geeks brand.

This pipeline **outputs prospects only**. It does not send email.

## Architecture

```
Google Places API
      |
  places.py       <- search_pittsburgh(category)
      |
  ingest.py       <- dedupes by gmaps_place_id, inserts status='unevaluated'
      |
  evaluate.py     <- visits site, classifies site_status, populates signals jsonb
      |
  prospects table <- Marketing Researcher reads status='evaluated', orders by signals
      |
  [Marketing Pod] <- Researcher enriches, Writer drafts, Mercer sends one at a time
```

Alternative ingest (no Places match): `scrape.py` — stub, not yet active.

## Running each step manually

```bash
cd /home/discnxt/aib/clientele

# 1. Confirm DB connection (zero rows expected after scaffold)
python ingest.py

# 2. Search + ingest (ONLY after operator provisions GOOGLE_PLACES_API_KEY)
python -c "from main import ingest_search; print(ingest_search('coffee shop'))"

# 3. Evaluate one prospect (visits the site, classifies it)
python evaluate.py

# 4. Evaluate a batch of 10
python -c "from main import evaluate_batch; print(evaluate_batch(10))"
```

## Secrets

`/home/discnxt/.secrets/google-places.env` — set `GOOGLE_PLACES_API_KEY=<real key>` before ingesting.

## DB schema

`/home/discnxt/customers/schema/0006_prospects.sql`

Key columns:
- `status`: unevaluated → evaluated → queued → contacted → replied → converted | dead
- `site_status`: no-site | wix | squarespace | godaddy | wordpress | static | unknown | broken
- `signals`: jsonb with `flags[]` array (no-https, mobile-broken, wix-detected, etc.)

## Where Marketing Pod reads from

```sql
SELECT * FROM prospects
WHERE  status = 'evaluated'
ORDER  BY jsonb_array_length(signals->'flags') DESC,
          last_outreach_at NULLS FIRST;
```

Researcher enriches (contact email, contact name, neighborhood detail), flips status to
`'queued'`, hands the enriched row to Writer. Writer drafts a per-recipient email issue
in Paperclip. Mercer sends after operator approval. One at a time — no blasts.

## Pricing to reference in copy

Setup: $500 new / $700 migrate (founding rate: $400 / $600, first 20 customers).
Retainer: $195 every 3 years (no monthly fees). Year-1 total: $695 or $895.
