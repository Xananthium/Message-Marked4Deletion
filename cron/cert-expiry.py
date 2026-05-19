#!/usr/bin/env python3
"""
WORKER:      cert-expiry
CADENCE:     daily 03:00
OPT-IN-KEY:  always-on (cert health is critical regardless of outbound prefs)
WHAT IT DOES:
    Opens a TLS connection to each active site, reads the cert NotAfter date.
    Files a high-priority Mercer issue if expiry is within 14 days.
    Idempotent: skips if an open issue with the same fingerprint already exists.
"""
from __future__ import annotations

import json, logging, os, socket, ssl, sys
from datetime import datetime, timezone

import psycopg2, psycopg2.extras

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.cert-expiry")

_DSN = os.environ.get("PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip")
_COMPANY_ID  = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_MERCER      = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
_WARN_DAYS   = 14
_TODAY       = datetime.now(timezone.utc).date().isoformat()


def _active_sites(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT fqdn FROM sites WHERE status='active' ORDER BY fqdn")
        return [dict(r) for r in cur.fetchall()]


def _cert_expiry(fqdn):
    ctx = ssl.create_default_context()
    with socket.create_connection((fqdn, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=fqdn) as ssock:
            cert = ssock.getpeercert()
    return datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def _open_issue_exists(conn, fp):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM issues WHERE origin_fingerprint=%s "
                    "AND status NOT IN ('done','cancelled') LIMIT 1", (fp,))
        return cur.fetchone() is not None


def _file_issue(conn, fqdn, days_left, expiry, fp, title, body):
    if _open_issue_exists(conn, fp):
        log.info("open issue already exists for fingerprint %s, skipping", fp)
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE companies SET issue_counter=issue_counter+1 "
                    "WHERE id=%s RETURNING issue_counter", (_COMPANY_ID,))
        n = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO issues (company_id,title,description,status,priority,
               assignee_agent_id,issue_number,identifier,origin_kind,
               origin_fingerprint,created_at,updated_at)
               VALUES (%s,%s,%s,'todo','high',%s,%s,%s,'worker',%s,now(),now())
               RETURNING id""",
            (_COMPANY_ID, title, body, _MERCER, n, f"DIS-{n}", fp),
        )
        iid = cur.fetchone()[0]
        cur.execute("INSERT INTO issue_comments (issue_id,body,created_at) VALUES (%s,%s,now())",
                    (iid, json.dumps({"source":"cert-expiry","fqdn":fqdn,
                                      "expiry":expiry.isoformat(),"days_left":days_left})))
    log.warning("filed DIS-%d: cert for %s expires in %d days", n, fqdn, days_left)


def do_one(site, conn):
    fqdn = site["fqdn"]
    try:
        expiry    = _cert_expiry(fqdn)
        days_left = (expiry - datetime.now(timezone.utc)).days
        log.info("cert %s expires %s (%d days)", fqdn, expiry.date(), days_left)
        if days_left < _WARN_DAYS:
            _file_issue(conn, fqdn, days_left, expiry,
                        fp=f"cert-expiry:{fqdn}:{_TODAY}",
                        title=f"[cert-expiry] {fqdn} cert expires in {days_left} day(s)",
                        body=f"TLS cert for {fqdn} expires {expiry.date()}. Days left: {days_left}.\nRenew immediately.")
    except Exception as exc:
        log.error("cert-expiry failed for %s: %s", fqdn, exc)
        _file_issue(conn, fqdn, -1, datetime.now(timezone.utc),
                    fp=f"cert-expiry:crash:{fqdn}:{_TODAY}",
                    title=f"[cert-expiry] error checking {fqdn}",
                    body=f"Exception while checking cert for {fqdn}: {exc}")


def main():
    log.info("cert-expiry starting")
    with psycopg2.connect(_DSN) as conn:
        for site in _active_sites(conn):
            do_one(site, conn)
        conn.commit()
    log.info("cert-expiry done")


if __name__ == "__main__":
    main()
