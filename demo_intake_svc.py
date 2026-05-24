#!/usr/bin/env python3
"""
demo_intake_svc.py — try-it-live demo intake service (port 8721).

Endpoints:
  POST /demo-intake       — 14-step pipeline: validate → moderate → deploy
  GET  /demo-status/<id>  — returns status/expires_at for a request
  GET  /health            — liveness check

Runs as a systemd service on the workstation (192.168.1.135:8721).
Contabo Caddy reverse-proxies /demo-intake and /demo-status/* via the
demo-tunnel.service autossh reverse tunnel.

stdlib + psycopg2 only (matches contact-form.py policy).
"""
from __future__ import annotations

import base64
import email.mime.text
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8721
MAX_BODY_BYTES = 8 * 1024
OLLAMA_BASE = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"
TTL_MINUTES = 30
ALLOWED_ORIGINS = {"https://discnxt.com", "https://www.discnxt.com"}
VALID_FQDNS = {"waynecrouse.cloud", "isnotreal.site"}
DEPLOY_SCRIPT = "/home/discnxt/aib/deploy-site.sh"
SNAPSHOT_BASE = "/var/sites/{fqdn}/demo-snapshots/{id}"
DIFF_JSON_PATH = "/var/sites/{fqdn}/public/diff/{id}.json"
SITE_PUBLIC = "/var/sites/{fqdn}/public"
GOOGLE_SA_KEY = "/home/discnxt/.secrets/google-agents.json"
GMAIL_SUBJECT = "team@digitaldisconnections.com"
DEMO_FROM = "demo@digitaldisconnections.com"

# ---- rules filter ----
FORBIDDEN_RE = re.compile(
    r"(<[a-zA-Z])|"                        # HTML tags
    r"(https?://)|"                        # URLs
    r"(SELECT\s|INSERT\s|UPDATE\s|DROP\s|DELETE\s)",  # SQL keywords
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("demo-intake")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_rate_lock = Lock()
# {email_hash: last_unix_ts} — in-memory; DB is authoritative for uniqueness
_rate_state: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _db_conn():
    import psycopg2
    db_url = os.environ["DATABASE_URL"]
    return psycopg2.connect(db_url)


def _ollama(prompt: str, timeout: int = 5) -> str:
    """Call Ollama; raises on any failure (fail-closed)."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }).encode()
    req = Request(OLLAMA_BASE, data=payload, method="POST",
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data.get("response", "").strip()


def _send_demo_email(to: str, subject: str, body: str) -> None:
    """Send email via Gmail API using service account + DWD impersonation."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SA_KEY,
        scopes=["https://mail.google.com/"],
    ).with_subject(GMAIL_SUBJECT)

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = email.mime.text.MIMEText(body, "plain")
    msg["From"] = DEMO_FROM
    msg["To"] = to
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


def _take_snapshot(fqdn: str, req_id: str) -> str:
    """Copy public/ to demo-snapshots/<id>/ and return the snapshot path."""
    src = SITE_PUBLIC.format(fqdn=fqdn)
    dst = SNAPSHOT_BASE.format(fqdn=fqdn, id=req_id)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def _deploy(fqdn: str) -> None:
    """Run deploy-site.sh; raise RuntimeError on failure."""
    result = subprocess.run(
        [DEPLOY_SCRIPT, fqdn],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"deploy-site.sh failed rc={result.returncode} "
            f"stderr={result.stderr.decode(errors='replace')[:500]}"
        )


def _restore_snapshot(fqdn: str, snapshot_path: str) -> None:
    dst = SITE_PUBLIC.format(fqdn=fqdn)
    subprocess.run(
        ["rsync", "-av", "--delete", snapshot_path + "/", dst + "/"],
        capture_output=True, timeout=120, check=True,
    )


def _mutate_html(fqdn: str, element: str, before: str, after: str) -> None:
    """In-place replace the matched element text in index.html."""
    idx = os.path.join(SITE_PUBLIC.format(fqdn=fqdn), "index.html")
    with open(idx, "r", encoding="utf-8") as f:
        html = f.read()
    if before not in html:
        raise ValueError(f"before-text not found in index.html: {before[:80]!r}")
    html = html.replace(before, after, 1)
    with open(idx, "w", encoding="utf-8") as f:
        f.write(html)


