---
id: 05
title: Implement rsync / git / aider / caddy-reload helpers in poller.py
platform: BACKEND
depends_on: [02]
files_touched: [/home/discnxt/aib/poller.py]
estimate_minutes: 50
estimate_loc: 130
---

## Description
Add the subprocess-driven mutation helpers: pull the site from Contabo to a local tmp dir, run aider against an email body, detect whether anything changed, push back, and reload Caddy only when the Caddyfile differs. These are the only functions in poller.py that shell out.

## Implementation notes
- One shared internal helper: `def _run(cmd: list[str], cwd: str | None = None, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess`. Use `subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)`. If `check` and returncode != 0, raise `RuntimeError(f"{cmd[0]} rc={cp.returncode}: {cp.stderr.strip()[:500]}")`.
- `def rsync_pull(ssh_alias: str, contabo_path: str, local_dir: str)`: `_run(["rsync", "-a", "--delete", f"{ssh_alias}:{contabo_path}/", f"{local_dir}/"])`. Trailing slashes are required — enforce in code.
- `def rsync_push(local_dir: str, ssh_alias: str, contabo_path: str)`: `_run(["rsync", "-a", "--delete", "--checksum", f"{local_dir}/", f"{ssh_alias}:{contabo_path}/"])`.
- `def git_head_sha(local_dir: str) -> str | None`: `_run(["git", "rev-parse", "HEAD"], cwd=local_dir, check=False)`; if rc==0 return stdout.strip(); else return None (newly-init repo with no commits).
- `@dataclass(frozen=True) class AiderResult: returncode: int; stdout: str; stderr: str; summary: str` where `summary` is the first non-empty line of stdout (fallback empty string).
- `def run_aider(local_dir: str, body_path: str, model: str) -> AiderResult`: invoke `["aider", "--message-file", body_path, "--model", model, "--yes", "--auto-commits", "--no-pretty"]` via `_run(..., check=False, timeout=900)`. Always return an `AiderResult` regardless of returncode (caller decides what to do).
- `def caddyfile_changed(local_dir: str) -> bool`: True iff a file named `Caddyfile` exists in `local_dir` AND `git_head_sha` shows the most recent commit modified it. Implement via `_run(["git", "diff", "--name-only", "HEAD~1", "HEAD"], cwd=local_dir, check=False)`; if `HEAD~1` doesn't exist (single commit), return True when `Caddyfile` exists. Otherwise check `"Caddyfile"` in the output lines.
- `def ssh_caddy_reload(ssh_alias: str)`: `_run(["ssh", ssh_alias, "sudo", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"], timeout=60)`.
- No global state; all paths come in as arguments. No retries — failures bubble to `process_message`.

## Acceptance criteria
- [ ] `_run` raises a clear `RuntimeError` including the failing command's stderr (truncated)
- [ ] `rsync_pull` / `rsync_push` use exact flags from ARCHITECTURE.md (`-a --delete`, push adds `--checksum`)
- [ ] `run_aider` never raises on non-zero rc — it returns `AiderResult` with rc captured
- [ ] `git_head_sha` returns `None` (not raise) for a repo with no commits
- [ ] `caddyfile_changed` correctly handles both the "first commit" and "Nth commit" cases
- [ ] `ssh_caddy_reload` runs `sudo caddy reload` over the alias
- [ ] No TODO comments
- [ ] All error paths handled
- [ ] No placeholder functions or fake data
