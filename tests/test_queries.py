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
    with patch("asyncio.sleep"):
        points = [{"ix_id": 1, "asn": 15169, "ipaddr4": "1.2.3.4"}]
        respx.get(f"{_API}/netixlan").mock(return_value=_ok(points))
        respx.get(f"{_API}/ix").mock(return_value=_ok([{"id": 1, "name": "AMS-IX"}]))
        result = await queries.get_network_peering_points(_KEY, 15169)
    assert len(result) == 1
    assert result[0]["ix_name"] == "AMS-IX"
    assert result[0]["ix_id"] == 1


@respx.mock
async def test_get_network_peering_points_empty():
    with patch("asyncio.sleep"):
        respx.get(f"{_API}/netixlan").mock(return_value=_ok([]))
        result = await queries.get_network_peering_points(_KEY, 15169)
    assert result == []


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
        netixlans_a = [{"ix_id": 26, "asn": 15169, "name": "Google LLC", "ipaddr4": "1.1.1.1"}]
        netixlans_b = [{"ix_id": 26, "asn": 32934, "name": "Meta", "ipaddr4": "2.2.2.2"}]
        ix_data = [{"id": 26, "name": "AMS-IX", "ixfac_set": []}]

        respx.get(f"{_API}/netixlan").mock(
            side_effect=[_ok(netixlans_a), _ok(netixlans_b)]
        )
        respx.get(f"{_API}/ix").mock(return_value=_ok(ix_data))

        result = await queries.find_common_exchanges(_KEY, 15169, 32934)

    assert len(result) == 1
    row = result[0]
    assert row["ix_id"] == 26
    assert row["ix_name"] == "AMS-IX"
    assert "ixfac_set" in row  # included for scope annotation
    assert row["asn_a"] == 15169
    assert row["network_a_name"] == "Google LLC"
    assert row["asn_b"] == 32934
    assert row["network_b_name"] == "Meta"


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
        netfac_a = {
            "fac_id": 99,
            "net_id": 10,
            "fac": {"name": "Equinix AM1", "city": "Amsterdam", "country": "NL"},
            "net": {"name": "Google LLC", "asn": 15169},
        }
        netfac_b = {
            "fac_id": 99,
            "net_id": 20,
            "fac": {"name": "Equinix AM1", "city": "Amsterdam", "country": "NL"},
            "net": {"name": "Meta", "asn": 32934},
        }
        respx.get(f"{_API}/netfac").mock(
            side_effect=[_ok([netfac_a]), _ok([netfac_b])]
        )
        result = await queries.find_common_facilities(_KEY, 15169, 32934)

    assert len(result) == 1
    row = result[0]
    assert row["fac_id"] == 99
    assert row["facility_name"] == "Equinix AM1"
    assert row["asn_a"] == 15169
    assert row["network_a_name"] == "Google LLC"
    assert row["asn_b"] == 32934
    assert row["network_b_name"] == "Meta"


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


# ── _traffic_json_url ──────────────────────────────────────────────────────────

def test_traffic_json_url_replaces_type_log():
    url = "https://ix.example.com/statistics?type=log&period=day"
    result = queries._traffic_json_url(url, "day", "bits")
    assert "type=json" in result
    assert "type=log" not in result


def test_traffic_json_url_sets_period():
    url = "https://ix.example.com/statistics?type=log"
    result = queries._traffic_json_url(url, "week", "bits")
    assert "period=week" in result


def test_traffic_json_url_sets_category():
    url = "https://ix.example.com/statistics?type=log"
    result = queries._traffic_json_url(url, "day", "pkts")
    assert "category=pkts" in result


def test_traffic_json_url_replaces_existing_period():
    url = "https://ix.example.com/statistics?type=log&period=month"
    result = queries._traffic_json_url(url, "week", "bits")
    assert "period=week" in result
    assert "period=month" not in result


def test_traffic_json_url_no_type_param():
    url = "https://ix.example.com/statistics"
    result = queries._traffic_json_url(url, "day", "bits")
    assert "type=json" in result


def test_traffic_json_url_preserves_host_and_path():
    url = "https://www.ams-ix.net/ams/statistics?type=log"
    result = queries._traffic_json_url(url, "day", "bits")
    assert result.startswith("https://www.ams-ix.net/ams/statistics")


# ── get_ix_enrichment ──────────────────────────────────────────────────────────

_IXPDB_LIST = "https://api.ixpdb.net/v1/provider/list"

_IXPDB_PROVIDERS = [
    {
        "id": 42,
        "pdb_id": 26,
        "name": "AMS-IX",
        "manrs": True,
        "looking_glass": [{"url": "https://lg.ams-ix.net"}],
        "apis": {"traffic": "https://www.ams-ix.net/ams/statistics?type=log"},
        "organization": {"association": "Euro-IX"},
        "participant_count": 950,
        "location_count": 5,
    },
    {
        "id": 7,
        "pdb_id": 99,
        "name": "SAMPLE-IX",
        "manrs": False,
        "looking_glass": [],
        "apis": {},
        "organization": {},
        "participant_count": 10,
        "location_count": 1,
    },
]


