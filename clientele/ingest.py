"""
ingest.py — Dedupe and insert Google Places results into prospects table.

Takes output of places.search_pittsburgh(), skips rows already in DB
by gmaps_place_id, inserts new rows with status='unevaluated'.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

_DSN = (
    "postgres://paperclip:"
    "3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730"
    "@127.0.0.1:5432/paperclip"
)

# Maps Places category query substrings -> our business_category enum
_CATEGORY_MAP = {
    "coffee": "coffee-shop", "cafe": "coffee-shop",
    "roofing": "contractor", "hvac": "contractor", "plumb": "contractor",
    "account": "accounting", "bookkeep": "accounting",
    "law": "law", "attorney": "law",
    "chiropract": "medical", "dentist": "medical",
    "optometrist": "medical", "physical therapy": "medical",
}


def _normalize_category(query: str) -> str:
    lower = query.lower()
    for k, v in _CATEGORY_MAP.items():
        if k in lower:
            return v
    return "unknown"


def _fqdn(url: str | None) -> str | None:
    return urlparse(url).hostname if url else None


def ingest_places(places: list[dict[str, Any]], query_category: str) -> dict[str, int]:
    """
    Insert non-duplicate Places results into prospects. Returns {inserted, skipped}.
    """
    inserted = skipped = 0
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            for p in places:
                pid = p.get("place_id")
                if not pid:
                    skipped += 1
                    continue
                cur.execute("SELECT 1 FROM prospects WHERE gmaps_place_id=%s", (pid,))
                if cur.fetchone():
                    skipped += 1
                    continue
                url = p.get("website")
                cur.execute(
                    """
                    INSERT INTO prospects (
                        business_name, fqdn, site_status, gmaps_place_id,
                        address, business_category, website_url,
                        contact_phone, source, source_metadata
                    ) VALUES (%s,%s,'unknown',%s,%s,%s,%s,%s,'google-places',%s)
                    """,
                    (
                        p.get("name", "Unknown"),
                        _fqdn(url),
                        pid,
                        p.get("formatted_address"),
                        _normalize_category(query_category),
                        url,
                        p.get("formatted_phone_number"),
                        psycopg2.extras.Json(p),
                    ),
                )
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    log.info("ingest_places: inserted=%d skipped=%d", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}


if __name__ == "__main__":
    conn = psycopg2.connect(_DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM prospects")
        (n,) = cur.fetchone()
    conn.close()
    print(f"prospects table reachable. Current row count: {n}")
