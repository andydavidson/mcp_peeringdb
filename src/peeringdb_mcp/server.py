from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

import tomli_w
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from . import queries

log = logging.getLogger(__name__)


# ── Grounding constants ────────────────────────────────────────────────────────

_SERVER_INSTRUCTIONS = (
    "You are connected to a live PeeringDB data source. Follow these rules strictly:\n"
    "1. For ANY question about a network, AS number, internet exchange, or facility, "
    "call the appropriate tool first — never answer from training data, which is stale "
    "and often wrong (network names, operators, and peering data change constantly).\n"
    "2. Report ONLY the field values returned by the tool call. Do not add, infer, "
    "correct, or supplement any value using prior knowledge.\n"
    "3. If a field is absent from the tool result, say it is not recorded in PeeringDB "
    "— do not guess or fill it in from memory.\n"
    "4. ASN-to-operator mappings and network names change; always use the live tool "
    "result, never your training data."
)

_GROUNDING = (
    "Source: PeeringDB live API — authoritative real-time data. "
    "IMPORTANT: Report only the field values present in this result. "
    "Do not supplement, infer, or override any value from prior knowledge. "
    "If a field is absent, say it is not recorded — do not guess."
)

mcp = Server("peeringdb-mcp", instructions=_SERVER_INSTRUCTIONS)


# ── Serialisation ──────────────────────────────────────────────────────────────

def _clean(obj: Any) -> Any:
    if obj is None:
        return ""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_clean(item) for item in obj]
    return str(obj)


def _dump(data: dict) -> str:
    try:
        return tomli_w.dumps(_clean(data))
    except Exception as exc:
        return f'error = "TOML serialisation failed: {exc}"\n'


def _result(data: dict) -> str:
    return _dump({**data, "_data_policy": _GROUNDING})


def _desc(text: str) -> str:
    return (
        text
        + " Returns live data from PeeringDB."
        " Report only what this tool returns — do not use prior knowledge."
    )


# ── Exchange scope detection ───────────────────────────────────────────────────

def _ix_countries(ix_data: dict) -> list[str]:
    """Return sorted list of unique countries found in an IX's ixfac_set.

    At depth=2, each ixfac entry has a nested 'fac' dict with a 'country' field.
    Returns an empty list when ixfac_set is absent or contains no country data.
    """
    countries: set[str] = set()
    for ixfac in (ix_data.get("ixfac_set") or []):
        if not isinstance(ixfac, dict):
            continue
        fac = ixfac.get("fac") or {}
        country = (
            (fac.get("country") if isinstance(fac, dict) else None)
            or ixfac.get("country")
            or ""
        ).strip()
        if country:
            countries.add(country)
    return sorted(countries)


def _annotate_ix_scope(ix_data: dict) -> dict:
    """Add ix_scope, ix_countries_present, and scope_warning to an IX dict.

    Modifies the dict in place and returns it.
    ix_scope values:
      "local"              — all known facilities in one country (or only one found)
      "regional_dispersed" — facilities confirmed in more than one country
      "unknown"            — no facility country data available
    """
    countries = _ix_countries(ix_data)
    ix_data["ix_countries_present"] = countries

    if len(countries) > 1:
        ix_data["ix_scope"] = "regional_dispersed"
        ix_data["scope_warning"] = (
            f"DISPERSED EXCHANGE — NOT A LOCAL IXP: This exchange has infrastructure "
            f"in {len(countries)} countries ({', '.join(countries)}). Unlike a true "
            "internet exchange point, which connects networks in close proximity, "
            "this exchange spans multiple countries. Traffic between members may "
            "traverse long-haul inter-country links within the exchange fabric, "
            "delivering the latency characteristics of long-distance transit while "
            "appearing as local peering. Do not assume low-latency paths — verify "
            "that your peer's port is at the same physical facility as your own."
        )
    elif len(countries) == 1:
        ix_data["ix_scope"] = "local"
    else:
        ix_data["ix_scope"] = "unknown"

    return ix_data


# ── Batch projection helpers ───────────────────────────────────────────────────

