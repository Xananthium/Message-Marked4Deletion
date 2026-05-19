#!/usr/bin/env python3
"""
WORKER:      blog-draft
CADENCE:     daily 02:00; acts only when weekday matches sites.blog_draft_day
OPT-IN-KEY:  blog_enabled (per-site outbound_prefs)
WHAT IT DOES:
    For each opted-in site whose blog_draft_day matches today's weekday,
    creates a Paperclip issue assigned to the Marketing Writer (falls back to
    Jordan, Marketing Operations Specialist, if Writer not yet hired). The
    issue is the trigger for the Marketing Pod to produce a blog draft.
    Idempotent: skips if an open draft issue already exists for this week.
"""
from __future__ import annotations

import json, logging, os, sys
from datetime import datetime, timezone

import psycopg2, psycopg2.extras
sys.path.insert(0, "/home/discnxt/aib")
from lib.policy import is_paused, outbound_enabled

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.blog-draft")

_DSN = os.environ.get("PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip")
_COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
# Jordan (Marketing Operations Specialist) is the fallback until Writer is hired (task #28)
_JORDAN     = "49b01a5f-3df2-4f34-a5f1-d06e0a292851"
_TODAY      = datetime.now(timezone.utc).date().isoformat()
_WEEKDAY    = datetime.now(timezone.utc).weekday()  # 0=Mon … 6=Sun
_ISO_WEEK   = datetime.now(timezone.utc).strftime("%Y-W%W")


def _writer_id(conn) -> str:
    """Return Marketing Writer agent UUID, or fall back to Jordan."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM agents WHERE title ILIKE '%marketing writer%' "
                    "AND company_id=%s LIMIT 1", (_COMPANY_ID,))
        row = cur.fetchone()
    return str(row[0]) if row else _JORDAN


def _opted_in_sites(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT fqdn, blog_draft_day FROM sites "
                    "WHERE status='active' AND blog_draft_day IS NOT NULL ORDER BY fqdn")
        rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows
            if r["blog_draft_day"] == _WEEKDAY
            and outbound_enabled(r["fqdn"], "blog_enabled")
            and not is_paused(r["fqdn"], "blog_draft")]


def _open_issue_exists(conn, fp):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM issues WHERE origin_fingerprint=%s "
                    "AND status NOT IN ('done','cancelled') LIMIT 1", (fp,))
        return cur.fetchone() is not None


def do_one(site, conn, writer_id):
    fqdn = site["fqdn"]
    fp   = f"blog-draft:{fqdn}:{_ISO_WEEK}"
    if _open_issue_exists(conn, fp):
        log.info("blog-draft: issue already exists for %s week %s", fqdn, _ISO_WEEK)
        return
    try:
        body = (
            f"Weekly blog draft request for {fqdn}.\n\n"
            f"Week: {_ISO_WEEK}\n"
            f"Site context: /var/sites/{fqdn}/\n\n"
            "Produce a draft post and attach it to this issue. If the site "
            "has customer_email_for_blog_approval set, send the draft to the "
            "customer and leave the issue at 'todo' so their reply pulls it "
            "back; mark 'done' once the customer signs off. If no customer "
            "review is configured, mark 'done' when the draft is final and "
            "blog-publish will pick it up on the next publish day."
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
                (_COMPANY_ID, f"[blog-draft] {fqdn} — week {_ISO_WEEK}",
                 body, writer_id, n, f"DIS-{n}", fp),
            )
            iid = cur.fetchone()[0]
            cur.execute("INSERT INTO issue_comments (issue_id,body,created_at) VALUES (%s,%s,now())",
                        (iid, json.dumps({"source":"blog-draft","fqdn":fqdn,"week":_ISO_WEEK,
                                          "assignee":writer_id})))
        log.info("filed DIS-%d: blog-draft for %s", n, fqdn)
    except Exception as exc:
        log.error("blog-draft failed for %s: %s", fqdn, exc)


def main():
    log.info("blog-draft starting (weekday=%d)", _WEEKDAY)
    with psycopg2.connect(_DSN) as conn:
        sites = _opted_in_sites(conn)
        log.info("%d site(s) match today's blog_draft_day", len(sites))
        if not sites:
            return
        writer_id = _writer_id(conn)
        log.info("assigning to agent %s", writer_id)
        for site in sites:
            do_one(site, conn, writer_id)
        conn.commit()
    log.info("blog-draft done")


if __name__ == "__main__":
    main()
