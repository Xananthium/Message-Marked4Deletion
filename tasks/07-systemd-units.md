---
id: 07
title: Author aib-poller.service + aib-poller.timer systemd units
platform: BACKEND
depends_on: [02, 06]
files_touched: [/etc/systemd/system/aib-poller.service, /etc/systemd/system/aib-poller.timer, /home/discnxt/aib/install-systemd.sh]
estimate_minutes: 20
estimate_loc: 60
---

## Description
Write the two systemd units that drive the 5-minute poll cadence, plus a small installer that copies them into place, runs `daemon-reload`, and enables the timer. Combined service+timer must be ≤30 lines per the file budget.

## Implementation notes
- `aib-poller.service` (Type=oneshot):
  ```
  [Unit]
  Description=Agent in a Box poller (one-shot)
  After=network-online.target postgresql.service
  Wants=network-online.target

  [Service]
  Type=oneshot
  User=discnxt
  WorkingDirectory=/home/discnxt/aib
  EnvironmentFile=/home/discnxt/aib/.env
  ExecStart=/usr/bin/python3 /home/discnxt/aib/poller.py
  StandardOutput=journal
  StandardError=journal
  ```
  No `Restart=` — the timer is the only driver.
- `aib-poller.timer`:
  ```
  [Unit]
  Description=Agent in a Box poller timer (every 5 min)

  [Timer]
  OnBootSec=2min
  OnUnitActiveSec=5min
  Persistent=true
  Unit=aib-poller.service

  [Install]
  WantedBy=timers.target
  ```
- `install-systemd.sh` (`set -euo pipefail`): require root via `[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }`; copy both unit files from `/home/discnxt/aib/systemd/` (source repo location) to `/etc/systemd/system/`; `systemctl daemon-reload`; `systemctl enable --now aib-poller.timer`; print `systemctl list-timers aib-poller.timer` for confirmation.
- Author the unit files under `/home/discnxt/aib/systemd/` in-repo (source of truth) AND target `/etc/systemd/system/` is where the installer drops them. `files_touched` lists the deployed paths since the installer is what writes them.

## Acceptance criteria
- [ ] Both unit files combined are ≤30 non-blank lines
- [ ] `aib-poller.timer` has `OnBootSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true`
- [ ] `aib-poller.service` is `Type=oneshot`, runs as user `discnxt`, sources `.env`, no `Restart=`
- [ ] `install-systemd.sh` is idempotent (re-running succeeds, `enable --now` is safe to repeat)
- [ ] After install, `systemctl list-timers` shows `aib-poller.timer` with next-fire time
- [ ] No TODO comments
- [ ] All error paths handled (set -euo pipefail, root check)
- [ ] No placeholder functions or fake data
