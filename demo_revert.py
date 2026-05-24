#!/usr/bin/env python3
"""
demo_revert.py — oneshot revert script run by demo-revert.timer every 60s.

For each demo_request row where status='deployed' and expires_at < NOW():
  1. rsync restore snapshot → public/
  2. run deploy-site.sh
  3. delete diff JSON
  4. UPDATE status='reverted'
  5. rmtree snapshot
  6. send revert email
  7. Privacy: null change_text for rows >24h past terminal
"""
from __future__ import annotations

import base64
import email.mime.text
import json
import logging
import os
import shutil
import subprocess
import sys

DEPLOY_SCRIPT = "/home/discnxt/aib/deploy-site.sh"
GOOGLE_SA_KEY = "/home/discnxt/.secrets/google-agents.json"
GMAIL_SUBJECT = "team@digitaldisconnections.com"
DEMO_FROM = "demo@digitaldisconnections.com"
DIFF_JSON_PATH = "/var/sites/{fqdn}/public/diff/{id}.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("demo-revert")


def _db_conn():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _deploy(fqdn: str) -> None:
    result = subprocess.run(
        [DEPLOY_SCRIPT, fqdn],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"deploy-site.sh failed rc={result.returncode} "
            f"stderr={result.stderr.decode(errors='replace')[:500]}"
        )


def _send_revert_email(email_hash_not_plaintext: str, fqdn: str, req_id: str) -> None:
    """
    We don't store plaintext email — only hash. So revert notifications
    go to the team inbox instead. A future version could store the encrypted
    email during the session and decrypt here, but v1 notifies the team.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SA_KEY,
            scopes=["https://mail.google.com/"],
        ).with_subject(GMAIL_SUBJECT)

        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        msg = email.mime.text.MIMEText(
            f"Demo reverted.\n\nSite: {fqdn}\nRequest ID: {req_id}\n",
            "plain",
        )
        msg["From"] = DEMO_FROM
        msg["To"] = "team@digitaldisconnections.com"
        msg["Subject"] = f"[demo-revert] {fqdn} restored — {req_id[:8]}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:
        log.warning("revert email failed (non-fatal): %s", exc)


def main() -> int:
    conn = _db_conn()
    errors = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, site_fqdn, snapshot_path, email_hash
                   FROM demo_requests
                   WHERE status='deployed' AND expires_at < NOW()"""
            )
            rows = cur.fetchall()

        log.info("found %d expired demo(s) to revert", len(rows))

        for req_id, fqdn, snapshot_path, email_hash in rows:
            req_id = str(req_id)
            log.info("reverting req_id=%s fqdn=%s", req_id, fqdn)

            try:
                # 1. rsync restore
                if snapshot_path and os.path.isdir(snapshot_path):
                    dst = f"/var/sites/{fqdn}/public"
                    result = subprocess.run(
                        ["rsync", "-av", "--delete", snapshot_path + "/", dst + "/"],
                        capture_output=True, timeout=120,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"rsync failed rc={result.returncode} "
                            f"stderr={result.stderr.decode(errors='replace')[:500]}"
                        )
                else:
                    log.warning("req_id=%s snapshot_path missing or gone: %s", req_id, snapshot_path)

                # 2. deploy-site.sh
                _deploy(fqdn)

                # 3. Delete diff JSON
                diff_path = DIFF_JSON_PATH.format(fqdn=fqdn, id=req_id)
                try:
                    os.remove(diff_path)
                except FileNotFoundError:
                    pass

                # 4. UPDATE status='reverted'
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE demo_requests SET status='reverted', snapshot_path=NULL WHERE id=%s",
                        (req_id,),
                    )
                conn.commit()

                # 5. rmtree snapshot
                if snapshot_path and os.path.isdir(snapshot_path):
                    shutil.rmtree(snapshot_path, ignore_errors=True)

                # 6. Revert email (team inbox — we only store email hash not plaintext)
                _send_revert_email(email_hash, fqdn, req_id)

                log.info("reverted req_id=%s fqdn=%s ok", req_id, fqdn)

            except Exception as exc:
                log.exception("revert failed for req_id=%s: %s", req_id, exc)
                errors += 1
                conn.rollback()

        # 7. Privacy: null change_text for rows >24h past terminal
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE demo_requests
                   SET change_text = NULL
                   WHERE status IN ('reverted', 'rejected')
                     AND change_text IS NOT NULL
                     AND created_at < NOW() - INTERVAL '24 hours'"""
            )
            pruned = cur.rowcount
        conn.commit()
        if pruned > 0:
            log.info("privacy pruned change_text for %d terminal rows", pruned)

    finally:
        conn.close()

    if errors:
        log.error("revert completed with %d error(s)", errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
