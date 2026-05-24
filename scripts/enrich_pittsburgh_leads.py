#!/usr/bin/env python3
"""
DIS-188: Enrich Pittsburgh leads — dedup, verify, exclude, mark ready.

Phases:
  1. Mark duplicates for collapse (same name + phone → keep one)
  2. Mark exclusions (restaurants, national franchises, e-commerce)
  3. Reclassify 'other' vertical where possible
  4. Places API enrichment (verify, fill gaps) for priority verticals
  5. Set outreach_doc_ready = true on in-scope, enriched leads
"""

import os
import sys
import json
import time
import re
import urllib.request
import urllib.parse
import urllib.error

sys.path.insert(0, '/home/discnxt/aib/lib')

DB_URL = os.environ.get('DATABASE_URL')
if not DB_URL:
    env_path = '/home/discnxt/.secrets/paperclip-pg.env'
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('DATABASE_URL='):
                DB_URL = line.split('=', 1)[1].strip().strip("'\"")
                break

if not DB_URL:
    print("FATAL: no DATABASE_URL found")
    sys.exit(1)

import psycopg2
import psycopg2.extras

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

PLACES_API_KEY = open('/home/discnxt/.secrets/google.env').read().split('GOOGLE_PLACES_API_KEY=')[1].split()[0]

# ── National franchises (corporate marketing controls website) ──────────────
NATIONAL_FRANCHISE_NAMES = [
    'Intoxalock Ignition Interlock',
    'Sola Salon Studios',
    'Barnes & Noble',
    'Edible Arrangements',
    'KeyMe Locksmiths',
    'Caliber Collision',
    "Dunham's Sports",
    "Men's Wearhouse",
    "Gabe's",
    'Half Price Books',
    'The Exchange',
    'Minute Key',
    "Carter's",
    'Red Robin Gourmet Burgers and Brews',
    'Tropical Smoothie Cafe',
    'Rainbow Shops',
    'Mattress Warehouse',
    'Picadeli',
    'Speedway',  # gas station chain
]

# ── Restaurant/bar/pizzeria keywords for exclusion ─────────────────────────
# Note: "cafe" and "coffee" are intentionally excluded — independent coffee
# shops + cafes are #1 V1 target per outreach strategy.
RESTAURANT_PATTERNS = [
    r'\brestaurant\b', r'\bpizz[ao]\b', r'\bdiner\b', r'\beatery\b',
    r'\btavern\b', r'\bbar\s*[&+]\s*grill\w*\b',
    r'\bbrew(?:ery|ing|pub|)\b', r'\bpasta\b', r'\bsushi\b',
    r'\bhibachi\b', r'\bgyro\b', r'\bwing[s]?\b',
    r'\bpub\b', r'\bBBQ\b', r'\bbarbecue\b',
    r'\bchinese\b', r'\bmexican\b', r'\bitalian\b',
    r'\bindian\b', r'\bthai\b', r'\bjamaican\b', r'\bcaribbean\b',
    r'\bfusion\b', r'\bdonut\b', r'\bdoughnut\b',
    r'\bhoagie\b', r'\bsteak(?:house)?\b', r'\bburger\b',
    r'\bramen\b', r'\bmediterranean\b',
]

def is_restaurant(name):
    if not name:
        return False
    name_lower = name.lower()
    for pat in RESTAURANT_PATTERNS:
        if re.search(pat, name_lower):
            return True
    return False

# ── Phase 1: Mark duplicates for collapse ──────────────────────────────────
print("=== Phase 1: Flagging duplicates ===")

# True duplicates: same name + phone. Keep the one with more enrichment data.
cur.execute("""
    SELECT id, business_name, contact_phone, extra
    FROM leads
    WHERE (business_name, contact_phone) IN (
        SELECT business_name, contact_phone
        FROM leads
        WHERE contact_phone IS NOT NULL AND contact_phone != ''
        GROUP BY business_name, contact_phone
        HAVING count(*) > 1
    )
    ORDER BY business_name, contact_phone
""")
dupes = cur.fetchall()

# Group by (name, phone) and keep the one with the most filled extra fields
from collections import defaultdict
groups = defaultdict(list)
for d in dupes:
    key = (d['business_name'], d['contact_phone'])
    groups[key].append(d)

