"""
scrape.py — Alternative ingest path for prospects without a Google Places match.

STUB — flesh out when LinkedIn lookup or directory scraping is approved.

Planned sources:
  - LinkedIn company search (Pittsburgh + category)
  - Pittsburgh city business license directory
  - Yelp (no API — HTML only, check ToS before running)

None of these paths are active. Operator must green-light before calling
any external endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def scrape_linkedin(category: str, limit: int = 50) -> list[dict[str, Any]]:
    """
    STUB — LinkedIn company search for Pittsburgh businesses.

    Args:
        category: e.g. 'roofing contractor'
        limit: max results to return

    Returns:
        List of dicts compatible with ingest.ingest_places() shape:
        {name, place_id (None), formatted_address, website, ...}

    Raises:
        NotImplementedError until LinkedIn scraping is approved and implemented.
    """
    raise NotImplementedError(
        "scrape_linkedin is a stub. Implement after operator approves LinkedIn lookup path."
    )


def scrape_city_licenses(category: str) -> list[dict[str, Any]]:
    """
    STUB — Pittsburgh city business license directory.

    Source: https://apps.pittsburghpa.gov/businesslicense
    Raises NotImplementedError until implemented.
    """
    raise NotImplementedError(
        "scrape_city_licenses is a stub. Implement after confirming ToS allows automated reads."
    )


if __name__ == "__main__":
    print("scrape.py is a stub. Nothing runs yet.")
    print("Implement scrape_linkedin() or scrape_city_licenses() before calling.")
