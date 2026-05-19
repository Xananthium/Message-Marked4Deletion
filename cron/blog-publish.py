#!/usr/bin/env python3
"""
WORKER:      blog-publish
CADENCE:     daily 02:30; acts only when blog_publish_day matches today
OPT-IN-KEY:  blog_enabled (per-site outbound_prefs)
WHAT IT DOES:
    For each opted-in site whose blog_publish_day matches today, finds the
    most recently approved blog draft issue (status=done, origin_fingerprint
    starts with blog-draft:<fqdn>). Reads the attached draft body, writes it
    to /var/sites/<fqdn>/content/blog/<date>.md, and commits + deploys via
    deploy-site.sh. Idempotent: marks issue origin_fingerprint consumed.
"""
from __future__ import annotations

import json, logging, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2, psycopg2.extras
sys.path.insert(0, "/home/discnxt/aib")
from lib.policy import can_autoedit, is_paused, outbound_enabled

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.blog-publish")

_DSN = os.environ.get("PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip")
_COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_MERCER     = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
_TODAY      = datetime.now(timezone.utc).date().isoformat()
_WEEKDAY    = datetime.now(timezone.utc).weekday()
_DEPLOY_SH  = "/home/discnxt/aib/deploy-site.sh"


def _opted_in_sites(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT fqdn, blog_publish_day FROM sites "
                    "WHERE status='active' AND blog_publish_day IS NOT NULL ORDER BY fqdn")
        rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows
            if r["blog_publish_day"] == _WEEKDAY
            and outbound_enabled(r["fqdn"], "blog_enabled")
            and can_autoedit(r["fqdn"])
            and not is_paused(r["fqdn"], "blog_publish")]


def _approved_draft(conn, fqdn) -> dict | None:
    """Return newest awaiting_approval issue for this site's blog, or None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT i.id, i.description, i.origin_fingerprint
               FROM issues i
               WHERE i.company_id=%s
                 AND i.status='awaiting_approval'
                 AND i.origin_fingerprint LIKE %s
               ORDER BY i.created_at DESC LIMIT 1""",
            (_COMPANY_ID, f"blog-draft:{fqdn}:%"),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _publish(fqdn, draft_body: str) -> Path:
    out_dir = Path(f"/var/sites/{fqdn}/content/blog")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{_TODAY}.md"
    out_file.write_text(draft_body)
    log.info("wrote %s", out_file)
    if os.path.exists(_DEPLOY_SH):
        subprocess.run([_DEPLOY_SH, fqdn], check=True, timeout=120)
    else:
        log.warning("deploy-site.sh not found at %s, skipping deploy", _DEPLOY_SH)
    return out_file


def do_one(site, conn):
    fqdn = site["fqdn"]
    try:
        draft = _approved_draft(conn, fqdn)
        if not draft:
            log.info("blog-publish: no approved draft for %s", fqdn)
            return
        out_file = _publish(fqdn, draft["description"] or "")
        with conn.cursor() as cur:
            cur.execute("UPDATE issues SET status='done', completed_at=now(), updated_at=now() "
                        "WHERE id=%s", (draft["id"],))
            cur.execute("INSERT INTO issue_comments (issue_id,body,created_at) VALUES (%s,%s,now())",
                        (draft["id"], f"[blog-publish] Published to {out_file} on {_TODAY}"))
        log.info("blog-publish: published %s for %s", out_file, fqdn)
    except Exception as exc:
        log.error("blog-publish failed for %s: %s", fqdn, exc)
        with conn.cursor() as cur:
            cur.execute("UPDATE companies SET issue_counter=issue_counter+1 "
                        "WHERE id=%s RETURNING issue_counter", (_COMPANY_ID,))
            n = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO issues (company_id,title,description,status,priority,
                   assignee_agent_id,issue_number,identifier,origin_kind,
                   origin_fingerprint,created_at,updated_at)
                   VALUES (%s,%s,%s,'todo','high',%s,%s,%s,'worker',%s,now(),now())""",
                (_COMPANY_ID, f"[blog-publish] publish failed for {fqdn}",
                 f"Exception: {exc}", _MERCER, n, f"DIS-{n}",
                 f"blog-publish:crash:{fqdn}:{_TODAY}"),
            )


def main():
    log.info("blog-publish starting (weekday=%d)", _WEEKDAY)
    with psycopg2.connect(_DSN) as conn:
        sites = _opted_in_sites(conn)
        log.info("%d site(s) match today's blog_publish_day", len(sites))
        for site in sites:
            do_one(site, conn)
        conn.commit()
    log.info("blog-publish done")


if __name__ == "__main__":
    main()
