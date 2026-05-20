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
         status_after='in_progress', from_alias=None) -> str
"""
from __future__ import annotations

import base64
import email.mime.text
import json
import logging
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("aib.send_email")

_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)
_SA_PATH = os.environ.get("AIB_SA_PATH", "/home/discnxt/.secrets/google-agents.json.enc.json")
_MAILBOX = os.environ.get("AIB_MAILBOX", "team@digitaldisconnections.com")
_OPERATOR_EMAIL = os.environ.get("AIB_OPERATOR_EMAIL", "cass@digitaldisconnections.com")


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

    Returns:
        Gmail message ID of the sent message.

    Raises:
        RuntimeError: on Gmail API failure or DB write failure.
    """
    recipient = to or _OPERATOR_EMAIL

    svc = _gmail_svc()
    raw = _build_raw(subject, body, recipient, _MAILBOX, from_alias)
    result = svc.users().messages().send(
        userId=_MAILBOX, body={"raw": raw}
    ).execute()
    gmail_msg_id = result["id"]

    log.info("sent email issue=%s gmail_id=%s to=%s", issue_id, gmail_msg_id, recipient)

    with psycopg2.connect(_DSN) as conn, conn.cursor() as cur:
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
        conn.commit()

    return gmail_msg_id
