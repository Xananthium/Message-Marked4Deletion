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
import datetime
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

import urllib.request

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

_GMAIL_SCOPES = ["https://mail.google.com/"]


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


def _collect_attachments(payload: dict) -> list[dict]:
    """Walk the MIME tree and collect attachment descriptors.

    Returns list of dicts: filename, mimeType, attachmentId, size, data (inline b64url).
    Skips text/plain, text/html, and container multipart/* parts with no filename.

    Inline images in multipart/related may arrive with no filename and no
    attachmentId when Gmail API only populates body.size.  We collect any
    leaf non-text part that carries content so inline images are not lost.
    """
    results: list[dict] = []
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    filename = (payload.get("filename") or "").strip()
    attachment_id = body.get("attachmentId", "")
    data = body.get("data", "")
    size = body.get("size", 0)

    # Collect any non-text leaf part that carries content (filename, attachmentId,
    # inline data, or non-zero size).  This catches inline images in
    # multipart/related that may lack both filename and attachmentId.
    is_leaf = not payload.get("parts")
    has_content = filename or attachment_id or data or (is_leaf and size > 0)

    if has_content and mime not in ("text/plain", "text/html"):
        results.append({
            "filename": filename or "attachment",
            "mimeType": mime,
            "attachmentId": attachment_id,
            "size": size,
            "data": data,
        })

    for part in payload.get("parts", []):
        results.extend(_collect_attachments(part))

    return results


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


def _walk_delivery_status(payload: dict) -> str:
    """Walk the MIME tree and return the message/delivery-status body (RFC 3464)."""
    mime = payload.get("mimeType", "")
    if mime == "message/delivery-status":
        data = (payload.get("body") or {}).get("data", "")
        return _decode_part(data) if data else ""
    for part in payload.get("parts", []):
        result = _walk_delivery_status(part)
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
        "delivery_status": _walk_delivery_status(payload),
        "message_id_hdr": headers.get("Message-ID", ""),
        "references": headers.get("References", ""),
        "in_reply_to": headers.get("In-Reply-To", ""),
        "attachments": _collect_attachments(payload),
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


_ATTACHMENT_BASE = pathlib.Path("/home/discnxt/aib/attachments")


def save_attachments(
    svc,
    mailbox: str,
    gmail_msg_id: str,
    issue_id: str,
    attachments: list[dict],
    dry_run: bool = False,
) -> list[dict]:
    """Fetch and save Gmail attachments to /home/discnxt/aib/attachments/{issue_id}/.

    Returns list of {path, filename, mimeType, size} for each saved file.
    In dry_run mode the Gmail API is not called and no files are written;
    descriptors are returned with projected paths so callers can still log them.
    """
    if not attachments:
        return []

    save_dir = _ATTACHMENT_BASE / issue_id
    if not dry_run:
        save_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for att in attachments:
        filename = att.get("filename") or "attachment"
        safe_name = re.sub(r"[^\w.\-]", "_", filename) or "attachment"
        mime = att.get("mimeType", "")

        raw_data = att.get("data", "")
        if not raw_data and att.get("attachmentId"):
            if dry_run:
                saved.append({"path": str(save_dir / safe_name), "filename": filename, "mimeType": mime, "size": att.get("size", 0)})
                continue
            try:
                resp = svc.users().messages().attachments().get(
                    userId=mailbox,
                    messageId=gmail_msg_id,
                    id=att["attachmentId"],
                ).execute()
                raw_data = resp.get("data", "")
            except HttpError as exc:
                log.warning("failed to fetch attachment %s from msg %s: %s", filename, gmail_msg_id, exc)
                continue

        if not raw_data:
            if att.get("size", 0) > 0:
                log.warning(
                    "attachment %s (%s, %d bytes) has no inline data and no attachmentId; "
                    "possible inline image with missing Gmail API fields",
                    filename, mime, att.get("size", 0),
                )
            continue

        padding = 4 - len(raw_data) % 4
        try:
            file_bytes = base64.urlsafe_b64decode(raw_data + "=" * (padding % 4))
        except Exception as exc:
            log.warning("failed to decode attachment %s: %s", filename, exc)
            continue

        if dry_run:
            saved.append({"path": str(save_dir / safe_name), "filename": filename, "mimeType": mime, "size": len(file_bytes)})
            continue

        out_path = save_dir / safe_name
        if out_path.exists():
            stem, suffix = out_path.stem, out_path.suffix
            i = 1
            while out_path.exists():
                out_path = save_dir / f"{stem}_{i}{suffix}"
                i += 1
        out_path.write_bytes(file_bytes)
        log.info("saved attachment %s -> %s (%d bytes)", filename, out_path, len(file_bytes))
        saved.append({"path": str(out_path), "filename": filename, "mimeType": mime, "size": len(file_bytes)})

    return saved


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


