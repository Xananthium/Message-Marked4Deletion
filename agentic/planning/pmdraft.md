# PM draft — tech stack for Agent in a Box V1

## Approved plan reference

Full plan: `/home/discnxt/.claude/plans/nooooooo-it-s-not-forwardingf-squishy-clarke.md`

The operator approved a deliberately minimal stack. Treat the constraints
below as hard — anything beyond this list is out of scope and must be
rejected during architecture review.

## Stack (fixed — do not propose alternatives)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3 (system python3, no virtualenv unless absolutely needed) | |
| Deps | stdlib + `google-api-python-client` + `psycopg[binary]` + `requests` | nothing else |
| LLM harness | `aider` (pip install aider-chat) | invoked as subprocess; never imported |
| Inference | Ollama cloud, model `kimi-k2.6:cloud` | local `ollama` daemon proxies cloud after `ollama signin` |
| Mailbox | Workspace `team@digitaldisconnections.com` via Gmail API | DWD service account at `~/.secrets/google-agents.json` |
| Customer-domain mail | Namecheap email-forwarding | `*@customerdomain.com` → customer's personal inbox |
| Site delivery | Contabo + Caddy (existing) | new sites at `/var/www/<domain>/` |
| State | Postgres on `:5432`, database `agentinabox`, one table `customer_sites` | system Postgres 18 already running |
| Scheduler | systemd timer (5 min) | no Procrastinate, no Celery, no FastAPI server |
| DNS provisioning | Namecheap API | key in `~/.secrets/namecheap.env` |
| SSH to Contabo | alias in `~/.ssh/config` | already configured |

## Hard prohibitions

- ❌ No FastAPI, HTMX, web server, REST API
- ❌ No Letta, Procrastinate, Celery, Redis, RabbitMQ
- ❌ No ORM (raw SQL via psycopg only)
- ❌ No vector DB, embeddings, memory store
- ❌ No multi-process workers, no agent pools
- ❌ No Docker, no containers
- ❌ No new wrapper script around aider or ollama
- ❌ No config DSL, no YAML schemas, no plugin system
- ❌ No "future-proofing" abstractions

## File budget

Total V1 implementation should fit in:

- `poller.py` ≤ 200 lines
- `schema.sql` ≤ 30 lines (one table)
- `provision-site.sh` ≤ 150 lines
- `aib-poller.service` + `aib-poller.timer` ≤ 30 lines combined
- `.env.example` ≤ 20 lines

Total: ≤ ~430 lines of code across all files. If architecture proposes
more, simplify.

## Platforms in scope

Single platform: **BACKEND** (a Python script + a shell script + systemd
units). No frontend, no mobile, no API. Skip per-platform decomposition.

## Working directory

All product code lives under `/home/discnxt/aib/`. Cycle artifacts
(`agentic/planning/*.md`, `agentic/db/agentic.db`, `tasks/*.md`,
`ARCHITECTURE.md`, `BLUEPRINT.md`) also live under `/home/discnxt/aib/`.