for key, items in groups.items():
    # Sort by extra depth — keep the one with more non-null keys
    items.sort(key=lambda x: sum(1 for v in (x['extra'] or {}).values() if v and v not in ('', 'none', 'n/a')), reverse=True)
    keeper = items[0]
    for dupe in items[1:]:
        cur.execute("""
            UPDATE leads
            SET notes = COALESCE(notes || '; ', '') || 'Collapsed duplicate of ' || %s,
                extra = extra || %s::jsonb
            WHERE id = %s
        """, (keeper['business_name'], json.dumps({'merged_into_id': str(keeper['id']), 'duplicate': True}), dupe['id']))
        print(f"  Collapsed {dupe['business_name']} ({dupe['id']}) → {keeper['id']}")

print(f"  Found {len(groups)} duplicate groups, processed.")

# ── Phase 2: Mark exclusions ───────────────────────────────────────────────
print("\n=== Phase 2: Marking exclusions ===")

# 2a. National franchises
for name in NATIONAL_FRANCHISE_NAMES:
    cur.execute("""
        UPDATE leads
        SET disqualified_reason = 'national_franchise',
            notes = COALESCE(notes, '') || 'National franchise — corporate marketing controls website; excluded per V1 ICP.'
        WHERE business_name = %s
          AND disqualified_reason IS NULL
    """, (name,))
    if cur.rowcount > 0:
        print(f"  Excluded {cur.rowcount} '{name}' entries (national franchise)")

# 2b. Restaurants, bars, pizzerias
cur.execute("""
    SELECT id, business_name, vertical FROM leads
    WHERE disqualified_reason IS NULL
""")
all_leads = cur.fetchall()

restaurant_count = 0
for lead in all_leads:
    if is_restaurant(lead['business_name']):
        cur.execute("""
            UPDATE leads
            SET disqualified_reason = 'restaurant',
                notes = COALESCE(notes, '') || 'Restaurant/bar — margins too thin, ops too distracted; excluded per V1 ICP.'
            WHERE id = %s AND disqualified_reason IS NULL
        """, (lead['id'],))
        restaurant_count += cur.rowcount

print(f"  Excluded {restaurant_count} restaurants/bars/pizzerias")

# 2c. Check for e-commerce patterns
cur.execute("""
    UPDATE leads
    SET disqualified_reason = 'ecommerce',
        notes = COALESCE(notes, '') || 'E-commerce — different product; excluded per V1 ICP.'
    WHERE disqualified_reason IS NULL
      AND (business_name ILIKE '%etsy%'
        OR business_name ILIKE '%amazon%'
        OR business_name ILIKE '%ebay%'
        OR business_name ILIKE '%shopify%'
        OR website_url ILIKE '%etsy.com%'
        OR website_url ILIKE '%shopify.com%'
        OR website_url ILIKE '%amazon.com%'
        OR website_url ILIKE '%ebay.com%')
""")
print(f"  Excluded {cur.rowcount} e-commerce entries")

# ── Phase 3: Set outreach_doc_ready on in-scope, enriched leads ────────────
print("\n=== Phase 3: Marking outreach_doc_ready ===")

# Set outreach_doc_ready = true on leads that:
# - Are not disqualified
# - Have a place_id (verified via Places exists)
# - Have a phone number (reachable)
# Note: contact_name is mostly junk scraped text; June will find real names
# from the website during drafting.
cur.execute("""
    UPDATE leads
    SET extra = extra || '{"outreach_doc_ready": "true"}'::jsonb
    WHERE disqualified_reason IS NULL
      AND extra->>'place_id' IS NOT NULL
      AND extra->>'place_id' != ''
      AND contact_phone IS NOT NULL
      AND contact_phone != ''
""")
print(f"  outreach_doc_ready=true for {cur.rowcount} enriched, in-scope leads")

# ── Phase 4: Places API enrichment for priority verticals missing place_ids ─
print("\n=== Phase 4: Places API enrichment ===")

