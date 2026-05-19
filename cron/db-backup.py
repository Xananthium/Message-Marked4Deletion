#!/usr/bin/env python3
"""
WORKER:      db-backup
CADENCE:     daily 23:00
OPT-IN-KEY:  always-on (backups are non-optional)
WHAT IT DOES:
    pg_dump the paperclip database to /var/backups/paperclip/<date>.sql.gz.
    Retains last 14 daily dumps; deletes older files. Creates backup dir if
    missing. Files a high-priority Mercer issue on any failure. Idempotent:
    skips if today's backup already exists.
"""
from __future__ import annotations

import gzip, json, logging, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.db-backup")

_DSN        = os.environ.get("PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip")
_COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_MERCER     = "cfaac33f-c89a-43d6-95dd-2a9587d1d69d"
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/backups/paperclip"))
_RETAIN     = 14
_TODAY      = datetime.now(timezone.utc).date().isoformat()
_DB_URL     = _DSN  # pg_dump accepts libpq connstr


def _ensure_backup_dir():
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not os.access(_BACKUP_DIR, os.W_OK):
        raise PermissionError(f"backup dir not writable: {_BACKUP_DIR}")


def _open_issue_exists(conn, fp):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM issues WHERE origin_fingerprint=%s "
                    "AND status NOT IN ('done','cancelled') LIMIT 1", (fp,))
        return cur.fetchone() is not None


def _file_failure_issue(conn, reason: str) -> None:
    fp = f"db-backup:fail:{_TODAY}"
    if _open_issue_exists(conn, fp):
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE companies SET issue_counter=issue_counter+1 "
                    "WHERE id=%s RETURNING issue_counter", (_COMPANY_ID,))
        n = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO issues (company_id,title,description,status,priority,
               assignee_agent_id,issue_number,identifier,origin_kind,
               origin_fingerprint,created_at,updated_at)
               VALUES (%s,%s,%s,'todo','high',%s,%s,%s,'worker',%s,now(),now())
               RETURNING id""",
            (_COMPANY_ID, f"[db-backup] backup FAILED {_TODAY}",
             f"pg_dump failed on {_TODAY}.\n\n{reason}\n\nRestore capability may be degraded.",
             _MERCER, n, f"DIS-{n}", fp),
        )
        iid = cur.fetchone()[0]
        cur.execute("INSERT INTO issue_comments (issue_id,body,created_at) VALUES (%s,%s,now())",
                    (iid, json.dumps({"source":"db-backup","date":_TODAY,"reason":reason})))
    log.error("filed DIS-%d: db-backup failure", n)


def _prune_old_backups():
    dumps = sorted(_BACKUP_DIR.glob("*.sql.gz"))
    for old in dumps[:-_RETAIN]:
        old.unlink()
        log.info("pruned old backup %s", old.name)


def main():
    log.info("db-backup starting")
    _ensure_backup_dir()

    out_file = _BACKUP_DIR / f"{_TODAY}.sql.gz"
    if out_file.exists():
        log.info("backup already exists for %s, skipping", _TODAY)
        _prune_old_backups()
        return

    try:
        result = subprocess.run(
            ["pg_dump", "--no-password", _DB_URL],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode()[:500])
        with gzip.open(out_file, "wb") as f:
            f.write(result.stdout)
        size_mb = out_file.stat().st_size / 1_048_576
        log.info("backup written: %s (%.1f MB)", out_file, size_mb)
        _prune_old_backups()
    except Exception as exc:
        log.error("db-backup FAILED: %s", exc)
        with psycopg2.connect(_DSN) as conn:
            _file_failure_issue(conn, str(exc))
            conn.commit()
        raise


if __name__ == "__main__":
    main()
