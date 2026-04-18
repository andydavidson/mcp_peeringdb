"""Tests for peeringdb_mcp.pricing_data — data loading and search/filter logic."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from peeringdb_mcp.pricing_data import IX_PRICING, _load, search_ix_pricing

_REQUIRED_KEYS = {
    "ixp",
    "location",
    "secure_route_servers",
    "bcp214",
    "no_public_pricing",
    "price_400g_eur_month",
    "price_100g_eur_month",
    "price_10g_eur_month",
    "cost_per_mbps_400g_85pct",
    "cost_per_mbps_400g_40pct",
    "cost_per_mbps_100g_85pct",
    "cost_per_mbps_100g_40pct",
    "cost_per_mbps_10g_85pct",
    "cost_per_mbps_10g_40pct",
    "notes",
}


# ── Data loading ───────────────────────────────────────────────────────────────

def test_ix_pricing_is_non_empty():
    assert len(IX_PRICING) > 0


def test_ix_pricing_entry_schema():
    for entry in IX_PRICING:
        assert _REQUIRED_KEYS <= entry.keys(), f"Missing keys in entry: {entry['ixp']}"


def test_ix_pricing_ixp_names_are_strings():
    assert all(isinstance(r["ixp"], str) and r["ixp"] for r in IX_PRICING)


def test_ix_pricing_no_public_pricing_is_bool():
    assert all(isinstance(r["no_public_pricing"], bool) for r in IX_PRICING)


def test_ix_pricing_numeric_fields_are_float_or_none():
    numeric_keys = [
        "price_400g_eur_month", "price_100g_eur_month", "price_10g_eur_month",
        "cost_per_mbps_400g_85pct", "cost_per_mbps_400g_40pct",
        "cost_per_mbps_100g_85pct", "cost_per_mbps_100g_40pct",
        "cost_per_mbps_10g_85pct", "cost_per_mbps_10g_40pct",
    ]
    for entry in IX_PRICING:
        for key in numeric_keys:
            val = entry[key]
            assert val is None or isinstance(val, float), (
                f"{entry['ixp']}.{key} should be float or None, got {type(val)}"
            )


def test_load_missing_file_raises(tmp_path):
    missing = tmp_path / "missing.json"
    with patch("peeringdb_mcp.pricing_data._DATA_FILE", missing):
        with pytest.raises(FileNotFoundError, match="refresh_pricing.py"):
            _load()


def test_load_reads_json_file(tmp_path):
    data = [{"ixp": "TEST-IX", "location": "Testland"}]
    data_file = tmp_path / "ix_pricing.json"
    data_file.write_text(json.dumps(data), encoding="utf-8")
    with patch("peeringdb_mcp.pricing_data._DATA_FILE", data_file):
        result = _load()
    assert result == data


# ── search_ix_pricing: filtering ───────────────────────────────────────────────

def test_no_filters_returns_all():
    results = search_ix_pricing("key", limit=1000)
    assert len(results) == len(IX_PRICING)


def test_filter_by_name_exact():
    results = search_ix_pricing("key", name="AMS-IX")
    assert len(results) == 1
    assert results[0]["ixp"] == "AMS-IX"


def test_filter_by_name_partial():
    results = search_ix_pricing("key", name="LINX")
    assert all("LINX" in r["ixp"] for r in results)
    assert len(results) > 1


def test_filter_by_name_case_insensitive():
    lower = search_ix_pricing("key", name="ams-ix")
    upper = search_ix_pricing("key", name="AMS-IX")
    assert {r["ixp"] for r in lower} == {r["ixp"] for r in upper}


def test_filter_by_name_no_match():
    results = search_ix_pricing("key", name="DOESNOTEXIST_XYZZY")
    assert results == []


def test_filter_by_location():
    results = search_ix_pricing("key", location="Amsterdam")
    assert all("Amsterdam" in r["location"] for r in results)
    assert len(results) >= 1


def test_filter_by_location_country():
    results = search_ix_pricing("key", location="Germany")
    assert all("Germany" in r["location"] for r in results)
    assert len(results) >= 1


def test_filter_by_location_case_insensitive():
    lower = search_ix_pricing("key", location="germany")
    upper = search_ix_pricing("key", location="Germany")
    assert {r["ixp"] for r in lower} == {r["ixp"] for r in upper}


def test_filter_secure_route_servers_only():
    results = search_ix_pricing("key", secure_route_servers_only=True)
    assert len(results) > 0
    assert all(r["secure_route_servers"] is True for r in results)


def test_filter_has_public_pricing_true():
    results = search_ix_pricing("key", has_public_pricing=True)
    assert len(results) > 0
    assert all(not r["no_public_pricing"] for r in results)


def test_filter_has_public_pricing_false():
    results = search_ix_pricing("key", has_public_pricing=False)
    assert len(results) > 0
    assert all(r["no_public_pricing"] for r in results)


def test_filter_has_public_pricing_covers_all():
    with_pricing = search_ix_pricing("key", has_public_pricing=True, limit=1000)
    without_pricing = search_ix_pricing("key", has_public_pricing=False, limit=1000)
    assert len(with_pricing) + len(without_pricing) == len(IX_PRICING)


def test_filter_max_price_100g():
    ceiling = 500.0
    results = search_ix_pricing("key", max_price_100g=ceiling, has_public_pricing=True)
    assert len(results) > 0
    for r in results:
        assert r["price_100g_eur_month"] is not None
        assert r["price_100g_eur_month"] <= ceiling


def test_filter_max_price_100g_excludes_no_pricing():
    # Entries with no_public_pricing=True have price_100g_eur_month=None,
    # so they must be excluded when max_price_100g is set.
    results = search_ix_pricing("key", max_price_100g=9999.0)
    assert all(r["price_100g_eur_month"] is not None for r in results)


def test_filter_combined():
    results = search_ix_pricing(
        "key",
        location="Germany",
        secure_route_servers_only=True,
        has_public_pricing=True,
    )
    assert len(results) > 0
    for r in results:
        assert "Germany" in r["location"]
        assert r["secure_route_servers"] is True
        assert not r["no_public_pricing"]


# ── search_ix_pricing: sorting ─────────────────────────────────────────────────

def test_sort_by_cost_100g_85pct_default():
    results = search_ix_pricing("key", has_public_pricing=True)
    costs = [r["cost_per_mbps_100g_85pct"] for r in results if r["cost_per_mbps_100g_85pct"] is not None]
    assert costs == sorted(costs)


def test_sort_none_values_last():
    results = search_ix_pricing("key", sort_by="cost_per_mbps_100g_85pct", limit=1000)
    none_indices = [i for i, r in enumerate(results) if r["cost_per_mbps_100g_85pct"] is None]
    non_none_indices = [i for i, r in enumerate(results) if r["cost_per_mbps_100g_85pct"] is not None]
    if none_indices and non_none_indices:
        assert min(none_indices) > max(non_none_indices)


def test_sort_by_ixp_name():
    results = search_ix_pricing("key", sort_by="ixp", has_public_pricing=True, limit=20)
    names = [r["ixp"] for r in results]
    assert names == sorted(names)


def test_sort_by_price_100g():
    results = search_ix_pricing("key", sort_by="price_100g_eur_month", has_public_pricing=True)
    prices = [r["price_100g_eur_month"] for r in results if r["price_100g_eur_month"] is not None]
    assert prices == sorted(prices)


def test_sort_by_invalid_key_falls_back_gracefully():
    results = search_ix_pricing("key", sort_by="totally_invalid_field", limit=5)
    assert isinstance(results, list)
    assert len(results) <= 5


# ── search_ix_pricing: limit ───────────────────────────────────────────────────

def test_limit_respected():
    results = search_ix_pricing("key", limit=3)
    assert len(results) == 3


def test_limit_zero():
    results = search_ix_pricing("key", limit=0)
    assert results == []


def test_limit_larger_than_dataset():
    results = search_ix_pricing("key", limit=10_000)
    assert len(results) == len(IX_PRICING)