# Check how many in priority verticals (services, medical) lack place_ids
cur.execute("""
    SELECT count(*) as missing
    FROM leads
    WHERE vertical IN ('services', 'medical')
      AND disqualified_reason IS NULL
      AND (extra->>'place_id' IS NULL OR extra->>'place_id' = '')
""")
missing = cur.fetchone()['missing']
print(f"  Priority leads missing place_ids: {missing}")

# Only do Places API for a limited batch in this run
BATCH_SIZE = 50

cur.execute("""
    SELECT id, business_name, street_address, city, state, postal_code, contact_phone
    FROM leads
    WHERE vertical IN ('services', 'medical')
      AND disqualified_reason IS NULL
      AND (extra->>'place_id' IS NULL OR extra->>'place_id' = '')
    LIMIT %s
""", (BATCH_SIZE,))
to_enrich = cur.fetchall()

def places_text_search_new(query):
    """Call Google Places API (New) Text Search."""
    url = 'https://places.googleapis.com/v1/places:searchText'
    payload = json.dumps({'textQuery': query, 'maxResultCount': 1}).encode()
    headers = {
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': PLACES_API_KEY,
        'X-Goog-FieldMask': 'places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount',
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data
    except Exception as e:
        print(f"    API error: {e}")
        return None

enriched_count = 0
for lead in to_enrich:
    # Build search query
    location_parts = [p for p in [lead['city'], lead['state']] if p]
    address = lead['street_address'] or ''
    query = f"{lead['business_name']} {address} {' '.join(location_parts)}"
    query = query.strip()

    if not query:
        continue

    print(f"  Looking up: {lead['business_name']}...", end=' ')
    result = places_text_search_new(query)

    if result and result.get('places'):
        place = result['places'][0]
        place_id = place.get('id', '')

        extra_update = {
            'place_id': place_id,
            'rating': str(place.get('rating', '')),
            'user_ratings_total': str(place.get('userRatingCount', '')),
            'enriched_via_places_api': 'true',
            'business_status': place.get('businessStatus', ''),
            'google_maps_url': f"https://maps.google.com/?q={urllib.parse.quote(place.get('formattedAddress', lead['business_name']))}",
            'places_types': place.get('types', []),
            'google_places': {
                'raw': place,
                'fetched_at': datetime.utcnow().isoformat() + 'Z',
                'api_version': 'places.v1',
            },
        }

        phone = place.get('nationalPhoneNumber')
        website = place.get('websiteUri')

        # Update contact_phone if missing
        if phone and (not lead['contact_phone']):
            cur.execute("UPDATE leads SET contact_phone = %s WHERE id = %s", (phone, lead['id']))

        # Update website if missing
        if website:
            cur.execute("UPDATE leads SET website_url = %s WHERE id = %s", (website, lead['id']))

        cur.execute("UPDATE leads SET extra = extra || %s::jsonb WHERE id = %s",
                    (json.dumps(extra_update), lead['id']))
        print(f"✓ place_id={place_id}")
        enriched_count += 1
    else:
        print(f"✗ not found")
        # Mark as not found
        cur.execute("UPDATE leads SET extra = extra || %s::jsonb WHERE id = %s",
                    (json.dumps({'places_lookup_attempted': 'true', 'places_not_found': 'true'}), lead['id']))

    # Rate limit: 1 request per 50ms
    time.sleep(0.1)

print(f"  Enriched {enriched_count} leads via Places API")

# ── Phase 5: Summary ───────────────────────────────────────────────────────
print("\n=== Summary ===")
cur.execute("""
    SELECT
        count(*) as total,
        count(*) FILTER (WHERE disqualified_reason IS NOT NULL) as excluded,
        count(*) FILTER (WHERE disqualified_reason = 'national_franchise') as franchise_excluded,
        count(*) FILTER (WHERE disqualified_reason = 'restaurant') as restaurant_excluded,
        count(*) FILTER (WHERE disqualified_reason = 'ecommerce') as ecom_excluded,
        count(*) FILTER (WHERE extra->>'outreach_doc_ready' = 'true') as outreach_ready,
        count(*) FILTER (WHERE extra->>'place_id' IS NOT NULL AND extra->>'place_id' != '') as with_place_id
    FROM leads
""")
row = cur.fetchone()
for k, v in row.items():
    print(f"  {k}: {v}")

cur.close()
conn.close()
print("\nDone.")
