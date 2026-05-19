#!/usr/bin/env python3
"""
WORKER:      seo-research
CADENCE:     bi-weekly Monday 05:00
OPT-IN-KEY:  seo_research_enabled (per-site outbound_prefs)
WHAT IT DOES:
    For each opted-in site, runs seo-audit-site.py to get current on-page
    scores. Compares to last audit; files an internal seo_recommendation
    issue for Mercer if any check regressed. GSC/rank-tracking deferred —
    no external API keys are set up yet; audit deltas serve as proxy signal.
"""
from __future__ import annotations

import json, logging, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2, psycopg2.extras
sys.path.insert(0, "/home/discnxt/aib")
from lib.policy import is_paused, outbound_enabled

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.seo-research")

_DSN = os.environ.get("PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip")
_COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_MERCER     = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
_TODAY      = datetime.now(timezone.utc).date().isoformat()
_AUDIT_BIN  = "/home/discnxt/aib/cron/seo-audit-site.py"


def _active_opted_in_sites(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT fqdn FROM sites WHERE status='active' ORDER BY fqdn")
        rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows if outbound_enabled(r["fqdn"], "seo_research_enabled")
            and not is_paused(r["fqdn"], "seo_research")]


def _latest_audit(fqdn: str) -> dict | None:
    seo_dir = Path(f"/var/sites/{fqdn}/seo")
    if not seo_dir.exists():
        return None
    audits = sorted(seo_dir.glob("audit-*.md"))
    if len(audits) < 2:
        return None
    prev = audits[-2]
    checks: dict = {}
    for line in prev.read_text().splitlines():
        if line.startswith("- ["):
            mark = "PASS" if "[PASS]" in line else "FAIL"
            key  = line.split("] ", 1)[-1].strip()
            checks[key] = mark == "PASS"
    return checks


def _open_issue_exists(conn, fp):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM issues WHERE origin_fingerprint=%s "
                    "AND status NOT IN ('done','cancelled') LIMIT 1", (fp,))
        return cur.fetchone() is not None


def _file_issue(conn, fqdn, regressions, current):
    fp = f"seo-research:{fqdn}:{_TODAY}"
    if _open_issue_exists(conn, fp):
        log.info("seo-research: issue already exists for %s %s", fqdn, _TODAY)
        return
    body = (
        f"SEO audit regression detected for {fqdn} on {_TODAY}.\n\n"
        f"Failed checks: {', '.join(regressions)}\n\n"
        f"Full results: /var/sites/{fqdn}/seo/audit-{_TODAY}.md\n"
        "Note: Google Search Console rank tracking deferred (no API key configured)."
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
            (_COMPANY_ID, f"[seo-research] regression on {fqdn}: {', '.join(regressions)}",
             body, _MERCER, n, f"DIS-{n}", fp),
        )
        iid = cur.fetchone()[0]
        cur.execute("INSERT INTO issue_comments (issue_id,body,created_at) VALUES (%s,%s,now())",
                    (iid, json.dumps({"source":"seo-research","fqdn":fqdn,
                                      "regressions":regressions,"current":current})))
    log.warning("filed DIS-%d: SEO regression on %s", n, fqdn)


def do_one(site, conn):
    fqdn = site["fqdn"]
    try:
        prev = _latest_audit(fqdn)
        result = subprocess.run(
            [sys.executable, _AUDIT_BIN, fqdn],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"audit exited {result.returncode}: {result.stderr[:300]}")
        data     = json.loads(result.stdout)
        current  = data["findings"]["checks"]
        regressions = [k for k, passed in current.items()
                       if not passed and (prev is None or prev.get(k, True))]
        if regressions:
            _file_issue(conn, fqdn, regressions, current)
        else:
            log.info("seo-research: %s — no regressions", fqdn)
    except Exception as exc:
        log.error("seo-research failed for %s: %s", fqdn, exc)


def main():
    log.info("seo-research starting")
    with psycopg2.connect(_DSN) as conn:
        sites = _active_opted_in_sites(conn)
        log.info("%d site(s) opted in to seo_research_enabled", len(sites))
        for site in sites:
            do_one(site, conn)
        conn.commit()
    log.info("seo-research done")


if __name__ == "__main__":
    main()