def _project_netixlan(entry: dict, detail: str, ix_info: dict[int, dict]) -> dict:
    """Project a netixlan record to the fields appropriate for detail level.

    detail values:
      "presence" — ix identity + route-server flag only (default for batch)
      "routing"  — adds peering IPs, port speed, ixlan_id
      "ids_only" — just ix_id
    """
    ix_id = entry.get("ix_id")
    info = ix_info.get(ix_id, {}) if ix_id is not None else {}

    if detail == "ids_only":
        return {"ix_id": ix_id}

    base: dict = {
        "ix_id": ix_id,
        "ix_name": info.get("name", ""),
        "ix_city": info.get("city", ""),
        "ix_country": info.get("country", ""),
        "is_rs_peer": entry.get("is_rs_peer"),
    }
    if detail == "routing":
        base["ipaddr4"] = entry.get("ipaddr4")
        base["ipaddr6"] = entry.get("ipaddr6")
        base["speed"] = entry.get("speed")
        base["ixlan_id"] = entry.get("ixlan_id")
    return base


def _project_netfac(entry: dict, detail: str, fac_info: dict[int, dict]) -> dict:
    """Project a netfac record to the fields appropriate for detail level.

    detail values:
      "presence" / "routing" — facility identity (city/country); no routing
                               concept exists at the facility layer
      "ids_only"             — just fac_id
    """
    fac_id = entry.get("fac_id")
    if detail == "ids_only":
        return {"fac_id": fac_id}
    info = fac_info.get(fac_id, {}) if fac_id is not None else {}
    return {
        "fac_id": fac_id,
        "fac_name": info.get("name", ""),
        "city": info.get("city", ""),
        "country": info.get("country", ""),
    }


# ── Tool definitions ───────────────────────────────────────────────────────────

_API_KEY_PARAM = {
    "peeringdb_api_key": {
        "type": "string",
        "description": (
            "Your PeeringDB API key (from peeringdb.com/profile/). "
            "Used only for this request — never stored."
        ),
    }
}