def fetch_pending_due(
    conn: psycopg.Connection,
    known_customer_emails: set[str] | None = None,
) -> list[tuple[str, dict]]:
    """Return list of (gmail_msg_id, message_dict) for pending emails due for retry.

    Returns rows where retry_count < MAX_RETRIES and next_retry <= now().
    For unknown_sender rows, only includes them when the sender's parsed address
    appears in known_customer_emails (lowercase set from the customers table).
    Pass None to skip all unknown_sender retries (old safe behavior).
    Other rows are included unconditionally.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gmail_msg_id, sender, subject, reason FROM pending_emails "
            "WHERE retry_count < %s AND next_retry <= now() "
            "ORDER BY next_retry",
            (MAX_RETRIES,)
        )
        rows = cur.fetchall()

    result = []
    for gmail_msg_id, sender, subject, reason in rows:
        if reason == "unknown_sender":
            if not known_customer_emails:
                continue
            sender_addr = email.utils.parseaddr(sender or "")[1].lower().strip()
            if sender_addr not in known_customer_emails:
                continue
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
_OPEN_STATUSES = ['todo', 'in_progress', 'in_review', 'blocked']

# ---------------------------------------------------------------------------
# Auto-close filter: machine-generated email patterns
# ---------------------------------------------------------------------------

MACHINE_SENDER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'no-reply@dmarc\.google\.com', re.I), 'dmarc_report'),
    (re.compile(r'mailer-daemon@', re.I), 'bounce'),
    (re.compile(r'^postmaster@', re.I), 'postmaster'),
    (re.compile(r'noreply@.*\.bounces\.google\.com', re.I), 'bounce'),
]

MACHINE_SUBJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'^Report domain:', re.I), 'dmarc_report'),
    (re.compile(r'delivery status notification', re.I), 'bounce'),
    (re.compile(r'undeliverable', re.I), 'bounce'),
    (re.compile(r'failure notice', re.I), 'bounce'),
    (re.compile(r'mail delivery failed', re.I), 'bounce'),
    (re.compile(r'returned mail', re.I), 'bounce'),
    (re.compile(r'^auto-reply:', re.I), 'machine_report'),
    (re.compile(r'^out of office', re.I), 'machine_report'),
    (re.compile(r'\[spam\]', re.I), 'spam'),
]

_KNOWN_MACHINE_CATEGORIES = frozenset(
    ['dmarc_report', 'bounce', 'postmaster', 'machine_report', 'spam']
)

LLM_AUTO_CLOSE_PROMPT_TEMPLATE = (
    "You are an email classifier. Classify the following email as one of:\n"
    "dmarc_report, bounce, postmaster, machine_report, spam, human\n\n"
    "Respond with ONLY the category label — nothing else.\n\n"
    "From: {sender}\n"
    "Subject: {subject}\n"
    "Body (first 200 chars): {body_excerpt}\n\n"
    "Category:"
)


def classify_inbound(sender: str, subject: str, body: str) -> tuple[str, str] | None:
    """Classify an inbound email as machine-generated or return None for human mail.

    Returns (category, rule_matched) if machine-generated, None otherwise.
    Fail-open: LLM errors return None so normal triage continues.
    """
    for pattern, category in MACHINE_SENDER_PATTERNS:
        if pattern.search(sender):
            return (category, f'sender:{pattern.pattern}')

    for pattern, category in MACHINE_SUBJECT_PATTERNS:
        if pattern.search(subject):
            return (category, f'subject:{pattern.pattern}')

    # LLM fallback — local Ollama only
    try:
        model = os.environ.get('AIB_MODEL', 'llama3')
        prompt = LLM_AUTO_CLOSE_PROMPT_TEMPLATE.format(
            sender=sender,
            subject=subject,
            body_excerpt=body[:200],
        )
        payload = json.dumps({'model': model, 'prompt': prompt, 'stream': False}).encode()
        req = urllib.request.Request(
            'http://localhost:11434/api/generate',
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        category = data.get('response', '').strip().lower()
        if category in _KNOWN_MACHINE_CATEGORIES:
            return (category, f'llm:{model}')
    except Exception:
        pass  # fail-open: LLM unreachable → normal triage

    return None


# ---------------------------------------------------------------------------
# Bounce recipient extraction and outreach flagging
# ---------------------------------------------------------------------------

# Ordered from most-specific (RFC 3464 machine-readable) to broadest fallback.
_BOUNCE_RECIPIENT_PATTERNS: list[re.Pattern] = [
    re.compile(r'Final-Recipient:\s*(?:rfc822;\s*)?([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I),
    re.compile(r'Original-Recipient:\s*(?:rfc822;\s*)?([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I),
    re.compile(r'X-Failed-Recipients:\s*([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I),
    # Google Workspace style
    re.compile(r'The following address(?:es)? had.*?(?:errors?|failures?)[\s\S]{0,200}?([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I),
    # Outlook/MS Exchange
    re.compile(r'Delivery has failed to (?:these|this) recipient[s\s]*(?:or groups?)?[\s:]*\n+\s*([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I),
    # Generic "failed permanently" or "delivery failed" with address on next line
    re.compile(r'(?:failed permanently|delivery failed|undeliverable)[^\n]{0,100}\n+\s*([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I),
]


def extract_bounce_recipient(subject: str, body: str, delivery_status: str = "") -> str | None:
    """Extract the failed recipient address from a DSN.

    Searches the RFC 3464 delivery-status payload first, then the human-readable
    body, then the subject.  Returns a lowercase email string or None.
    """
    # delivery_status is highest fidelity (machine-readable per RFC 3464)
    search_text = f"{delivery_status}\n{subject}\n{body}"
    for pattern in _BOUNCE_RECIPIENT_PATTERNS:
        m = pattern.search(search_text)
        if m:
            candidate = m.group(1).strip().lower().rstrip('.')
            if '@' in candidate and '.' in candidate.split('@')[-1]:
                return candidate
    return None


_DSN_STATUS_RE = re.compile(r'Status:\s*(\d\.\d+\.\d+)', re.I)
_DSN_DIAGNOSTIC_RE = re.compile(r'Diagnostic-Code:\s*smtp;\s*(\d{3})\b', re.I)


def extract_dsn_status(delivery_status: str, body: str = "") -> tuple[str | None, bool]:
    """Parse the DSN status code from delivery-status or body text.

    Returns (status_code, is_permanent).  is_permanent is True for 5xx,
    False for 4xx.  Returns (None, False) if no code found.
    """
    search_text = f"{delivery_status}\n{body}"
    m = _DSN_STATUS_RE.search(search_text)
    if m:
        code = m.group(1)
        return code, code.startswith("5")
    m = _DSN_DIAGNOSTIC_RE.search(search_text)
    if m:
        smtp_code = m.group(1)
        return smtp_code, smtp_code.startswith("5")
    return None, False


_OPS_DSN = os.environ.get(
    "OPS_DSN",
    "postgresql:///discnxt_ops",
)


def _record_bounce_suppression(
    bounced_email: str, status_code: str | None, is_permanent: bool
) -> None:
    """Write a suppression row to discnxt_ops.email_suppressions.

    4xx → domain suppression for 24h.  5xx → permanent address suppression.
    No status code → domain suppression for 24h (safe default).
    """
    domain = bounced_email.rsplit("@", 1)[-1].lower() if "@" in bounced_email else ""
    if is_permanent:
        target, scope = bounced_email.lower(), "address"
        reason = f"hard bounce ({status_code or 'unknown'})"
        expires = None
    else:
        target, scope = domain, "domain"
        reason = f"transient bounce ({status_code or 'unknown'})"
        expires = "now() + interval '24 hours'"

    try:
        with psycopg.connect(_OPS_DSN) as conn:
            with conn.cursor() as cur:
                if expires:
                    cur.execute(
                        """
                        INSERT INTO email_suppressions
                            (address_or_domain, scope, reason, status_code, expires_at)
                        VALUES (%s, %s, %s, %s, now() + interval '24 hours')
                        """,
                        (target, scope, reason, status_code),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO email_suppressions
                            (address_or_domain, scope, reason, status_code, expires_at)
                        VALUES (%s, %s, %s, %s, NULL)
                        """,
                        (target, scope, reason, status_code),
                    )
            conn.commit()
        log.info(
            "bounce suppression: %s scope=%s reason=%s permanent=%s",
            target, scope, reason, is_permanent,
        )
    except Exception:
        log.exception("failed to record bounce suppression for %s", bounced_email)


