"""
_site_classifier.py — Website fingerprinting for evaluate.py.
Fetches a URL, detects platform (wix/squarespace/etc.), collects signals.
"""
from __future__ import annotations
import re, ssl, urllib.request
from html.parser import HTMLParser
from typing import Any

_FINGERPRINTS: list[tuple[str, str, str]] = [
    (r"wix\.com", "wix-detected", "wix"),
    (r"squarespace\.com", "squarespace-detected", "squarespace"),
    (r"godaddy\.com|myftpupload\.com", "godaddy-detected", "godaddy"),
    (r"wp-content|wp-includes", "wordpress-detected", "wordpress"),
]
_UA = "Mozilla/5.0 (compatible; PghGeeksBot/1.0)"


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.generator = ""
        self.img_count = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        amap = dict(attrs)
        if tag == "meta" and amap.get("name", "").lower() == "generator":
            self.generator = amap.get("content", "")
        if tag == "img":
            self.img_count += 1


def _fetch(url: str) -> tuple[str, bool]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        html = resp.read(256_000).decode("utf-8", errors="replace")
        used_https = resp.url.startswith("https://")
    return html, used_https


def classify_url(url: str | None) -> dict[str, Any]:
    """
    Visit url, return {"site_status": str, "signals": dict}.
    Never raises — broken sites return site_status='broken'.
    """
    signals: dict[str, Any] = {"flags": []}
    if not url:
        return {"site_status": "no-site", "signals": {"flags": ["no-website-url"]}}
    if not url.startswith("http"):
        url = "https://" + url
    try:
        html, used_https = _fetch(url)
    except ssl.SSLError:
        try:
            html, used_https = _fetch(url.replace("https://", "http://"))
            signals["flags"].append("no-https")
        except Exception:
            return {"site_status": "broken", "signals": {"flags": ["unreachable"]}}
    except Exception:
        return {"site_status": "broken", "signals": {"flags": ["unreachable"]}}
    if not used_https and "no-https" not in signals["flags"]:
        signals["flags"].append("no-https")
    site_status = "unknown"
    for pattern, flag, platform in _FINGERPRINTS:
        if re.search(pattern, html, re.IGNORECASE):
            signals["flags"].append(flag)
            site_status = platform
            break
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    if parser.generator:
        signals["generator"] = parser.generator
        gen = parser.generator.lower()
        if site_status == "unknown":
            if "wordpress" in gen:
                site_status = "wordpress"
            elif "wix" in gen:
                site_status = "wix"
    if "viewport" not in html.lower():
        signals["flags"].append("mobile-broken")
    if site_status == "unknown" and html:
        site_status = "static"
    return {"site_status": site_status, "signals": signals}
