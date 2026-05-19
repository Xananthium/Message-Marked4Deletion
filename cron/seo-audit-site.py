#!/usr/bin/env python3
"""
WORKER:      seo-audit-site
CADENCE:     on-demand (called by other workers, no timer of its own)
OPT-IN-KEY:  N/A — invoked per-site by callers who have already checked opt-in
WHAT IT DOES:
    Lightweight HTML audit for a single fqdn. Checks: <title> non-empty,
    all <img> have alt text, sitemap.xml returns 200, robots.txt returns 200,
    schema.org JSON-LD present. Writes findings to
    /var/sites/<fqdn>/seo/audit-<date>.md. No Lighthouse dep — stdlib only.
"""
from __future__ import annotations

import html.parser, json, logging, os, sys, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("aib.seo-audit-site")

_UA = "discnxt-seo-audit/1.0"
_TIMEOUT = 15


class _HtmlExtract(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self.imgs_missing_alt: list[str] = []
        self.has_jsonld = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "img":
            if not d.get("alt", "").strip():
                self.imgs_missing_alt.append(d.get("src", "(no src)"))
        if tag == "script" and d.get("type") == "application/ld+json":
            self.has_jsonld = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


def _fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.status, r.read(500_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)


def audit(fqdn: str) -> dict:
    """Run the audit and return a findings dict."""
    findings: dict = {"fqdn": fqdn, "checks": {}, "errors": []}

    # HTML checks
    code, body = _fetch(f"https://{fqdn}/")
    if code == 0:
        findings["errors"].append(f"homepage unreachable: {body}")
    else:
        p = _HtmlExtract()
        p.feed(body)
        findings["checks"]["title_present"]     = bool(p.title.strip())
        findings["checks"]["imgs_all_have_alt"]  = len(p.imgs_missing_alt) == 0
        findings["checks"]["has_jsonld"]         = p.has_jsonld
        if p.imgs_missing_alt:
            findings["imgs_missing_alt"] = p.imgs_missing_alt[:20]

    sitemap_code, _ = _fetch(f"https://{fqdn}/sitemap.xml")
    findings["checks"]["sitemap_200"] = sitemap_code == 200

    robots_code, _  = _fetch(f"https://{fqdn}/robots.txt")
    findings["checks"]["robots_200"]  = robots_code == 200

    return findings


def write_report(fqdn: str, findings: dict) -> Path:
    date = datetime.now(timezone.utc).date().isoformat()
    out_dir = Path(f"/var/sites/{fqdn}/seo")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"audit-{date}.md"
    lines = [f"# SEO Audit: {fqdn} ({date})\n"]
    for check, passed in findings.get("checks", {}).items():
        mark = "PASS" if passed else "FAIL"
        lines.append(f"- [{mark}] {check}")
    if findings.get("imgs_missing_alt"):
        lines.append("\n## Images missing alt text")
        for src in findings["imgs_missing_alt"]:
            lines.append(f"- {src}")
    if findings.get("errors"):
        lines.append("\n## Errors")
        for e in findings["errors"]:
            lines.append(f"- {e}")
    out_file.write_text("\n".join(lines) + "\n")
    log.info("audit written to %s", out_file)
    return out_file


def main():
    if len(sys.argv) < 2:
        print("Usage: seo-audit-site.py <fqdn>", file=sys.stderr)
        sys.exit(1)
    fqdn = sys.argv[1].lower().strip()
    findings = audit(fqdn)
    report_path = write_report(fqdn, findings)
    print(json.dumps({"report": str(report_path), "findings": findings}, indent=2))


if __name__ == "__main__":
    main()
