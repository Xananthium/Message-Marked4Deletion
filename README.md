# Message-Marked4Deletion

> A static-site customer writes plain-English emails. A 5-minute timer reads
> them, hands the body to `aider` running against an Ollama-cloud model, lets
> the model edit the live site directory, commits the change, syncs it back to
> the web server, and replies with the commit SHA. No dashboard. No SDK.
> No agent framework. ~500 lines of Python total.

This is the runtime for "Agent in a Box" — a sellable product where a
customer pays once for a hosted static site plus a yearly retainer, and from
then on they email change requests in plain language.

## The whole architecture in one diagram

```
Workspace inbox  team@digitaldisconnections.com
        │
        │   Gmail API (Service Account + DWD)
        ▼
poller.py  (systemd timer, every 5 min, on workstation)
        │
        │   1. for each unread message:
        │   2.   parse sender → SELECT customer_email FROM customer_sites
        │   3.   pg_try_advisory_lock(hashtext(domain))    # one-edit-per-domain
        │   4.   mark message read    (claim it)
        │   5.   rsync_pull   contabo:/var/www/<domain>/  →  /tmp/aib-<rand>/
        │   6.   write email body to /tmp/aib-<rand>/.aib-msg.txt
        │   7.   subprocess.run(['aider', '--message-file', ...,
        │                        '--model', 'ollama_chat/kimi-k2.6:cloud',
        │                        '--yes', '--auto-commits'])
        │   8.   git rev-parse HEAD: if unchanged → reply apology, bail
        │   9.   rsync_push   /tmp/aib-<rand>/   →   contabo:/var/www/<domain>/
        │  10.   if Caddyfile changed: ssh contabo "sudo caddy reload"
        │  11.   reply to original thread with "Done. Commit abc1234."
        │  12.   finally: pg_advisory_unlock + rmtree
```

Everything pre-aider is deterministic Python. Same email + same DB state →
same outcome every time. The LLM enters exactly once, inside the `aider`
subprocess. After aider returns it's back to plumbing.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3, stdlib + `google-api-python-client` + `psycopg[binary]` + `requests` |
| LLM harness | [`aider`](https://aider.chat) invoked as a subprocess |
| Inference | Ollama cloud (`ollama signin`, model `kimi-k2.6:cloud`) — proxied through the local `ollama` daemon |
| Mailbox | Google Workspace + Gmail API, SA with domain-wide delegation for `gmail.modify` |
| Customer-domain mail | Namecheap email-forwarding (`*@customerdomain.com` → customer's personal inbox) |
| Web serving | Caddy on a single VPS (Contabo or wherever) |
| State | One Postgres database, one table |
| Scheduler | systemd timer, 5 min |

## What's NOT in the box

No FastAPI. No Letta. No Procrastinate. No vector DB. No orchestrator. No
agent pool. No multi-tenant routing layer. No web UI. No admin console.
No tests directory (the smoke test is the operator sending an email).
No `requirements.txt` containing anything you don't pip-install in 30 seconds.

The product surface is a mailbox.

## Repo layout

```
poller.py                 — the 500-line runtime (the entire poller)
schema.sql                — two-table Postgres schema
init-db.sh                — `createdb` + apply schema, idempotent
provision-site.sh         — operator one-shot: DNS + Caddy + email forwarding + DB row
install-systemd.sh        — copy unit files, enable timer
systemd/
  aib-poller.service      — Type=oneshot, runs poller.py
  aib-poller.timer        — OnUnitActiveSec=5min
.env.example              — 12 env vars; copy + fill before first run
requirements.txt          — 4 lines; pipx + pip both work
ARCHITECTURE.md           — function-by-function map of the poller
BUILD-STATUS.md           — output of the smoke-test verification pass
tasks/                    — granular dev tasks (build artifact, kept for reference)
agentic/planning/         — the experience + tech-stack documents
                            the architecture was synthesized from
```

## Getting started

```bash
# 1. clone + install
git clone https://github.com/Xananthium/Message-Marked4Deletion.git
cd Message-Marked4Deletion
pip install -r requirements.txt
pipx install aider-chat

# 2. ollama (cloud) signin so kimi-k2.6:cloud routes through your daemon
ollama signin

# 3. create the Postgres DB + schema
./init-db.sh

# 4. fill in real values
cp .env.example .env
$EDITOR .env

# 5. enable the timer
sudo ./install-systemd.sh

# 6. provision your first site (DNS + Caddy + DB row, all in one)
./provision-site.sh customerdomain.com customer@example.com
```

## Onboarding a customer (full path)

1. Customer pays.
2. Operator: `./provision-site.sh customerdomain.com customer@example.com`
   - Creates `/var/www/customerdomain.com/` on the VPS as a git repo.
   - Adds a Caddy block + reloads.
   - Sets Namecheap DNS: A → VPS IP, MX → email-forwarding, SPF/DKIM/DMARC.
   - Sets `*@customerdomain.com` to forward to `customer@example.com`.
   - Inserts `customer_sites(customer@example.com, customerdomain.com,
     /var/www/customerdomain.com, active)`.
3. Operator sends a welcome email manually.
4. From then on, customer emails `team@your-workspace.com` in plain English.
   The poller does the rest.

## Customer-facing instructions

The customer's whole interface is one inbox. Examples that work:

- *"the big text up top should say 'Sal's Pizza — Open Til 10' instead of the
  placeholder"*
- *"the menu link is too small, can you make it bigger and bolder"*
- *"add my phone number 412-555-0123 next to the address in the footer"*
- *"the green is ugly, can it be more like a warm orange"*
- *"add a page for tonight's specials with the items I'm listing below"*

The customer never sees the source. The model reads the file tree, figures
out what they mean, edits the right thing, commits, and replies.

## Operational notes

- Concurrency: serialized per-domain via a Postgres advisory lock keyed on
  `hashtext(domain)`. The poller is safe to run more often, but 5 min is
  generally plenty.
- Idempotency: Gmail's UNREAD label is the source of truth for "what's
  queued." Crashes mid-process leave the message unread, so the next tick
  retries cleanly. Successful processing flips UNREAD off.
- Triage: anything not in `customer_sites`, or anything aider can't make
  sense of, gets a row in `pending_emails` and is forwarded to the operator.
- Inference cost: model time only — the workstation's RAM footprint is
  ~300 MB per aider call, and aider exits between calls. There's no resident
  service to babysit.
- Swappable brain: change `AIB_MODEL` in `.env` to point at a different
  Ollama model, an OpenAI-compatible endpoint, or anything aider supports.
  The shell around aider doesn't care.

## Known V1 rough edges

- Unknown-sender mail stays UNREAD per spec, so without intervention it gets
  re-forwarded to the operator every 5 min. Future: query `pending_emails`
  for the `gmail_msg_id` before forwarding, skip if already logged.
- The first aider run on an existing site can take several minutes if the
  model decides to rewrite many files in one go; the subprocess timeout is
  600 seconds.
- `provision-site.sh` refuses to provision protected domains
  (digitaldisconnections.com, discnxt.com, pittsburgh-geeks.com) so the
  Workspace MX records on those operator-owned domains never get
  overwritten by the customer pipeline. Edit the protected list at the top
  of the script if you need to.

## License

MIT — see [LICENSE](LICENSE).
