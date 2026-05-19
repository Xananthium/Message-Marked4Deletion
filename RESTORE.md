# Postgres restore procedure — paperclip DB

Companion to `backup-postgres.sh` (DIS-83).

## What backups look like

- Location: `/home/discnxt/backups/postgres/`
- Filename: `paperclip-YYYY-MM-DDTHH-MM.dump`
- Format: Postgres custom format (`pg_dump -Fc`) — restored with `pg_restore`.
- Retention: 14 most recent dumps; older ones deleted by the script itself.
- Cadence: nightly at 03:00 local via `discnxt-backup.timer` (see install command below).

## Install the timer (one time)

```bash
sudo cp /home/discnxt/aib/systemd/discnxt-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discnxt-backup.timer
systemctl list-timers discnxt-backup.timer
```

## Verify the backup ran

```bash
ls -lh /home/discnxt/backups/postgres/ | tail -5
tail -n 5 /var/log/discnxt-backup.log
systemctl status discnxt-backup.service
```

## Test restore (always do this against a TEMP DB; never against the live `paperclip` DB)

```bash
# 0. Pick the dump you want to restore
DUMP=$(ls -t /home/discnxt/backups/postgres/paperclip-*.dump | head -1)
echo "Restoring: $DUMP"

# 1. Create a temp DB owned by `paperclip`
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d postgres \
       -c "CREATE DATABASE paperclip_restore_test;"

# 2. Restore the dump (custom format, parallel-safe with -j 4)
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  pg_restore -h 127.0.0.1 -U paperclip \
             -d paperclip_restore_test \
             -j 4 \
             "$DUMP"

# 3. Verify expected tables + rowcounts
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d paperclip_restore_test -c "
    SELECT 'issues'    AS t, count(*) FROM issues
    UNION ALL SELECT 'agents',    count(*) FROM agents
    UNION ALL SELECT 'customers', count(*) FROM customers
    UNION ALL SELECT 'domains',   count(*) FROM domains
    UNION ALL SELECT 'invoices',  count(*) FROM invoices;"

# 4. Compare to the live DB
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d paperclip -c "
    SELECT 'issues'    AS t, count(*) FROM issues
    UNION ALL SELECT 'agents',    count(*) FROM agents
    UNION ALL SELECT 'customers', count(*) FROM customers
    UNION ALL SELECT 'domains',   count(*) FROM domains
    UNION ALL SELECT 'invoices',  count(*) FROM invoices;"

# 5. If counts match, drop the test DB
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d postgres \
       -c "DROP DATABASE paperclip_restore_test;"
```

## Real recovery — swap a restored DB in for the live one

This is the **break-glass** procedure if `paperclip` is corrupt. Stops all
writers, swaps the database, and restarts them.

```bash
# 0. Stop everything that writes to the live DB
sudo systemctl stop paperclip.service        # Paperclip itself
sudo systemctl stop aib-poller.timer || true # poller, if it's running

# 1. Rename the broken DB out of the way (KEEP IT — don't drop)
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d postgres \
       -c "ALTER DATABASE paperclip RENAME TO paperclip_broken_$(date +%Y%m%d_%H%M);"

# 2. Restore into a fresh `paperclip` DB
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d postgres \
       -c "CREATE DATABASE paperclip;"

DUMP=$(ls -t /home/discnxt/backups/postgres/paperclip-*.dump | head -1)
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  pg_restore -h 127.0.0.1 -U paperclip -d paperclip -j 4 "$DUMP"

# 3. Sanity-check before bringing services back
PGPASSWORD=3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730 \
  psql -h 127.0.0.1 -U paperclip -d paperclip -c "
    SELECT count(*) AS issues FROM issues;
    SELECT count(*) AS agents FROM agents;
    SELECT count(*) AS customers FROM customers;"

# 4. Bring services back up
sudo systemctl start paperclip.service
sudo systemctl start aib-poller.timer || true

# 5. Once verified healthy for a day, drop the broken copy
# PGPASSWORD=... psql ... -c "DROP DATABASE paperclip_broken_YYYYMMDD_HHMM;"
```

## Verified test (2026-05-18)

- Backup file: `paperclip-2026-05-18T18-39.dump`, 5.9 MB
- Restored into: `paperclip_restore_test`
- Rowcounts matched live: `issues=92`, `agents=15`, `customers=1`
- Temp DB dropped after verification.

## Alerting

The backup script logs JSON lines to `/var/log/discnxt-backup.log`. To alert
when the nightly backup fails, the simplest rule is:

```bash
# In cron or a systemd OnFailure unit:
if ! grep -q "$(date -u +%Y-%m-%d)" /var/log/discnxt-backup.log; then
  mail -s "discnxt backup missed $(date -u +%Y-%m-%d)" cass@digitaldisconnections.com < /dev/null
fi
```

If the backup itself fails (non-zero exit), the systemd unit will go to
`failed` state — `systemctl status discnxt-backup.service` shows it. Add
this to whatever daily health check the operator runs.

## Who to alert if backup fails

- Primary: operator (cass@digitaldisconnections.com, jamal@digitaldisconnections.com)
- Operationally: Backend DevOps agent owns this once paperclip is unpaused.
