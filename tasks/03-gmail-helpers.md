---
id: 03
title: Implement Gmail API helpers in poller.py
platform: BACKEND
depends_on: [02]
files_touched: [/home/discnxt/aib/poller.py]
estimate_minutes: 60
estimate_loc: 140
---

## Description
Add all Gmail-side functions to `poller.py`: build the authenticated client with domain-wide delegation, list unread, fetch and parse a message, mark read/unread, reply on the same thread (preserving `References`/`In-Reply-To`), and forward an unknown-sender message to the operator. These are pure helpers — no DB, no rsync, no aider.

## Implementation notes
- Imports to add: `from google.oauth2 import service_account`, `from googleapiclient.discovery import build`, `from googleapiclient.errors import HttpError`.
- `def gmail_client(sa_path: str, subject: str)`: build SA credentials with scopes `["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"]`, call `.with_subject(subject)` for DWD, return `build("gmail", "v1", credentials=creds, cache_discovery=False)`.
- `def list_unread(svc, mailbox: str) -> list[dict]`: `svc.users().messages().list(userId=mailbox, q="is:unread", maxResults=25).execute().get("messages", [])`.
- `def fetch_message(svc, mailbox: str, msg_id: str) -> dict`: `format="full"`. Extract headers `From`, `Subject`, `Message-ID`, `References`, `In-Reply-To`, `threadId`. Body: walk `payload.parts` recursively for the first `text/plain` part; base64url-decode (`base64.urlsafe_b64decode`); fall back to `payload.body.data` for non-multipart. Return `{"id": msg_id, "thread_id": ..., "from": ..., "subject": ..., "body": ..., "message_id_hdr": ..., "references": ...}`.
- `def parse_sender(raw_from: str) -> str`: `email.utils.parseaddr(raw_from)[1].lower().strip()`; raise `ValueError` if empty.
- `def mark_read(svc, mailbox, msg_id)` / `def mark_unread(...)`: `svc.users().messages().modify(userId=mailbox, id=msg_id, body={"removeLabelIds":["UNREAD"]}).execute()` and the inverse `addLabelIds`.
- `def reply(svc, mailbox, thread_id, references, in_reply_to, to_addr, subject, body)`: build `email.message.EmailMessage`, set `To`, `From=mailbox`, `Subject` (prepend `Re: ` if missing), `In-Reply-To`, `References` (append `in_reply_to` if present), `.set_content(body)`. Encode `base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")`. Send via `svc.users().messages().send(userId=mailbox, body={"raw": raw, "threadId": thread_id}).execute()`.
- `def forward_to_operator(svc, mailbox, operator_email, original: dict)`: build a new `EmailMessage`, `Subject=f"[AIB forward] {original['subject']}"`, body = f"From: {original['from']}\nOriginal subject: {original['subject']}\n\n{original['body']}", `To=operator_email`. Send without `threadId`.
- Wrap each Gmail call's HttpError so callers see `RuntimeError(f"gmail {op}: {e}")` — never swallow.

## Acceptance criteria
- [ ] All 7 functions exist with the signatures listed
- [ ] `gmail_client` uses DWD via `.with_subject(mailbox)`
- [ ] `fetch_message` correctly decodes both multipart and non-multipart text bodies
- [ ] `reply` sets `threadId` AND `In-Reply-To`/`References` so Gmail threads it
- [ ] `forward_to_operator` produces a new thread (no threadId) addressed to the operator
- [ ] `HttpError` from any call is re-raised as `RuntimeError` with operation name
- [ ] No TODO comments
- [ ] All error paths handled
- [ ] No placeholder functions or fake data
