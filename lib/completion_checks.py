"""Pre-send verification for customer-completion emails.

Runs factual checks against the live world before allowing the completion
email to be sent.  Any failure blocks the send, files a Paperclip issue
against the responsible owner, and raises CompletionCheckError.

Public API:
    run_checks(fqdn, checks=None) -> list[CheckResult]
    send_completion_email(issue_id, subject, body, fqdn, ...) -> str | None

CLI (dry-run):
    python3 -m lib.completion_checks <fqdn>
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import sys
from typing import Callable

import psycopg2
import requests

log = logging.getLogger("aib.completion_checks")

_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)
_COMPANY_ID = "3f6ac8c4-e9ec-4fd3-b644-b7cb5d15bfa6"
_ONBOARDING_PROJECT_ID = "29a7e908-5385-44f2-b116-435f47e847f8"
_SHERRY_UUID = "78d5d2cf-463d-4792-b609-ce2ddb70c5b7"

_OWNER_UUIDS: dict[str, str] = {
    "reed": "d9431040-6d05-4bb2-be63-3a87e79abf32",
    "sherry": _SHERRY_UUID,
    "infra": "38d8400a-3d0a-44ff-b430-a228180bc1e5",  # Paulina fallback
    "unknown": "38d8400a-3d0a-44ff-b430-a228180bc1e5",
}

_HTTP_TIMEOUT = 15
_TRACKING_PATTERNS = [
    r"googletagmanager\.com",
    r"google-analytics\.com",
    r"gtag\s*\(",
    r"fbq\s*\(",
    r"connect\.facebook\.net",
    r"hotjar\.com",
    r"analytics\.js",
    r"ga\.js",
]


@dataclasses.dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    owner: str  # "reed" | "sherry" | "infra"


class CompletionCheckError(Exception):
    def __init__(self, failures: list[CheckResult]):
        self.failures = failures
        names = ", ".join(f.name for f in failures)
        super().__init__(f"Completion checks failed: {names}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_site_live(fqdn: str) -> CheckResult:
    """HTTPS 200 with non-empty body."""
    url = f"https://{fqdn}/"
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200 and resp.text.strip():
            return CheckResult("site_live", True, f"HTTP 200 ({len(resp.text)} bytes)", "reed")
        return CheckResult("site_live", False, f"HTTP {resp.status_code} from {url}", "reed")
    except Exception as exc:
        return CheckResult("site_live", False, f"{url}: {exc}", "infra")


def check_www_variant(fqdn: str) -> CheckResult:
    """www.<fqdn> resolves to 200 or redirects to apex."""
    url = f"https://www.{fqdn}/"
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, allow_redirects=True)
        if resp.ok or resp.status_code in (301, 302, 307, 308):
            return CheckResult("www_variant", True, f"HTTP {resp.status_code}", "infra")
        return CheckResult("www_variant", False, f"HTTP {resp.status_code} from {url}", "infra")
    except Exception as exc:
        return CheckResult("www_variant", False, f"{url}: {exc}", "infra")


def check_gsc_verified(fqdn: str) -> CheckResult:
    """Domain appears as a verified property in Google Search Console."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from lib.gsc import is_verified
        if is_verified(fqdn):
            return CheckResult("gsc_verified", True, "domain verified in GSC", "sherry")
        return CheckResult("gsc_verified", False, "domain NOT verified in GSC", "sherry")
    except Exception as exc:
        return CheckResult("gsc_verified", False, f"GSC API error: {exc}", "sherry")


def check_dds_gates(fqdn: str) -> CheckResult:
    """og:image present, no tracking scripts."""
    url = f"https://{fqdn}/"
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, allow_redirects=True)
        if not resp.ok:
            return CheckResult("dds_gates", False, f"fetch returned HTTP {resp.status_code}", "reed")
        html = resp.text

        # og:image
        if not re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'][^"\']+["\']', html, re.IGNORECASE):
            if not re.search(r'<meta[^>]+content=["\'][^"\']+["\'][^>]+property=["\']og:image["\']', html, re.IGNORECASE):
                return CheckResult("dds_gates", False, "og:image meta tag missing or empty", "reed")

        # Tracking scripts
        for pattern in _TRACKING_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                return CheckResult("dds_gates", False, f"tracking script detected: {pattern!r}", "reed")

        return CheckResult("dds_gates", True, "og:image ok, no trackers", "reed")
    except Exception as exc:
        return CheckResult("dds_gates", False, f"HTML fetch error: {exc}", "infra")


_DEFAULT_CHECKS: list[Callable[[str], CheckResult]] = [
    check_site_live,
    check_www_variant,
    check_gsc_verified,
    check_dds_gates,
]


# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------

def run_checks(
    fqdn: str,
    checks: list[Callable[[str], CheckResult]] | None = None,
) -> list[CheckResult]:
    """Run checks against *fqdn* and return all results."""
    check_fns = checks if checks is not None else _DEFAULT_CHECKS
    results: list[CheckResult] = []
    for fn in check_fns:
        try:
            r = fn(fqdn)
        except Exception as exc:
            r = CheckResult(fn.__name__, False, f"check raised: {exc}", "unknown")
        level = logging.INFO if r.passed else logging.WARNING
        log.log(level, "check %-15s %s — %s", r.name, "PASS" if r.passed else "FAIL", r.detail)
        results.append(r)
    return results


# ---------------------------------------------------------------------------
# Failure issue filer
# ---------------------------------------------------------------------------