@mcp.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Network tools ──────────────────────────────────────────────────────
        types.Tool(
            name="get_network_by_asn",
            description=_desc(
                "Look up a network by AS number. Returns the full network record "
                "including name, peering policy (policy_general), NOC contact info, "
                "info_prefixes4/6, netixlan_set (peering points), and netfac_set "
                "(facility presences). Use this to find a network's PeeringDB profile."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "asn": {
                        "type": "integer",
                        "description": "AS number (without 'AS' prefix, e.g. 15169 for Google)",
                    },
                },
                "required": ["peeringdb_api_key", "asn"],
            },
        ),
        types.Tool(
            name="get_network",
            description=_desc(
                "Look up a network by its PeeringDB network ID. Returns the full "
                "network record. Use get_network_by_asn instead if you have an ASN."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "id": {"type": "integer", "description": "PeeringDB network ID"},
                    "depth": {
                        "type": "integer",
                        "description": "Expansion depth 0–2 (default 2)",
                        "default": 2,
                    },
                },
                "required": ["peeringdb_api_key", "id"],
            },
        ),
        types.Tool(
            name="search_networks",
            description=_desc(
                "Search for networks by name, peering policy, network type, or country. "
                "Returns a list of network records (depth=0). "
                "policy_general values: Open, Selective, Restrictive, No. "
                "info_type values: NSP, Content, Cable/DSL/ISP, Enterprise, Educational, "
                "Non-Profit, Route Server, Network Services, Route Collector, Government."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "name": {
                        "type": "string",
                        "description": "Partial network name (contains match)",
                    },
                    "policy_general": {
                        "type": "string",
                        "description": "Open, Selective, Restrictive, or No",
                    },
                    "info_type": {
                        "type": "string",
                        "description": (
                            "NSP, Content, Cable/DSL/ISP, Enterprise, Educational, "
                            "Non-Profit, Route Server, Network Services, Route Collector, Government"
                        ),
                    },
                    "country": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code (e.g. US, DE, JP)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 250)",
                        "default": 20,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination (default 0)",
                        "default": 0,
                    },
                },
                "required": ["peeringdb_api_key"],
            },
        ),
        types.Tool(
            name="get_network_peering_points",
            description=_desc(
                "Return all IX peering points (netixlan records) for a network identified "
                "by ASN. Each record includes ix_id, ixlan_id, ipaddr4, ipaddr6, speed "
                "(Mbps), and is_rs_peer (route-server peer flag). "
                "Use this to find where a network peers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "asn": {"type": "integer", "description": "AS number"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 100)",
                        "default": 100,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["peeringdb_api_key", "asn"],
            },
        ),
        types.Tool(
            name="get_network_facilities",
            description=_desc(
                "Return all colocation facilities where a network is present, identified "
                "by ASN. Each record includes fac_id, facility name, city, and country."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "asn": {"type": "integer", "description": "AS number"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["peeringdb_api_key", "asn"],
            },
        ),
        # ── Internet Exchange tools ────────────────────────────────────────────
        types.Tool(
            name="get_exchange",
            description=_desc(
                "Retrieve a single internet exchange by PeeringDB IX ID. "
                "Returns name, name_long, country, region_continent, net_count, "
                "and ixlan_set (LAN segments with prefix info)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "id": {"type": "integer", "description": "PeeringDB IX ID"},
                    "depth": {
                        "type": "integer",
                        "description": "Expansion depth 0–2 (default 2)",
                        "default": 2,
                    },
                },
                "required": ["peeringdb_api_key", "id"],
            },
        ),
        types.Tool(
            name="search_exchanges",
            description=_desc(
                "Search internet exchanges by name, country, continent, or city. "
                "Returns a list of IX records. "
                "region_continent values: Africa, Asia Pacific, Australia, Europe, "
                "Middle East, North America, South America."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "name": {"type": "string", "description": "Partial IX name"},
                    "country": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code",
                    },
                    "region_continent": {
                        "type": "string",
                        "description": (
                            "Africa, Asia Pacific, Australia, Europe, "
                            "Middle East, North America, South America"
                        ),
                    },
                    "city": {"type": "string", "description": "City name"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["peeringdb_api_key"],
            },
        ),
        types.Tool(
            name="get_exchange_members",
            description=_desc(
                "Return all networks (netixlan records) present at an internet exchange. "
                "Use detail='presence' for a compact member list (ASN + name only) — "
                "recommended for large IXPs like AMS-IX or DE-CIX which have 900+ members. "
                "Use detail='routing' (default) for full records including peering IPs, "
                "port speed (Mbps), and is_rs_peer flag."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "ix_id": {"type": "integer", "description": "PeeringDB IX ID"},
                    "detail": {
                        "type": "string",
                        "enum": ["presence", "routing"],
                        "description": (
                            "routing (default): full record with IPs, speed, is_rs_peer. "
                            "presence: ASN and network name only — much lower token cost."
                        ),
                        "default": "routing",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 200)",
                        "default": 200,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["peeringdb_api_key", "ix_id"],
            },
        ),
        # ── Facility tools ─────────────────────────────────────────────────────
        types.Tool(
            name="get_facility",
            description=_desc(
                "Retrieve a single colocation facility by PeeringDB facility ID. "
                "Returns name, city, country, region_continent, net_count, ix_count, "
                "and org_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "id": {"type": "integer", "description": "PeeringDB facility ID"},
                    "depth": {
                        "type": "integer",
                        "description": "Expansion depth 0–2 (default 2)",
                        "default": 2,
                    },
                },
                "required": ["peeringdb_api_key", "id"],
            },
        ),
        types.Tool(
            name="search_facilities",
            description=_desc(
                "Search for colocation facilities by name, city, or country. "
                "Returns a list of facility records including name, city, country, "
                "net_count, and ix_count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "name": {
                        "type": "string",
                        "description": "Partial facility name (contains match)",
                    },
                    "city": {"type": "string", "description": "City name"},
                    "country": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["peeringdb_api_key"],
            },
        ),
        types.Tool(
            name="get_facility_networks",
            description=_desc(
                "List all networks present at a facility (netfac records). "
                "Use detail='presence' for a compact list (net_id, name, ASN only) — "
                "recommended for large facilities like Equinix NY which can have 500+ networks. "
                "Use detail='full' (default) for complete depth=1 records including local_asn "
                "and availability flags."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "fac_id": {"type": "integer", "description": "PeeringDB facility ID"},
                    "detail": {
                        "type": "string",
                        "enum": ["presence", "full"],
                        "description": (
                            "full (default): complete netfac record. "
                            "presence: net_id, network name, and ASN only — much lower token cost."
                        ),
                        "default": "full",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 100)",
                        "default": 100,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["peeringdb_api_key", "fac_id"],
            },
        ),
        types.Tool(
            name="get_facility_exchanges",
            description=_desc(
                "List all internet exchanges present at a facility (ixfac records). "
                "Each record includes ix_id and exchange name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "fac_id": {"type": "integer", "description": "PeeringDB facility ID"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["peeringdb_api_key", "fac_id"],
            },
        ),
        # ── Batch lookup tools ─────────────────────────────────────────────────
        types.Tool(
            name="get_networks_by_asn_batch",
            description=_desc(
                "Look up multiple networks by AS number in a single call. "
                "Returns one record per ASN with key scalar fields plus projected "
                "netixlan_set (IX peering points) and netfac_set (facility presences). "
                "Use detail='presence' (default) for IX name/city/country and RS flag — "
                "low token cost, good for comparing many networks. "
                "Use detail='routing' to also include peering IPs and port speed. "
                "Use detail='ids_only' to get just ix_id / fac_id for follow-up calls. "
                "Use network_fields to restrict which top-level scalar fields are returned "
                "(e.g. ['name','asn','policy_general','website']) — "
                "netixlan_set and netfac_set are always included regardless. "
                "Maximum 20 ASNs per call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "asns": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of AS numbers (max 20)",
                    },
                    "detail": {
                        "type": "string",
                        "enum": ["presence", "routing", "ids_only"],
                        "description": (
                            "presence (default): IX/facility name, city, country, RS flag. "
                            "routing: adds peering IPs and port speed. "
                            "ids_only: just ix_id / fac_id."
                        ),
                        "default": "presence",
                    },
                    "network_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Top-level scalar fields to return for each network record "
                            "(e.g. ['name','asn','policy_general','info_prefixes4','website']). "
                            "Omit to receive all scalar fields. "
                            "netixlan_set and netfac_set are always included."
                        ),
                    },
                },
                "required": ["peeringdb_api_key", "asns"],
            },
        ),
        types.Tool(
            name="get_exchanges_batch",
            description=_desc(
                "Look up multiple internet exchanges by PeeringDB IX ID in a single call. "
                "Returns one record per ID with scalar fields, ixlan_set (LAN prefix info), "
                "and ixfac_set projected to facility id/name/city/country. "
                "Scope annotation (ix_scope, ix_countries_present, scope_warning) is applied "
                "automatically — check scope_warning for dispersed multi-country exchanges. "
                "Maximum 20 IDs per call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of PeeringDB IX IDs (max 20)",
                    },
                },
                "required": ["peeringdb_api_key", "ids"],
            },
        ),
        types.Tool(
            name="get_facilities_batch",
            description=_desc(
                "Look up multiple colocation facilities by PeeringDB facility ID in a single call. "
                "Returns one record per ID with scalar fields (name, city, country, net_count, "
                "ix_count, org) and ixfac_set projected to IX id/name — showing which exchanges "
                "operate at each facility. The full network list is omitted (use "
                "get_facility_networks for that). Maximum 20 IDs per call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of PeeringDB facility IDs (max 20)",
                    },
                },
                "required": ["peeringdb_api_key", "ids"],
            },
        ),
        # ── Cross-object / intelligence tools ──────────────────────────────────
        types.Tool(
            name="find_common_exchanges",
            description=_desc(
                "Find internet exchanges where two networks are both present. "
                "Useful for identifying potential peering locations. "
                "Use detail='presence' to return only exchange-level info (name, country, "
                "scope annotation) without per-network port entries — lower token cost. "
                "Use detail='routing' (default) for full records including both networks' "
                "peering IPs, port speeds, and route-server peer status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "asn_a": {"type": "integer", "description": "First AS number"},
                    "asn_b": {"type": "integer", "description": "Second AS number"},
                    "detail": {
                        "type": "string",
                        "enum": ["presence", "routing"],
                        "description": (
                            "routing (default): includes each network's peering IPs, "
                            "port speed, and RS participation at every common exchange. "
                            "presence: exchange name, country, and scope only."
                        ),
                        "default": "routing",
                    },
                },
                "required": ["peeringdb_api_key", "asn_a", "asn_b"],
            },
        ),
        types.Tool(
            name="find_common_facilities",
            description=_desc(
                "Find colocation facilities where two networks both have a presence. "
                "Useful for identifying where two networks could establish cross-connects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "asn_a": {"type": "integer", "description": "First AS number"},
                    "asn_b": {"type": "integer", "description": "Second AS number"},
                },
                "required": ["peeringdb_api_key", "asn_a", "asn_b"],
            },
        ),
        types.Tool(
            name="get_organisation",
            description=_desc(
                "Retrieve an organisation record by PeeringDB org ID. "
                "Returns org name, website, and associated networks/exchanges."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "id": {"type": "integer", "description": "PeeringDB organisation ID"},
                },
                "required": ["peeringdb_api_key", "id"],
            },
        ),
        # ── IX Pricing tools ───────────────────────────────────────────────────
        types.Tool(
            name="search_ix_pricing",
            description=_desc(
                "Search and compare internet exchange port pricing from a crowd-sourced "
                "dataset (source: peering.exposed, maintained by Job Snijders et al.). "
                "All prices are in EUR/month; cost/Mbps values assume 85% or 40% port "
                "utilisation with NRC amortised over 3 years. "
                "Returns entries sorted by cost efficiency (cheapest first by default). "
                "Use this to find affordable IXPs, compare pricing across regions, or "
                "check whether a specific exchange has public pricing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "name": {
                        "type": "string",
                        "description": "Partial IXP name to search (case-insensitive)",
                    },
                    "location": {
                        "type": "string",
                        "description": (
                            "Partial location string to filter by (city, country, or region). "
                            "E.g. 'Amsterdam', 'Germany', 'United States'"
                        ),
                    },
                    "secure_route_servers_only": {
                        "type": "boolean",
                        "description": (
                            "If true, only return IXPs with IRR/RPKI-filtering route servers "
                            "(secure_route_servers = Yes)"
                        ),
                        "default": False,
                    },
                    "has_public_pricing": {
                        "type": "boolean",
                        "description": (
                            "If true, only return IXPs with publicly available pricing. "
                            "If false, only return those without public pricing."
                        ),
                    },
                    "max_price_100g": {
                        "type": "number",
                        "description": "Maximum 100GE port price in EUR/month",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": (
                            "Field to sort by. Options: cost_per_mbps_100g_85pct (default), "
                            "cost_per_mbps_100g_40pct, cost_per_mbps_10g_85pct, "
                            "cost_per_mbps_10g_40pct, price_100g_eur_month, "
                            "price_10g_eur_month, price_400g_eur_month, ixp, location"
                        ),
                        "default": "cost_per_mbps_100g_85pct",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default 50, max 158)",
                        "default": 50,
                    },
                },
                "required": ["peeringdb_api_key"],
            },
        ),
        types.Tool(
            name="get_my_profile",
            description=_desc(
                "Return the authenticated user's PeeringDB profile. "
                "Returns id, name, verified_user, verified_email, and networks array "
                "(each with asn, name, perms bitmask — low 4 bits are CRUD). "
                "Useful for confirming the API key is valid and checking managed networks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                },
                "required": ["peeringdb_api_key"],
            },
        ),
        # ── IXPDB real-time enrichment tools ───────────────────────────────────
        types.Tool(
            name="get_ix_enrichment",
            description=_desc(
                "Fetch real-time supplementary data for an internet exchange from IXPDB "
                "(api.ixpdb.net), keyed by its PeeringDB IX ID. "
                "Returns MANRS routing-security certification status (bool), "
                "looking glass URLs, traffic API URL, industry association membership, "
                "and IXPDB participant/location counts. "
                "Data is fetched live — not cached. "
                "Returns not-found if the IXP is not registered in IXPDB (~1100 of ~1900 "
                "PeeringDB IXPs have IXPDB coverage). "
                "MANRS certification means the IXP filters routes on its route servers "
                "using IRR and/or RPKI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "ix_id": {
                        "type": "integer",
                        "description": "PeeringDB IX ID (same as the 'id' field in get_exchange)",
                    },
                },
                "required": ["peeringdb_api_key", "ix_id"],
            },
        ),
        types.Tool(
            name="get_ix_traffic",
            description=_desc(
                "Fetch live aggregate traffic statistics for an internet exchange directly "
                "from its IXP Manager instance, via the traffic API URL registered in IXPDB. "
                "Data is fetched in real time — not cached. "
                "Returns current, average, peak, and total traffic (in bps or pps) for the "
                "chosen time period. "
                "Only works for IXPs that (a) have IXPDB coverage and (b) have registered "
                "a traffic API URL — approximately 96 IXPs globally. "
                "period: day | week | month | year. "
                "category: bits (bps) | pkts (pps)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "ix_id": {
                        "type": "integer",
                        "description": "PeeringDB IX ID",
                    },
                    "period": {
                        "type": "string",
                        "description": "Time period: day, week, month, or year (default: day)",
                        "enum": ["day", "week", "month", "year"],
                        "default": "day",
                    },
                    "category": {
                        "type": "string",
                        "description": "Metric: bits (bps) or pkts (packets/s) (default: bits)",
                        "enum": ["bits", "pkts"],
                        "default": "bits",
                    },
                },
                "required": ["peeringdb_api_key", "ix_id"],
            },
        ),
    ]


