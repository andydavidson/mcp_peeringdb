"""Tests for peeringdb_mcp.server — serialisation, call_tool, and _dispatch."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from peeringdb_mcp.server import _clean, _dispatch, _dump, call_tool
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


# ── create_app ─────────────────────────────────────────────────────────────────

def test_create_app_returns_starlette():
    from starlette.applications import Starlette
    from peeringdb_mcp.server import create_app
    app = create_app()
    assert isinstance(app, Starlette)
