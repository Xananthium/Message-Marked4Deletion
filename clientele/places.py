"""
places.py — Google Places API client for Pittsburgh Geeks prospect pipeline.

Key: /home/discnxt/.secrets/google-places.env  (GOOGLE_PLACES_API_KEY)
DO NOT call this against the live API until operator green-lights ingest.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SECRETS = Path("/home/discnxt/.secrets/google-places.env")
_PLACES_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"

# Pittsburgh bounding box (sw_lat, sw_lng, ne_lat, ne_lng)
PITTSBURGH_BBOX = (40.3614, -80.0954, 40.5016, -79.8659)


def _load_key() -> str:
    if not _SECRETS.exists():
        raise FileNotFoundError(f"Missing secrets file: {_SECRETS}")
    for line in _SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith("GOOGLE_PLACES_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key == "PLACEHOLDER":
                raise ValueError("GOOGLE_PLACES_API_KEY is still PLACEHOLDER — provision before calling.")
            return key
    raise KeyError("GOOGLE_PLACES_API_KEY not found in secrets file.")


def search_pittsburgh(
    category: str,
    lat_lng_box: tuple[float, float, float, float] = PITTSBURGH_BBOX,
) -> list[dict[str, Any]]:
    """
    Query Google Places text search for `category` businesses in Pittsburgh.

    Args:
        category: e.g. 'coffee shop', 'roofing contractor', 'accountant'
        lat_lng_box: (sw_lat, sw_lng, ne_lat, ne_lng) bounding box

    Returns:
        List of raw Place dicts from the API (name, place_id, formatted_address,
        website, formatted_phone_number if present, geometry).
    """
    api_key = _load_key()
    sw_lat, sw_lng, ne_lat, ne_lng = lat_lng_box
    center_lat = (sw_lat + ne_lat) / 2
    center_lng = (sw_lng + ne_lng) / 2

    results: list[dict] = []
    next_page_token: str | None = None

    while True:
        params: dict[str, str] = {
            "query": f"{category} in Pittsburgh PA",
            "location": f"{center_lat},{center_lng}",
            "key": api_key,
        }
        if next_page_token:
            params["pagetoken"] = next_page_token

        url = _PLACES_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())

        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(f"Places API error: {data.get('status')} — {data.get('error_message', '')}")

        results.extend(data.get("results", []))
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break

    return results


if __name__ == "__main__":
    # Manual smoke-test — only runs if key is provisioned.
    sample = search_pittsburgh("coffee shop")
    print(f"Found {len(sample)} results")
    if sample:
        print(json.dumps(sample[0], indent=2))
