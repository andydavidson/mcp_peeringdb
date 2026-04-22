"""Tests for peeringdb_mcp.server — serialisation, call_tool, and _dispatch."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from peeringdb_mcp.server import (
    _annotate_ix_scope, _clean, _dispatch, _dump, _ix_countries,
    _project_netixlan, _project_netfac, call_tool,
)
from mcp import types


# ── _clean ─────────────────────────────────────────────────────────────────────

def test_clean_none_becomes_empty_string():
    assert _clean(None) == ""


def test_clean_bool_preserved():
    assert _clean(True) is True
    assert _clean(False) is False


def test_clean_int_preserved():
    assert _clean(42) == 42


def test_clean_float_preserved():
    assert _clean(3.14) == 3.14


def test_clean_string_preserved():
    assert _clean("hello") == "hello"


def test_clean_date_to_isoformat():
    assert _clean(date(2024, 1, 15)) == "2024-01-15"


def test_clean_datetime_to_isoformat():
    assert _clean(datetime(2024, 1, 15, 12, 0, 0)) == "2024-01-15T12:00:00"


def test_clean_dict_recursed():
    result = _clean({"a": 1, "b": None, "c": True})
    assert result == {"a": 1, "c": True}  # None value dropped


def test_clean_dict_drops_none_values():
    result = _clean({"keep": "yes", "drop": None})
    assert "drop" not in result
    assert result["keep"] == "yes"


def test_clean_dict_keys_coerced_to_str():
    result = _clean({1: "numeric key"})
    assert "1" in result


def test_clean_list_recursed():
    result = _clean([1, None, "x"])
    assert result == [1, "", "x"]


def test_clean_nested():
    result = _clean({"items": [{"id": 1, "note": None}]})
    assert result == {"items": [{"id": 1}]}


def test_clean_unknown_type_stringified():
    class Weird:
        def __str__(self): return "weird"
    assert _clean(Weird()) == "weird"


# ── _dump ──────────────────────────────────────────────────────────────────────

def test_dump_produces_toml_string():
    result = _dump({"name": "AMS-IX", "price": 3600})
    assert 'name = "AMS-IX"' in result
    assert "price = 3600" in result


def test_dump_handles_nested_dict():
    result = _dump({"exchange": {"id": 1, "name": "AMS-IX"}})
    assert "[exchange]" in result
    assert 'name = "AMS-IX"' in result


def test_dump_handles_list_of_dicts():
    result = _dump({"items": [{"id": 1}, {"id": 2}]})
    assert "items" in result
    assert "id = 1" in result
    assert "id = 2" in result


def test_dump_error_on_bad_input():
    # A type that can't be serialised even after _clean (edge case)
    # _dump should return an error string, not raise.
    with patch("peeringdb_mcp.server.tomli_w.dumps", side_effect=Exception("boom")):
        result = _dump({"x": 1})
    assert "error" in result
    assert "boom" in result


# ── call_tool: API key guard ───────────────────────────────────────────────────

async def test_call_tool_missing_api_key_returns_error():
    result = await call_tool("get_network_by_asn", {"asn": 15169})
    assert len(result) == 1
    assert "peeringdb_api_key is required" in result[0].text


async def test_call_tool_empty_api_key_returns_error():
    result = await call_tool("get_network_by_asn", {"peeringdb_api_key": "  ", "asn": 15169})
    assert "peeringdb_api_key is required" in result[0].text


async def test_call_tool_none_arguments_returns_error():
    result = await call_tool("get_network_by_asn", None)
    assert "peeringdb_api_key is required" in result[0].text


async def test_call_tool_dispatch_exception_returns_error():
    with patch("peeringdb_mcp.server._dispatch", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await call_tool("get_network_by_asn", {"peeringdb_api_key": "key", "asn": 1})
    assert len(result) == 1
    assert "boom" in result[0].text


# ── _dispatch ─────────────────────────────────────────────────────────────────

async def test_dispatch_unknown_tool():
    result = await _dispatch("no_such_tool", {}, "key")
    assert "Unknown tool" in result


async def test_dispatch_get_network_by_asn_found():
    net = {"id": 1, "name": "Google", "asn": 15169}
    with patch("peeringdb_mcp.server.queries.get_network_by_asn", new=AsyncMock(return_value=net)):
        result = await _dispatch("get_network_by_asn", {"asn": "15169"}, "key")
    assert "Google" in result


async def test_dispatch_get_network_by_asn_not_found():
    with patch("peeringdb_mcp.server.queries.get_network_by_asn", new=AsyncMock(return_value=None)):
        result = await _dispatch("get_network_by_asn", {"asn": "99999"}, "key")
    assert "not found" in result


async def test_dispatch_get_network_found():
    net = {"id": 42, "name": "Cloudflare"}
    with patch("peeringdb_mcp.server.queries.get_network", new=AsyncMock(return_value=net)):
        result = await _dispatch("get_network", {"id": "42"}, "key")
    assert "Cloudflare" in result


async def test_dispatch_search_networks():
    with patch("peeringdb_mcp.server.queries.search_networks", new=AsyncMock(return_value=[])):
        result = await _dispatch("search_networks", {"name": "Google"}, "key")
    assert "networks" in result


async def test_dispatch_get_exchange_found():
    ix = {"id": 26, "name": "AMS-IX"}
    with patch("peeringdb_mcp.server.queries.get_exchange", new=AsyncMock(return_value=ix)):
        result = await _dispatch("get_exchange", {"id": "26"}, "key")
    assert "AMS-IX" in result


async def test_dispatch_get_exchange_not_found():
    with patch("peeringdb_mcp.server.queries.get_exchange", new=AsyncMock(return_value=None)):
        result = await _dispatch("get_exchange", {"id": "9999"}, "key")
    assert "not found" in result


async def test_dispatch_get_facility_found():
    fac = {"id": 1, "name": "Equinix AM1"}
    with patch("peeringdb_mcp.server.queries.get_facility", new=AsyncMock(return_value=fac)):
        result = await _dispatch("get_facility", {"id": "1"}, "key")
    assert "Equinix AM1" in result


async def test_dispatch_get_facility_not_found():
    with patch("peeringdb_mcp.server.queries.get_facility", new=AsyncMock(return_value=None)):
        result = await _dispatch("get_facility", {"id": "9999"}, "key")
    assert "not found" in result


async def test_dispatch_get_organisation_not_found():
    with patch("peeringdb_mcp.server.queries.get_organisation", new=AsyncMock(return_value=None)):
        result = await _dispatch("get_organisation", {"id": "9999"}, "key")
    assert "not found" in result


async def test_dispatch_get_my_profile_not_found():
    with patch("peeringdb_mcp.server.queries.get_my_profile", new=AsyncMock(return_value=None)):
        result = await _dispatch("get_my_profile", {}, "key")
    assert "not found" in result


async def test_dispatch_find_common_exchanges():
    common = [{"ix_id": 26, "ix_name": "AMS-IX"}]
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=common)):
        result = await _dispatch("find_common_exchanges", {"asn_a": "15169", "asn_b": "32934"}, "key")
    assert "common_exchanges" in result
    assert "AMS-IX" in result


async def test_dispatch_find_common_facilities():
    common = [{"fac_id": 1}]
    with patch("peeringdb_mcp.server.queries.find_common_facilities", new=AsyncMock(return_value=common)):
        result = await _dispatch("find_common_facilities", {"asn_a": "15169", "asn_b": "32934"}, "key")
    assert "common_facilities" in result


async def test_dispatch_search_ix_pricing_no_filters():
    result = await _dispatch("search_ix_pricing", {}, "key")
    assert "ix_pricing" in result
    assert "peering.exposed" in result


async def test_dispatch_search_ix_pricing_by_name():
    result = await _dispatch("search_ix_pricing", {"name": "AMS-IX"}, "key")
    assert "AMS-IX" in result


async def test_dispatch_search_ix_pricing_respects_limit():
    result = await _dispatch("search_ix_pricing", {"limit": "2"}, "key")
    # TOML: count = 2
    assert "count = 2" in result


async def test_dispatch_search_ix_pricing_limit_capped_at_158():
    result = await _dispatch("search_ix_pricing", {"limit": "9999"}, "key")
    # The dataset has 158 entries; count should not exceed that.
    import re
    m = re.search(r"count = (\d+)", result)
    assert m and int(m.group(1)) <= 158


# ── _ix_countries ─────────────────────────────────────────────────────────────

def test_ix_countries_empty_ixfac_set():
    assert _ix_countries({"ixfac_set": []}) == []


def test_ix_countries_no_ixfac_set_key():
    assert _ix_countries({}) == []


def test_ix_countries_single_country():
    ix = {"ixfac_set": [{"fac": {"country": "NL"}}, {"fac": {"country": "NL"}}]}
    assert _ix_countries(ix) == ["NL"]


def test_ix_countries_multiple_countries():
    ix = {
        "ixfac_set": [
            {"fac": {"country": "NL"}},
            {"fac": {"country": "DE"}},
            {"fac": {"country": "IE"}},
        ]
    }
    assert _ix_countries(ix) == ["DE", "IE", "NL"]  # sorted


def test_ix_countries_fallback_to_ixfac_country():
    # When fac is absent, fall back to country on the ixfac object itself
    ix = {"ixfac_set": [{"country": "FR"}]}
    assert _ix_countries(ix) == ["FR"]


def test_ix_countries_skips_non_dict_entries():
    ix = {"ixfac_set": [42, None, {"fac": {"country": "US"}}]}
    assert _ix_countries(ix) == ["US"]


def test_ix_countries_skips_empty_country():
    ix = {"ixfac_set": [{"fac": {"country": ""}}, {"fac": {"country": "SE"}}]}
    assert _ix_countries(ix) == ["SE"]


# ── _annotate_ix_scope ─────────────────────────────────────────────────────────

def test_annotate_scope_local_single_country():
    ix = {"name": "AMS-IX", "ixfac_set": [{"fac": {"country": "NL"}}]}
    result = _annotate_ix_scope(ix)
    assert result["ix_scope"] == "local"
    assert result["ix_countries_present"] == ["NL"]
    assert "scope_warning" not in result


def test_annotate_scope_regional_multi_country():
    ix = {
        "name": "NL-ix",
        "ixfac_set": [
            {"fac": {"country": "NL"}},
            {"fac": {"country": "DE"}},
            {"fac": {"country": "IE"}},
        ],
    }
    result = _annotate_ix_scope(ix)
    assert result["ix_scope"] == "regional_dispersed"
    assert set(result["ix_countries_present"]) == {"NL", "DE", "IE"}
    assert "scope_warning" in result
    warning = result["scope_warning"]
    assert "DISPERSED EXCHANGE" in warning
    assert "NOT A LOCAL IXP" in warning
    assert "3 countries" in warning
    assert "long-haul" in warning


def test_annotate_scope_unknown_no_facility_data():
    ix = {"name": "MYSTERY-IX", "ixfac_set": []}
    result = _annotate_ix_scope(ix)
    assert result["ix_scope"] == "unknown"
    assert result["ix_countries_present"] == []
    assert "scope_warning" not in result


def test_annotate_scope_modifies_in_place():
    ix = {"name": "TEST-IX", "ixfac_set": [{"fac": {"country": "US"}}]}
    returned = _annotate_ix_scope(ix)
    assert returned is ix  # same object


def test_annotate_scope_regional_lists_countries_in_warning():
    ix = {
        "ixfac_set": [
            {"fac": {"country": "GB"}},
            {"fac": {"country": "FR"}},
        ]
    }
    _annotate_ix_scope(ix)
    assert "FR" in ix["scope_warning"]
    assert "GB" in ix["scope_warning"]


# ── _dispatch: scope annotation on get_exchange ─────────────────────────────

async def test_dispatch_get_exchange_local_scope():
    ix = {"id": 1, "name": "LOCAL-IX", "ixfac_set": [{"fac": {"country": "NL"}}]}
    with patch("peeringdb_mcp.server.queries.get_exchange", new=AsyncMock(return_value=ix)):
        result = await _dispatch("get_exchange", {"id": "1"}, "key")
    assert 'ix_scope = "local"' in result
    assert "scope_warning" not in result


async def test_dispatch_get_exchange_regional_scope():
    ix = {
        "id": 99,
        "name": "NL-ix",
        "ixfac_set": [
            {"fac": {"country": "NL"}},
            {"fac": {"country": "IE"}},
            {"fac": {"country": "DE"}},
        ],
    }
    with patch("peeringdb_mcp.server.queries.get_exchange", new=AsyncMock(return_value=ix)):
        result = await _dispatch("get_exchange", {"id": "99"}, "key")
    assert 'ix_scope = "regional_dispersed"' in result
    assert "DISPERSED EXCHANGE" in result


# ── _dispatch: scope annotation on find_common_exchanges ────────────────────

async def test_dispatch_find_common_exchanges_annotates_scope():
    common = [
        {
            "ix_id": 26,
            "ix_name": "AMS-IX",
            "ixfac_set": [{"fac": {"country": "NL"}}],
            "network_a_entries": [],
            "network_b_entries": [],
        }
    ]
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=common)):
        result = await _dispatch("find_common_exchanges", {"asn_a": "15169", "asn_b": "32934"}, "key")
    assert "common_exchanges" in result
    assert "ix_scope" in result
    # ixfac_set must be stripped from the output
    assert "ixfac_set" not in result


async def test_dispatch_find_common_exchanges_regional_flagged():
    common = [
        {
            "ix_id": 999,
            "ix_name": "NL-ix",
            "ixfac_set": [
                {"fac": {"country": "NL"}},
                {"fac": {"country": "IE"}},
            ],
            "network_a_entries": [],
            "network_b_entries": [],
        }
    ]
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=common)):
        result = await _dispatch("find_common_exchanges", {"asn_a": "1", "asn_b": "2"}, "key")
    assert "regional_dispersed" in result
    assert "DISPERSED EXCHANGE" in result
    assert "ixfac_set" not in result


# ── _dispatch: get_ix_enrichment ────────────────────────────────────────────

async def test_dispatch_get_ix_enrichment_found():
    enrichment = {
        "ixpdb_id": 42,
        "pdb_id": 26,
        "name": "AMS-IX",
        "manrs": True,
        "looking_glass_urls": ["https://lg.ams-ix.net"],
        "traffic_api_url": "https://www.ams-ix.net/ams/statistics?type=log",
        "association": "Euro-IX",
        "participant_count": 950,
        "location_count": 5,
    }
    with patch("peeringdb_mcp.server.queries.get_ix_enrichment", new=AsyncMock(return_value=enrichment)):
        result = await _dispatch("get_ix_enrichment", {"ix_id": "26"}, "key")
    assert "AMS-IX" in result
    assert "manrs" in result
    assert "ix_enrichment" in result


async def test_dispatch_get_ix_enrichment_not_found():
    with patch("peeringdb_mcp.server.queries.get_ix_enrichment", new=AsyncMock(return_value=None)):
        result = await _dispatch("get_ix_enrichment", {"ix_id": "9999"}, "key")
    assert "not found" in result
    assert "IXPDB" in result


# ── _dispatch: get_ix_traffic ────────────────────────────────────────────────

async def test_dispatch_get_ix_traffic_success():
    traffic = {
        "ix_id": 26,
        "ixpdb_name": "AMS-IX",
        "period": "day",
        "category": "bits",
        "traffic_url": "https://www.ams-ix.net/ams/statistics?type=json&period=day&category=bits",
        "current_in_bps": 8_500_000_000_000,
        "current_out_bps": 8_200_000_000_000,
        "average_in_bps": 7_000_000_000_000,
        "average_out_bps": 6_800_000_000_000,
        "peak_in_bps": 10_200_000_000_000,
        "peak_out_bps": 9_800_000_000_000,
        "peak_in_at": "2025-01-15 14:00:00",
        "peak_out_at": "2025-01-15 14:05:00",
        "total_in_bits": 5_040_000_000_000_000,
        "total_out_bits": 4_896_000_000_000_000,
    }
    with patch("peeringdb_mcp.server.queries.get_ix_traffic", new=AsyncMock(return_value=traffic)):
        result = await _dispatch("get_ix_traffic", {"ix_id": "26"}, "key")
    assert "ix_traffic" in result
    assert "AMS-IX" in result
    assert "current_in_bps" in result


async def test_dispatch_get_ix_traffic_defaults():
    traffic = {"ix_id": 26, "ixpdb_name": "AMS-IX", "period": "day", "category": "bits"}
    mock = AsyncMock(return_value=traffic)
    with patch("peeringdb_mcp.server.queries.get_ix_traffic", new=mock):
        await _dispatch("get_ix_traffic", {"ix_id": "26"}, "key")
    mock.assert_called_once_with("key", 26, period="day", category="bits")


async def test_dispatch_get_ix_traffic_custom_period():
    traffic = {"ix_id": 26, "ixpdb_name": "AMS-IX", "period": "week", "category": "pkts"}
    mock = AsyncMock(return_value=traffic)
    with patch("peeringdb_mcp.server.queries.get_ix_traffic", new=mock):
        await _dispatch("get_ix_traffic", {"ix_id": "26", "period": "week", "category": "pkts"}, "key")
    mock.assert_called_once_with("key", 26, period="week", category="pkts")


# ── create_app ─────────────────────────────────────────────────────────────────

def test_create_app_returns_starlette():
    from starlette.applications import Starlette
    from peeringdb_mcp.server import create_app
    app = create_app()
    assert isinstance(app, Starlette)


# ── _project_netixlan ──────────────────────────────────────────────────────────

_IX_INFO = {26: {"name": "AMS-IX", "city": "Amsterdam", "country": "NL"}}
_NETIXLAN = {
    "ix_id": 26, "ipaddr4": "80.249.208.1", "ipaddr6": "2001:7f8:1::a500:1:1",
    "speed": 10000, "is_rs_peer": True, "ixlan_id": 42,
}


def test_project_netixlan_presence_includes_name_and_city():
    result = _project_netixlan(_NETIXLAN, "presence", _IX_INFO)
    assert result["ix_id"] == 26
    assert result["ix_name"] == "AMS-IX"
    assert result["ix_city"] == "Amsterdam"
    assert result["ix_country"] == "NL"
    assert result["is_rs_peer"] is True
    assert "ipaddr4" not in result
    assert "speed" not in result


def test_project_netixlan_routing_includes_ips_and_speed():
    result = _project_netixlan(_NETIXLAN, "routing", _IX_INFO)
    assert result["ix_id"] == 26
    assert result["ix_name"] == "AMS-IX"
    assert result["ipaddr4"] == "80.249.208.1"
    assert result["ipaddr6"] == "2001:7f8:1::a500:1:1"
    assert result["speed"] == 10000
    assert result["is_rs_peer"] is True
    assert result["ixlan_id"] == 42


def test_project_netixlan_ids_only():
    result = _project_netixlan(_NETIXLAN, "ids_only", _IX_INFO)
    assert result == {"ix_id": 26}


def test_project_netixlan_unknown_ix_id_uses_empty_strings():
    entry = {**_NETIXLAN, "ix_id": 9999}
    result = _project_netixlan(entry, "presence", _IX_INFO)
    assert result["ix_id"] == 9999
    assert result["ix_name"] == ""
    assert result["ix_city"] == ""


def test_project_netixlan_none_ix_id():
    entry = {**_NETIXLAN, "ix_id": None}
    result = _project_netixlan(entry, "presence", _IX_INFO)
    assert result["ix_id"] is None
    assert result["ix_name"] == ""


# ── _project_netfac ────────────────────────────────────────────────────────────

_FAC_INFO = {1: {"name": "Equinix AM1", "city": "Amsterdam", "country": "NL"}}
_NETFAC = {"fac_id": 1}


def test_project_netfac_presence_includes_name_and_location():
    result = _project_netfac(_NETFAC, "presence", _FAC_INFO)
    assert result["fac_id"] == 1
    assert result["fac_name"] == "Equinix AM1"
    assert result["city"] == "Amsterdam"
    assert result["country"] == "NL"


def test_project_netfac_routing_same_as_presence():
    # No routing concept at the facility layer — routing and presence are identical.
    presence = _project_netfac(_NETFAC, "presence", _FAC_INFO)
    routing = _project_netfac(_NETFAC, "routing", _FAC_INFO)
    assert presence == routing


def test_project_netfac_ids_only():
    result = _project_netfac(_NETFAC, "ids_only", _FAC_INFO)
    assert result == {"fac_id": 1}


def test_project_netfac_unknown_fac_id_uses_empty_strings():
    entry = {"fac_id": 9999}
    result = _project_netfac(entry, "presence", _FAC_INFO)
    assert result["fac_id"] == 9999
    assert result["fac_name"] == ""
    assert result["city"] == ""
    assert result["country"] == ""


# ── _dispatch: get_networks_by_asn_batch ──────────────────────────────────────

_BATCH_NETWORKS = [
    {
        "id": 1, "asn": 15169, "name": "Google LLC",
        "netixlan_set": [{"ix_id": 26, "ipaddr4": "1.2.3.4", "speed": 10000, "is_rs_peer": True, "ixlan_id": 5}],
        "netfac_set": [{"fac_id": 1}],
    }
]
_BATCH_IX_INFO = {26: {"name": "AMS-IX", "city": "Amsterdam", "country": "NL"}}
_BATCH_FAC_INFO = {1: {"name": "Equinix AM1", "city": "Amsterdam", "country": "NL"}}


async def test_dispatch_batch_networks_presence_projects_correctly():
    mock = AsyncMock(return_value=(_BATCH_NETWORKS, _BATCH_IX_INFO, _BATCH_FAC_INFO))
    with patch("peeringdb_mcp.server.queries.get_networks_by_asn_batch", new=mock):
        result = await _dispatch("get_networks_by_asn_batch", {"asns": [15169]}, "key")
    assert "AMS-IX" in result
    assert "Amsterdam" in result
    assert "Equinix AM1" in result
    assert "ipaddr4" not in result   # presence default strips IPs
    assert "found_count" in result
    assert "requested_count" in result


async def test_dispatch_batch_networks_routing_includes_ips():
    mock = AsyncMock(return_value=(_BATCH_NETWORKS, _BATCH_IX_INFO, _BATCH_FAC_INFO))
    with patch("peeringdb_mcp.server.queries.get_networks_by_asn_batch", new=mock):
        result = await _dispatch(
            "get_networks_by_asn_batch", {"asns": [15169], "detail": "routing"}, "key"
        )
    assert "ipaddr4" in result
    assert "speed" in result


async def test_dispatch_batch_networks_ids_only():
    mock = AsyncMock(return_value=(_BATCH_NETWORKS, _BATCH_IX_INFO, _BATCH_FAC_INFO))
    with patch("peeringdb_mcp.server.queries.get_networks_by_asn_batch", new=mock):
        result = await _dispatch(
            "get_networks_by_asn_batch", {"asns": [15169], "detail": "ids_only"}, "key"
        )
    assert "ix_id" in result
    assert "fac_id" in result
    assert "AMS-IX" not in result   # no name enrichment at ids_only


async def test_dispatch_batch_networks_network_fields_forwarded():
    mock = AsyncMock(return_value=([], {}, {}))
    with patch("peeringdb_mcp.server.queries.get_networks_by_asn_batch", new=mock):
        await _dispatch(
            "get_networks_by_asn_batch",
            {"asns": [15169], "network_fields": ["name", "policy_general"]},
            "key",
        )
    mock.assert_called_once_with("key", [15169], network_fields=["name", "policy_general"])


async def test_dispatch_batch_networks_empty_asns_returns_error():
    result = await _dispatch("get_networks_by_asn_batch", {"asns": []}, "key")
    assert "error" in result


async def test_dispatch_batch_networks_over_limit_returns_error():
    result = await _dispatch(
        "get_networks_by_asn_batch", {"asns": list(range(21))}, "key"
    )
    assert "error" in result
    assert "20" in result


async def test_dispatch_batch_networks_counts_in_result():
    two_networks = [
        {**_BATCH_NETWORKS[0], "asn": 15169},
        {**_BATCH_NETWORKS[0], "asn": 32934, "id": 2},
    ]
    mock = AsyncMock(return_value=(two_networks, _BATCH_IX_INFO, _BATCH_FAC_INFO))
    with patch("peeringdb_mcp.server.queries.get_networks_by_asn_batch", new=mock):
        result = await _dispatch(
            "get_networks_by_asn_batch", {"asns": [15169, 32934, 99999]}, "key"
        )
    assert "requested_count = 3" in result
    assert "found_count = 2" in result


# ── _dispatch: get_exchanges_batch ────────────────────────────────────────────

_BATCH_IX = [
    {
        "id": 26, "name": "AMS-IX",
        "ixfac_set": [{"fac_id": 1, "name": "Equinix AM1", "city": "Amsterdam", "country": "NL"}],
        "ixlan_set": [{"id": 5, "name": "AMS-IX LAN", "netixlan_set": [{"asn": 15169}]}],
    }
]


async def test_dispatch_batch_exchanges_returns_exchanges():
    mock = AsyncMock(return_value=_BATCH_IX)
    with patch("peeringdb_mcp.server.queries.get_exchanges_batch", new=mock):
        result = await _dispatch("get_exchanges_batch", {"ids": [26]}, "key")
    assert "AMS-IX" in result
    assert "exchanges" in result
    assert "found_count" in result


async def test_dispatch_batch_exchanges_applies_scope_annotation():
    ix = [{"id": 26, "name": "AMS-IX", "ixfac_set": [{"fac": {"country": "NL"}}], "ixlan_set": []}]
    with patch("peeringdb_mcp.server.queries.get_exchanges_batch", new=AsyncMock(return_value=ix)):
        result = await _dispatch("get_exchanges_batch", {"ids": [26]}, "key")
    assert "ix_scope" in result


async def test_dispatch_batch_exchanges_strips_member_list_from_ixlan():
    mock = AsyncMock(return_value=_BATCH_IX)
    with patch("peeringdb_mcp.server.queries.get_exchanges_batch", new=mock):
        result = await _dispatch("get_exchanges_batch", {"ids": [26]}, "key")
    # netixlan_set inside ixlan_set entries must be stripped
    assert "15169" not in result


async def test_dispatch_batch_exchanges_projects_ixfac_set():
    mock = AsyncMock(return_value=_BATCH_IX)
    with patch("peeringdb_mcp.server.queries.get_exchanges_batch", new=mock):
        result = await _dispatch("get_exchanges_batch", {"ids": [26]}, "key")
    assert "Equinix AM1" in result
    assert "fac_id" in result


async def test_dispatch_batch_exchanges_empty_ids_returns_error():
    result = await _dispatch("get_exchanges_batch", {"ids": []}, "key")
    assert "error" in result


async def test_dispatch_batch_exchanges_over_limit_returns_error():
    result = await _dispatch("get_exchanges_batch", {"ids": list(range(21))}, "key")
    assert "error" in result
    assert "20" in result


# ── _dispatch: get_facilities_batch ───────────────────────────────────────────

_BATCH_FAC = [
    {
        "id": 1, "name": "Equinix AM1", "city": "Amsterdam", "country": "NL",
        "net_count": 500, "ix_count": 3,
        "netfac_set": [{"net_id": 99, "name": "BigNetwork"}],
        "ixfac_set": [{"ix_id": 26, "name": "AMS-IX"}],
    }
]


async def test_dispatch_batch_facilities_returns_facilities():
    mock = AsyncMock(return_value=_BATCH_FAC)
    with patch("peeringdb_mcp.server.queries.get_facilities_batch", new=mock):
        result = await _dispatch("get_facilities_batch", {"ids": [1]}, "key")
    assert "Equinix AM1" in result
    assert "facilities" in result
    assert "found_count" in result


async def test_dispatch_batch_facilities_strips_netfac_set():
    mock = AsyncMock(return_value=_BATCH_FAC)
    with patch("peeringdb_mcp.server.queries.get_facilities_batch", new=mock):
        result = await _dispatch("get_facilities_batch", {"ids": [1]}, "key")
    # Full network list must be stripped — too large to return in batch
    assert "BigNetwork" not in result
    assert "netfac_set" not in result


async def test_dispatch_batch_facilities_projects_ixfac_set():
    mock = AsyncMock(return_value=_BATCH_FAC)
    with patch("peeringdb_mcp.server.queries.get_facilities_batch", new=mock):
        result = await _dispatch("get_facilities_batch", {"ids": [1]}, "key")
    # IX presence must still appear
    assert "AMS-IX" in result
    assert "ix_id" in result


async def test_dispatch_batch_facilities_empty_ids_returns_error():
    result = await _dispatch("get_facilities_batch", {"ids": []}, "key")
    assert "error" in result


async def test_dispatch_batch_facilities_over_limit_returns_error():
    result = await _dispatch("get_facilities_batch", {"ids": list(range(21))}, "key")
    assert "error" in result
    assert "20" in result


# ── _dispatch: get_exchange_members detail ────────────────────────────────────

_MEMBERS = [
    {"asn": 15169, "name": "Google LLC", "net_id": 1, "ipaddr4": "80.249.208.1",
     "ipaddr6": "2001:7f8:1::1", "speed": 10000, "is_rs_peer": True},
    {"asn": 32934, "name": "Meta", "net_id": 2, "ipaddr4": "80.249.209.1",
     "ipaddr6": None, "speed": 1000, "is_rs_peer": False},
]


async def test_dispatch_exchange_members_routing_default_returns_full_records():
    with patch("peeringdb_mcp.server.queries.get_exchange_members", new=AsyncMock(return_value=_MEMBERS)):
        result = await _dispatch("get_exchange_members", {"ix_id": "26"}, "key")
    assert "ipaddr4" in result
    assert "speed" in result
    assert "is_rs_peer" in result


async def test_dispatch_exchange_members_presence_returns_asn_and_name_only():
    with patch("peeringdb_mcp.server.queries.get_exchange_members", new=AsyncMock(return_value=_MEMBERS)):
        result = await _dispatch(
            "get_exchange_members", {"ix_id": "26", "detail": "presence"}, "key"
        )
    assert "Google LLC" in result
    assert "15169" in result
    assert "ipaddr4" not in result
    assert "speed" not in result
    assert "is_rs_peer" not in result


async def test_dispatch_exchange_members_presence_still_includes_net_id():
    with patch("peeringdb_mcp.server.queries.get_exchange_members", new=AsyncMock(return_value=_MEMBERS)):
        result = await _dispatch(
            "get_exchange_members", {"ix_id": "26", "detail": "presence"}, "key"
        )
    assert "net_id" in result


# ── _dispatch: get_facility_networks detail ───────────────────────────────────

_NETFAC_RECORDS = [
    {"net_id": 1, "local_asn": 15169, "net": {"name": "Google LLC", "asn": 15169},
     "avail_sonet": False, "avail_ethernet": True},
    {"net_id": 2, "local_asn": 32934, "net": {"name": "Meta", "asn": 32934},
     "avail_sonet": False, "avail_ethernet": True},
]


async def test_dispatch_facility_networks_full_default_returns_complete_records():
    with patch("peeringdb_mcp.server.queries.get_facility_networks", new=AsyncMock(return_value=_NETFAC_RECORDS)):
        result = await _dispatch("get_facility_networks", {"fac_id": "1"}, "key")
    assert "avail_ethernet" in result
    assert "local_asn" in result


async def test_dispatch_facility_networks_presence_returns_net_id_name_asn():
    with patch("peeringdb_mcp.server.queries.get_facility_networks", new=AsyncMock(return_value=_NETFAC_RECORDS)):
        result = await _dispatch(
            "get_facility_networks", {"fac_id": "1", "detail": "presence"}, "key"
        )
    assert "Google LLC" in result
    assert "15169" in result
    assert "avail_ethernet" not in result
    assert "local_asn" not in result


async def test_dispatch_facility_networks_presence_handles_missing_net_dict():
    records = [{"net_id": 5, "local_asn": 99, "net": None}]
    with patch("peeringdb_mcp.server.queries.get_facility_networks", new=AsyncMock(return_value=records)):
        result = await _dispatch(
            "get_facility_networks", {"fac_id": "1", "detail": "presence"}, "key"
        )
    assert "net_id" in result


# ── _dispatch: find_common_exchanges detail ───────────────────────────────────

_COMMON_EX = [
    {
        "ix_id": 26, "ix_name": "AMS-IX",
        "ixfac_set": [{"fac": {"country": "NL"}}],
        "asn_a": 15169, "network_a_name": "Google LLC",
        "network_a_entries": [{"ipaddr4": "1.2.3.4", "speed": 10000}],
        "asn_b": 32934, "network_b_name": "Meta",
        "network_b_entries": [{"ipaddr4": "5.6.7.8", "speed": 10000}],
    }
]


async def test_dispatch_find_common_exchanges_routing_default_includes_entries():
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=_COMMON_EX)):
        result = await _dispatch(
            "find_common_exchanges", {"asn_a": "15169", "asn_b": "32934"}, "key"
        )
    assert "network_a_entries" in result
    assert "network_b_entries" in result
    assert "1.2.3.4" in result


async def test_dispatch_find_common_exchanges_presence_strips_entries():
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=_COMMON_EX)):
        result = await _dispatch(
            "find_common_exchanges",
            {"asn_a": "15169", "asn_b": "32934", "detail": "presence"},
            "key",
        )
    assert "network_a_entries" not in result
    assert "network_b_entries" not in result
    assert "1.2.3.4" not in result


async def test_dispatch_find_common_exchanges_presence_keeps_ix_level_info():
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=_COMMON_EX)):
        result = await _dispatch(
            "find_common_exchanges",
            {"asn_a": "15169", "asn_b": "32934", "detail": "presence"},
            "key",
        )
    assert "AMS-IX" in result
    assert "ix_scope" in result
    assert "Google LLC" in result   # network_a_name kept even in presence mode
    assert "Meta" in result


async def test_dispatch_find_common_exchanges_ixfac_set_always_stripped():
    with patch("peeringdb_mcp.server.queries.find_common_exchanges", new=AsyncMock(return_value=_COMMON_EX)):
        for detail in ("routing", "presence"):
            result = await _dispatch(
                "find_common_exchanges",
                {"asn_a": "15169", "asn_b": "32934", "detail": detail},
                "key",
            )
            assert "ixfac_set" not in result
