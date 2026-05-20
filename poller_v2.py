"""
aib-poller v2 — paperclip-issue-of-record flow.

For each inbound email to AIB_MAILBOX:
  1. Identify the sender.
  2. Look them up in the NEW `customers` + `domains` schema (paperclip db).
  3. Unknown sender:    forward to operator, mark read, record pending.
  4. Known sender:
       - find an OPEN issue whose first comment carries
         metadata->>'gmail_thread_id' = inbound thread_id;
       - if found  -> append comment, ACK.
       - if absent -> create a new `todo` issue with identifier DIS-N
                      (bumps companies.issue_counter atomically),
                      seed it with a first comment carrying the gmail
                      thread/message metadata, ACK.
  5. We do NOT run aider here in v2. The paperclip issue is the system
     of record; an agent (or operator while agents are paused) executes
     the actual change via a follow-up flow outside this poller.

v1 (poller.py) is kept untouched as a fallback. v2 reuses v1's Gmail
helpers via direct import; the v1 module's main()/aider helpers are
NOT invoked.
"""

import os
import re
import sys
import dataclasses
import json
import logging
from typing import Optional

import psycopg

# Reuse Gmail + helpers from v1.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poller import (  # noqa: E402  (v1 helpers)
    Config,
    load_config,
    gmail_client,
    list_unread,
    fetch_message,
    mark_read,
    mark_unread,
    parse_sender,
    reply,
    forward_to_operator,
    record_pending,
    clear_pending,
    fetch_pending_due,
)

log = logging.getLogger("aib")

# Mercer is the triager fallback per the operator-locked routing model
# (2026-05-19): if no agent alias matches the To: header, the issue is
# assigned to her and she routes or owns it. If she can't decide, she
# escalates to Paulina. No more unassigned team@ queue.
MERCER_AGENT_ID = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"

# ---------------------------------------------------------------------------
# Routing: To: / Delivered-To: header -> agents.email_alias
# No alias match -> defaults to Mercer at the call site.
# ---------------------------------------------------------------------------


def match_agent_by_to_header(conn, message) -> Optional[str]:
    """Look at To: / Delivered-To: headers, lowercase, match against agents.email_alias.
    Returns the agent UUID string if a match, else None."""
    addrs = []
    for hdr in ('To', 'Delivered-To', 'X-Original-To'):
        v = message.get(hdr, '')
        # crude bare-email extraction; the poller already imports email.utils elsewhere
        for piece in re.findall(r'[\w.+-]+@[\w.-]+', v):
            addrs.append(piece.lower())
    if not addrs:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM agents WHERE LOWER(email_alias) = ANY(%s) LIMIT 1", (addrs,))
        row = cur.fetchone()
        return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# Paperclip DSN — second EnvironmentFile= line provides PAPERCLIP_DSN +
# PAPERCLIP_COMPANY_ID + PAPERCLIP_API_KEY.
# ---------------------------------------------------------------------------

_PAPERCLIP_REQUIRED = ("PAPERCLIP_DSN", "PAPERCLIP_COMPANY_ID")


def load_paperclip_env() -> tuple[str, str, str | None]:
    missing = [k for k in _PAPERCLIP_REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing paperclip env: {missing}")
    # Optional: agent UUID to route new customer_email issues to. None = unassigned.
    assignee = os.environ.get("PAPERCLIP_EMAIL_ASSIGNEE_AGENT_ID") or None
    return os.environ["PAPERCLIP_DSN"], os.environ["PAPERCLIP_COMPANY_ID"], assignee


# ---------------------------------------------------------------------------
# Customer + domain lookup (NEW schema)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Customer:
    customer_id: str
    email: str
    name: str | None
    business_name: str | None
    fqdn: str | None
    contabo_path: str | None


def lookup_customer(pc_conn: psycopg.Connection, sender_email: str, mailbox: str) -> Customer | None:
    """Find an active customer by email; prefer the domain whose agent_mailbox
    matches the inbound mailbox, else most recently updated."""
    with pc_conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id::text, c.email, c.name, c.business_name,
                   d.fqdn, d.contabo_path
            FROM customers c
            LEFT JOIN domains d ON d.customer_id = c.id AND d.status = 'active'
            WHERE LOWER(c.email) = LOWER(%s) AND c.status = 'active'
            ORDER BY
                (d.agent_mailbox = %s) DESC NULLS LAST,
                d.updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (sender_email, mailbox),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Customer(*row)


# ---------------------------------------------------------------------------
# Issue lookup / create / comment append
# ---------------------------------------------------------------------------


def find_issue_by_thread(pc_conn: psycopg.Connection, company_id: str, thread_id: str) -> str | None:
    """Find an open (non-terminal) issue whose comments carry the given gmail_thread_id.
    Returns the issue id string if found, else None."""
    with pc_conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id::text
            FROM issues i
            JOIN issue_comments ic ON ic.issue_id = i.id
            WHERE ic.company_id = %s
              AND ic.metadata->>'gmail_thread_id' = %s
              AND i.status NOT IN ('done', 'cancelled')
            ORDER BY i.created_at DESC
            LIMIT 1
            """,
            (company_id, thread_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def append_comment(
    pc_conn: psycopg.Connection,
    company_id: str,
    issue_id: str,
    body: str,
    metadata: dict,
    author_user_id: str = "customer",
) -> str:
    """Insert an issue_comment; return its id."""
    with pc_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO issue_comments
              (company_id, issue_id, author_user_id, author_type, body, metadata)
            VALUES (%s, %s, %s, 'user', %s, %s::jsonb)
            RETURNING id::text
            """,
            (company_id, issue_id, author_user_id, body, json.dumps(metadata)),
        )
        return cur.fetchone()[0]


