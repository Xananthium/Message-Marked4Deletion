"""Per-site policy checks for AIB workers.

Workers call these before performing any autonomous action. Failures are
visible (raise), never silently swallowed.

Public API:
    can_autoedit(fqdn) -> bool
    is_paused(fqdn, job) -> bool
    outbound_enabled(fqdn, kind) -> bool
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extras

_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)


def _fetch_site(fqdn: str, cur) -> dict:
    cur.execute(
        "SELECT no_autonomous_edits, paused_jobs, outbound_prefs, status FROM sites WHERE fqdn = %s",
        (fqdn.lower().strip(),),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"site not found in sites table: {fqdn!r}")
    return dict(row)


def can_autoedit(fqdn: str) -> bool:
    """Return True if autonomous content edits are permitted for this site.

    Returns False when no_autonomous_edits=TRUE or site status != 'active'.
    Workers must check this before writing any content to the site.

    Raises:
        KeyError: fqdn not in sites table.
    """
    with psycopg2.connect(_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            site = _fetch_site(fqdn, cur)
    return not site["no_autonomous_edits"] and site["status"] == "active"


def is_paused(fqdn: str, job: str) -> bool:
    """Return True if the named job is paused for this site.

    Args:
        fqdn: Site domain name.
        job:  Job identifier string (e.g. 'blog_draft', 'uptime_check').

    Raises:
        KeyError: fqdn not in sites table.
    """
    with psycopg2.connect(_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            site = _fetch_site(fqdn, cur)
    paused = site["paused_jobs"] or []
    return job in paused or site["status"] != "active"


def outbound_enabled(fqdn: str, kind: str) -> bool:
    """Return True if the given outbound job kind is opted-in for this site.

    Workers ship with all outbound disabled. Customers opt in per-job via
    outbound_prefs jsonb (e.g. {"blog_publish": true}).

    Args:
        fqdn: Site domain name.
        kind: Outbound job key (e.g. 'blog_publish', 'weekly_report').

    Raises:
        KeyError: fqdn not in sites table.
    """
    with psycopg2.connect(_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            site = _fetch_site(fqdn, cur)
    prefs = site.get("outbound_prefs") or {}
    return bool(prefs.get(kind, False))