@respx.mock
async def test_get_ix_enrichment_found():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    result = await queries.get_ix_enrichment(_KEY, 26)
    assert result is not None
    assert result["ixpdb_id"] == 42
    assert result["pdb_id"] == 26
    assert result["name"] == "AMS-IX"
    assert result["manrs"] is True
    assert result["looking_glass_urls"] == ["https://lg.ams-ix.net"]
    assert result["traffic_api_url"] == "https://www.ams-ix.net/ams/statistics?type=log"
    assert result["association"] == "Euro-IX"
    assert result["participant_count"] == 950
    assert result["location_count"] == 5


@respx.mock
async def test_get_ix_enrichment_not_found():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    result = await queries.get_ix_enrichment(_KEY, 9999)
    assert result is None


@respx.mock
async def test_get_ix_enrichment_no_traffic_url():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    result = await queries.get_ix_enrichment(_KEY, 99)
    assert result is not None
    assert result["traffic_api_url"] is None
    assert result["looking_glass_urls"] == []


@respx.mock
async def test_get_ix_enrichment_looking_glass_string_array():
    # Some providers may return looking_glass as plain string URLs
    providers = [{
        "id": 1, "pdb_id": 5, "name": "TEST-IX", "manrs": False,
        "looking_glass": ["https://lg.test-ix.net"],
        "apis": {}, "organization": {},
    }]
    respx.get(_IXPDB_LIST).mock(return_value=httpx.Response(200, json=providers))
    result = await queries.get_ix_enrichment(_KEY, 5)
    assert result["looking_glass_urls"] == ["https://lg.test-ix.net"]


@respx.mock
async def test_get_ix_enrichment_ixpdb_server_error():
    respx.get(_IXPDB_LIST).mock(return_value=httpx.Response(500))
    with pytest.raises(ValueError, match="IXPDB returned HTTP 500"):
        await queries.get_ix_enrichment(_KEY, 26)


@respx.mock
async def test_get_ix_enrichment_network_error():
    respx.get(_IXPDB_LIST).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ValueError, match="Could not reach IXPDB"):
        await queries.get_ix_enrichment(_KEY, 26)


# ── get_ix_traffic ─────────────────────────────────────────────────────────────

_TRAFFIC_URL = "https://www.ams-ix.net/ams/statistics?type=log"
_TRAFFIC_JSON_URL = "https://www.ams-ix.net/ams/statistics?type=json&period=day&category=bits"

_TRAFFIC_RESPONSE = {
    "curin": 8_500_000_000_000,
    "curout": 8_200_000_000_000,
    "averagein": 7_000_000_000_000,
    "averageout": 6_800_000_000_000,
    "maxin": 10_200_000_000_000,
    "maxout": 9_800_000_000_000,
    "maxinat": "2025-01-15 14:00:00",
    "maxoutat": "2025-01-15 14:05:00",
    "totalin": 5_040_000_000_000_000,
    "totalout": 4_896_000_000_000_000,
}


@respx.mock
async def test_get_ix_traffic_success():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    # Match any URL that starts with the statistics path (params may vary in order)
    respx.get(url__startswith="https://www.ams-ix.net/ams/statistics").mock(
        return_value=httpx.Response(200, json=_TRAFFIC_RESPONSE)
    )
    result = await queries.get_ix_traffic(_KEY, 26, period="day", category="bits")
    assert result["ix_id"] == 26
    assert result["ixpdb_name"] == "AMS-IX"
    assert result["period"] == "day"
    assert result["category"] == "bits"
    assert result["current_in_bps"] == 8_500_000_000_000
    assert result["peak_in_bps"] == 10_200_000_000_000
    assert result["peak_in_at"] == "2025-01-15 14:00:00"


@respx.mock
async def test_get_ix_traffic_not_in_ixpdb():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    with pytest.raises(ValueError, match="not found in IXPDB"):
        await queries.get_ix_traffic(_KEY, 9999)


@respx.mock
async def test_get_ix_traffic_no_traffic_url():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    # pdb_id=99 has no traffic URL
    with pytest.raises(ValueError, match="No traffic API URL"):
        await queries.get_ix_traffic(_KEY, 99)


@respx.mock
async def test_get_ix_traffic_api_error():
    respx.get(_IXPDB_LIST).mock(
        return_value=httpx.Response(200, json=_IXPDB_PROVIDERS)
    )
    respx.get(url__startswith="https://www.ams-ix.net/ams/statistics").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(ValueError, match="Traffic API returned HTTP 503"):
        await queries.get_ix_traffic(_KEY, 26)
