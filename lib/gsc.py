#!/usr/bin/env python3
"""
GSC (Google Search Console) helper for the AIB deployment pipeline.

Provides domain verification, sitemap submission, indexing requests, and
search analytics queries via the GSC and SiteVerification APIs.

Uses the agent-runtime service account at
``/home/discnxt/.secrets/google-agents.json``.

Usage (CLI):
    python3 -m lib.gsc verify <domain>         # verify + add to GSC
    python3 -m lib.gsc sitemap <domain>         # submit sitemap
    python3 -m lib.gsc index <domain>           # request indexing
    python3 -m lib.gsc analytics <domain>       # print 7-day analytics
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("aib.gsc")

_SA_PATH = Path(os.environ.get(
    "AIB_SA_PATH", "/home/discnxt/.secrets/google-agents.json"))
_GSC_SCOPES = [
    "https://www.googleapis.com/auth/webmasters",
    "https://www.googleapis.com/auth/siteverification",
]
_INDEXING_SCOPE = ["https://www.googleapis.com/auth/indexing"]

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _build(service_name, version, scopes=None, subject=None):
    """Return an authenticated google-api client for *service_name*."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        str(_SA_PATH), scopes=scopes or _GSC_SCOPES, subject=subject)
    return build(service_name, version, credentials=creds)


# ---------------------------------------------------------------------------
# Domain verification
# ---------------------------------------------------------------------------

def get_verification_token(domain: str) -> dict | None:
    """Request a DNS TXT verification token from the SiteVerification API.

    Returns the token dict (``{"kind", "id", "token", "method"}``) or
    ``None`` if already verified.
    """
    sv = _build("siteVerification", "v1")
    body = {
        "site": {"identifier": domain, "type": "INET_DOMAIN"},
        "verificationMethod": "DNS_TXT",
    }
    try:
        token = sv.webResource().getToken(body=body).execute()
        log.info("got verification token for %s: %s", domain, token["token"][:20])
        return token
    except Exception as exc:
        msg = str(exc).lower()
        if "already verified" in msg or "already exists" in msg:
            log.info("%s already verified in GSC", domain)
            return None
        log.warning("getToken failed for %s: %s", domain, exc)
        raise


def verify_domain(domain: str) -> bool:
    """Verify a domain with GSC using the already-placed DNS TXT record.

    Call *after* the DNS TXT record has been added.  Returns ``True`` on
    success.
    """
    sv = _build("siteVerification", "v1")
    body = {
        "site": {"identifier": domain, "type": "INET_DOMAIN"},
    }
    try:
        sv.webResource().insert(verificationMethod="DNS_TXT", body=body).execute()
        log.info("verified domain %s in GSC", domain)
        return True
    except Exception as exc:
        log.error("GSC verification failed for %s: %s", domain, exc)
        return False


# ---------------------------------------------------------------------------
# Site list / add-site (for the GSC property, not verification)
# ---------------------------------------------------------------------------

def list_gsc_sites() -> list[dict]:
    """List all sites/properties visible to the SA in GSC."""
    wm = _build("webmasters", "v3")
    resp = wm.sites().list().execute()
    return resp.get("siteEntry", [])


def is_verified(domain: str) -> bool:
    """Return ``True`` if the domain appears as a verified GSC property."""
    for site in list_gsc_sites():
        url = site.get("siteUrl", "")
        if domain in url and site.get("permissionLevel", "") != "siteUnverifiedUser":
            return True
    return False


# ---------------------------------------------------------------------------
# Sitemap management
# ---------------------------------------------------------------------------

def submit_sitemap(domain: str, sitemap_url: str | None = None) -> bool:
    """Submit a sitemap for the domain to GSC.

    *sitemap_url* defaults to ``https://<domain>/sitemap.xml``.
    """
    wm = _build("webmasters", "v3")
    if sitemap_url is None:
        sitemap_url = f"https://{domain}/sitemap.xml"
    site_urls = [f"scoped:domain:{domain}", f"https://{domain}/", f"http://{domain}/"]
    for site_url in site_urls:
        try:
            wm.sitemaps().submit(siteUrl=site_url, feedpath=sitemap_url).execute()
            log.info("submitted sitemap %s for %s", sitemap_url, site_url)
            return True
        except Exception:
            continue
    log.error("sitemap submission failed for %s (tried domain/https/http)", domain)
    return False