def _file_failure_issues(failures: list[CheckResult], fqdn: str, parent_issue_id: str | None) -> list[str]:
    """Insert one Paperclip issue per failure; returns list of new identifiers."""
    created: list[str] = []
    try:
        with psycopg2.connect(_DSN) as conn, conn.cursor() as cur:
            for f in failures:
                assignee = _OWNER_UUIDS.get(f.owner, _OWNER_UUIDS["unknown"])
                title = f"[completion-check fail] {fqdn}: {f.name}"
                description = (
                    f"**Check:** `{f.name}`\n"
                    f"**Domain:** `{fqdn}`\n"
                    f"**Failure detail:** {f.detail}\n"
                    f"**Owner role:** {f.owner}\n\n"
                    f"This issue was filed automatically by `completion_checks.py` "
                    f"because the customer-completion email for `{fqdn}` was blocked. "
                    f"Fix the underlying problem and re-run the completion email send.\n\n"
                    + (f"Parent issue: {parent_issue_id}" if parent_issue_id else "")
                )
                cur.execute(
                    """
                    WITH bump AS (
                        UPDATE companies
                           SET issue_counter = issue_counter + 1
                         WHERE id = %s
                        RETURNING issue_counter, issue_prefix
                    )
                    INSERT INTO issues (
                        company_id, project_id, parent_id,
                        title, description, status, priority,
                        assignee_agent_id, created_by_agent_id,
                        identifier, issue_number
                    )
                    SELECT
                        %s, %s, %s::uuid,
                        %s, %s, 'todo', 'high',
                        %s::uuid, %s::uuid,
                        bump.issue_prefix || '-' || bump.issue_counter,
                        bump.issue_counter
                    FROM bump
                    RETURNING identifier
                    """,
                    (
                        _COMPANY_ID,
                        _COMPANY_ID,
                        _ONBOARDING_PROJECT_ID,
                        parent_issue_id,
                        title,
                        description,
                        assignee,
                        _SHERRY_UUID,
                    ),
                )
                row = cur.fetchone()
                identifier = row[0] if row else "(no identifier)"
                created.append(identifier)
                log.info("filed failure issue %s for check %s owner %s", identifier, f.name, f.owner)
            conn.commit()
    except Exception as exc:
        log.error("failed to file failure issues: %s", exc)
    return created


# ---------------------------------------------------------------------------
# send_completion_email
# ---------------------------------------------------------------------------

def send_completion_email(
    issue_id: str,
    subject: str,
    body: str,
    fqdn: str,
    to: str | None = None,
    status_after: str = "done",
    from_alias: str | None = None,
    dry_run: bool = False,
    checks: list[Callable[[str], CheckResult]] | None = None,
    file_issues_on_failure: bool = True,
) -> str | None:
    """Send a customer-completion email after verifying all claims.

    Runs factual checks against the live site before calling send().
    Raises CompletionCheckError if any check fails (and optionally files
    Paperclip issues against the responsible owner).

    Args:
        issue_id:              Paperclip issue UUID this email belongs to.
        subject:               Email subject.
        body:                  Plain-text email body.
        fqdn:                  Customer site domain (e.g. "creesjunkremoval.com").
        to:                    Recipient address.
        status_after:          Issue status to set after sending. Defaults to 'done'.
        from_alias:            Send-as alias (e.g. "mercer@digitaldisconnections.com").
        dry_run:               If True, print check status + what would be sent; no send.
        checks:                Override the default check list (for testing).
        file_issues_on_failure: If True (default), file a Paperclip issue for each failure.

    Returns:
        Gmail message ID (str) on success, or None on dry_run.

    Raises:
        CompletionCheckError: if any check fails (even in dry_run mode).
    """
    results = run_checks(fqdn, checks=checks)
    failures = [r for r in results if not r.passed]

    if dry_run:
        print(f"\n=== DRY RUN: completion email for {fqdn} ===")
        print(f"To:      {to or '(operator fallback)'}")
        print(f"Subject: {subject}")
        print(f"Body:\n{body}\n")
        print("--- Pre-send checks ---")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name}: {r.detail}")
        if failures:
            print(f"\nBLOCKED — {len(failures)} check(s) failed. Email NOT sent.")
        else:
            print("\nAll checks passed. Would send (dry_run=True, skipping Gmail).")
        if failures:
            raise CompletionCheckError(failures)
        return None

    if failures:
        if file_issues_on_failure:
            filed = _file_failure_issues(failures, fqdn, issue_id)
            log.warning(
                "completion email for %s BLOCKED — %d check(s) failed; filed: %s",
                fqdn, len(failures), filed,
            )
        raise CompletionCheckError(failures)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lib.send_email import send
    return send(
        issue_id=issue_id,
        subject=subject,
        body=body,
        to=to,
        status_after=status_after,
        from_alias=from_alias,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if len(sys.argv) < 2:
        print("Usage: python3 -m lib.completion_checks <fqdn>", file=sys.stderr)
        sys.exit(1)

    fqdn = sys.argv[1]
    results = run_checks(fqdn)
    failures = [r for r in results if not r.passed]

    print(f"\nCompletion checks for {fqdn}:")
    for r in results:
        mark = "✓" if r.passed else "✗"
        print(f"  {mark} {r.name}: {r.detail}")

    if failures:
        print(f"\nFAIL — {len(failures)} check(s) did not pass. Would block completion email.")
        sys.exit(1)
    else:
        print("\nPASS — all checks passed. Completion email may be sent.")


if __name__ == "__main__":
    main()
