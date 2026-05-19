#!/usr/bin/env python3
"""
Hourly scope monitor — cancels out-of-scope Paperclip issues and logs what went wrong.
Runs as a systemd timer. Checks issues created in the last 65 minutes (overlap handles
edge cases at timer boundaries).
"""

import os, sys, logging, psycopg2, json
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/site-flow/scope-monitor.log"),
    ],
)
log = logging.getLogger("scope-monitor")

DB = "postgresql://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip"
COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
OPERATOR_DIRECTIVE_ISSUE = "b13f4829-d4dc-429a-b307-f259e13ff972"

# Clear-cancel patterns: titles/descriptions matching these are empire-building
CANCEL_PATTERNS = [
    "mailbox", "postal address", "ups store", "virtual mailbox",
    "prometheus", "metrics pipeline", "monitoring stack", "grafana",
    "design token", "token system",
    "tco calculator", "cost calculator",
    "term renewal reminder", "renewal reminder",
    "subscription billing", "dues management",
    "letta", "procrastinate", "fastapi", "htmx stack",
    "new saas", "third-party service", "saas subscription",
    "microservice", "kubernetes", "docker swarm",
    "sendgrid", "mailchimp", "twilio",
]

# Flag-only patterns: might be legit, needs operator review
FLAG_PATTERNS = [
    "new service", "new product", "expand", "launch new",
    "automate billing", "payment integration",
    "hire ", "new hire", "recruit",
]

def matches(text, patterns):
    t = text.lower()
    return next((p for p in patterns if p in t), None)

def run():
    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    # Fetch issues created in the last 65 minutes that aren't already done/cancelled
    cur.execute("""
        SELECT i.id, i.title, i.description, i.status, a.name as agent_name
        FROM issues i
        LEFT JOIN agents a ON i.assignee_agent_id = a.id
        WHERE i.company_id = %s
          AND i.created_at > now() - interval '65 minutes'
          AND i.status NOT IN ('done', 'cancelled')
        ORDER BY i.created_at ASC
    """, (COMPANY_ID,))
    rows = cur.fetchall()

    if not rows:
        log.info("No new issues in window — queue looks clean.")
        conn.close()
        return

    cancelled = []
    flagged = []

    for issue_id, title, description, status, agent_name in rows:
        search_text = f"{title or ''} {description or ''}"
        cancel_hit = matches(search_text, CANCEL_PATTERNS)
        flag_hit = matches(search_text, FLAG_PATTERNS)

        if cancel_hit:
            # Cancel it and leave a comment
            cur.execute("""
                UPDATE issues SET status = 'cancelled', updated_at = now()
                WHERE id = %s
            """, (issue_id,))
            comment = (
                f"Auto-cancelled by scope monitor: this issue matches the pattern "
                f"'{cancel_hit}', which is outside Discnxt's current scope "
                f"(website migrations only — beat big tech by using resources carefully). "
                f"If this is genuinely needed, bring it as a one-paragraph proposal to "
                f"the operator directive issue ({OPERATOR_DIRECTIVE_ISSUE[:8]}) and wait "
                f"for a green-light. Agent: {agent_name or 'unassigned'}."
            )
            cur.execute("""
                INSERT INTO issue_comments (issue_id, body, created_at, updated_at)
                VALUES (%s, %s, now(), now())
            """, (issue_id, comment))
            conn.commit()
            cancelled.append((title, agent_name, cancel_hit))
            log.warning("CANCELLED [%s] '%s' (agent: %s, hit: %s)", issue_id[:8], title, agent_name, cancel_hit)

        elif flag_hit:
            # Just comment, don't cancel
            comment = (
                f"Scope monitor flagged this issue for operator review: matched pattern "
                f"'{flag_hit}'. Discnxt's current scope is website migrations only. "
                f"If this is in scope, no action needed. If not, cancel it and note why "
                f"in the operator directive issue ({OPERATOR_DIRECTIVE_ISSUE[:8]})."
            )
            cur.execute("""
                INSERT INTO issue_comments (issue_id, body, created_at, updated_at)
                VALUES (%s, %s, now(), now())
            """, (issue_id, comment))
            conn.commit()
            flagged.append((title, agent_name, flag_hit))
            log.info("FLAGGED  [%s] '%s' (agent: %s, hit: %s)", issue_id[:8], title, agent_name, flag_hit)

    # Diagnose agent drift — if one agent cancelled 2+, their file needs attention
    from collections import Counter
    agent_cancel_counts = Counter(a for _, a, _ in cancelled if a)
    for agent, count in agent_cancel_counts.items():
        if count >= 2:
            log.warning(
                "DRIFT: %s filed %d out-of-scope issues in this window — "
                "their AGENTS.md scope anchor may need reinforcing.",
                agent, count
            )

    log.info(
        "Done. Cancelled: %d, Flagged: %d, Clean: %d",
        len(cancelled), len(flagged), len(rows) - len(cancelled) - len(flagged)
    )
    conn.close()

if __name__ == "__main__":
    run()