# ── Tool dispatch ──────────────────────────────────────────────────────────────

@mcp.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}
    api_key = args.get("peeringdb_api_key", "").strip()
    if not api_key:
        return [types.TextContent(
            type="text",
            text=_dump({"error": "peeringdb_api_key is required", "tool": name}),
        )]
    try:
        result = await _dispatch(name, args, api_key)
        return [types.TextContent(type="text", text=result)]
    except Exception as exc:
        log.error("Tool %s failed: %s", name, exc)
        return [types.TextContent(
            type="text",
            text=_dump({"error": str(exc), "tool": name}),
        )]


async def _dispatch(name: str, args: dict, api_key: str) -> str:

    if name == "get_network_by_asn":
        asn = int(args["asn"])
        result = await queries.get_network_by_asn(api_key, asn)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _result({"network": result})

    elif name == "get_network":
        id_ = int(args["id"])
        depth = int(args.get("depth", 2))
        result = await queries.get_network(api_key, id_, depth=depth)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _result({"network": result})

    elif name == "search_networks":
        limit = min(int(args.get("limit", 20)), 250)
        skip = int(args.get("skip", 0))
        rows = await queries.search_networks(
            api_key,
            name=args.get("name"),
            policy_general=args.get("policy_general"),
            info_type=args.get("info_type"),
            country=args.get("country"),
            limit=limit,
            skip=skip,
        )
        return _result({"networks": rows, "limit": limit, "skip": skip,
                        "note": "Use skip to paginate"})

    elif name == "get_network_peering_points":
        asn = int(args["asn"])
        limit = int(args.get("limit", 100))
        skip = int(args.get("skip", 0))
        rows = await queries.get_network_peering_points(api_key, asn, limit=limit, skip=skip)
        return _result({"peering_points": rows, "limit": limit, "skip": skip,
                        "note": "Use skip to paginate"})

    elif name == "get_network_facilities":
        asn = int(args["asn"])
        limit = int(args.get("limit", 50))
        rows = await queries.get_network_facilities(api_key, asn, limit=limit)
        return _result({"facilities": rows})

    elif name == "get_exchange":
        id_ = int(args["id"])
        depth = int(args.get("depth", 2))
        result = await queries.get_exchange(api_key, id_, depth=depth)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _result({"exchange": _annotate_ix_scope(result)})

    elif name == "search_exchanges":
        limit = int(args.get("limit", 20))
        skip = int(args.get("skip", 0))
        rows = await queries.search_exchanges(
            api_key,
            name=args.get("name"),
            country=args.get("country"),
            region_continent=args.get("region_continent"),
            city=args.get("city"),
            limit=limit,
            skip=skip,
        )
        return _result({"exchanges": rows, "limit": limit, "skip": skip,
                        "note": "Use skip to paginate"})

    elif name == "get_exchange_members":
        ix_id = int(args["ix_id"])
        limit = int(args.get("limit", 200))
        skip = int(args.get("skip", 0))
        detail = args.get("detail", "routing")
        rows = await queries.get_exchange_members(api_key, ix_id, limit=limit, skip=skip)
        if detail == "presence":
            rows = [
                {"asn": r.get("asn"), "name": r.get("name"), "net_id": r.get("net_id")}
                for r in rows
            ]
        return _result({"members": rows, "limit": limit, "skip": skip,
                        "note": "Use skip to paginate"})

    elif name == "get_facility":
        id_ = int(args["id"])
        depth = int(args.get("depth", 2))
        result = await queries.get_facility(api_key, id_, depth=depth)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _result({"facility": result})

    elif name == "search_facilities":
        limit = int(args.get("limit", 20))
        skip = int(args.get("skip", 0))
        rows = await queries.search_facilities(
            api_key,
            name=args.get("name"),
            city=args.get("city"),
            country=args.get("country"),
            limit=limit,
            skip=skip,
        )
        return _result({"facilities": rows, "limit": limit, "skip": skip,
                        "note": "Use skip to paginate"})

    elif name == "get_facility_networks":
        fac_id = int(args["fac_id"])
        limit = int(args.get("limit", 100))
        skip = int(args.get("skip", 0))
        detail = args.get("detail", "full")
        rows = await queries.get_facility_networks(api_key, fac_id, limit=limit, skip=skip)
        if detail == "presence":
            projected = []
            for r in rows:
                net = r.get("net") or {}
                projected.append({
                    "net_id": r.get("net_id"),
                    "name": net.get("name", "") if isinstance(net, dict) else "",
                    "asn": net.get("asn") if isinstance(net, dict) else r.get("local_asn"),
                })
            rows = projected
        return _result({"networks": rows, "limit": limit, "skip": skip,
                        "note": "Use skip to paginate"})

    elif name == "get_facility_exchanges":
        fac_id = int(args["fac_id"])
        limit = int(args.get("limit", 50))
        rows = await queries.get_facility_exchanges(api_key, fac_id, limit=limit)
        return _result({"exchanges": rows})

    elif name == "get_networks_by_asn_batch":
        asns = [int(a) for a in args["asns"]]
        if not asns:
            return _dump({"error": "asns list is empty", "tool": name})
        if len(asns) > 20:
            return _dump({"error": "Maximum 20 ASNs per batch request", "tool": name})
        detail = args.get("detail", "presence")
        network_fields = args.get("network_fields")
        networks, ix_info, fac_info = await queries.get_networks_by_asn_batch(
            api_key, asns, network_fields=network_fields
        )
        for net in networks:
            net["netixlan_set"] = [
                _project_netixlan(e, detail, ix_info)
                for e in net.get("netixlan_set", [])
            ]
            net["netfac_set"] = [
                _project_netfac(e, detail, fac_info)
                for e in net.get("netfac_set", [])
            ]
        return _result({
            "networks": networks,
            "requested_count": len(asns),
            "found_count": len(networks),
        })

    elif name == "get_exchanges_batch":
        ids = [int(i) for i in args["ids"]]
        if not ids:
            return _dump({"error": "ids list is empty", "tool": name})
        if len(ids) > 20:
            return _dump({"error": "Maximum 20 exchange IDs per batch request", "tool": name})
        exchanges = await queries.get_exchanges_batch(api_key, ids)
        for ex in exchanges:
            _annotate_ix_scope(ex)
            ex["ixfac_set"] = [
                {
                    "fac_id": f.get("fac_id"),
                    "fac_name": f.get("name", ""),
                    "city": f.get("city", ""),
                    "country": f.get("country", ""),
                }
                for f in ex.get("ixfac_set", [])
            ]
            for ixlan in ex.get("ixlan_set", []):
                ixlan.pop("netixlan_set", None)
        return _result({
            "exchanges": exchanges,
            "requested_count": len(ids),
            "found_count": len(exchanges),
        })

    elif name == "get_facilities_batch":
        ids = [int(i) for i in args["ids"]]
        if not ids:
            return _dump({"error": "ids list is empty", "tool": name})
        if len(ids) > 20:
            return _dump({"error": "Maximum 20 facility IDs per batch request", "tool": name})
        facilities = await queries.get_facilities_batch(api_key, ids)
        for fac in facilities:
            fac.pop("netfac_set", None)
            fac["ixfac_set"] = [
                {"ix_id": f.get("ix_id"), "ix_name": f.get("name", "")}
                for f in fac.get("ixfac_set", [])
            ]
        return _result({
            "facilities": facilities,
            "requested_count": len(ids),
            "found_count": len(facilities),
        })

    elif name == "find_common_exchanges":
        asn_a = int(args["asn_a"])
        asn_b = int(args["asn_b"])
        detail = args.get("detail", "routing")
        rows = await queries.find_common_exchanges(api_key, asn_a, asn_b)
        for row in rows:
            _annotate_ix_scope(row)
            row.pop("ixfac_set", None)  # strip raw facility data after annotation
            if detail == "presence":
                row.pop("network_a_entries", None)
                row.pop("network_b_entries", None)
        return _result({"common_exchanges": rows})

    elif name == "find_common_facilities":
        asn_a = int(args["asn_a"])
        asn_b = int(args["asn_b"])
        rows = await queries.find_common_facilities(api_key, asn_a, asn_b)
        return _result({"common_facilities": rows})

    elif name == "get_organisation":
        id_ = int(args["id"])
        result = await queries.get_organisation(api_key, id_)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _result({"organisation": result})

    elif name == "get_my_profile":
        result = await queries.get_my_profile(api_key)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _result({"profile": result})

    elif name == "search_ix_pricing":
        limit = min(int(args.get("limit", 50)), 158)
        rows = queries.search_ix_pricing(
            api_key,
            name=args.get("name"),
            location=args.get("location"),
            secure_route_servers_only=bool(args.get("secure_route_servers_only", False)),
            has_public_pricing=args.get("has_public_pricing"),
            max_price_100g=args.get("max_price_100g"),
            sort_by=args.get("sort_by", "cost_per_mbps_100g_85pct"),
            limit=limit,
        )
        return _result({
            "ix_pricing": rows,
            "count": len(rows),
            "source": "peering.exposed — Job Snijders et al. All prices EUR/month.",
            "note": (
                "cost_per_mbps values are cents/month/Mbps. "
                "85pct = port at 85% utilisation, 40pct = 40% utilisation. "
                "NRC amortised over 3 years."
            ),
        })

    elif name == "get_ix_enrichment":
        ix_id = int(args["ix_id"])
        result = await queries.get_ix_enrichment(api_key, ix_id)
        if result is None:
            return _dump({"error": "not found", "tool": name,
                          "note": "This IXP is not registered in IXPDB"})
        return _result({"ix_enrichment": result})

    elif name == "get_ix_traffic":
        ix_id = int(args["ix_id"])
        period = args.get("period", "day")
        category = args.get("category", "bits")
        result = await queries.get_ix_traffic(api_key, ix_id, period=period, category=category)
        return _result({"ix_traffic": result})

    return _dump({"error": f"Unknown tool: {name}", "tool": name})


# ── App factory ────────────────────────────────────────────────────────────────

def create_app() -> Any:
    session_manager = StreamableHTTPSessionManager(
        app=mcp,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and not scope.get("path"):
            scope = {**scope, "path": "/"}
        await session_manager.handle_request(scope, receive, send)

    return Starlette(
        routes=[Mount("/", app=handle_mcp)],
        lifespan=lifespan,
    )
