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

mcp = Server("peeringdb-mcp")


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
            description=(
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
            description=(
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
            description=(
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
            description=(
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
            description=(
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
            description=(
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
            description=(
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
            description=(
                "Return all networks (netixlan records) present at an internet exchange. "
                "Each record includes asn, net_id, ipaddr4, ipaddr6, speed (Mbps), "
                "and is_rs_peer. Useful for listing who peers at a given IX."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "ix_id": {"type": "integer", "description": "PeeringDB IX ID"},
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
            description=(
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
            description=(
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
            description=(
                "List all networks present at a facility (netfac records). "
                "Each record includes net_id, network name, ASN, and local_asn."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_API_KEY_PARAM,
                    "fac_id": {"type": "integer", "description": "PeeringDB facility ID"},
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
            description=(
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
        # ── Cross-object / intelligence tools ──────────────────────────────────
        types.Tool(
            name="find_common_exchanges",
            description=(
                "Find internet exchanges where two networks are both present. "
                "Useful for identifying potential peering locations. "
                "Returns a list of common exchanges, each with both networks' "
                "peering IPs, port speeds, and route-server peer status."
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
            name="find_common_facilities",
            description=(
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
            description=(
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
            description=(
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
            description=(
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
        return _dump({"network": result})

    elif name == "get_network":
        id_ = int(args["id"])
        depth = int(args.get("depth", 2))
        result = await queries.get_network(api_key, id_, depth=depth)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _dump({"network": result})

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
        return _dump({"networks": rows, "limit": limit, "skip": skip,
                      "note": "Use skip to paginate"})

    elif name == "get_network_peering_points":
        asn = int(args["asn"])
        limit = int(args.get("limit", 100))
        skip = int(args.get("skip", 0))
        rows = await queries.get_network_peering_points(api_key, asn, limit=limit, skip=skip)
        return _dump({"peering_points": rows, "limit": limit, "skip": skip,
                      "note": "Use skip to paginate"})

    elif name == "get_network_facilities":
        asn = int(args["asn"])
        limit = int(args.get("limit", 50))
        rows = await queries.get_network_facilities(api_key, asn, limit=limit)
        return _dump({"facilities": rows})

    elif name == "get_exchange":
        id_ = int(args["id"])
        depth = int(args.get("depth", 2))
        result = await queries.get_exchange(api_key, id_, depth=depth)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _dump({"exchange": _annotate_ix_scope(result)})

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
        return _dump({"exchanges": rows, "limit": limit, "skip": skip,
                      "note": "Use skip to paginate"})

    elif name == "get_exchange_members":
        ix_id = int(args["ix_id"])
        limit = int(args.get("limit", 200))
        skip = int(args.get("skip", 0))
        rows = await queries.get_exchange_members(api_key, ix_id, limit=limit, skip=skip)
        return _dump({"members": rows, "limit": limit, "skip": skip,
                      "note": "Use skip to paginate"})

    elif name == "get_facility":
        id_ = int(args["id"])
        depth = int(args.get("depth", 2))
        result = await queries.get_facility(api_key, id_, depth=depth)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _dump({"facility": result})

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
        return _dump({"facilities": rows, "limit": limit, "skip": skip,
                      "note": "Use skip to paginate"})

    elif name == "get_facility_networks":
        fac_id = int(args["fac_id"])
        limit = int(args.get("limit", 100))
        skip = int(args.get("skip", 0))
        rows = await queries.get_facility_networks(api_key, fac_id, limit=limit, skip=skip)
        return _dump({"networks": rows, "limit": limit, "skip": skip,
                      "note": "Use skip to paginate"})

    elif name == "get_facility_exchanges":
        fac_id = int(args["fac_id"])
        limit = int(args.get("limit", 50))
        rows = await queries.get_facility_exchanges(api_key, fac_id, limit=limit)
        return _dump({"exchanges": rows})

    elif name == "find_common_exchanges":
        asn_a = int(args["asn_a"])
        asn_b = int(args["asn_b"])
        rows = await queries.find_common_exchanges(api_key, asn_a, asn_b)
        for row in rows:
            _annotate_ix_scope(row)
            row.pop("ixfac_set", None)  # strip raw facility data after annotation
        return _dump({"common_exchanges": rows})

    elif name == "find_common_facilities":
        asn_a = int(args["asn_a"])
        asn_b = int(args["asn_b"])
        rows = await queries.find_common_facilities(api_key, asn_a, asn_b)
        return _dump({"common_facilities": rows})

    elif name == "get_organisation":
        id_ = int(args["id"])
        result = await queries.get_organisation(api_key, id_)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _dump({"organisation": result})

    elif name == "get_my_profile":
        result = await queries.get_my_profile(api_key)
        if result is None:
            return _dump({"error": "not found", "tool": name})
        return _dump({"profile": result})

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
        return _dump({
            "ix_pricing": rows,
            "count": len(rows),
            "source": "peering.exposed — Job Snijders et al. All prices EUR/month.",
            "note": (
                "cost_per_mbps values are cents/month/Mbps. "
                "85pct = port at 85% utilisation, 40pct = 40% utilisation. "
                "NRC amortised over 3 years."
            ),
        })

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