def _write_diff_json(fqdn: str, req_id: str, change_text: str,
                     element: str, before: str, after: str,
                     deployed_at: str, expires_at: str) -> None:
    path = DIFF_JSON_PATH.format(fqdn=fqdn, id=req_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "request_id": req_id,
            "site_fqdn": fqdn,
            "change_text": change_text,
            "before": before,
            "after": after,
            "element": element,
            "deployed_at": deployed_at,
            "expires_at": expires_at,
        }, f)


# ---------------------------------------------------------------------------
# 14-step pipeline
# ---------------------------------------------------------------------------

def _intake_pipeline(fields: dict[str, str], remote_ip: str) -> dict:
    """Raises ValueError(human-msg) or RuntimeError on hard failures."""
    import psycopg2

    # 1. Validate fields
    email_raw = fields.get("email", "").strip()
    fqdn = fields.get("site_fqdn", "").strip()
    change_text = fields.get("change_text", "").strip()
    honeypot = fields.get("hp_url", "").strip()

    if honeypot:
        raise ValueError("honeypot triggered")
    if not EMAIL_RE.match(email_raw):
        raise ValueError("Invalid email address.")
    if fqdn not in VALID_FQDNS:
        raise ValueError("Unknown demo site.")
    if not change_text:
        raise ValueError("Describe the change you want to try.")
    if len(change_text) > 280:
        raise ValueError("Keep your change to 280 characters.")

    email_h = hash_email(email_raw)

    # 2. Per-email rate limit (in-memory; DB unique index is authoritative)
    now = time.time()
    with _rate_lock:
        if len(_rate_state) > 8192:
            cutoff = now - 3600
            stale = [k for k, v in _rate_state.items() if v < cutoff]
            for k in stale:
                del _rate_state[k]
        last = _rate_state.get(email_h, 0)
        if now - last < 30:
            raise ValueError("Too many requests. Wait a moment and try again.")
        _rate_state[email_h] = now

    # 3. Per-site overlap check (via DB)
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM demo_requests WHERE site_fqdn=%s AND status='deployed'",
                (fqdn,),
            )
            if cur.fetchone():
                raise ValueError(
                    f"A demo is already running on {fqdn}. Try the other site or wait for it to expire."
                )

        # 4. Stage 1 rules (no DB row yet — just reject)
        if FORBIDDEN_RE.search(change_text):
            log.info("rules-filter rejected ip=%s", remote_ip)
            raise ValueError("That change request contains content we can't process.")

        # 5. Stage 2 — Ollama moderation (fail-closed: any exception → reject)
        mod_start = time.monotonic()
        try:
            mod_prompt = (
                f"You are a content moderator. A user wants to change text on a small business website demo.\n"
                f"Change request: {change_text!r}\n"
                f"Reply with exactly one word: APPROVE or REJECT. "
                f"Reject hate speech, spam, defacement, competitor promotions, illegal content. "
                f"Approve business-appropriate text changes (hours, headlines, contact info, descriptions)."
            )
            mod_response = _ollama(mod_prompt, timeout=5)
            mod_latency = int((time.monotonic() - mod_start) * 1000)
            approved = mod_response.upper().startswith("APPROVE")
        except Exception as exc:
            log.warning("Ollama moderation failed (fail-closed): %s", exc)
            # Fail-closed: treat Ollama unavailability as rejection
            approved = False
            mod_latency = int((time.monotonic() - mod_start) * 1000)

        if not approved:
            # No demo_requests row yet — log to journald; moderation_log is post-insert only
            reason_str = mod_response[:200] if 'mod_response' in locals() else "ollama_unavailable"
            log.info("ollama-moderation rejected ip=%s reason=%s", remote_ip, reason_str)
            raise ValueError("That change doesn't look like a valid business update. Please try a different request.")

        # 6. INSERT pending row (UNIQUE violation → 429)
        req_id = str(uuid.uuid4())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO demo_requests(id, email_hash, site_fqdn, status, change_text)
                       VALUES(%s, %s, %s, 'pending', %s)""",
                    (req_id, email_h, fqdn, change_text),
                )
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            raise ValueError("You already have an active demo. Wait for it to expire or try the other site.")

        # 7. Take baseline snapshot
        snapshot_path = _take_snapshot(fqdn, req_id)

        # 8. Edit generation via Ollama (fail → restore snapshot, reject row)
        gen_start = time.monotonic()
        try:
            gen_prompt = (
                f"You are editing the home page of a small business website.\n"
                f"The user's change request: {change_text!r}\n\n"
                f"Read the index.html below and respond with a JSON object (and nothing else) in exactly this form:\n"
                f'{{ "element": "h1|hours|paragraph", "before": "exact current text", "after": "new text" }}\n\n'
                f"Rules: before must be verbatim text from the page. after must match the request. "
                f"element is a label for what changed.\n\n"
                f"index.html excerpt (first 4000 chars):\n"
                f"{_read_index_excerpt(fqdn)}"
            )
            gen_response = _ollama(gen_prompt, timeout=15)
            gen_latency = int((time.monotonic() - gen_start) * 1000)
        except Exception as exc:
            log.warning("Ollama edit-gen failed: %s", exc)
            _restore_and_reject(conn, req_id, snapshot_path, fqdn, "edit_gen_failed")
            raise ValueError("Couldn't generate the edit. Please try a more specific change request.")

        # Parse JSON from Ollama response
        edit = _extract_json(gen_response)
        if not edit or not all(k in edit for k in ("element", "before", "after")):
            _restore_and_reject(conn, req_id, snapshot_path, fqdn, "edit_gen_bad_json")
            raise ValueError("Couldn't parse the edit. Please try a more specific change request.")

        element = str(edit["element"])[:50]
        before_text = str(edit["before"])
        after_text = str(edit["after"])[:500]

        # 9. Mutate index.html
        try:
            _mutate_html(fqdn, element, before_text, after_text)
        except ValueError as exc:
            _restore_and_reject(conn, req_id, snapshot_path, fqdn, f"mutate_failed: {exc}")
            raise ValueError(f"Couldn't apply that change: {exc}")

        # 10. Write diff JSON
        from datetime import datetime, timezone, timedelta
        deployed_at = datetime.now(timezone.utc)
        expires_at = deployed_at + timedelta(minutes=TTL_MINUTES)
        deployed_str = deployed_at.isoformat()
        expires_str = expires_at.isoformat()

        _write_diff_json(fqdn, req_id, change_text, element,
                         before_text, after_text, deployed_str, expires_str)

        # 11. UPDATE row: deployed
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE demo_requests SET status='deployed', expires_at=%s, snapshot_path=%s
                   WHERE id=%s""",
                (expires_str, snapshot_path, req_id),
            )
        conn.commit()

        # 12. Send approval-viz email (non-blocking on failure)
        site_url = f"https://{fqdn}"
        diff_url = f"{site_url}/diff.html?id={req_id}"
        try:
            _send_demo_email(
                to=email_raw,
                subject=f"Your demo change is live on {fqdn}",
                body=(
                    f"Your change is live at {site_url}\n\n"
                    f"What changed: {change_text}\n"
                    f"Diff view: {diff_url}\n\n"
                    f"This demo reverts automatically at {expires_str}.\n\n"
                    f"— The Discnxt team"
                ),
            )
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE demo_requests SET notified_at=NOW() WHERE id=%s", (req_id,)
                )
            conn.commit()
        except Exception as exc:
            log.warning("approval email failed (non-fatal): %s", exc)

        # 13. Run deploy-site.sh; on failure restore snapshot
        try:
            _deploy(fqdn)
        except Exception as exc:
            log.error("deploy failed, restoring snapshot: %s", exc)
            try:
                _restore_snapshot(fqdn, snapshot_path)
                _deploy(fqdn)
            except Exception as exc2:
                log.error("snapshot restore also failed: %s", exc2)
            _restore_and_reject(conn, req_id, snapshot_path, fqdn, f"deploy_failed: {exc}")
            raise ValueError("Deployment failed — we've restored the site. Please try again.")

        # 14. Send live-notification email (non-blocking on failure)
        try:
            _send_demo_email(
                to=email_raw,
                subject=f"Your Discnxt demo is live — {fqdn}",
                body=(
                    f"It's live. See your change at:\n{site_url}\n\n"
                    f"Diff view: {diff_url}\n\n"
                    f"Reverts automatically in {TTL_MINUTES} minutes.\n\n"
                    f"— Discnxt"
                ),
            )
        except Exception as exc:
            log.warning("live-notification email failed (non-fatal): %s", exc)

        return {
            "ok": True,
            "site_url": site_url,
            "diff_url": diff_url,
            "expires_at": expires_str,
            "request_id": req_id,
        }

    finally:
        conn.close()