def flag_outreach_bounce(pc_conn: psycopg.Connection, bounced_email: str) -> bool:
    """Mark a lead and its most-recent sent contact_attempt as bounced.

    Sets leads.stage = 'do_not_contact' and contact_attempts.outcome = 'bounced'
    for the most-recent email attempt that was still 'sent'.
    Does NOT commit — caller is responsible.
    Returns True if a lead was found and updated.
    """
    with pc_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM leads WHERE LOWER(contact_email) = LOWER(%s)"
            " AND stage != 'do_not_contact' LIMIT 1",
            (bounced_email,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        lead_id = row[0]

        cur.execute(
            """
            UPDATE contact_attempts
               SET outcome = 'bounced', outcome_at = now()
             WHERE id = (
               SELECT id FROM contact_attempts
                WHERE lead_id = %s AND channel = 'email' AND outcome = 'sent'
                ORDER BY attempted_at DESC
                LIMIT 1
             )
            """,
            (lead_id,),
        )
        cur.execute(
            "UPDATE leads SET stage = 'do_not_contact', updated_at = now() WHERE id = %s",
            (lead_id,),
        )

    log.info("bounce: flagged lead %s (%s) as do_not_contact", lead_id, bounced_email)
    return True


def auto_close_message(
    svc,
    pc_conn: psycopg.Connection,
    pending_conn: psycopg.Connection,
    cfg,
    company_id: str,
    msg: dict,
    sender: str,
    category: str,
    rule_matched: str,
    llm_used: bool,
) -> None:
    """Create a done issue for a machine-generated email and audit-log it."""
    machine_customer = Customer(
        customer_id='(machine)',
        email=sender,
        name=None,
        business_name=None,
        fqdn=None,
        contabo_path=None,
    )
    issue_id, identifier = create_issue_for_email(
        pc_conn,
        company_id,
        machine_customer,
        msg,
        assignee_agent_id=None,
        status='done',
        origin_kind='machine_report',
    )
    seed_body = (
        f'Auto-closed: {category} (rule: {rule_matched})\n\n'
        f'From: {msg["from"]}\n'
        f'Subject: {msg.get("subject", "")}'
    )
    append_comment(
        pc_conn,
        company_id=company_id,
        issue_id=issue_id,
        body=seed_body,
        metadata={
            'auto_close': True,
            'category': category,
            'rule_matched': rule_matched,
            'llm_used': llm_used,
        },
        author_user_id='system',
    )
    mark_read(svc, cfg.mailbox, msg['id'])

    # For bounces: extract the failed recipient, flag CRM, record suppression.
    if category == 'bounce':
        bounced = extract_bounce_recipient(
            msg.get('subject', ''),
            msg.get('body', ''),
            msg.get('delivery_status', ''),
        )
        if bounced:
            try:
                flagged = flag_outreach_bounce(pc_conn, bounced)
                log.info('bounce: extracted recipient=%s flagged=%s', bounced, flagged)
            except Exception:
                log.exception('bounce outreach flagging failed for %s', bounced)

            # Record suppression based on DSN status code
            try:
                status_code, is_permanent = extract_dsn_status(
                    msg.get('delivery_status', ''), msg.get('body', ''),
                )
                _record_bounce_suppression(bounced, status_code, is_permanent)
            except Exception:
                log.exception('bounce suppression recording failed for %s', bounced)
        else:
            log.info('bounce: could not extract recipient from msg=%s', msg.get('id', ''))

    with pc_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_auto_closes
              (company_id, gmail_msg_id, sender, subject, category, rule_matched, llm_used, issue_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid)
            """,
            (
                company_id,
                msg.get('id', ''),
                sender,
                msg.get('subject', ''),
                category,
                rule_matched,
                llm_used,
                issue_id,
            ),
        )
    pc_conn.commit()


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
    """Find an active customer by email or sender domain.

    Pass 1: exact match on customers.email.
    Pass 2: if no match, match sender domain against active domains rows.
    Returns None if neither pass resolves.
    """
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
    if row is not None:
        return Customer(*row)

    # Pass 2: domain alias fallback — sender is from a customer's registered domain.
    sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""
    if not sender_domain:
        return None
    with pc_conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id::text, c.email, c.name, c.business_name,
                   d.fqdn, d.contabo_path
            FROM customers c
            JOIN domains d ON d.customer_id = c.id AND d.status = 'active'
            WHERE LOWER(d.fqdn) = LOWER(%s)
              AND c.status = 'active'
            ORDER BY
                (d.agent_mailbox = %s) DESC NULLS LAST,
                d.updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (sender_domain, mailbox),
        )
        row = cur.fetchone()
    if row is None:
        return None
    log.info("lookup_customer: domain_alias_match=True sender=%s domain=%s customer_id=%s", sender_email, sender_domain, row[0])
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


def find_open_issues_by_sender(
    pc_conn: psycopg.Connection, company_id: str, sender_email: str
) -> list[tuple[str, str]]:
    """Return (issue_id, identifier) list for open issues from sender_email, oldest first.

    Matches on inbound_sender_email (normalized) or inbound_from (raw header).
    """
    with pc_conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id::text, i.identifier
            FROM issues i
            WHERE i.company_id = %s
              AND i.status = ANY(%s)
              AND EXISTS (
                SELECT 1 FROM issue_comments ic
                WHERE ic.issue_id = i.id
                  AND ic.company_id = %s
                  AND (
                    LOWER(ic.metadata->>'inbound_sender_email') = %s
                    OR ic.metadata->>'inbound_from' ILIKE %s
                  )
              )
            ORDER BY i.updated_at DESC
            """,
            (company_id, _OPEN_STATUSES, company_id, sender_email.lower(), f"%{sender_email}%"),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _thread_attach_body(sender: str, msg: dict, saved_attachments: list[dict] | None = None) -> tuple[str, dict]:
    """Build (comment_body, metadata) for attaching a new email to an existing open issue."""
    subject_line = (msg.get("subject") or "").strip()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = f"**Inbound email from {sender} at {ts}** — Subject: {subject_line}"
    body = f"{header}\n\n{(msg.get('body') or '').strip() or '(empty body)'}"
    if saved_attachments:
        lines = "\n".join(
            f"- `{a['filename']}` ({a['mimeType']}, {a['size']} bytes) → `{a['path']}`"
            for a in saved_attachments
        )
        body += f"\n\n**Attachments ({len(saved_attachments)}):**\n{lines}"
    metadata: dict = {
        "gmail_thread_id": msg.get("thread_id"),
        "gmail_msg_id": msg.get("id"),
        "inbound_subject": msg.get("subject"),
        "inbound_from": msg.get("from"),
        "inbound_sender_email": sender,
        "thread_attach": True,
    }
    if saved_attachments:
        metadata["attachments"] = [
            {"path": a["path"], "filename": a["filename"], "mimeType": a["mimeType"]}
            for a in saved_attachments
        ]
    return body, metadata


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
    origin_kind: str = "customer_email",
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
                   %s, %s
              FROM bump
            RETURNING id::text, identifier
            """,
            (company_id, company_id, title, description, status, assignee_agent_id, origin_kind, msg.get("id") or ""),
        )
        issue_id, identifier = cur.fetchone()

    metadata = {
        "gmail_thread_id": msg.get("thread_id"),
        "gmail_msg_id": msg.get("id"),
        "inbound_subject": subject,
        "inbound_from": msg.get("from"),
        "inbound_sender_email": email.utils.parseaddr(msg.get("from") or "")[1].lower().strip(),
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

    # Auto-close filter: catch machine-generated email before any queue work.
    classification = classify_inbound(sender_lc, msg.get('subject', ''), msg.get('body', ''))
    if classification is not None:
        category, rule_matched = classification
        llm_used = rule_matched.startswith('llm:')
        log.info('auto-close: category=%s rule=%s msg=%s', category, rule_matched, msg_id)
        if not dry_run:
            auto_close_message(svc, pc_conn, pending_conn, cfg, company_id, msg, sender_lc, category, rule_matched, llm_used)
        return {'action': 'auto_closed', 'category': category, 'rule_matched': rule_matched}

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
                open_by_sender = find_open_issues_by_sender(pc_conn, company_id, sender)
                if len(open_by_sender) == 1:
                    target_issue_id, target_identifier = open_by_sender[0]
                    forward_to_operator(svc, cfg.mailbox, cfg.operator_email, msg)
                    saved_atts = save_attachments(svc, cfg.mailbox, msg["id"], target_issue_id, msg.get("attachments") or [])
                    comment_body, meta = _thread_attach_body(sender, msg, saved_atts or None)
                    meta["trusted_internal"] = False
                    comment_id = append_comment(
                        pc_conn, company_id=company_id, issue_id=target_issue_id,
                        body=comment_body, metadata=meta, author_user_id="customer",
                    )
                    mark_read(svc, cfg.mailbox, msg_id)
                    clear_pending(pending_conn, msg["id"])
                    pc_conn.commit()
                    log.info(
                        "thread-attached unknown-sender email from %s onto %s",
                        sender, target_identifier,
                    )
                    return {
                        "action": "comment_appended",
                        "issue_id": target_issue_id,
                        "identifier": target_identifier,
                        "comment_id": comment_id,
                        "thread_attach": True,
                        "attachments": saved_atts,
                    }
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
                saved_atts = save_attachments(svc, cfg.mailbox, msg["id"], issue_id, msg.get("attachments") or [])
                if saved_atts:
                    att_lines = "\n".join(
                        f"- `{a['filename']}` ({a['mimeType']}, {a['size']} bytes) → `{a['path']}`"
                        for a in saved_atts
                    )
                    append_comment(
                        pc_conn, company_id=company_id, issue_id=issue_id,
                        body=f"**Attachments ({len(saved_atts)}):**\n{att_lines}",
                        metadata={"attachments": [{"path": a["path"], "filename": a["filename"], "mimeType": a["mimeType"]} for a in saved_atts]},
                        author_user_id="customer",
                    )
                if len(open_by_sender) > 1:
                    oldest_id, oldest_identifier = open_by_sender[0]
                    append_comment(
                        pc_conn, company_id=company_id, issue_id=oldest_id,
                        body=(
                            f"**Related thread opened as {identifier}.**\n"
                            f"Another email from {sender} arrived. "
                            f"Check {identifier} — Mercer can merge if it's the same conversation."
                        ),
                        metadata={"system": True, "related_issue": issue_id},
                        author_user_id="customer",
                    )
                pc_conn.commit()
            return {
                "action": "unknown_sender",
                "sender": sender,
                "issue_id": issue_id,
                "identifier": identifier,
            }

    # Known sender — route purely on (sender, open DIS). gmail thread_id is
    # stored in comment metadata for forensics only; it is never a routing key.
    open_by_sender = find_open_issues_by_sender(pc_conn, company_id, sender)

    if len(open_by_sender) == 1:
        target_issue_id, target_identifier = open_by_sender[0]
        saved_atts = save_attachments(svc, cfg.mailbox, msg["id"], target_issue_id, msg.get("attachments") or [], dry_run=dry_run)
        comment_body, meta = _thread_attach_body(sender, msg, saved_atts or None)
        meta["trusted_internal"] = trusted_internal
        comment_id = append_comment(
            pc_conn, company_id=company_id, issue_id=target_issue_id,
            body=comment_body, metadata=meta, author_user_id="customer",
        )
        pc_conn.commit()
        log.info("appended email from %s onto %s", sender, target_identifier)
        if not dry_run:
            mark_read(svc, cfg.mailbox, msg_id)
            clear_pending(pending_conn, msg["id"])
        return {
            "action": "comment_appended",
            "issue_id": target_issue_id,
            "identifier": target_identifier,
            "comment_id": comment_id,
            "thread_attach": True,
            "attachments": saved_atts,
        }

    if len(open_by_sender) > 1:
        # Multiple open issues: attach to most-recently-updated, post a conflict
        # note on each of the others so Paulina / the assignee can merge or close.
        target_issue_id, target_identifier = open_by_sender[0]
        saved_atts = save_attachments(svc, cfg.mailbox, msg["id"], target_issue_id, msg.get("attachments") or [], dry_run=dry_run)
        comment_body, meta = _thread_attach_body(sender, msg, saved_atts or None)
        meta["trusted_internal"] = trusted_internal
        comment_id = append_comment(
            pc_conn, company_id=company_id, issue_id=target_issue_id,
            body=comment_body, metadata=meta, author_user_id="customer",
        )
        for conflict_id, conflict_identifier in open_by_sender[1:]:
            append_comment(
                pc_conn, company_id=company_id, issue_id=conflict_id,
                body=(
                    f"**Routing conflict:** new email from {sender} arrived while "
                    f"this issue was open and was attached to {target_identifier} "
                    f"(most recently updated). Merge or close as appropriate."
                ),
                metadata={"system": True, "conflict_target": target_issue_id},
                author_user_id="customer",
            )
        pc_conn.commit()
        log.info(
            "multi-open: attached email from %s to %s; flagged %d other(s)",
            sender, target_identifier, len(open_by_sender) - 1,
        )
        if not dry_run:
            mark_read(svc, cfg.mailbox, msg_id)
            clear_pending(pending_conn, msg["id"])
        return {
            "action": "comment_appended",
            "issue_id": target_issue_id,
            "identifier": target_identifier,
            "comment_id": comment_id,
            "thread_attach": True,
            "conflict_count": len(open_by_sender) - 1,
            "attachments": saved_atts,
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
    saved_atts = save_attachments(svc, cfg.mailbox, msg["id"], issue_id, msg.get("attachments") or [], dry_run=dry_run)
    if saved_atts:
        att_lines = "\n".join(
            f"- `{a['filename']}` ({a['mimeType']}, {a['size']} bytes) → `{a['path']}`"
            for a in saved_atts
        )
        append_comment(
            pc_conn, company_id=company_id, issue_id=issue_id,
            body=f"**Attachments ({len(saved_atts)}):**\n{att_lines}",
            metadata={"attachments": [{"path": a["path"], "filename": a["filename"], "mimeType": a["mimeType"]} for a in saved_atts]},
            author_user_id="customer",
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
        "attachments": saved_atts,
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

        # Snapshot known customer emails so unknown_sender retries only fire
        # for senders who have since been added to the customers table.
        with pc_conn.cursor() as cur:
            cur.execute("SELECT LOWER(email) FROM customers WHERE status = 'active'")
            known_emails: set[str] = {row[0] for row in cur.fetchall()}

        # Retry pending emails first.
        for msg_id, _ in fetch_pending_due(pending_conn, known_customer_emails=known_emails):
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
