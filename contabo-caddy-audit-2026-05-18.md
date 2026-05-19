# Contabo Caddy audit — 2026-05-18

**Auditor:** Claude Opus 4.7 on behalf of operator
**Issue:** DIS-84
**Caddy version:** v2.11.2
**Service state:** `active (running)` since 2026-05-11 20:48 CEST (1 week uptime)
**Caddyfile path:** `/etc/caddy/Caddyfile` (53 lines)
**Log dir:** `/var/log/caddy/` — owned by `caddy:caddy`, mode 0755

## Sites declared

| FQDN | root | extras | TLS | gzip/zstd |
| ---- | ---- | ------ | --- | --------- |
| `digitaldisconnections.com` + `www.` | `/var/www/digitaldisconnections.com` | `/downloads/*` → `/var/www/downloads` (browse) | auto | yes |
| `agentmanifesto.xyz` + `www.` | `/var/www/agentmanifesto.xyz` | — | auto | yes |
| `discnxt.com` + `www.` | `/var/www/discnxt.com` | `/api/contact` → `127.0.0.1:8765` | auto | yes |
| `pittsburgh-geeks.com` + `www.` | `/var/www/pittsburgh-geeks.com` | — | auto | yes |
| `brangembringem.com` + `www.` | `/var/www/brangembringem.com` | — | auto | yes |
| `ericbasham.com` + `www.` | `/var/www/ericbasham.com` | — | auto | yes |
| `mail.contabo.discnxt.net` | (reverse-proxy) | `localhost:8080` | auto | — |
| `isnotreal.site` + `www.` | `/var/www/isnotreal.site` | `www.` → apex redirect, `/api/contact` → `127.0.0.1:8765` | auto | yes |

All three operator domains (`digitaldisconnections.com`, `discnxt.com`,
`pittsburgh-geeks.com`) are present and serve HTTP/2 200 with valid TLS
(Caddy-managed Let's Encrypt). Smoke checked via `curl -sI` 2026-05-18.

## Comparison against V1 spec
(see `/home/discnxt/docs/self-hosting-infrastructure.md` §5 "Customer site delivery")

| V1 requirement | Observed | Verdict |
| -------------- | -------- | ------- |
| One `file_server` block per domain pointing at `/var/www/<domain>/` | yes for all static sites | OK |
| TLS auto-managed (Let's Encrypt, no manual cert paths) | yes — no `tls` directive anywhere | OK |
| `encode gzip zstd` on each block | yes on all 7 file_server blocks | OK |
| `caddy reload` is the deploy ritual (no full restart) | yes — script `caddy-reload-if-changed.sh` mentioned in plan but not yet present on server | **MINOR DRIFT** |
| Caddy access + error log at `/var/log/caddy/{access,error}.log` | log dir exists, but only legacy `*.log` files (catmugshots, codetovibe, etc., all 0 bytes); current blocks have **no `log` directive** → access logs go to journald instead | **DRIFT** |
| No webhook surface on Contabo | `mail.contabo.discnxt.net` reverse-proxies to localhost:8080 (Stalwart admin), and discnxt.com / isnotreal.site both reverse-proxy `/api/contact` to `127.0.0.1:8765` | **DRIFT (intentional?)** |
| Security headers (HSTS, etc.) | not configured | **DRIFT** |
| Caddy's automatic HTTP→HTTPS | yes (default) | OK |

## Detailed findings

### F1 — No structured access logs (drift)
The V1 spec asks for `/var/log/caddy/{access,error}.log` per-site. Currently
no `log` directive is in the Caddyfile, so all access logs go through
journald (`journalctl -u caddy`). The legacy per-domain `*.log` files in
`/var/log/caddy/` are 0 bytes leftovers from a previous config.

**Fix:** Add a global `log` block at top of Caddyfile, or one inside each site
block, e.g.:

```caddy
discnxt.com, www.discnxt.com {
    log {
        output file /var/log/caddy/discnxt.com.access.log {
            roll_size 50mb
            roll_keep 5
        }
        format json
    }
    root * /var/www/discnxt.com
    reverse_proxy /api/contact 127.0.0.1:8765
    file_server
    encode gzip zstd
}
```

### F2 — No security headers (drift)
None of the site blocks set HSTS, X-Frame-Options, X-Content-Type-Options,
or Referrer-Policy. Static sites are not high-risk but free baseline
hardening should be in:

```caddy
header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    X-Content-Type-Options "nosniff"
    X-Frame-Options "SAMEORIGIN"
    Referrer-Policy "strict-origin-when-cross-origin"
    -Server   # hide "Caddy" header
}
```

Recommend adding once globally via a snippet, e.g. `(common_headers)` plus
`import common_headers` in each block.

### F3 — Stale `/var/www/` cruft
- `/var/www/agentmanifesto.xyz-backup-20260511-232336/` (20K — stale backup)
- `/var/www/discnxt.com.bak.20260512184929/` (4.5M — stale backup of the old marketing site)
- `/var/www/downloads/` (80M — only served via `digitaldisconnections.com/downloads/`; unrelated to discnxt brand)

The `.bak` directories aren't served (no Caddy block) but they take up disk
and confuse readers. Backend DevOps should clean them up.

### F4 — `caddy-reload-if-changed.sh` not on Contabo
The V1 plan mentions a small script on Contabo that diff-checks the
Caddyfile and only reloads when needed. Not present. The deploy wrapper
(`/home/discnxt/aib/deploy-site.sh`, just shipped) does the diff
client-side and pushes a new Caddyfile + reload only when needed, which
accomplishes the same goal from the workstation side. Acceptable.

### F5 — `mail.contabo.discnxt.net` reverse-proxy
Reverse-proxy to Stalwart's admin on `localhost:8080`. Stalwart is
"installed-but-idle" per the V1 doc. Either decommission this block now
or document why it stays. Recommend Backend DevOps confirm and either
remove the block or annotate the Caddyfile with a `# stalwart admin —
idle zero-cost insurance` comment.

### F6 — `/api/contact` reverse-proxy on discnxt.com + isnotreal.site
Both forward `/api/contact` to `127.0.0.1:8765`. Nothing is listening on
8765 right now (Stalwart-era contact form). Either:
  - point this at the real contact-form backend when DIS-87 (contact form
    backend) is done, or
  - drop the `reverse_proxy` line until then so visitors get a clean 404
    instead of a 502.

## Non-discnxt sites present

`agentmanifesto.xyz`, `brangembringem.com`, `ericbasham.com`, `isnotreal.site`
are non-discnxt brands the operator hosts on the same Contabo. They don't
need to follow our V1 spec but they currently follow the same minimal
shape, so the global header / log additions would apply to them too.

## Recommended follow-up issues (assignee: Backend DevOps)

1. **Add per-site access logs + security headers** — F1, F2 combined.
2. **Clean up stale `/var/www/*.bak.*` and `*-backup-*`** — F3.
3. **Decide: keep or remove `mail.contabo.discnxt.net` reverse-proxy** — F5.
4. **Either implement contact-form backend on :8765 or remove the
   reverse-proxy** — F6 (already tracked as DIS-87 partial).

## Snapshot artifacts

- Captured Caddyfile: `/tmp/contabo-caddyfile.txt` (cleaned and inlined above).
- Caddy version: `v2.11.2`.
- Service: `caddy.service` enabled + running.

End of audit.
