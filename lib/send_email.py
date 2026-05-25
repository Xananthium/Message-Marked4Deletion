"""Outbound email gateway for all AIB workers.

ALL outbound mail must ride through send(). Direct Gmail calls outside
this module are forbidden for worker code.

Agents decide and send. No approval workflow exists. The status defaults
to 'in_progress' after sending; pass 'todo' if your reply ends with a
question and you want the customer's reply to pull the issue back, or
'done' if the thread is closed. Operator-in-the-loop happens by the
operator being a participant in the email thread, not by a code gate.

Public API:
    send(issue_id, subject, body, to=None,
         status_after='in_progress', from_alias=None, dry_run=False) -> str

For customer-completion emails specifically, use send_completion_email() from
lib.completion_checks instead — it verifies all factual claims (site live, GSC,
DDS gates) before allowing the send.
"""
from __future__ import annotations

import base64
import email.mime.text
import json
import logging
import os
import sys
import time
import threading

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aib.crm.outreach import log_outreach

log = logging.getLogger("aib.send_email")

_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)
_OPS_DSN = os.environ.get(
    "OPS_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/discnxt_ops",
)
_SA_PATH = os.environ.get("AIB_SA_PATH", "/home/discnxt/.secrets/google-agents.json.enc.json")
_MAILBOX = os.environ.get("AIB_MAILBOX", "team@digitaldisconnections.com")
_OPERATOR_EMAIL = os.environ.get("AIB_OPERATOR_EMAIL", "cass@digitaldisconnections.com")

# ---------------------------------------------------------------------------
# Per-domain throttle: in-memory, 30s default between sends to same domain
# ---------------------------------------------------------------------------
_THROTTLE_SECONDS = int(os.environ.get("EMAIL_THROTTLE_SECONDS", "30"))
_domain_last_send: dict[str, float] = {}
_throttle_lock = threading.Lock()


def _extract_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def _throttle_wait(domain: str) -> float:
    """Sleep if needed to respect per-domain send spacing. Returns seconds waited."""
    if not domain or _THROTTLE_SECONDS <= 0:
        return 0.0
    with _throttle_lock:
        now = time.monotonic()
        last = _domain_last_send.get(domain, 0.0)
        gap = _THROTTLE_SECONDS - (now - last)
        if gap > 0:
            log.info("throttle: sleeping %.1fs for domain %s", gap, domain)
            time.sleep(gap)
        _domain_last_send[domain] = time.monotonic()
    return max(gap, 0.0)


# ---------------------------------------------------------------------------
# Suppression check against discnxt_ops.email_suppressions
# ---------------------------------------------------------------------------

