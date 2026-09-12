"""Shared field normalization, validation, and resilience helpers.

Kept free of Selenium imports so it is unit-testable without a browser and
reusable by every future ingestion source (browser scrapers, bulk-file
parsers, API feeds).
"""

from __future__ import annotations

import random
import re
from typing import Any


KNOWN_TRANSFORMS = {
    "text",
    "money",
    "integer",
    "number",
    "detail_beds",
    "detail_baths",
    "detail_sqft",
    "brokerage",
}


def normalize(value: str, transform: str) -> str:
    """Collapse whitespace and extract the canonical form of a field.

    Returns the empty string when nothing usable is present, never raising
    for unparseable content — a parse miss is an empty cell, not a crash.
    """
    value = " ".join(value.split())
    if not value:
        return ""
    if transform == "text":
        return value
    if transform == "money":
        cleaned = re.sub(r"[^\d,.\-]", "", value).replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        return match.group(0) if match else ""
    if transform == "integer":
        match = re.search(r"-?[\d,]+", value)
        return match.group(0).replace(",", "") if match else ""
    if transform == "number":
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        return match.group(0) if match else ""
    if transform == "detail_beds":
        if re.search(r"\bstudio\b", value, flags=re.IGNORECASE):
            return "0"
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:bd|bds|bed|beds|bedroom|bedrooms)\b",
            value,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""
    if transform == "detail_baths":
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:ba|bath|baths|bathroom|bathrooms)\b",
            value,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""
    if transform == "detail_sqft":
        match = re.search(
            r"([\d,]+)\s*(?:sq\.?\s*ft\.?|sqft|ft²)\b",
            value,
            flags=re.IGNORECASE,
        )
        return match.group(1).replace(",", "") if match else ""
    if transform == "brokerage":
        # Live cards commonly prefix the brokerage with an MLS identifier.
        return re.sub(r"^MLS®?\s*ID\s*#[^,]+,\s*", "", value).strip()
    raise ValueError(f"Unknown transform: {transform}")


def apply_validation(value: str, transform: str, validation: dict[str, Any] | None) -> str:
    """Enforce optional numeric bounds; out-of-range values become empty.

    This exists so a selector that starts matching the wrong element (markup
    churn) cannot poison the dataset with absurd values like a $2,000,000,000
    "price" or 400 "beds". The failure surfaces as missing data, which is
    visible in a quality report — a wrong number is not.
    """
    if not value or not validation:
        return value
    if transform not in {"money", "integer", "number"}:
        return value
    try:
        numeric = float(value)
    except ValueError:
        return ""
    minimum = validation.get("min")
    maximum = validation.get("max")
    if minimum is not None and numeric < minimum:
        return ""
    if maximum is not None and numeric > maximum:
        return ""
    return value


def backoff_seconds(attempt: int, base: float, cap: float = 120.0, jitter: float = 0.25) -> float:
    """Exponential backoff with jitter: base * 2**(attempt-1), bounded by cap.

    ``attempt`` is 1-based. The jitter band prevents a fleet of retries from
    re-syncing into lockstep after a transient outage.
    """
    delay = min(base * (2 ** max(0, attempt - 1)), cap)
    return max(0.0, delay + random.uniform(-jitter * delay, jitter * delay))


def normalize_record_key(listing_id: str, url: str) -> str:
    """Canonical dedup key: prefer a stable id, fall back to a normalized URL.

    URL keys are lowercased and trailing slashes stripped so the same page
    reached via two spellings dedups to one record. The canonical form of the
    stored URL is not altered — only the comparison key.
    """
    listing_id = listing_id.strip()
    if listing_id:
        return f"id:{listing_id}"
    url = url.strip().lower().rstrip("/")
    if not url:
        return ""
    return f"url:{url}"


def as_bool(value: object, default: bool) -> bool:
    """String-safe boolean parsing; JSON configs often smuggle strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
