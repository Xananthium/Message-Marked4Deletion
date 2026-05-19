"""
main.py — Orchestrator for the Pittsburgh Geeks prospect pipeline.

Importable. No CLI yet — run each function manually or via future cron.

Usage (manual):
    python -c "from main import ingest_search; ingest_search('coffee shop')"
    python -c "from main import evaluate_one; evaluate_one()"
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def ingest_search(category: str) -> dict[str, int]:
    """
    Search Google Places for Pittsburgh businesses in `category`,
    dedupe, and insert into prospects with status='unevaluated'.

    Args:
        category: e.g. 'coffee shop', 'roofing contractor', 'accountant'

    Returns:
        {"inserted": N, "skipped": N}

    Raises:
        ValueError if GOOGLE_PLACES_API_KEY is still PLACEHOLDER.
    """
    from places import search_pittsburgh
    from ingest import ingest_places

    log.info("Searching Google Places for '%s' in Pittsburgh...", category)
    results = search_pittsburgh(category)
    log.info("Got %d results from Places API.", len(results))

    return ingest_places(results, category)


def evaluate_one() -> str | None:
    """
    Pick the oldest unevaluated prospect, visit its site, classify it,
    and flip its status to 'evaluated'.

    Returns:
        business_name of the prospect evaluated, or None if queue is empty.
    """
    from evaluate import evaluate_one as _evaluate_one

    return _evaluate_one()


def evaluate_batch(n: int = 10) -> list[str]:
    """
    Evaluate up to n unevaluated prospects in sequence.

    Returns:
        List of business_names evaluated.
    """
    evaluated = []
    for _ in range(n):
        name = evaluate_one()
        if name is None:
            break
        evaluated.append(name)
    log.info("evaluate_batch: processed %d prospects.", len(evaluated))
    return evaluated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("main.py loaded. Available functions: ingest_search(category), evaluate_one(), evaluate_batch(n)")
    print("DO NOT call ingest_search() until operator has provisioned GOOGLE_PLACES_API_KEY.")