def is_suppressed(recipient: str) -> dict | None:
    """Check if a recipient or its domain is suppressed.

    Returns a dict with suppression info if suppressed, None otherwise.
    """
    domain = _extract_domain(recipient)
    addr_lower = recipient.lower()
    try:
        with psycopg2.connect(_OPS_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT address_or_domain, scope, reason, status_code, expires_at
                    FROM email_suppressions
                    WHERE address_or_domain IN (%s, %s)
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY scope ASC
                    LIMIT 1
                    """,
                    (addr_lower, domain),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "address_or_domain": row[0],
                        "scope": row[1],
                        "reason": row[2],
                        "status_code": row[3],
                        "expires_at": str(row[4]) if row[4] else None,
                    }
    except Exception:
        log.exception("suppression check failed — sending anyway (fail-open)")
    return None


def record_suppression(
    address_or_domain: str,
    scope: str,
    reason: str,
    status_code: str | None = None,
    expires_at=None,
) -> None:
    """Record a suppression entry in discnxt_ops.email_suppressions."""
    try:
        with psycopg2.connect(_OPS_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_suppressions
                        (address_or_domain, scope, reason, status_code, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (address_or_domain.lower(), scope, reason, status_code, expires_at),
                )
            conn.commit()
        log.info(
            "suppression: recorded %s scope=%s reason=%s expires=%s",
            address_or_domain, scope, reason, expires_at,
        )
    except Exception:
        log.exception("failed to record suppression for %s", address_or_domain)


def _gmail_svc():
    """Return an authenticated Gmail service object."""
    import importlib
    poller_dir = os.path.join(os.path.dirname(__file__), "..", "..")
    sys.path.insert(0, os.path.abspath(poller_dir))
    poller = importlib.import_module("aib.poller") if "aib.poller" in sys.modules else None
    if poller is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "poller", os.path.join(os.path.dirname(__file__), "..", "poller.py")
        )
        poller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(poller)
    return poller.gmail_client(_SA_PATH, _MAILBOX)


def _build_raw(
    subject: str, body: str, to: str, from_addr: str, from_alias: str | None = None
) -> str:
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_alias or from_addr
    msg["To"] = to
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def send(
    issue_id: str,
    subject: str,
    body: str,
    to: str | None = None,
    status_after: str = "in_progress",
    from_alias: str | None = None,
    dry_run: bool = False,
    lead_id: str | None = None,
    attempt_kind: str = "reply",
    agent_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Send an outbound email linked to a Paperclip issue.

    The agent decides what to send and sends it. No approval gate.

    Args:
        issue_id:     Paperclip issue UUID this email belongs to.
        subject:      Email subject line.
        body:         Plain-text email body.
        to:           Recipient address. Defaults to operator email.
        status_after: Issue status to set after sending. Defaults to
                      'in_progress'. Pass 'todo' if your reply ends with
                      a question (so the customer's reply pulls it back)
                      or 'done' if the thread is closed.
        from_alias:   Optional per-agent send-as identity (e.g.
                      'mercer@digitaldisconnections.com'). Must be a
                      confirmed send-as alias on the authenticated
                      mailbox. Defaults to the team mailbox.
        dry_run:      If True, log what would be sent but skip Gmail and
                      the DB update. Returns a synthetic message ID.
        lead_id:      If provided, writes the canonical CRM triple
                      (contact_attempts + communications + leads update).
        attempt_kind: CRM attempt_kind when lead_id is provided.
                      Defaults to 'reply'.
        agent_id:     Agent UUID for CRM attribution. Defaults to None.
        metadata:     JSONB dict for CRM metadata. Defaults to None.

    Returns:
        Gmail message ID of the sent message, 'DRY_RUN_<id>' if dry_run,
        or 'SUPPRESSED_<id>' if the recipient is suppressed.

    Raises:
        RuntimeError: on Gmail API failure or DB write failure.
    """
    recipient = to or _OPERATOR_EMAIL

    if dry_run:
        log.info(
            "DRY RUN send issue=%s to=%s subject=%r (Gmail skipped)",
            issue_id, recipient, subject,
        )
        return f"DRY_RUN_{issue_id[:8]}"

    # --- Suppression check: skip send if recipient/domain is suppressed ---
    suppression = is_suppressed(recipient)
    if suppression:
        log.warning(
            "SUPPRESSED send issue=%s to=%s reason=%s (scope=%s, expires=%s)",
            issue_id, recipient, suppression["reason"],
            suppression["scope"], suppression.get("expires_at"),
        )
        return f"SUPPRESSED_{issue_id[:8]}"

    # --- Per-domain throttle: sleep if we recently sent to this domain ---
    domain = _extract_domain(recipient)
    waited = _throttle_wait(domain)
    if waited > 0:
        log.info("throttle: waited %.1fs before sending to %s", waited, recipient)

    svc = _gmail_svc()
    raw = _build_raw(subject, body, recipient, _MAILBOX, from_alias)
    result = svc.users().messages().send(
        userId=_MAILBOX, body={"raw": raw}
    ).execute()
    gmail_msg_id = result["id"]

    log.info("sent email issue=%s gmail_id=%s to=%s", issue_id, gmail_msg_id, recipient)

    with psycopg2.connect(_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO issue_comments (issue_id, company_id, body, created_at)
                VALUES (%s, %s, %s, now())
                """,
                (
                    issue_id,
                    '3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6',
                    f"[send_email] Sent to {recipient} | gmail_id={gmail_msg_id}",
                ),
            )
            cur.execute(
                "UPDATE issues SET status = %s, updated_at = now() WHERE id = %s",
                (status_after, issue_id),
            )

        if lead_id:
            log_outreach(
                conn,
                lead_id=lead_id,
                channel="email",
                direction="outbound",
                attempt_kind=attempt_kind,
                subject=subject,
                body=body,
                email_message_id=gmail_msg_id,
                sender=from_alias or _MAILBOX,
                recipient=recipient,
                agent_id=agent_id,
                metadata=metadata or {"source": "send_email.py"},
            )

        conn.commit()

    return gmail_msg_id
