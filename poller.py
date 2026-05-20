import os
import sys
import dataclasses
import pathlib
import tempfile
import subprocess
import shutil
import base64
import email.utils
import email.message
import logging
from datetime import datetime, timezone, timedelta
import psycopg

def maybe_decrypt(path: str) -> str:
    """If path ends with .enc.json, decrypt with sops and return temp file path."""
    if path.endswith('.enc.json'):
        import tempfile
        import subprocess
        # Use sops from PATH
        sops_path = shutil.which('sops')
        if not sops_path:
            raise RuntimeError('sops not found in PATH')
        # Decrypt to a temporary file
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

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("aib")

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


_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def gmail_client(sa_path: str, subject: str):
    # Decrypt if encrypted
    decrypted_path = maybe_decrypt(sa_path)
    try:
        if decrypted_path != sa_path:
            # Read decrypted JSON from temporary file
            import json
            with open(decrypted_path, 'r') as f:
                info = json.load(f)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=_GMAIL_SCOPES
            ).with_subject(subject)
            # Clean up temp file
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


# ---------------------------------------------------------------------------
# Unsubscribe / opt-out detection
# ---------------------------------------------------------------------------

_UNSUBSCRIBE_KEYWORDS = [
    "unsubscribe", "stop", "opt out", "opt-out",
    "remove", "no further", "do not contact",
]


def is_unsubscribe_request(msg: dict) -> bool:
    """Return True if *msg* appears to be an unsubscribe/opt-out request.

    Checks: (1) recipient contains unsubscribe@, (2) subject or body
    contains any known opt-out keyword.
    """
    # Check recipient addresses — both the delivery target and original To
    for hdr in (msg.get("to", ""), msg.get("to_header_raw", "")):
        addr = email.utils.parseaddr(hdr)[1].lower()
        if "unsubscribe" in addr:
            return True

    # Check message content for opt-out keywords
    text = f"{msg.get('subject', '')} {msg.get('body', '')}".lower()
    return any(kw in text for kw in _UNSUBSCRIBE_KEYWORDS)


def process_unsubscribe(conn: psycopg.Connection, sender_email: str) -> None:
    """Find the lead matching *sender_email* and mark as do-not-contact.

    Sets do_not_contact_until to 5 years from now, appends the
    do_not_contact marketing tag, sets stage, and logs a timestamped
    note.  Commits the transaction on success.

    If no lead is found the call is a no-op (log only).
    """
    cutoff = datetime.now(timezone.utc) + timedelta(days=365 * 5)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    note_entry = f"[{timestamp}] Opt-out request honored (unsubscribe/stop)"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, notes FROM leads WHERE LOWER(contact_email) = %s",
            (sender_email.lower(),),
        )
        row = cur.fetchone()
        if row is None:
            log.info("unsubscribe: no lead found for %s", sender_email)
            return

        lead_id, existing_notes = row
        new_notes = (
            f"{existing_notes}\n{note_entry}".strip()
            if existing_notes
            else note_entry
        )

        cur.execute(
            "UPDATE leads SET"
            "  do_not_contact_until = %s,"
            "  stage = 'do_not_contact',"
            "  marketing_tags = array_append(marketing_tags, %s),"
            "  notes = %s,"
            "  updated_at = now()"
            " WHERE id = %s",
            (cutoff, "do_not_contact", new_notes, lead_id),
        )
    conn.commit()
    log.info(
        "unsubscribe: lead %s (%s) marked do_not_contact until %s",
        lead_id, sender_email, cutoff.isoformat(),
    )


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


def reply(
    svc,
    mailbox: str,
    thread_id: str,
    references: str,
    in_reply_to: str,
    to_addr: str,
    subject: str,
    body: str,
) -> None:
    msg = email.message.EmailMessage()
    msg["To"] = to_addr
    msg["From"] = mailbox
    msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = f"{references} {in_reply_to}".strip() if references else in_reply_to
        msg["References"] = refs
    elif references:
        msg["References"] = references
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    try:
        svc.users().messages().send(
            userId=mailbox, body={"raw": raw, "threadId": thread_id}
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"gmail reply: {e}") from e


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




# ---------------------------------------------------------------------------
# Task 04: DB helpers — site lookup, advisory lock, pending_emails
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Site:
    customer_email: str
    domain: str
    contabo_path: str
    status: str


def lookup_site(conn: psycopg.Connection, sender_email: str) -> "Site | None":
    """Return the active Site for sender_email, or None if not found / inactive."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT customer_email, domain, contabo_path, status"
            " FROM customer_sites"
            " WHERE customer_email = %s AND status = 'active'",
            (sender_email,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Site(*row)


def try_lock_domain(conn: psycopg.Connection, domain: str) -> bool:
    """Attempt a session-level advisory lock keyed on hashtext(domain).

    Returns True if the lock was acquired, False if already held by another
    connection.  The caller MUST call unlock_domain(conn, domain) in a
    finally block — the lock is tied to this exact connection.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (domain,))
        row = cur.fetchone()
    return bool(row[0])


