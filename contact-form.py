#!/usr/bin/env python3
"""
discnxt.com contact-form endpoint.

Tiny stdlib-only HTTP server that:
  - Receives POSTs from https://discnxt.com/api/contact
  - Validates the payload + honeypot field
  - Rate-limits to 1 request per IP per 60s (in-memory dict)
  - Sends the form contents to hello@discnxt.com via /usr/sbin/sendmail
    (forwarded by Namecheap email forwarding to the operator's inbox)
  - Returns JSON {ok: true, redirect: "/thank-you.html"} on success

Runs as a systemd service on the workstation (192.168.1.135:8765).
Caddy on Contabo reverse-proxies /api/contact -> http://192.168.1.135:8765/contact.

Tech-stack policy: vanilla Python stdlib only. No Django for one endpoint.
Free-on-Namecheap email forwarding stays the actual delivery channel; this
script is only a JS-progressive-enhancement convenience layer so the user
doesn't have their mail client pop open mid-flow.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8765
RECIPIENT = "hello@discnxt.com"
SENDER = "contact-form@discnxt.com"
MAX_BODY_BYTES = 32 * 1024  # 32KB plenty for a contact form
RATE_LIMIT_WINDOW_SEC = 60
ALLOWED_ORIGINS = {
    "https://discnxt.com",
    "https://www.discnxt.com",
    "http://localhost",
    "http://127.0.0.1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("contact-form")

# {ip: last_request_unix_ts}
_rate_state: dict[str, float] = {}
_rate_lock = Lock()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def rate_limit_check(ip: str) -> bool:
    """Return True if the request is allowed (not rate-limited)."""
    now = time.time()
    with _rate_lock:
        # Garbage-collect old entries opportunistically
        if len(_rate_state) > 4096:
            cutoff = now - RATE_LIMIT_WINDOW_SEC
            for k in [k for k, v in _rate_state.items() if v < cutoff]:
                del _rate_state[k]
        last = _rate_state.get(ip, 0)
        if now - last < RATE_LIMIT_WINDOW_SEC:
            return False
        _rate_state[ip] = now
    return True


def parse_payload(raw: bytes, content_type: str) -> dict[str, str]:
    """Accept either application/x-www-form-urlencoded or application/json."""
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == "application/json":
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        if not isinstance(obj, dict):
            return {}
        return {k: str(v) for k, v in obj.items()}
    # default: form-urlencoded
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in parsed.items()}


def validate(fields: dict[str, str]) -> tuple[bool, str]:
    # Honeypot: any value in "company_website" means bot
    if fields.get("company_website", "").strip():
        return False, "honeypot triggered"
    required = ["name", "business", "url", "goals", "email"]
    for k in required:
        if not fields.get(k, "").strip():
            return False, f"missing {k}"
    if not EMAIL_RE.match(fields.get("email", "").strip()):
        return False, "invalid email"
    # cheap sanity bounds
    for k, v in fields.items():
        if len(v) > 8000:
            return False, f"{k} too long"
    return True, ""


def send_mail(fields: dict[str, str], remote_ip: str) -> None:
    msg = EmailMessage()
    msg["From"] = SENDER
    msg["To"] = RECIPIENT
    msg["Reply-To"] = fields.get("email", SENDER)
    msg["Subject"] = (
        f"[discnxt contact] {fields.get('business','(no business)')} "
        f"— {fields.get('name','(no name)')}"
    )
    body_lines = [
        "New contact-form submission from discnxt.com",
        "",
        f"Name:        {fields.get('name','')}",
        f"Business:    {fields.get('business','')}",
        f"Email:       {fields.get('email','')}",
        f"Phone:       {fields.get('phone','')}",
        f"Current URL: {fields.get('url','')}",
        f"Platform:    {fields.get('platform','')}",
        "",
        "Goals / change request:",
        fields.get("goals", ""),
        "",
        "---",
        f"Submitter IP: {remote_ip}",
        f"Received:     {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
    ]
    msg.set_content("\n".join(body_lines))
    # /usr/sbin/sendmail -t reads recipients from the To: header
    proc = subprocess.run(
        ["/usr/sbin/sendmail", "-t", "-oi"],
        input=msg.as_bytes(),
        check=False,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"sendmail failed rc={proc.returncode} stderr={proc.stderr.decode(errors='replace')[:500]}"
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "discnxt-contact/1.0"

    # Quieter access log; goes to journald via systemd
    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - " + fmt, self.address_string(), *args)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def _client_ip(self) -> str:
        # Caddy on Contabo proxies in; trust X-Forwarded-For when present
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

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
        if self.path in ("/health", "/contact/health"):
            self._respond_json(200, {"ok": True, "service": "discnxt-contact"})
            return
        self._respond_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/contact", "/api/contact"):
            self._respond_json(404, {"ok": False, "error": "not found"})
            return
        ip = self._client_ip()
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond_json(413, {"ok": False, "error": "payload too large"})
            return
        raw = self.rfile.read(length)
        fields = parse_payload(raw, self.headers.get("Content-Type", ""))
        ok, err = validate(fields)
        if not ok:
            log.info("validation failed ip=%s reason=%s", ip, err)
            self._respond_json(400, {"ok": False, "error": err})
            return
        # Only rate-limit AFTER passing validation — otherwise a failed
        # first-try-with-typo locks the user out for 60s.
        if not rate_limit_check(ip):
            log.info("rate-limited ip=%s", ip)
            self._respond_json(429, {"ok": False, "error": "Slow down. Try again in a minute."})
            return
        try:
            send_mail(fields, ip)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_mail failed: %s", exc)
            self._respond_json(500, {"ok": False, "error": "couldn't send. email hello@discnxt.com directly."})
            return
        log.info(
            "delivered ip=%s name=%r business=%r email=%r",
            ip,
            fields.get("name", "")[:80],
            fields.get("business", "")[:80],
            fields.get("email", "")[:120],
        )
        self._respond_json(200, {"ok": True, "redirect": "/thank-you.html"})


def main() -> int:
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("discnxt-contact listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
