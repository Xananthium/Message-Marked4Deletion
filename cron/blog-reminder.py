#!/usr/bin/env python3
"""
WORKER:      blog-reminder
CADENCE:     daily 09:00
OPT-IN-KEY:  blog_enabled + outbound_prefs.customer_email_for_blog_approval set
WHAT IT DOES:
    For each opted-in site with a blog draft issue in awaiting_approval status
    that is older than 48 hours, files a Mercer issue requesting a customer
    reminder email ("your draft is waiting, please approve or send edits").
    Idempotent: one open reminder issue per (fqdn, draft_issue_id).
"""
from __future__ import annotations

import json, logging, os, sys
from datetime import datetime, timedelta, timezone

import psycopg2, psycopg2.extras
sys.path.insert(0, "/home/discnxt/aib")
from lib.policy import is_paused, outbound_enabled
from lib.send_email import send

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.blog-reminder")

_DSN = os.environ.get("PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip")
_COMPANY_ID  = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_MERCER      = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
_STALE_HOURS = 48
_TODAY       = datetime.now(timezone.utc).date().isoformat()


def _active_sites(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT fqdn, outbound_prefs FROM sites "
                    "WHERE status='active' ORDER BY fqdn")
        rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows
            if outbound_enabled(r["fqdn"], "blog_enabled")
            and (r.get("outbound_prefs") or {}).get("customer_email_for_blog_approval")
            and not is_paused(r["fqdn"], "blog_reminder")]


def _stale_drafts(conn, fqdn) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_STALE_HOURS)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT id, created_at, origin_fingerprint FROM issues
               WHERE company_id=%s AND status='awaiting_approval'
                 AND origin_fingerprint LIKE %s AND created_at < %s
               ORDER BY created_at DESC""",
            (_COMPANY_ID, f"blog-draft:{fqdn}:%", cutoff),
        )
        return [dict(r) for r in cur.fetchall()]


def _open_reminder_exists(conn, fp) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM issues WHERE origin_fingerprint=%s "
                    "AND status NOT IN ('done','cancelled') LIMIT 1", (fp,))
        return cur.fetchone() is not None


def do_one(site, conn):
    fqdn  = site["fqdn"]
    prefs = site.get("outbound_prefs") or {}
    to    = prefs.get("customer_email_for_blog_approval")
    try:
        for draft in _stale_drafts(conn, fqdn):
            fp = f"blog-reminder:{fqdn}:{draft['id']}"
            if _open_reminder_exists(conn, fp):
                log.info("blog-reminder: reminder already filed for %s draft %s", fqdn, draft["id"])
                continue
            # File Mercer issue; Mercer will send the customer email
            age_h = int((datetime.now(timezone.utc) - draft["created_at"]).total_seconds() / 3600)
            body = (
                f"Blog draft for {fqdn} has been awaiting approval for {age_h} hours.\n\n"
                f"Draft issue: DIS reference — id {draft['id']}\n"
                f"Customer email: {to}\n\n"
                "Please send a polite reminder asking the customer to approve or send edits."
            )
            with conn.cursor() as cur:
                cur.execute("UPDATE companies SET issue_counter=issue_counter+1 "
                            "WHERE id=%s RETURNING issue_counter", (_COMPANY_ID,))
                n = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO issues (company_id,title,description,status,priority,
                       assignee_agent_id,issue_number,identifier,origin_kind,
                       origin_fingerprint,created_at,updated_at)
                       VALUES (%s,%s,%s,'todo','medium',%s,%s,%s,'worker',%s,now(),now())
                       RETURNING id""",
                    (_COMPANY_ID, f"[blog-reminder] send approval nudge for {fqdn}",
                     body, _MERCER, n, f"DIS-{n}", fp),
                )
                iid = cur.fetchone()[0]
                cur.execute("INSERT INTO issue_comments (issue_id,body,created_at) VALUES (%s,%s,now())",
                            (iid, json.dumps({"source":"blog-reminder","fqdn":fqdn,
                                              "draft_id":str(draft["id"]),"to":to})))
            log.info("filed DIS-%d: blog-reminder for %s", n, fqdn)
    except Exception as exc:
        log.error("blog-reminder failed for %s: %s", fqdn, exc)


def main():
    log.info("blog-reminder starting")
    with psycopg2.connect(_DSN) as conn:
        sites = _active_sites(conn)
        log.info("%d site(s) eligible for blog-reminder", len(sites))
        for site in sites:
            do_one(site, conn)
        conn.commit()
    log.info("blog-reminder done")


if __name__ == "__main__":
    main()