def unlock_domain(conn: psycopg.Connection, domain: str) -> None:
    """Release the session-level advisory lock for domain.

    Logs a warning (does not raise) if the lock was not held — this keeps
    finally blocks clean even when try_lock_domain returned False.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (domain,))
        row = cur.fetchone()
    if not row[0]:
        log.warning("unlock_domain: lock was not held for domain=%r", domain)


def record_pending(conn: psycopg.Connection, msg: dict, reason: str) -> None:
    """Insert or update a row in pending_emails, then commit.

    Idempotent on gmail_msg_id: a duplicate call updates the reason and
    refreshes created_at so the row reflects the latest failure context.
    Increments retry_count and schedules next retry with exponential backoff.
    psycopg.Error is not caught here — it bubbles to the per-message handler.
    """
    MAX_RETRIES = 5
    BASE_DELAY_SECONDS = 300  # 5 minutes

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
        # Reconstruct minimal message dict with fields process_message expects
        msg = {
            "id": gmail_msg_id,
            "from": sender or "",
            "subject": subject or "",
            "body": "",  # body not stored in pending_emails; will be fetched fresh
            "thread_id": "",  # will be fetched fresh
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
# Task 05: site mutation helpers — rsync, aider, git, caddy
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    cwd: str | None = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Run *cmd* and return the CompletedProcess.

    If *check* is True and the process exits non-zero, raises RuntimeError
    with the first 500 chars of stderr — enough for journald without flooding.
    """
    cp = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(f"{cmd[0]} rc={cp.returncode}: {cp.stderr.strip()[:500]}")
    return cp


@dataclasses.dataclass(frozen=True)
class AiderResult:
    returncode: int
    stdout: str
    stderr: str
    summary: str  # first non-empty line of stdout, truncated to 200 chars


def rsync_pull(ssh_alias: str, contabo_path: str, local_dir: str) -> None:
    """Pull site files from Contabo into a local working directory.

    Trailing slashes on both sides ensure rsync copies directory *contents*,
    not the directory itself.
    """
    _run(["rsync", "-a", "--delete", f"{ssh_alias}:{contabo_path}/", f"{local_dir}/"])


def rsync_push(local_dir: str, ssh_alias: str, contabo_path: str) -> None:
    """Push local working directory back to Contabo.

    --checksum skips files whose content is unchanged even if mtime differs,
    preventing spurious overwrites after an aider run that touched timestamps.
    """
    _run(["rsync", "-a", "--delete", "--checksum", f"{local_dir}/", f"{ssh_alias}:{contabo_path}/"])


def git_head_sha(local_dir: str) -> str | None:
    """Return the current HEAD SHA of the repo at *local_dir*.

    Returns None for a repo with no commits yet (e.g. freshly init'd by
    provision-site.sh before any content is committed).
    """
    cp = _run(["git", "rev-parse", "HEAD"], cwd=local_dir, check=False)
    return cp.stdout.strip() if cp.returncode == 0 else None


def run_aider(local_dir: str, body_path: str, model: str) -> AiderResult:
    """Run aider against *body_path* in *local_dir* and always return an AiderResult.

    Never raises on non-zero aider exit — the caller (process_message) decides
    how to handle failure based on returncode and stdout content.
    """
    cp = _run(
        ["aider", "--message-file", body_path, "--model", model,
         "--yes", "--auto-commits", "--no-pretty"],
        cwd=local_dir,
        check=False,
        timeout=900,
    )
    summary = next(
        (line.strip() for line in cp.stdout.splitlines() if line.strip()), ""
    )[:200]
    return AiderResult(
        returncode=cp.returncode,
        stdout=cp.stdout,
        stderr=cp.stderr,
        summary=summary,
    )


def caddyfile_changed(local_dir: str) -> bool:
    """Return True if the most recent commit in *local_dir* touched the Caddyfile.

    Handles two cases:
    - Single-commit repo (no HEAD~1): returns True when Caddyfile exists,
      because this IS the commit that introduced it.
    - Multi-commit repo: checks git diff --name-only HEAD~1..HEAD.
    Returns False when Caddyfile is absent — no reload needed.
    """
    if not pathlib.Path(local_dir, "Caddyfile").exists():
        return False
    cp_head = _run(["git", "rev-parse", "--verify", "HEAD~1"], cwd=local_dir, check=False)
    if cp_head.returncode != 0:
        # Single commit — Caddyfile exists so it was part of this commit.
        return True
    cp = _run(["git", "diff", "--name-only", "HEAD~1", "HEAD"], cwd=local_dir, check=False)
    return "Caddyfile" in cp.stdout.splitlines()


