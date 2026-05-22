"""
aib-poller — paperclip-issue-of-record flow.

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
"""

import base64
import dataclasses
import email.message
import email.utils
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

import psycopg
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("aib")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_REQUIRED = {
    "AIB_DSN": "dsn",
    "AIB_SA_PATH": "sa_path",
    "AIB_MAILBOX": "mailbox",
    "AIB_OPERATOR_EMAIL": "operator_email",
    "AIB_SSH_ALIAS": "ssh_alias",
    "AIB_MODEL": "model",
    "AIB_TMP_ROOT": "tmp_root",
}

MAX_RETRIES = 5
BASE_DELAY_SECONDS = 300  # 5 minutes


@dataclasses.dataclass(frozen=True)
class Config:
    dsn: str
    sa_path: str
    mailbox: str
    operator_email: str
    ssh_alias: str
    model: str
    tmp_root: str


def load_config() -> Config:
    vals: dict[str, str] = {}
    for key, field in _REQUIRED.items():
        val = os.environ.get(key)
        if val is None:
            raise RuntimeError(f"missing env: {key}")
        vals[field] = os.path.expanduser(val)
    os.makedirs(vals["tmp_root"], exist_ok=True)
    return Config(**vals)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def maybe_decrypt(path: str) -> str:
    """If path ends with .enc.json, decrypt with sops and return temp file path."""
    if path.endswith('.enc.json'):
        sops_path = shutil.which('sops')
        if not sops_path:
            raise RuntimeError('sops not found in PATH')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            subprocess.run(
                [sops_path, '--decrypt', path],
                check=True,
                stdout=f,
                text=True,
                env={**os.environ, 'SOPS_AGE_KEY_FILE': os.path.expanduser('~/.config/age/keys.txt')}
            )
            return f.name
    return path


def gmail_client(sa_path: str, subject: str):
    decrypted_path = maybe_decrypt(sa_path)
    try:
        if decrypted_path != sa_path:
            with open(decrypted_path, 'r') as f:
                info = json.load(f)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=_GMAIL_SCOPES
            ).with_subject(subject)
            os.unlink(decrypted_path)
        else:
            creds = service_account.Credentials.from_service_account_file(
                sa_path, scopes=_GMAIL_SCOPES
            ).with_subject(subject)
    except Exception:
        if decrypted_path != sa_path and os.path.exists(decrypted_path):
            os.unlink(decrypted_path)
        raise
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_unread(svc, mailbox: str) -> list[dict]:
    try:
        resp = svc.users().messages().list(
            userId=mailbox, q="is:unread", maxResults=25
        ).execute()
        return resp.get("messages", [])
    except HttpError as e:
        raise RuntimeError(f"gmail list_unread: {e}") from e


def _decode_part(data: str) -> str:
    padding = 4 - len(data) % 4
    return base64.urlsafe_b64decode(data + "=" * (padding % 4)).decode("utf-8", errors="replace")


