# Experience plan — Agent in a Box V1

## Product

"Agent in a Box" — $1000 onboarding + yearly retainer. Customer gets a hosted
static site on Contabo and an email address they can write to whenever they
want a change. Changes happen within minutes (one 5-min poll cycle).

## Personas

- **Customer** — has a small business. Owns their personal email
  (e.g. joe@gmail.com). Doesn't want to learn HTML, git, or hosting.
- **Operator** — discnxt. Provisions sites, sets DNS, sends welcome mail,
  watches the queue for failures. One person.

## End-to-end customer journey

1. Operator runs `provision-site.sh customerdomain.com joe@gmail.com`:
   - Creates `/var/www/customerdomain.com/` on Contabo (git repo).
   - Adds Caddy entry routing the domain to that dir.
   - Sets Namecheap DNS: A → Contabo IP. MX → email-forwarding service.
   - Sets `*@customerdomain.com` → joe@gmail.com forward.
   - Inserts row into `customer_sites(email='joe@gmail.com',
     domain='customerdomain.com', contabo_path='/var/www/customerdomain.com')`.
   - An aider run designs the initial site from a brief.
2. Operator emails Joe: "Your site is live at customerdomain.com."
3. Joe replies (or sends a new mail) to `team@digitaldisconnections.com`:
   "change the headline to 'Best laundromat in Pittsburgh'"
4. Within 5 minutes:
   - Poller reads the mail from Workspace inbox via Gmail API.
   - Looks up FROM `joe@gmail.com` in `customer_sites` → resolves to
     `customerdomain.com`.
   - rsyncs the site dir from Contabo to a local tmp dir.
   - Runs `aider --message-file=<email body> --model
     ollama_chat/kimi-k2.6:cloud --yes --auto-commits` in the tmp dir.
   - rsyncs back to Contabo.
   - ssh contabo: `caddy reload` (only if Caddyfile changed).
   - Replies to Joe: "Done. Commit abc1234. Live at customerdomain.com."

## Failure paths (single line each)

- Sender not in `customer_sites` → forward email to operator, mark unread.
- aider error / no diff → reply "couldn't make that change automatically;
  operator notified" + write row to `pending_emails` for triage.
- Two emails about the same domain at once → serialize per-domain via
  Postgres advisory lock keyed on the domain.

## Success criteria

End-to-end: send `team@digitaldisconnections.com` an email "change the H1
on digitaldisconnections.com homepage to 'Hello from aider'" from a sender
that exists in `customer_sites`. Within 10 minutes:
- New commit in `/var/www/digitaldisconnections.com/.git/log` on Contabo.
- `https://digitaldisconnections.com/` reflects new H1.
- Sender receives reply with commit SHA + summary.
