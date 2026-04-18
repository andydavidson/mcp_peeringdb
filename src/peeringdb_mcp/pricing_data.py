"""IX pricing data loader and query helpers.

Data is stored in ix_pricing.json alongside this file and is refreshed by
running scripts/refresh_pricing.py.

Source: http://peering.exposed/ — maintained by Job Snijders et al.
All prices are in EUR/month. cost_per_mbps values are cents/month/Mbps,
calculated at 85% or 40% port utilisation with NRC amortised over 3 years.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_FILE = Path(__file__).parent / "ix_pricing.json"

_SORT_KEYS = {
    "cost_per_mbps_100g_85pct",
    "cost_per_mbps_100g_40pct",
    "cost_per_mbps_10g_85pct",
    "cost_per_mbps_10g_40pct",
    "cost_per_mbps_400g_85pct",
    "cost_per_mbps_400g_40pct",
    "price_100g_eur_month",
    "price_10g_eur_month",
    "price_400g_eur_month",
    "ixp",
    "location",
}


def _load() -> list[dict]:
    if not _DATA_FILE.exists():
        raise FileNotFoundError(
            f"Pricing data not found at {_DATA_FILE}. "
            "Run scripts/refresh_pricing.py to fetch it."
        )
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


# Loaded once at import time.
IX_PRICING: list[dict] = _load()


def search_ix_pricing(
    api_key: str,  # accepted for interface consistency, not used
    name: str | None = None,
    location: str | None = None,
    secure_route_servers_only: bool = False,
    has_public_pricing: bool | None = None,
    max_price_100g: float | None = None,
    sort_by: str = "cost_per_mbps_100g_85pct",
    limit: int = 50,
) -> list[dict]:
    results = list(IX_PRICING)

    if name:
        nl = name.lower()
        results = [r for r in results if nl in r["ixp"].lower()]

    if location:
        ll = location.lower()
        results = [r for r in results if ll in r["location"].lower()]

    if secure_route_servers_only:
        results = [r for r in results if r["secure_route_servers"] is True]

    if has_public_pricing is True:
        results = [r for r in results if not r["no_public_pricing"]]
    elif has_public_pricing is False:
        results = [r for r in results if r["no_public_pricing"]]

    if max_price_100g is not None:
        results = [
            r for r in results
            if r["price_100g_eur_month"] is not None
            and r["price_100g_eur_month"] <= max_price_100g
        ]

    sort_key = sort_by if sort_by in _SORT_KEYS else "cost_per_mbps_100g_85pct"
    is_string_sort = sort_key in ("ixp", "location")

    def _sort_val(r: dict):
        v = r.get(sort_key)
        if v is None:
            return (1, "" if is_string_sort else float("inf"))
        return (0, v)

    results.sort(key=_sort_val)
    return results[:limit]
