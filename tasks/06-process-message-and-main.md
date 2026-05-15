---
id: 06
title: Implement process_message() state machine and main() loop in poller.py
platform: BACKEND
depends_on: [03, 04, 05]
files_touched: [/home/discnxt/aib/poller.py]
estimate_minutes: 60
estimate_loc: 150
---

## Description
Glue the helpers from tasks 03-05 into the per-message orchestration sequence from ARCHITECTURE.md and the `main()` entrypoint the systemd unit invokes. After this task, `poller.py` is feature-complete and runnable. Total poller.py must stay ≤200 lines (see pmdraft.md budget).

## Implementation notes
- `def process_message(svc, conn: psycopg.Connection, cfg: Config, msg_id: str) -> None`:
  1. `msg = fetch_message(svc, cfg.mailbox, msg_id)`.
  2. `sender = parse_sender(msg["from"])`.
  3. `site = lookup_site(conn, sender)`. If `None`: `forward_to_operator(svc, cfg.mailbox, cfg.operator_email, msg)`; `mark_unread(svc, cfg.mailbox, msg_id)` (was already unread, but explicit); `record_pending(conn, msg, "unknown_sender")`; return.
  4. `if not try_lock_domain(conn, site.domain): log.info("domain %s locked, skipping", site.domain); return` (leave unread).
  5. `mark_read(svc, cfg.mailbox, msg_id)` — claim it. Wrap remainder in `try/except/finally`.
  6. `local_dir = tempfile.mkdtemp(prefix="aib-", dir=cfg.tmp_root)`.
  7. `rsync_pull(cfg.ssh_alias, site.contabo_path, local_dir)`; `pre_sha = git_head_sha(local_dir)`.
  8. Write `msg["body"]` to `body_path = os.path.join(local_dir, ".aib-msg.txt")`.
  9. `result = run_aider(local_dir, body_path, cfg.model)`; `post_sha = git_head_sha(local_dir)`.
  10. If `result.returncode != 0 or post_sha == pre_sha or post_sha is None`: build reply body `"Couldn't apply that change automatically; the operator has been notified."`; `reply(...)`; `forward_to_operator(...)`; `record_pending(conn, msg, "aider_no_diff" if result.returncode == 0 else "aider_error")`; return (the `finally` still runs).
  11. Else `rsync_push(local_dir, cfg.ssh_alias, site.contabo_path)`. If `caddyfile_changed(local_dir)`: try `ssh_caddy_reload(cfg.ssh_alias)`; on RuntimeError, `record_pending(conn, msg, "caddy_reload")` and add `" (deploy may be stale)"` to the reply body.
  12. `reply(...)` body: `f"Done. Commit {post_sha[:7]}.\n\n{result.summary}"`.
  13. `except Exception as e: log.exception(...); mark_unread(svc, cfg.mailbox, msg_id); record_pending(conn, msg, f"exception:{e!r}"[:500])`.
  14. `finally: unlock_domain(conn, site.domain); shutil.rmtree(local_dir, ignore_errors=True)`.
- `def main() -> int`:
  - `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")`.
  - `cfg = load_config()`.
  - `with psycopg.connect(cfg.dsn) as conn: svc = gmail_client(cfg.sa_path, cfg.mailbox); for m in list_unread(svc, cfg.mailbox): try: process_message(svc, conn, cfg, m["id"]) except Exception: log.exception("process_message failed for %s", m["id"])`.
  - Return 0.
- `if __name__ == "__main__": sys.exit(main())`.
- The outer `for` loop must not let one bad message kill the run — wrap each call in try/except as shown.
- Verify total line count of `poller.py` is ≤200 after this task; if not, tighten (combine imports, drop redundant comments).

## Acceptance criteria
- [ ] All 10 steps from ARCHITECTURE.md "Orchestration sequence" are implemented in order
- [ ] Every branch in the "Error paths" table from ARCHITECTURE.md is reachable
- [ ] The `finally` block always runs `unlock_domain` + `rmtree`, even on early-return branches
- [ ] `main()` does not crash the systemd run if one message raises — it logs and continues
- [ ] `poller.py` is ≤200 lines total
- [ ] `python3 -c "import ast; ast.parse(open('/home/discnxt/aib/poller.py').read())"` passes
- [ ] No TODO comments
- [ ] All error paths handled (every Error-paths row maps to a code branch)
- [ ] No placeholder functions or fake data