def create_issue_for_email(
    pc_conn: psycopg.Connection,
    company_id: str,
    customer: Customer,
    msg: dict,
    assignee_agent_id: str | None = None,
) -> tuple[str, str]:
    """Create a new `todo` issue for an inbound customer email.

    - Bumps companies.issue_counter atomically.
    - Sets identifier = '<issue_prefix>-' || new_counter.
    - Seeds the issue with a first comment carrying the gmail thread metadata.

    Returns (issue_id, identifier).
    """
    subject = (msg.get("subject") or "").strip()
    body = (msg.get("body") or "").strip()
    title = (subject or body.split("\n", 1)[0] or "(no subject)")[:80]

    description = (
        f"Inbound customer email\n"
        f"\n"
        f"Customer: {customer.name or '(no name)'} ({customer.business_name or '-'})\n"
        f"Email:    {customer.email}\n"
        f"Customer ID: {customer.customer_id}\n"
        f"Domain:   {customer.fqdn or '(no domain)'}\n"
        f"Path:     {customer.contabo_path or '(no path)'}\n"
        f"Gmail thread: {msg.get('thread_id')}\n"
        f"Gmail msg-id: {msg.get('id')}\n"
        f"Subject:  {subject}\n"
        f"\n"
        f"--- email body ---\n"
        f"{body}\n"
    )

    with pc_conn.cursor() as cur:
        # Bump counter and create issue in one CTE — atomic.
        # assignee_agent_id is nullable; %s::uuid handles NULL cleanly.
        cur.execute(
            """
            WITH bump AS (
                UPDATE companies
                   SET issue_counter = issue_counter + 1
                 WHERE id = %s
                RETURNING issue_counter, issue_prefix
            )
            INSERT INTO issues
              (company_id, title, description, status, priority,
               assignee_agent_id,
               created_by_user_id, issue_number, identifier,
               origin_kind, origin_id)
            SELECT %s, %s, %s, 'todo', 'medium',
                   %s::uuid,
                   'operator', bump.issue_counter,
                   bump.issue_prefix || '-' || bump.issue_counter,
                   'customer_email', %s
              FROM bump
            RETURNING id::text, identifier
            """,
            (company_id, company_id, title, description, assignee_agent_id, msg.get("id") or ""),
        )
        issue_id, identifier = cur.fetchone()

    # Seed first comment with the gmail metadata so future replies
    # on the same thread can find this issue.
    metadata = {
        "gmail_thread_id": msg.get("thread_id"),
        "gmail_msg_id": msg.get("id"),
        "inbound_subject": subject,
        "inbound_from": msg.get("from"),
    }
    append_comment(
        pc_conn,
        company_id=company_id,
        issue_id=issue_id,
        body=body or "(empty body)",
        metadata=metadata,
        author_user_id="customer",
    )
    return issue_id, identifier


# ---------------------------------------------------------------------------
# Reply bodies
# ---------------------------------------------------------------------------


_ACK_NEW = (
    "Thanks — we got your request and opened a ticket ({identifier}). "
    "We'll get back to you with questions or a plan shortly.\n"
    "\n"
    "— Discnxt"
)

_ACK_FOLLOWUP = (
    "Thanks — we got your follow-up and added it to ticket {identifier}. "
    "We'll be in touch.\n"
    "\n"
    "— Discnxt"
)


def get_identifier(pc_conn: psycopg.Connection, issue_id: str) -> str:
    with pc_conn.cursor() as cur:
        cur.execute("SELECT identifier FROM issues WHERE id = %s", (issue_id,))
        row = cur.fetchone()
    return row[0] if row else "(unknown)"


# ---------------------------------------------------------------------------
# process_message — v2 paperclip-issue flow
# ---------------------------------------------------------------------------


