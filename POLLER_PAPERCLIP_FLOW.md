# aib-poller -> paperclip issue flow (v2)

How customer email becomes a tracked paperclip issue.

## Overview

The `aib-poller` runs every 5 minutes (via `aib-poller.timer`) and reads
unread mail from the shared mailbox `team@digitaldisconnections.com`.
Each inbound message is funneled into the **paperclip issue queue** so
the request is tracked from `todo` -> `in_progress` -> `in_review` -> `done`
(or `cancelled`). The issue is the **system of record** for the request;
the poller no longer runs aider directly.

This was a deliberate operator decision: while paperclip agents are
paused, the operator handles requests manually but still wants every
customer email visible in the paperclip board with a stable identifier,
not lost in an inbox.

## The flow (per inbound message)

1. **Fetch** the message from Gmail (via the shared service account).
2. **Identify** the sender by stripping the address part of the From header.
3. **Look up** the sender in the NEW schema:
   ```sql
   SELECT c.id, c.email, c.name, c.business_name, d.fqdn, d.contabo_path
   FROM customers c
   LEFT JOIN domains d ON d.customer_id = c.id AND d.status = 'active'
   WHERE LOWER(c.email) = LOWER(:sender) AND c.status = 'active'
   ORDER BY (d.agent_mailbox = :mailbox) DESC NULLS LAST,
            d.updated_at DESC NULLS LAST
   LIMIT 1;
   ```
   The JOIN is LEFT because a freshly promoted customer may not yet have
   a domain. For multi-domain customers, we prefer the domain whose
   `agent_mailbox` matches the inbound mailbox, else the most recently
   updated.
4. **Unknown sender** -> forward to the operator
   (`AIB_OPERATOR_EMAIL=cass@digitaldisconnections.com`), mark the
   message read, record `pending_emails` with `reason='unknown_sender'`
   so we don't re-forward on the next tick. No paperclip issue is
   created; the operator decides whether to promote the sender to a
   customer.
5. **Known sender** -> see if the inbound gmail thread already maps to
   an open issue:
   ```sql
   SELECT i.id FROM issue_comments ic
   JOIN issues i ON i.id = ic.issue_id
   WHERE ic.company_id = :company_id
     AND ic.metadata->>'gmail_thread_id' = :thread_id
     AND i.status NOT IN ('done', 'cancelled')
     AND i.hidden_at IS NULL
   ORDER BY ic.created_at ASC LIMIT 1;
   ```
   - **Found** -> append a new `issue_comments` row (author_type=`'user'`,
     author_user_id=`'customer'`) carrying the new gmail msg metadata.
     Reply to the customer: "got your follow-up, added to ticket DIS-N".
     **We never change the issue's status** -- the assigned agent or
     operator decides when to move it.
   - **Not found** -> create a new issue and seed it with a first
     comment that records the gmail thread. The first comment's
     `metadata->>'gmail_thread_id'` is how step 5 finds the thread next
     time.
6. **Mark** the gmail message read and commit DB.
7. **No aider** in v2. The paperclip issue is the system of record; the
   actual site-edit work happens via a follow-up flow (agent execution,
   or operator-driven manual edit) outside this poller's scope.

## Issue creation specifics

| Field | Value |
| --- | --- |
| `title` | first 80 chars of subject (or first line of body if no subject) |
| `description` | structured header (customer, business, domain, IDs) + raw body |
| `status` | `'todo'` |
| `priority` | `'medium'` |
| `assignee_agent_id` | `NULL` (agents paused; operator triages) |
| `created_by_user_id` | `'operator'` (text literal — `issues.created_by_user_id` is `text`) |
| `origin_kind` | `'customer_email'` |
| `origin_id` | the gmail msg-id |
| `issue_number` | bumped atomically via `UPDATE companies SET issue_counter = issue_counter + 1` |
| `identifier` | `<issue_prefix>-<new_counter>` (e.g. `DIS-95`) |

The counter bump and INSERT happen in one CTE so two concurrent pollers
can never collide on `issue_number`. (The poller is a one-shot timer so
this is belt-and-braces, but the property is cheap.)

## Schema additions

**None.** `issue_comments.metadata` is already `jsonb`, so we store
`{gmail_thread_id, gmail_msg_id, inbound_subject, inbound_from}` there
directly. No migration was needed. Future replies on the same gmail
thread find the issue via `metadata->>'gmail_thread_id'`.

## How agents pick up issues

When a paperclip agent unpauses, it sees new `todo` issues on the
company's board. The standard heartbeat flow already claims `todo`
issues into `in_progress`, posts comments as it works, moves to
`in_review` when waiting for operator sign-off, and `done` when the
work is shipped. Agents do not need to know the issue came from email —
the gmail metadata in the first comment is informational.

While agents are paused, the operator handles all of this manually by
editing `issues.status` directly (`todo` -> `in_progress` -> `in_review`
-> `done` / `cancelled`). The customer's email reply ACK already
references the identifier `DIS-N`, so the operator can find the ticket
fast.

## How replies on the same thread route

Gmail preserves a stable `threadId` across the whole conversation. The
poller writes the inbound `threadId` to the first comment's `metadata`
on issue creation; subsequent replies on the same thread are routed to
the same issue. If a customer starts a brand-new email (new `threadId`),
that's a new issue — by design, since it likely is a new request.

## What "cancel" and "close" look like

There is no special poller flow for these. The operator (or eventually
the assigned agent) sets `issues.status` to `'done'` or `'cancelled'`.
Once that's done, the thread-lookup query in step 5 will no longer find
the issue (the `status NOT IN ('done', 'cancelled')` filter excludes
it), so any further reply on the same gmail thread will open a fresh
issue. That is the correct behavior — a customer following up after
a closed ticket is a new request.

## Operator overrides

- **Manual issue creation:** insert directly into `issues` with
  `origin_kind='operator_manual'`; bump `companies.issue_counter` in
  the same transaction.
- **Manual reply on a thread:** send the reply from the Gmail UI; the
  poller doesn't care. Reuse the existing thread so future inbound
  replies still route to the same paperclip issue via the stored
  `gmail_thread_id`.
- **Force-rebind a thread to an existing issue:** insert a synthetic
  `issue_comments` row on that issue with
  `metadata->>'gmail_thread_id'` = the gmail threadId. Next inbound on
  that thread will be appended there.

## Files & service

- `/home/discnxt/aib/poller_v2.py` -- the active poller.
- `/home/discnxt/aib/poller.py` -- v1, kept untouched as fallback.
- `/etc/systemd/system/aib-poller.service` -- ExecStart points at
  `poller_v2.py`. Two `EnvironmentFile=` lines: `.env` (gmail + DSN for
  pending_emails) and `paperclip-poller-api.env` (paperclip DSN + API
  key + company id).
- `/etc/systemd/system/aib-poller.timer` -- unchanged, every 5 min.

## Two databases, on purpose

| DB | role |
| --- | --- |
| `agentinabox` (legacy) | `pending_emails` retry table only |
| `paperclip` (master) | `customers`, `domains`, `issues`, `issue_comments` |

The poller opens one connection to each. The `customer_sites` table in
paperclip is the legacy view and is **not** touched by v2.