def ssh_caddy_reload(ssh_alias: str) -> None:
    """Reload Caddy on the remote host via SSH.

    Uses a 60-second timeout; caddy reload is near-instant so anything longer
    indicates a hung connection or misconfiguration.
    """
    _run(
        ["ssh", ssh_alias, "sudo", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"],
        timeout=60,
    )

# ---------------------------------------------------------------------------
# Task 06: process_message() state machine and main() entrypoint
# ---------------------------------------------------------------------------


def process_message(svc, conn: psycopg.Connection, cfg: Config, msg_id: str) -> None:
    """Orchestrate a single inbound Gmail message through the AIB state machine."""
    # Step 1-2: fetch and identify sender
    msg = fetch_message(svc, cfg.mailbox, msg_id)
    sender = parse_sender(msg["from"])

    # Step 1b: skip operator-originated messages to prevent forwarding loops.
    # The operator replying to an [AIB forward] would otherwise be picked up,
    # re-processed, and re-forwarded — creating an infinite loop.
    if sender.lower() == cfg.operator_email.lower():
        log.info("skipping operator-originated message from %s", sender)
        mark_read(svc, cfg.mailbox, msg_id)
        return

    # Step 1c: skip already-forwarded messages as a secondary loop guard.
    if msg["subject"].strip().startswith("[AIB forward]"):
        log.info("skipping already-forwarded message (subject=%s)", msg["subject"][:80])
        mark_read(svc, cfg.mailbox, msg_id)
        return

    # Step 2a: unsubscribe/opt-out detection — handle silently, no Paperclip issue
    if is_unsubscribe_request(msg):
        log.info("unsubscribe detected from %s (thread=%s)", sender, msg["thread_id"])
        process_unsubscribe(conn, sender)
        mark_read(svc, cfg.mailbox, msg_id)
        clear_pending(conn, msg["id"])
        return

    # Step 3: site lookup — unknown sender path
    # Forward once, mark READ so we never re-forward the same message.
    # Operator triages via the audit row in pending_emails if they want history.
    site = lookup_site(conn, sender)
    if site is None:
        log.info("unknown sender %s, forwarding to operator", sender)
        forward_to_operator(svc, cfg.mailbox, cfg.operator_email, msg)
        mark_read(svc, cfg.mailbox, msg_id)
        record_pending(conn, msg, "unknown_sender")
        return

    # Step 4: advisory lock — leave unread so next tick retries
    if not try_lock_domain(conn, site.domain):
        log.info("domain %s locked, skipping", site.domain)
        return

    # Step 5: claim the message; everything from here runs under try/except/finally
    mark_read(svc, cfg.mailbox, msg_id)
    local_dir: str | None = None
    try:
        # Step 6: pull site files and record pre-aider sha
        local_dir = tempfile.mkdtemp(prefix="aib-", dir=cfg.tmp_root)
        rsync_pull(cfg.ssh_alias, site.contabo_path, local_dir)
        pre_sha = git_head_sha(local_dir)

        # Step 7-8: write request body, run aider, record post sha
        body_path = os.path.join(local_dir, ".aib-msg.txt")
        pathlib.Path(body_path).write_text(msg["body"], encoding="utf-8")
        result = run_aider(local_dir, body_path, cfg.model)
        post_sha = git_head_sha(local_dir)

        # Step 9: handle aider failure or no-diff
        if result.returncode != 0 or post_sha == pre_sha or post_sha is None:
            reason = "aider_no_diff" if result.returncode == 0 else "aider_error"
            log.warning("aider did not produce a commit (%s) for domain=%s", reason, site.domain)
            apology = "Couldn't apply that change automatically; the operator has been notified."
            reply(
                svc, cfg.mailbox,
                msg["thread_id"], msg["references"], msg["in_reply_to"],
                msg["from"], msg["subject"], apology,
            )
            forward_to_operator(svc, cfg.mailbox, cfg.operator_email, msg)
            record_pending(conn, msg, reason)
            return

        # Step 10: push changes; conditionally reload Caddy
        rsync_push(local_dir, cfg.ssh_alias, site.contabo_path)
        reply_body = f"Done. Commit {post_sha[:7]}.\n\n{result.summary}"
        if caddyfile_changed(local_dir):
            try:
                ssh_caddy_reload(cfg.ssh_alias)
            except RuntimeError as exc:
                log.warning("caddy reload failed: %s", exc)
                record_pending(conn, msg, "caddy_reload")
                reply_body += " (deploy may be stale)"

        # Step 11: reply with success
        reply(
            svc, cfg.mailbox,
            msg["thread_id"], msg["references"], msg["in_reply_to"],
            msg["from"], msg["subject"], reply_body,
        )
        clear_pending(conn, msg["id"])

    except Exception as exc:
        log.exception("process_message error for msg_id=%s domain=%s", msg_id, site.domain)
        mark_unread(svc, cfg.mailbox, msg_id)
        record_pending(conn, msg, f"exception:{exc!r}"[:500])

    finally:
        unlock_domain(conn, site.domain)
        if local_dir is not None:
            shutil.rmtree(local_dir, ignore_errors=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    with psycopg.connect(cfg.dsn) as conn:
        svc = gmail_client(cfg.sa_path, cfg.mailbox)

        # First, retry pending emails that are due
        for msg_id, _ in fetch_pending_due(conn):
            try:
                log.info("retrying pending email %s", msg_id)
                process_message(svc, conn, cfg, msg_id)
            except Exception:
                log.exception("process_message retry failed for %s", msg_id)

        # Then process new unread messages
        for m in list_unread(svc, cfg.mailbox):
            try:
                process_message(svc, conn, cfg, m["id"])
            except Exception:
                log.exception("process_message failed for %s", m["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
