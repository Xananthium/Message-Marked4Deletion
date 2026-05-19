#!/usr/bin/env python3
"""Uptime check worker — iterates active sites, curls each, files a Mercer
issue for any site that is down (HTTP non-2xx or connection failure).

Systemd timer: discnxt-uptime-check.timer (every 10 min, disabled until go).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("aib.uptime-check")

_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)
_COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_MERCER_AGENT_ID = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
_TIMEOUT = 10  # seconds per site
_USER_AGENT = "discnxt-uptime-check/1.0"


def _active_sites(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT fqdn FROM sites WHERE status = 'active' ORDER BY fqdn")
        return [dict(r) for r in cur.fetchall()]


def _check_site(fqdn: str) -> tuple[bool, int | None, str]:
    """Return (is_up, http_status_code_or_None, error_message_or_empty)."""
    url = f"https://{fqdn}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            code = resp.status
            return (200 <= code < 400), code, ""
    except urllib.error.HTTPError as e:
        # 4xx/5xx is a real response — site is reachable but unhealthy
        return False, e.code, f"HTTP {e.code} {e.reason}"
    except Exception as exc:
        return False, None, str(exc)


def _bump_issue_counter(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE companies SET issue_counter = issue_counter + 1 "
            "WHERE id = %s RETURNING issue_counter",
            (_COMPANY_ID,),
        )
        return cur.fetchone()[0]


def _file_issue(conn, fqdn: str, error_msg: str, http_code: int | None) -> str:
    counter = _bump_issue_counter(conn)
    identifier = f"DIS-{counter}"
    now = datetime.now(timezone.utc)
    code_str = str(http_code) if http_code is not None else "no response"
    title = f"[uptime] {fqdn} is DOWN ({code_str})"
    description = (
        f"Uptime check detected {fqdn} is unreachable.\n\n"
        f"Detected at: {now.isoformat()}\n"
        f"HTTP status: {code_str}\n"
        f"Error: {error_msg}\n\n"
        f"Please investigate and restore service."
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO issues (
                company_id, title, description, status, priority,
                assignee_agent_id, issue_number, identifier,
                origin_kind, created_at, updated_at
            ) VALUES (%s, %s, %s, 'todo', 'high', %s, %s, %s, 'worker', now(), now())
            RETURNING id
            """,
            (_COMPANY_ID, title, description, _MERCER_AGENT_ID, counter, identifier),
        )
        issue_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO issue_comments (issue_id, body, created_at)
            VALUES (%s, %s, now())
            """,
            (
                issue_id,
                json.dumps({
                    "source": "uptime-check",
                    "fqdn": fqdn,
                    "http_code": http_code,
                    "error": error_msg,
                    "detected_at": now.isoformat(),
                }),
            ),
        )
    log.warning("filed issue %s for down site %s", identifier, fqdn)
    return identifier


def main() -> None:
    log.info("uptime-check starting")
    with psycopg2.connect(_DSN) as conn:
        sites = _active_sites(conn)
        log.info("checking %d active sites", len(sites))

        down_count = 0
        for site in sites:
            fqdn = site["fqdn"]
            is_up, code, err = _check_site(fqdn)
            if is_up:
                log.info("UP   %s (HTTP %s)", fqdn, code)
            else:
                log.warning("DOWN %s (code=%s err=%s)", fqdn, code, err)
                _file_issue(conn, fqdn, err, code)
                down_count += 1

        conn.commit()

    log.info("uptime-check done: %d down out of %d", down_count, len(sites))


if __name__ == "__main__":
    main()