def _restore_and_reject(conn, req_id: str, snapshot_path: str, fqdn: str, reason: str) -> None:
    """Mark row rejected and restore snapshot (best-effort)."""
    try:
        _restore_snapshot(fqdn, snapshot_path)
    except Exception as exc:
        log.warning("snapshot restore failed during reject: %s", exc)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE demo_requests SET status='rejected' WHERE id=%s", (req_id,)
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB reject update failed: %s", exc)
    log.info("rejected req_id=%s reason=%s", req_id, reason)


def _read_index_excerpt(fqdn: str) -> str:
    idx = os.path.join(SITE_PUBLIC.format(fqdn=fqdn), "index.html")
    with open(idx, "r", encoding="utf-8", errors="replace") as f:
        return f.read(4000)


def _extract_json(text: str) -> dict | None:
    """Extract the first {...} JSON object from text."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def _get_status(req_id: str) -> dict | None:
    if not _UUID_RE.match(req_id):
        return None
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expires_at, site_fqdn, status, change_text FROM demo_requests WHERE id=%s",
                (req_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    expires_at, site_fqdn, status, change_text = row
    return {
        "expires_at": expires_at.isoformat() if expires_at else None,
        "summary": change_text or "",
        "site_fqdn": site_fqdn,
        "status": status,
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "discnxt-demo/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - " + fmt, self.address_string(), *args)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def _respond_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in ("/health", "/demo-intake/health"):
            self._respond_json(200, {"ok": True, "service": "demo-intake"})
            return
        if self.path.startswith("/demo-status/"):
            req_id = self.path[len("/demo-status/"):].strip("/")
            if not req_id:
                self._respond_json(400, {"ok": False, "error": "missing request id"})
                return
            data = _get_status(req_id)
            if data is None:
                self._respond_json(404, {"ok": False, "error": "not found"})
                return
            self._respond_json(200, data)
            return
        self._respond_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/demo-intake":
            self._respond_json(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond_json(413, {"ok": False, "error": "payload too large"})
            return

        raw = self.rfile.read(length)
        ct = self.headers.get("Content-Type", "").lower().split(";")[0].strip()
        if ct == "application/json":
            try:
                fields = json.loads(raw.decode("utf-8"))
                if not isinstance(fields, dict):
                    fields = {}
                fields = {k: str(v) for k, v in fields.items()}
            except Exception:
                fields = {}
        else:
            parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
            fields = {k: v[0] if v else "" for k, v in parsed.items()}

        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        log.info("intake ip=%s fqdn=%s", ip, fields.get("site_fqdn", "?"))

        try:
            result = _intake_pipeline(fields, ip)
            self._respond_json(200, result)
        except ValueError as exc:
            msg = str(exc)
            if "already have an active demo" in msg or "already running" in msg:
                self._respond_json(429, {"ok": False, "error": msg})
            else:
                self._respond_json(400, {"ok": False, "error": msg})
        except Exception as exc:
            log.exception("intake pipeline error: %s", exc)
            self._respond_json(500, {"ok": False, "error": "Something went wrong. Please try again."})


def main() -> int:
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("demo-intake-svc listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
