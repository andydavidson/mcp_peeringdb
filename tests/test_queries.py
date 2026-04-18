"""Tests for peeringdb_mcp.queries — PeeringDB API client functions."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from peeringdb_mcp import queries

_API = "https://www.peeringdb.com/api"
_AUTH_URL = "https://auth.peeringdb.com/profile/v1"
_KEY = "testkey"
_AUTH_HEADER = {"Authorization": f"Api-Key {_KEY}"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ok(data) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def _single_ok(data) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def _single_ok_list(data) -> httpx.Response:
    """Simulate PeeringDB wrapping a single record in a list (seen at depth=2)."""
    return httpx.Response(200, json={"data": [data]})


# ── _unwrap_single ─────────────────────────────────────────────────────────────

def test_unwrap_single_dict():
    assert queries._unwrap_single({"id": 1}) == {"id": 1}


def test_unwrap_single_list_one_element():
    assert queries._unwrap_single([{"id": 1}]) == {"id": 1}


def test_unwrap_single_empty_list():
    assert queries._unwrap_single([]) is None


def test_unwrap_single_none():
    assert queries._unwrap_single(None) is None


# ── get_network_by_asn ─────────────────────────────────────────────────────────

@respx.mock
async def test_get_network_by_asn_found():
    net = {"id": 1, "name": "Google", "asn": 15169}
    respx.get(f"{_API}/net").mock(return_value=_ok([net]))
    result = await queries.get_network_by_asn(_KEY, 15169)
    assert result == net


@respx.mock
async def test_get_network_by_asn_not_found_empty_data():
    respx.get(f"{_API}/net").mock(return_value=_ok([]))
    result = await queries.get_network_by_asn(_KEY, 99999)
    assert result is None


@respx.mock
async def test_get_network_by_asn_404():
    respx.get(f"{_API}/net").mock(return_value=httpx.Response(404))
    result = await queries.get_network_by_asn(_KEY, 99999)
    assert result is None


@respx.mock
async def test_get_network_by_asn_401_raises():
    respx.get(f"{_API}/net").mock(return_value=httpx.Response(401))
    with pytest.raises(ValueError, match="authentication failed"):
        await queries.get_network_by_asn(_KEY, 15169)


@respx.mock
async def test_get_network_by_asn_429_raises():
    respx.get(f"{_API}/net").mock(return_value=httpx.Response(429))
    with pytest.raises(ValueError, match="rate limit"):
        await queries.get_network_by_asn(_KEY, 15169)


@respx.mock
async def test_get_network_by_asn_500_raises():
    respx.get(f"{_API}/net").mock(return_value=httpx.Response(500))
    with pytest.raises(ValueError, match="server error"):
        await queries.get_network_by_asn(_KEY, 15169)


async def test_get_network_by_asn_network_error_raises():
    with respx.mock:
        respx.get(f"{_API}/net").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ValueError, match="Could not reach PeeringDB"):
            await queries.get_network_by_asn(_KEY, 15169)


# ── get_network ────────────────────────────────────────────────────────────────

@respx.mock
async def test_get_network_found():
    net = {"id": 42, "name": "Cloudflare"}
    respx.get(f"{_API}/net/42").mock(return_value=_single_ok(net))
    result = await queries.get_network(_KEY, 42)
    assert result == net


@respx.mock
async def test_get_network_not_found():
    respx.get(f"{_API}/net/99").mock(return_value=httpx.Response(404))
    result = await queries.get_network(_KEY, 99)
    assert result is None


@respx.mock
async def test_get_network_depth2_list_wrapped():
    """PeeringDB wraps the record in a list at depth=2."""
    net = {"id": 42, "name": "Cloudflare"}
    respx.get(f"{_API}/net/42").mock(return_value=_single_ok_list(net))
    result = await queries.get_network(_KEY, 42)
    assert result == net


# ── search_networks ────────────────────────────────────────────────────────────

@respx.mock
async def test_search_networks_no_filters():
    nets = [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}]
    respx.get(f"{_API}/net").mock(return_value=_ok(nets))
    result = await queries.search_networks(_KEY)
    assert result == nets


@respx.mock
async def test_search_networks_with_name_filter():
    nets = [{"id": 1, "name": "Cloudflare"}]
    route = respx.get(f"{_API}/net").mock(return_value=_ok(nets))
    result = await queries.search_networks(_KEY, name="Cloudflare")
    assert result == nets
    assert "name__contains=Cloudflare" in str(route.calls[0].request.url)


@respx.mock
async def test_search_networks_with_policy_filter():
    route = respx.get(f"{_API}/net").mock(return_value=_ok([]))
    await queries.search_networks(_KEY, policy_general="Open")
    assert "policy_general=Open" in str(route.calls[0].request.url)


@respx.mock
async def test_search_networks_401_raises():
    respx.get(f"{_API}/net").mock(return_value=httpx.Response(401))
    with pytest.raises(ValueError, match="authentication failed"):
        await queries.search_networks(_KEY)


# ── get_network_peering_points ─────────────────────────────────────────────────

@respx.mock
async def test_get_network_peering_points():
    points = [{"ix_id": 1, "ipaddr4": "1.2.3.4"}]
    respx.get(f"{_API}/netixlan").mock(return_value=_ok(points))
    result = await queries.get_network_peering_points(_KEY, 15169)
    assert result == points


# ── get_network_facilities ─────────────────────────────────────────────────────

@respx.mock
async def test_get_network_facilities_found():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/net").mock(return_value=_ok([{"id": 7}]))
        respx.get(f"{_API}/netfac").mock(return_value=_ok([{"fac_id": 10, "name": "Equinix"}]))
        result = await queries.get_network_facilities(_KEY, 15169)
    assert result == [{"fac_id": 10, "name": "Equinix"}]


@respx.mock
async def test_get_network_facilities_no_network():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/net").mock(return_value=_ok([]))
        result = await queries.get_network_facilities(_KEY, 99999)
    assert result == []


# ── get_exchange ───────────────────────────────────────────────────────────────

@respx.mock
async def test_get_exchange_found():
    ix = {"id": 26, "name": "AMS-IX"}
    respx.get(f"{_API}/ix/26").mock(return_value=_single_ok(ix))
    result = await queries.get_exchange(_KEY, 26)
    assert result == ix


@respx.mock
async def test_get_exchange_not_found():
    respx.get(f"{_API}/ix/9999").mock(return_value=httpx.Response(404))
    result = await queries.get_exchange(_KEY, 9999)
    assert result is None


@respx.mock
async def test_get_exchange_depth2_list_wrapped():
    """PeeringDB wraps the record in a list at depth=2 — must still return a dict."""
    ix = {"id": 26, "name": "AMS-IX", "ixfac_set": [{"fac": {"country": "NL"}}]}
    respx.get(f"{_API}/ix/26").mock(return_value=_single_ok_list(ix))
    result = await queries.get_exchange(_KEY, 26)
    assert result == ix


# ── search_exchanges ───────────────────────────────────────────────────────────

@respx.mock
async def test_search_exchanges_no_filters():
    ixs = [{"id": 1, "name": "AMS-IX"}]
    respx.get(f"{_API}/ix").mock(return_value=_ok(ixs))
    result = await queries.search_exchanges(_KEY)
    assert result == ixs


@respx.mock
async def test_search_exchanges_with_country():
    route = respx.get(f"{_API}/ix").mock(return_value=_ok([]))
    await queries.search_exchanges(_KEY, country="NL")
    assert "country=NL" in str(route.calls[0].request.url)


@respx.mock
async def test_search_exchanges_with_city():
    route = respx.get(f"{_API}/ix").mock(return_value=_ok([]))
    await queries.search_exchanges(_KEY, city="Amsterdam")
    assert "city=Amsterdam" in str(route.calls[0].request.url)


# ── get_exchange_members ───────────────────────────────────────────────────────

@respx.mock
async def test_get_exchange_members():
    members = [{"asn": 15169}, {"asn": 32934}]
    respx.get(f"{_API}/netixlan").mock(return_value=_ok(members))
    result = await queries.get_exchange_members(_KEY, ix_id=26)
    assert result == members


# ── get_facility ───────────────────────────────────────────────────────────────

@respx.mock
async def test_get_facility_found():
    fac = {"id": 1, "name": "Equinix AM1"}
    respx.get(f"{_API}/fac/1").mock(return_value=_single_ok(fac))
    result = await queries.get_facility(_KEY, 1)
    assert result == fac


@respx.mock
async def test_get_facility_not_found():
    respx.get(f"{_API}/fac/9999").mock(return_value=httpx.Response(404))
    result = await queries.get_facility(_KEY, 9999)
    assert result is None


@respx.mock
async def test_get_facility_depth2_list_wrapped():
    fac = {"id": 1, "name": "Equinix AM1"}
    respx.get(f"{_API}/fac/1").mock(return_value=_single_ok_list(fac))
    result = await queries.get_facility(_KEY, 1)
    assert result == fac


# ── search_facilities ──────────────────────────────────────────────────────────

@respx.mock
async def test_search_facilities_no_filters():
    facs = [{"id": 1, "name": "Equinix AM1"}]
    respx.get(f"{_API}/fac").mock(return_value=_ok(facs))
    result = await queries.search_facilities(_KEY)
    assert result == facs


@respx.mock
async def test_search_facilities_with_city():
    route = respx.get(f"{_API}/fac").mock(return_value=_ok([]))
    await queries.search_facilities(_KEY, city="Amsterdam")
    assert "city=Amsterdam" in str(route.calls[0].request.url)


# ── get_facility_networks ──────────────────────────────────────────────────────

@respx.mock
async def test_get_facility_networks():
    nets = [{"net_id": 1, "name": "Cloudflare"}]
    respx.get(f"{_API}/netfac").mock(return_value=_ok(nets))
    result = await queries.get_facility_networks(_KEY, fac_id=1)
    assert result == nets


# ── get_facility_exchanges ─────────────────────────────────────────────────────

@respx.mock
async def test_get_facility_exchanges():
    ixs = [{"ix_id": 26, "name": "AMS-IX"}]
    respx.get(f"{_API}/ixfac").mock(return_value=_ok(ixs))
    result = await queries.get_facility_exchanges(_KEY, fac_id=1)
    assert result == ixs


# ── find_common_exchanges ──────────────────────────────────────────────────────

@respx.mock
async def test_find_common_exchanges_found():
    with patch("asyncio.sleep"):
        netixlans_a = [{"ix_id": 26, "asn": 15169, "ipaddr4": "1.1.1.1"}]
        netixlans_b = [{"ix_id": 26, "asn": 32934, "ipaddr4": "2.2.2.2"}]
        ix_data = [{"id": 26, "name": "AMS-IX", "ixfac_set": []}]

        respx.get(f"{_API}/netixlan").mock(
            side_effect=[_ok(netixlans_a), _ok(netixlans_b)]
        )
        respx.get(f"{_API}/ix").mock(return_value=_ok(ix_data))

        result = await queries.find_common_exchanges(_KEY, 15169, 32934)

    assert len(result) == 1
    assert result[0]["ix_id"] == 26
    assert result[0]["ix_name"] == "AMS-IX"
    assert "ixfac_set" in result[0]  # included for scope annotation


@respx.mock
async def test_find_common_exchanges_ix_fetched_at_depth2():
    """Step 4 must use depth=2 so ixfac_set country data is available."""
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/netixlan").mock(
            side_effect=[
                _ok([{"ix_id": 26, "asn": 15169}]),
                _ok([{"ix_id": 26, "asn": 32934}]),
            ]
        )
        route = respx.get(f"{_API}/ix").mock(
            return_value=_ok([{"id": 26, "name": "AMS-IX", "ixfac_set": []}])
        )
        await queries.find_common_exchanges(_KEY, 15169, 32934)

    assert "depth=2" in str(route.calls[0].request.url)


@respx.mock
async def test_find_common_exchanges_no_common():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/netixlan").mock(
            side_effect=[
                _ok([{"ix_id": 1, "asn": 15169}]),
                _ok([{"ix_id": 2, "asn": 32934}]),
            ]
        )
        result = await queries.find_common_exchanges(_KEY, 15169, 32934)

    assert result == []


# ── find_common_facilities ─────────────────────────────────────────────────────

@respx.mock
async def test_find_common_facilities_found():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/net").mock(
            side_effect=[_ok([{"id": 10}]), _ok([{"id": 20}])]
        )
        respx.get(f"{_API}/netfac").mock(
            side_effect=[
                _ok([{"fac_id": 99, "name": "Equinix AM1"}]),
                _ok([{"fac_id": 99, "name": "Equinix AM1"}]),
            ]
        )
        result = await queries.find_common_facilities(_KEY, 15169, 32934)

    assert len(result) == 1
    assert result[0]["fac_id"] == 99


@respx.mock
async def test_find_common_facilities_no_network_a():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/net").mock(return_value=_ok([]))
        result = await queries.find_common_facilities(_KEY, 99999, 32934)
    assert result == []


@respx.mock
async def test_find_common_facilities_no_network_b():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/net").mock(
            side_effect=[_ok([{"id": 10}]), _ok([])]
        )
        result = await queries.find_common_facilities(_KEY, 15169, 99999)
    assert result == []


# ── get_organisation ───────────────────────────────────────────────────────────

@respx.mock
async def test_get_organisation_found():
    org = {"id": 1, "name": "Google LLC"}
    respx.get(f"{_API}/org/1").mock(return_value=_single_ok(org))
    result = await queries.get_organisation(_KEY, 1)
    assert result == org


@respx.mock
async def test_get_organisation_not_found():
    respx.get(f"{_API}/org/9999").mock(return_value=httpx.Response(404))
    result = await queries.get_organisation(_KEY, 9999)
    assert result is None


@respx.mock
async def test_get_organisation_depth2_list_wrapped():
    org = {"id": 1, "name": "Google LLC"}
    respx.get(f"{_API}/org/1").mock(return_value=_single_ok_list(org))
    result = await queries.get_organisation(_KEY, 1)
    assert result == org


# ── get_my_profile ─────────────────────────────────────────────────────────────

@respx.mock
async def test_get_my_profile_ok():
    profile = {"id": 1, "name": "Alice", "verified_user": True}
    respx.get(_AUTH_URL).mock(return_value=httpx.Response(200, json=profile))
    result = await queries.get_my_profile(_KEY)
    assert result == profile


@respx.mock
async def test_get_my_profile_403_raises():
    respx.get(_AUTH_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(ValueError, match="lacks permission"):
        await queries.get_my_profile(_KEY)


# ── Authorization header ───────────────────────────────────────────────────────

@respx.mock
async def test_auth_header_sent():
    route = respx.get(f"{_API}/net").mock(return_value=_ok([]))
    await queries.search_networks(_KEY)
    assert route.calls[0].request.headers["authorization"] == f"Api-Key {_KEY}"
