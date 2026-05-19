"""
evaluate.py — Visit one unevaluated prospect's website, classify it, save results.

Uses only stdlib urllib + html.parser (no scrapy / playwright).
Heavy lifting is in _site_classifier.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras

from _site_classifier import classify_url

log = logging.getLogger(__name__)

_DSN = (
    "postgres://paperclip:"
    "3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730"
    "@127.0.0.1:5432/paperclip"
)


def evaluate_one(conn: "psycopg2.connection | None" = None) -> str | None:
    """
    Pick the oldest unevaluated prospect, evaluate it, write results back.
    Returns business_name processed, or None if queue is empty.
    """
    own_conn = conn is None
    if own_conn:
        conn = psycopg2.connect(_DSN)

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT id, business_name, website_url
                FROM   prospects
                WHERE  status = 'unevaluated'
                ORDER  BY created_at
                LIMIT  1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if not row:
                return None

            prospect_id = row["id"]
            name = row["business_name"]
            url = row["website_url"]

            log.info("Evaluating: %s (%s)", name, url)
            result = classify_url(url)

            cur.execute(
                """
                UPDATE prospects
                SET    site_status  = %s,
                       signals      = %s,
                       status       = 'evaluated',
                       evaluated_at = %s
                WHERE  id = %s
                """,
                (
                    result["site_status"],
                    psycopg2.extras.Json(result["signals"]),
                    datetime.now(timezone.utc),
                    prospect_id,
                ),
            )
            conn.commit()
            log.info(
                "Evaluated %s: site_status=%s flags=%s",
                name, result["site_status"], result["signals"].get("flags"),
            )
            return name
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    import os
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    name = evaluate_one()
    print(f"Evaluated: {name}" if name else "No unevaluated prospects in queue.")
