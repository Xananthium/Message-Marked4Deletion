"""Site context loader for AIB workers.

Public API:
    load(fqdn) -> dict
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extras

_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)


def load(fqdn: str) -> dict:
    """Load full site context for a given FQDN.

    Args:
        fqdn: Fully-qualified domain name, lowercase (e.g. 'brangembringem.com').

    Returns:
        dict with keys:
            site          (dict)  — full sites row as dict
            brand_voice   (str)   — contents of brand_voice_path, or '' if missing
            mission       (str)   — contents of mission_path, or '' if missing

    Raises:
        KeyError: if fqdn not found in the sites table.
        RuntimeError: on DB connection failure.
    """
    fqdn = fqdn.lower().strip()

    with psycopg2.connect(_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM sites WHERE fqdn = %s", (fqdn,))
            row = cur.fetchone()

    if row is None:
        raise KeyError(f"site not found in sites table: {fqdn!r}")

    site = dict(row)

    brand_voice = ""
    if site.get("brand_voice_path"):
        try:
            with open(site["brand_voice_path"], "r", encoding="utf-8") as f:
                brand_voice = f.read()
        except FileNotFoundError:
            brand_voice = ""

    mission = ""
    if site.get("mission_path"):
        try:
            with open(site["mission_path"], "r", encoding="utf-8") as f:
                mission = f.read()
        except FileNotFoundError:
            mission = ""

    return {
        "site": site,
        "brand_voice": brand_voice,
        "mission": mission,
    }