def _walk_parts(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = (payload.get("body") or {}).get("data", "")
        return _decode_part(data) if data else ""
    if mime == "text/html":
        return ""
    for part in payload.get("parts", []):
        result = _walk_parts(part)
        if result:
            return result
    return ""


def fetch_message(svc, mailbox: str, msg_id: str) -> dict:
    try:
        raw = svc.users().messages().get(
            userId=mailbox, id=msg_id, format="full"
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"gmail fetch_message: {e}") from e

    headers = {h["name"]: h["value"] for h in raw.get("payload", {}).get("headers", [])}
    payload = raw.get("payload", {})

    body = _walk_parts(payload)
    if not body:
        data = (payload.get("body") or {}).get("data", "")
        body = _decode_part(data) if data else ""

    to_header = headers.get("Delivered-To", headers.get("To", ""))
    return {
        "id": msg_id,
        "thread_id": raw.get("threadId", ""),
        "from": headers.get("From", ""),
        "to": to_header,
        "to_header_raw": headers.get("To", ""),
        "subject": headers.get("Subject", ""),
        "body": body,
        "message_id_hdr": headers.get("Message-ID", ""),
        "references": headers.get("References", ""),
        "in_reply_to": headers.get("In-Reply-To", ""),
    }


def parse_sender(raw_from: str) -> str:
    addr = email.utils.parseaddr(raw_from)[1].lower().strip()
    if not addr:
        raise ValueError(f"cannot parse sender from: {raw_from!r}")
    return addr


def mark_read(svc, mailbox: str, msg_id: str) -> None:
    try:
        svc.users().messages().modify(
            userId=mailbox, id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"gmail mark_read: {e}") from e


def mark_unread(svc, mailbox: str, msg_id: str) -> None:
    try:
        svc.users().messages().modify(
            userId=mailbox, id=msg_id, body={"addLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"gmail mark_unread: {e}") from e


def forward_to_operator(svc, mailbox: str, operator_email: str, original: dict) -> None:
    msg = email.message.EmailMessage()
    msg["To"] = operator_email
    msg["From"] = mailbox
    msg["Subject"] = f"[AIB forward] {original['subject']}"
    msg.set_content(
        f"From: {original['from']}\nOriginal subject: {original['subject']}\n\n{original['body']}"
    )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    try:
        svc.users().messages().send(
            userId=mailbox, body={"raw": raw}
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"gmail forward_to_operator: {e}") from e


def _run(
    cmd: list[str],
    cwd: str | None = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    cp = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(f"{cmd[0]} rc={cp.returncode}: {cp.stderr.strip()[:500]}")
    return cp


# ---------------------------------------------------------------------------
# DB helpers — pending_emails
# ---------------------------------------------------------------------------


def record_pending(conn: psycopg.Connection, msg: dict, reason: str) -> None:
    """Insert or update a row in pending_emails, then commit.

    Idempotent on gmail_msg_id: a duplicate call updates the reason and
    refreshes created_at so the row reflects the latest failure context.
    Increments retry_count and schedules next retry with exponential backoff.
    psycopg.Error is not caught here — it bubbles to the per-message handler.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_emails (gmail_msg_id, sender, subject, reason, retry_count, next_retry)"
            " VALUES (%s, %s, %s, %s, 0, now() + interval '%s seconds')"
            " ON CONFLICT (gmail_msg_id)"
            " DO UPDATE SET reason = EXCLUDED.reason, created_at = now(),"
            "                retry_count = pending_emails.retry_count + 1,"
            "                next_retry = now() + interval '%s seconds' * POWER(2, pending_emails.retry_count)"
            " WHERE pending_emails.retry_count < %s",
            (msg["id"], msg["from"], msg["subject"], reason, BASE_DELAY_SECONDS, BASE_DELAY_SECONDS, MAX_RETRIES),
        )
    conn.commit()


def fetch_pending_due(conn: psycopg.Connection) -> list[tuple[str, dict]]:
    """Return list of (gmail_msg_id, message_dict) for pending emails due for retry.

    Only returns rows where retry_count < MAX_RETRIES and next_retry <= now()
    AND reason != 'unknown_sender' (these should not be retried).
    Each dict contains sender, subject, and reason from pending_emails.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gmail_msg_id, sender, subject, reason FROM pending_emails "
            "WHERE retry_count < %s AND next_retry <= now() AND reason != 'unknown_sender' "
            "ORDER BY next_retry",
            (MAX_RETRIES,)
        )
        rows = cur.fetchall()

    result = []
    for gmail_msg_id, sender, subject, reason in rows:
        msg = {
            "id": gmail_msg_id,
            "from": sender or "",
            "subject": subject or "",
            "body": "",
            "thread_id": "",
            "references": "",
            "in_reply_to": "",
            "message_id_hdr": "",
        }
        result.append((gmail_msg_id, msg))
    return result


def clear_pending(conn: psycopg.Connection, gmail_msg_id: str) -> None:
    """Delete a row from pending_emails after successful processing."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pending_emails WHERE gmail_msg_id = %s", (gmail_msg_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# v2 routing constants
# ---------------------------------------------------------------------------

# Mercer is the triager fallback per the operator-locked routing model
# (2026-05-19): if no agent alias matches the To: header, the issue is
# assigned to her and she routes or owns it. If she can't decide, she
# escalates to Paulina. No more unassigned team@ queue.
MERCER_AGENT_ID = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"

# ---------------------------------------------------------------------------
# Keyword-based routing: after alias matching fails, scan subject + body for
# keywords before falling to Mercer. Each entry is (keyword_set, agent_id,
# category_label). First match wins. The category_label and matched keyword
# are stored in issue metadata as suggested_route so Mercer (or anyone) can
# see what the poller thought.
# ---------------------------------------------------------------------------
KEYWORD_ROUTES: list[tuple[set[str], str, str]] = [
    # Billing / money → Paulina (CEO handles business ops)
    (
        {"invoice", "bill", "payment", "billing", "charge", "refund",
         "receipt", "pricing", "subscription", "cancel"},
        "38d8400a-3d0a-44ff-b430-a228180bc1e5",
        "billing",
    ),
    # Engineering / site problems → Reed (Coder)
    (
        {"bug", "broken", "404", "error", "crash", "down", "not working",
         "fix", "offline", "slow", "500", "503", "timeout"},
        "d9431040-6d05-4bb2-be63-3a87e79abf32",
        "engineering",
    ),
    # Visual / design / image requests → Hollis (image-gen craftsperson)
    (
        {"logo", "image", "graphic", "design", "brand", "banner", "hero",
         "photo", "picture", "icon"},
        "66133a39-6dde-4a11-86c1-3e1846d447d1",
        "design",
    ),
    # Marketing / SEO / content → Sage (Marketing Manager)
    (
        {"marketing", "seo", "search", "traffic", "ads", "campaign",
         "rank", "google", "content", "blog", "newsletter", "outreach"},
        "49b01a5f-3df2-4f34-a5f1-d06e0a292851",
        "marketing",
    ),
]

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
        for piece in re.findall(r'[\w.+-]+@[\w.-]+', v):
            addrs.append(piece.lower())
    if not addrs:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM agents WHERE LOWER(email_alias) = ANY(%s) LIMIT 1", (addrs,))
        row = cur.fetchone()
        return str(row[0]) if row else None


def match_agent_by_keywords(subject: str, body: str) -> tuple[str | None, str | None, str | None]:
    """Scan subject + body for routing keywords after alias matching fails.

    Returns (agent_id, matched_keyword, category_label) for the first match,
    or (None, None, None) if no keyword hits.
    """
    text = f"{subject} {body}".lower()
    for keyword_set, agent_id, category in KEYWORD_ROUTES:
        for kw in keyword_set:
            if re.search(rf'\b{re.escape(kw)}\b', text):
                return agent_id, kw, category
    return None, None, None


# ---------------------------------------------------------------------------
# Paperclip DSN — second EnvironmentFile= line provides PAPERCLIP_DSN +
# PAPERCLIP_COMPANY_ID + PAPERCLIP_API_KEY.
# ---------------------------------------------------------------------------

_PAPERCLIP_REQUIRED = ("PAPERCLIP_DSN", "PAPERCLIP_COMPANY_ID")

# Internal/operator senders. These are first-class trusted users who can start
# any conversation. They take the same downstream route as customers (To: alias →
# keyword → Mercer fallback) but carry trusted_internal=True in the seed comment
# metadata so receiving agents can distinguish admin steerage from customer mail.
# They are NEVER forwarded to the operator (they ARE the operators).
_INTERNAL_SENDERS = frozenset({
    "jc@digitaldisconnections.com",
    "jamal@digitaldisconnections.com",
    "cass@digitaldisconnections.com",
})

# Reuse the existing Cass/Operator customer row for synthetic Customer records.
# JC and Jamal have no customer row; we use deterministic sentinel UUIDs that
# appear only in the issue description (create_issue_for_email writes no
# customer_id FK column).
_INTERNAL_OPERATOR_CUSTOMER_ID = "d81c419d-1d90-451b-9f30-748fcca770c5"


def load_paperclip_env() -> tuple[str, str, str | None]:
    missing = [k for k in _PAPERCLIP_REQUIRED if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing paperclip env: {missing}")
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


def _internal_customer(sender_email: str) -> Customer:
    """Synthesize a Customer for an internal/operator sender.

    Cass reuses her existing Operator customer-row id; JC and Jamal get
    deterministic sentinel UUIDs. These ids appear only in the issue
    description string — create_issue_for_email writes no customer_id
    FK column, so the sentinels never need to exist in the customers table.
    """
    s = sender_email.lower()
    if s == "cass@digitaldisconnections.com":
        cid, name = _INTERNAL_OPERATOR_CUSTOMER_ID, "Cass (Operator)"
    elif s == "jc@digitaldisconnections.com":
        cid, name = "00000000-0000-0000-0000-00000000000c", "JC (Operator)"
    elif s == "jamal@digitaldisconnections.com":
        cid, name = "00000000-0000-0000-0000-00000000000d", "Jamal (Operator)"
    else:
        raise ValueError(f"not an internal sender: {sender_email}")
    return Customer(
        customer_id=cid,
        email=sender_email,
        name=name,
        business_name="Digital Disconnections (internal)",
        fqdn=None,
        contabo_path=None,
    )


# ---------------------------------------------------------------------------
# Issue lookup / create / comment append
# ---------------------------------------------------------------------------


def find_issue_by_thread(pc_conn: psycopg.Connection, company_id: str, thread_id: str) -> str | None:
    """Find the most recent issue whose comments carry the given gmail_thread_id.

    Returns the issue id string if found, else None. Includes done/cancelled
    issues: a reply on a closed thread should reattach (and reopen) the
    original issue rather than spawn a fresh ticket and lose the assignee.
    The caller is responsible for reopening if status is terminal.
    """
    with pc_conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id::text
            FROM issues i
            JOIN issue_comments ic ON ic.issue_id = i.id
            WHERE ic.company_id = %s
              AND ic.metadata->>'gmail_thread_id' = %s
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
    suggested_route: dict | None = None,
    trusted_internal: bool = False,
    status: str = "todo",
) -> tuple[str, str]:
    """Create a new `todo` issue for an inbound customer email.

    - Bumps companies.issue_counter atomically.
    - Sets identifier = '<issue_prefix>-' || new_counter.
    - Seeds the issue with a first comment carrying the gmail thread metadata.
    - If suggested_route is provided, includes keyword routing info in metadata.

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
            SELECT %s, %s, %s, %s, 'medium',
                   %s::uuid,
                   'operator', bump.issue_counter,
                   bump.issue_prefix || '-' || bump.issue_counter,
                   'customer_email', %s
              FROM bump
            RETURNING id::text, identifier
            """,
            (company_id, company_id, title, description, status, assignee_agent_id, msg.get("id") or ""),
        )
        issue_id, identifier = cur.fetchone()

    metadata = {
        "gmail_thread_id": msg.get("thread_id"),
        "gmail_msg_id": msg.get("id"),
        "inbound_subject": subject,
        "inbound_from": msg.get("from"),
        "trusted_internal": trusted_internal,
    }
    if suggested_route:
        metadata["suggested_route"] = suggested_route
    append_comment(
        pc_conn,
        company_id=company_id,
        issue_id=issue_id,
        body=body or "(empty body)",
        metadata=metadata,
        author_user_id="customer",
    )
    return issue_id, identifier


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
    sender_lc = sender.lower()

    # Loop guard FIRST: skip anything we previously forwarded back to ourselves.
    # Subject prefix is set by forward_to_operator; trumps all other gates.
    if msg["subject"].strip().startswith("[AIB forward]"):
        log.info("skipping already-forwarded message (subject=%s)", msg["subject"][:80])
        mark_read(svc, cfg.mailbox, msg_id)
        return {"action": "skipped_forward", "subject": msg["subject"][:80]}

    # Internal/operator sender path: trusted route. Skip the customers table
    # entirely; build a synthetic Customer and fall through to the shared
    # thread-match / new-issue logic with trusted_internal=True.
    trusted_internal = sender_lc in _INTERNAL_SENDERS
    if trusted_internal:
        log.info("internal sender %s — trusted_internal route", sender)
        customer = _internal_customer(sender)
    else:
        # Operator-from skip: secondary guard for any operator-aliased address
        # that is NOT a known internal sender (e.g. if AIB_OPERATOR_EMAIL is
        # ever pointed at a non-team mailbox). With cass now in
        # _INTERNAL_SENDERS the previous blanket skip would block her real mail.
        if sender_lc == cfg.operator_email.lower():
            log.info("skipping operator-originated message from %s", sender)
            mark_read(svc, cfg.mailbox, msg_id)
            return {"action": "skipped_operator", "sender": sender}

        # Customer lookup → unknown-sender forward.
        customer = lookup_customer(pc_conn, sender, cfg.mailbox)
        if customer is None:
            log.info("unknown sender %s, forwarding to operator", sender)
            issue_id = None
            identifier = None
            if not dry_run:
                forward_to_operator(svc, cfg.mailbox, cfg.operator_email, msg)
                mark_read(svc, cfg.mailbox, msg_id)
                record_pending(pending_conn, msg, "unknown_sender")
                unknown_customer = Customer(
                    customer_id="(unknown)",
                    email=sender,
                    name=None,
                    business_name=None,
                    fqdn=None,
                    contabo_path=None,
                )
                issue_id, identifier = create_issue_for_email(
                    pc_conn, company_id, unknown_customer, msg,
                    assignee_agent_id=MERCER_AGENT_ID,
                    status="blocked",
                )
                pc_conn.commit()
            return {
                "action": "unknown_sender",
                "sender": sender,
                "issue_id": issue_id,
                "identifier": identifier,
            }

    # Known sender — look for an existing open issue on this thread.
    existing_issue_id = find_issue_by_thread(pc_conn, company_id, msg.get("thread_id") or "")

    if existing_issue_id is not None:
        # Append comment.
        metadata = {
            "gmail_thread_id": msg.get("thread_id"),
            "gmail_msg_id": msg.get("id"),
            "inbound_subject": msg.get("subject"),
            "inbound_from": msg.get("from"),
            "trusted_internal": trusted_internal,
        }
        comment_id = append_comment(
            pc_conn,
            company_id=company_id,
            issue_id=existing_issue_id,
            body=(msg.get("body") or "").strip() or "(empty body)",
            metadata=metadata,
            author_user_id="customer",
        )

        # If the matched issue is terminal (done/cancelled), reopen it so the
        # original assignee picks up the follow-up rather than losing context.
        with pc_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE issues
                   SET status = 'todo', completed_at = NULL, updated_at = now()
                 WHERE id = %s AND status IN ('done', 'cancelled')
                RETURNING id
                """,
                (existing_issue_id,),
            )
            reopened = cur.fetchone() is not None
        if reopened:
            append_comment(
                pc_conn,
                company_id=company_id,
                issue_id=existing_issue_id,
                body="(reopened — follow-up received on closed thread)",
                metadata={"system": True, "reopened": True},
                author_user_id="customer",
            )
            log.info("reopened %s due to follow-up on closed thread", existing_issue_id)
        pc_conn.commit()
        identifier = get_identifier(pc_conn, existing_issue_id)
        log.info("appended comment to %s for sender=%s", identifier, sender)

        if not dry_run:
            mark_read(svc, cfg.mailbox, msg_id)
            clear_pending(pending_conn, msg["id"])

        return {
            "action": "comment_appended",
            "issue_id": existing_issue_id,
            "identifier": identifier,
            "comment_id": comment_id,
        }

    # No existing thread -> new issue.
    # Routing priority:
    #   1. To-header alias match (direct)
    #   2. Keyword match on subject + body (direct with suggested_route trail)
    #   3. email_assignee_agent_id env var
    #   4. Mercer (triager fallback)
    subject = (msg.get("subject") or "").strip()
    body = (msg.get("body") or "").strip()
    suggested_route = None

    assignee = match_agent_by_to_header(pc_conn, msg)
    if assignee is None:
        # Alias match failed — try keyword routing.
        kw_agent, kw_keyword, kw_category = match_agent_by_keywords(subject, body)
        if kw_agent:
            assignee = kw_agent
            suggested_route = {
                "method": "keyword",
                "category": kw_category,
                "matched_keyword": kw_keyword,
                "agent_id": kw_agent,
            }
            log.info(
                "keyword route: category=%s keyword=%r -> agent=%s",
                kw_category, kw_keyword, kw_agent,
            )
    if assignee is None:
        assignee = email_assignee_agent_id or MERCER_AGENT_ID
        if suggested_route is None:
            suggested_route = {
                "method": "fallback",
                "category": None,
                "matched_keyword": None,
                "agent_id": assignee,
            }

    issue_id, identifier = create_issue_for_email(
        pc_conn, company_id, customer, msg,
        assignee_agent_id=assignee,
        suggested_route=suggested_route,
        trusted_internal=trusted_internal,
    )
    pc_conn.commit()
    log.info("created issue %s for sender=%s subject=%r", identifier, sender, msg.get("subject"))

    if not dry_run:
        mark_read(svc, cfg.mailbox, msg_id)
        clear_pending(pending_conn, msg["id"])

    return {
        "action": "new_issue",
        "issue_id": issue_id,
        "identifier": identifier,
        "suggested_route": suggested_route,
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
