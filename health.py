#!/usr/bin/env python3
"""
Health check for AIB poller.

Exits with 0 if PostgreSQL and Gmail API are reachable,
otherwise exits with 1 and logs error.
"""

import sys
import logging
from poller import load_config, gmail_client, _GMAIL_SCOPES

def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
    except Exception as e:
        logging.error("Failed to load config: %s", e)
        return 1

    # Check PostgreSQL connectivity
    import psycopg
    try:
        with psycopg.connect(cfg.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row is None or row[0] != 1:
                    logging.error("PostgreSQL query did not return expected result")
                    return 1
    except Exception as e:
        logging.error("PostgreSQL connection failed: %s", e)
        return 1

    # Check Gmail API (list unread, limit 1)
    try:
        svc = gmail_client(cfg.sa_path, cfg.mailbox)
        # Attempt to list one unread message; ignore result, just ensure no exception
        resp = svc.users().messages().list(userId=cfg.mailbox, q="is:unread", maxResults=1).execute()
        # If quota exceeded or auth error, exception will be raised
        messages = resp.get("messages", [])
        logging.info("Gmail API OK, %d unread messages", len(messages))
    except Exception as e:
        logging.error("Gmail API check failed: %s", e)
        return 1

    logging.info("Health check passed")
    return 0

if __name__ == "__main__":
    sys.exit(main())