def list_sitemaps(domain: str) -> list[dict]:
    """Return the list of sitemaps known to GSC for this domain."""
    wm = _build("webmasters", "v3")
    site_urls = [f"scoped:domain:{domain}", f"https://{domain}/", f"http://{domain}/"]
    for site_url in site_urls:
        try:
            resp = wm.sitemaps().list(siteUrl=site_url).execute()
            return resp.get("sitemap", [])
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# Indexing API (separate scope)
# ---------------------------------------------------------------------------

def request_indexing(url: str) -> bool:
    """Notify Google that the content at *url* has changed (Indexing API).

    Requires the indexing scope, which must be enabled on the SA.
    Returns ``True`` if Google accepted the notification.
    """
    try:
        idx = _build("indexing", "v3", scopes=_INDEXING_SCOPE)
        body = {"url": url, "type": "URL_UPDATED"}
        idx.urlNotifications().publish(body=body).execute()
        log.info("indexing request sent for %s", url)
        return True
    except Exception as exc:
        log.warning("indexing request failed for %s: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Search Analytics
# ---------------------------------------------------------------------------

def get_search_analytics(
    domain: str,
    days: int = 7,
    row_limit: int = 10,
) -> list[dict]:
    """Return top queries for the domain from GSC Search Analytics.

    Returns a list of dicts with keys ``query``, ``clicks``, ``impressions``,
    ``ctr``, ``position``.
    """
    wm = _build("webmasters", "v3")
    site_urls = [f"scoped:domain:{domain}", f"https://{domain}/", f"http://{domain}/"]
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query"],
        "rowLimit": row_limit,
    }
    for site_url in site_urls:
        try:
            resp = wm.searchanalytics().query(siteUrl=site_url, body=body).execute()
            rows = resp.get("rows", [])
            result = []
            for r in rows:
                result.append({
                    "query": r["keys"][0],
                    "clicks": r["clicks"],
                    "impressions": r["impressions"],
                    "ctr": r["ctr"],
                    "position": r["position"],
                })
            return result
        except Exception:
            continue
    log.warning("search analytics failed for %s (tried domain/https/http)", domain)
    return []


# ---------------------------------------------------------------------------
# Combined provisioning step
# ---------------------------------------------------------------------------

def provision(domain: str, txt_record: str | None = None) -> dict:
    """Run the full GSC provisioning workflow.

    Steps:
    1. Get a verification token (if not already verified)
    2. Return the TXT record value the caller needs to add via DNS
    3. Verify the domain once DNS propagates
    4. Submit sitemap.xml

    Returns a dict with keys:
    - ``verified`` (bool) — whether verification succeeded
    - ``token`` (str | None) — the TXT record value to add (or None if already verified)
    - ``sitemap_submitted`` (bool | None)
    """
    result = {"verified": False, "token": None,
              "sitemap_submitted": None}

    # Step 1+2: Get verification token
    token = get_verification_token(domain) if not is_verified(domain) else None
    if token is None:
        # Already verified — skip to sitemap
        result["verified"] = True
    else:
        result["token"] = token.get("token")
        # DNS TXT record should be added by the caller
        # Step 3: verify (only works after DNS record is live)
        # We attempt it; if DNS hasn't propagated, it'll fail and caller retries
        if txt_record:
            # Caller already added the record and waited — try verification
            result["verified"] = verify_domain(domain)

    # Step 4: Submit sitemap
    if result["verified"]:
        result["sitemap_submitted"] = submit_sitemap(domain)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    domain = sys.argv[2]

    if command == "verify":
        try:
            token = get_verification_token(domain)
            if token:
                print(token["token"])
            else:
                print("ALREADY_VERIFIED")
        except Exception:
            print("FAIL")
            sys.exit(1)

    elif command == "verify-domain":
        ok = verify_domain(domain)
        print("OK" if ok else "FAIL")
        sys.exit(0 if ok else 1)

    elif command == "sitemap":
        ok = submit_sitemap(domain)
        print("OK" if ok else "FAIL")
        sys.exit(0 if ok else 1)

    elif command == "index":
        url = f"https://{domain}/"
        ok = request_indexing(url)
        print("OK" if ok else "FAIL")
        sys.exit(0 if ok else 1)

    elif command == "analytics":
        rows = get_search_analytics(domain)
        print(json.dumps(rows, indent=2))

    elif command == "provision":
        result = provision(domain)
        print(json.dumps(result, indent=2))
        if not result.get("verified") and result.get("token"):
            print("Add this TXT record to your DNS, then run: gsc.py verify-then-submit <domain>",
                  file=sys.stderr)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