def process_message(
    svc,
    pending_conn: psycopg.Connection,
    pc_conn: psycopg.Connection,
    cfg: Config,
    company_id: str,
    msg_id: str,
    dry_run: bool = False,
    fake_msg: dict | None = None,
    email_assignee_agent_id: str | None = None,
) -> dict:
    """Process a single inbound Gmail message under the v2 flow.

    Returns a dict describing what happened (useful for dry-run tests):
      {'action': 'skipped_operator' | 'skipped_forward' | 'unknown_sender'
                | 'new_issue' | 'comment_appended',
       'issue_id': ..., 'identifier': ..., 'comment_id': ...}
    """
    msg = fake_msg if fake_msg is not None else fetch_message(svc, cfg.mailbox, msg_id)
    sender = parse_sender(msg["from"])

    # Skip operator-originated messages to prevent forwarding loops.
    if sender.lower() == cfg.operator_email.lower():
        log.info("skipping operator-originated message from %s", sender)
        mark_read(svc, cfg.mailbox, msg_id)
        return {"action": "skipped_operator", "sender": sender}

    # Skip already-forwarded messages as a secondary loop guard.
    if msg["subject"].strip().startswith("[AIB forward]"):
        log.info("skipping already-forwarded message (subject=%s)", msg["subject"][:80])
        mark_read(svc, cfg.mailbox, msg_id)
        return {"action": "skipped_forward", "subject": msg["subject"][:80]}

    # Unknown sender path.
    customer = lookup_customer(pc_conn, sender, cfg.mailbox)
    if customer is None:
        log.info("unknown sender %s, forwarding to operator", sender)
        if not dry_run:
            forward_to_operator(svc, cfg.mailbox, cfg.operator_email, msg)
            mark_read(svc, cfg.mailbox, msg_id)
            record_pending(pending_conn, msg, "unknown_sender")
        return {"action": "unknown_sender", "sender": sender}

    # Known sender — look for an existing open issue on this thread.
    existing_issue_id = find_issue_by_thread(pc_conn, company_id, msg.get("thread_id") or "")

    if existing_issue_id is not None:
        # Append comment.
        metadata = {
            "gmail_thread_id": msg.get("thread_id"),
            "gmail_msg_id": msg.get("id"),
            "inbound_subject": msg.get("subject"),
            "inbound_from": msg.get("from"),
        }
        comment_id = append_comment(
            pc_conn,
            company_id=company_id,
            issue_id=existing_issue_id,
            body=(msg.get("body") or "").strip() or "(empty body)",
            metadata=metadata,
            author_user_id="customer",
        )
        pc_conn.commit()
        identifier = get_identifier(pc_conn, existing_issue_id)
        log.info("appended comment to %s for sender=%s", identifier, sender)

        if not dry_run:
            reply(
                svc, cfg.mailbox,
                msg["thread_id"], msg["references"], msg["in_reply_to"],
                msg["from"], msg["subject"],
                _ACK_FOLLOWUP.format(identifier=identifier),
            )
            mark_read(svc, cfg.mailbox, msg_id)
            clear_pending(pending_conn, msg["id"])

        return {
            "action": "comment_appended",
            "issue_id": existing_issue_id,
            "identifier": identifier,
            "comment_id": comment_id,
        }

    # No existing thread -> new issue.
    # Route by To: header against agents.email_alias; no alias match
    # defaults to Mercer for triage (operator-locked 2026-05-19).
    # email_assignee_agent_id is intentionally ignored.
    assignee = match_agent_by_to_header(pc_conn, msg) or email_assignee or MERCER_AGENT_ID
    issue_id, identifier = create_issue_for_email(
        pc_conn, company_id, customer, msg,
        assignee_agent_id=assignee,
    )
    pc_conn.commit()
    log.info("created issue %s for sender=%s subject=%r", identifier, sender, msg.get("subject"))

    if not dry_run:
        reply(
            svc, cfg.mailbox,
            msg["thread_id"], msg["references"], msg["in_reply_to"],
            msg["from"], msg["subject"],
            _ACK_NEW.format(identifier=identifier),
        )
        mark_read(svc, cfg.mailbox, msg_id)
        clear_pending(pending_conn, msg["id"])

    return {
        "action": "new_issue",
        "issue_id": issue_id,
        "identifier": identifier,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    pc_dsn, company_id, email_assignee = load_paperclip_env()
    if email_assignee:
        log.info(
            "PAPERCLIP_EMAIL_ASSIGNEE_AGENT_ID=%s set — will be used as fallback "
            "when no To-header alias match",
            email_assignee,
        )

    with psycopg.connect(cfg.dsn) as pending_conn, psycopg.connect(pc_dsn) as pc_conn:
        svc = gmail_client(cfg.sa_path, cfg.mailbox)

        # Retry pending emails first.
        for msg_id, _ in fetch_pending_due(pending_conn):
            try:
                log.info("retrying pending email %s", msg_id)
                process_message(svc, pending_conn, pc_conn, cfg, company_id, msg_id,
                                email_assignee_agent_id=email_assignee)
            except Exception:
                log.exception("process_message retry failed for %s", msg_id)

        # New unread.
        for m in list_unread(svc, cfg.mailbox):
            try:
                process_message(svc, pending_conn, pc_conn, cfg, company_id, m["id"],
                                email_assignee_agent_id=email_assignee)
            except Exception:
                log.exception("process_message failed for %s", m["id"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
