from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from .pricing_data import search_ix_pricing as search_ix_pricing  # re-export

_BASE_URL = "https://www.peeringdb.com/api/"
_AUTH_PROFILE_URL = "https://auth.peeringdb.com/profile/v1"
_IXPDB_BASE = "https://api.ixpdb.net/v1"

# Semaphore used to pace multi-request tools to ~1 req/s per PeeringDB guidelines.
_RATE_LIMIT = asyncio.Semaphore(1)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Api-Key {api_key}"}


def _unwrap_single(data: dict | list | None) -> dict | None:
    """Normalise PeeringDB single-record responses.

    PeeringDB returns the data field as a plain dict for shallow depths but
    wraps it in a list at depth=2. Accept either form and always return a
    dict (or None when nothing was found).
    """
    if isinstance(data, list):
        return data[0] if data else None
    return data  # dict or None


def _check_status(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise ValueError("PeeringDB authentication failed — check your API key")
    if resp.status_code == 403:
        raise ValueError("PeeringDB API key lacks permission for this data")
    if resp.status_code == 429:
        raise ValueError("PeeringDB rate limit exceeded — retry in 1 second")
    if resp.status_code >= 500:
        raise ValueError(f"PeeringDB server error: {resp.status_code}")


# ── Network tools ──────────────────────────────────────────────────────────────

async def get_network_by_asn(api_key: str, asn: int) -> dict | None:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                "net",
                params={"asn": asn, "depth": 2},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    if resp.status_code == 404:
        return None
    _check_status(resp)
    data = resp.json().get("data", [])
    return data[0] if data else None


async def get_network(api_key: str, id: int, depth: int = 2) -> dict | None:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                f"net/{id}",
                params={"depth": depth},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    if resp.status_code == 404:
        return None
    _check_status(resp)
    return _unwrap_single(resp.json().get("data"))


async def search_networks(
    api_key: str,
    name: str | None = None,
    policy_general: str | None = None,
    info_type: str | None = None,
    country: str | None = None,
    limit: int = 20,
    skip: int = 0,
) -> list:
    params: dict = {"depth": 0, "limit": limit, "skip": skip}
    if name:
        params["name__contains"] = name
    if policy_general:
        params["policy_general"] = policy_general
    if info_type:
        params["info_type"] = info_type
    if country:
        params["country"] = country
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get("net", params=params, headers=_headers(api_key))
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    _check_status(resp)
    return resp.json().get("data", [])


async def get_network_peering_points(
    api_key: str, asn: int, limit: int = 100, skip: int = 0
) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        # Step 1: fetch netixlan records for this ASN
        async with _RATE_LIMIT:
            try:
                resp = await client.get(
                    "netixlan",
                    params={"asn": asn, "depth": 0, "limit": limit, "skip": skip},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp)
        records = resp.json().get("data", [])
        if not records:
            return records

        # Step 2: batch-fetch IX names so each record includes ix_name
        ix_ids = sorted({r["ix_id"] for r in records if "ix_id" in r})
        async with _RATE_LIMIT:
            try:
                resp_ix = await client.get(
                    "ix",
                    params={
                        "id__in": ",".join(str(i) for i in ix_ids),
                        "depth": 0,
                        "fields": "id,name",
                    },
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_ix)
        ix_by_id = {r["id"]: r.get("name", "") for r in resp_ix.json().get("data", [])}

    for r in records:
        r["ix_name"] = ix_by_id.get(r.get("ix_id"), "")
    return records


async def get_network_facilities(api_key: str, asn: int, limit: int = 50) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        # Step 1: resolve ASN → net_id
        async with _RATE_LIMIT:
            try:
                resp = await client.get(
                    "net",
                    params={"asn": asn, "fields": "id"},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp)
        nets = resp.json().get("data", [])
        if not nets:
            return []
        net_id = nets[0]["id"]

        # Step 2: fetch netfac records
        async with _RATE_LIMIT:
            try:
                resp2 = await client.get(
                    "netfac",
                    params={"net_id": net_id, "depth": 1, "limit": limit},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

    _check_status(resp2)
    return resp2.json().get("data", [])


# ── Internet Exchange tools ────────────────────────────────────────────────────

async def get_exchange(api_key: str, id: int, depth: int = 2) -> dict | None:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                f"ix/{id}",
                params={"depth": depth},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    if resp.status_code == 404:
        return None
    _check_status(resp)
    return _unwrap_single(resp.json().get("data"))


async def search_exchanges(
    api_key: str,
    name: str | None = None,
    country: str | None = None,
    region_continent: str | None = None,
    city: str | None = None,
    limit: int = 20,
    skip: int = 0,
) -> list:
    params: dict = {"depth": 0, "limit": limit, "skip": skip}
    if name:
        params["name__contains"] = name
    if country:
        params["country"] = country
    if region_continent:
        params["region_continent"] = region_continent
    if city:
        params["city"] = city
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get("ix", params=params, headers=_headers(api_key))
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    _check_status(resp)
    return resp.json().get("data", [])


async def get_exchange_members(
    api_key: str, ix_id: int, limit: int = 200, skip: int = 0
) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                "netixlan",
                params={"ix_id": ix_id, "depth": 0, "limit": limit, "skip": skip},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    _check_status(resp)
    return resp.json().get("data", [])


# ── Facility tools ─────────────────────────────────────────────────────────────

async def get_facility(api_key: str, id: int, depth: int = 2) -> dict | None:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                f"fac/{id}",
                params={"depth": depth},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    if resp.status_code == 404:
        return None
    _check_status(resp)
    return _unwrap_single(resp.json().get("data"))


async def search_facilities(
    api_key: str,
    name: str | None = None,
    city: str | None = None,
    country: str | None = None,
    limit: int = 20,
    skip: int = 0,
) -> list:
    params: dict = {"depth": 0, "limit": limit, "skip": skip}
    if name:
        params["name__contains"] = name
    if city:
        params["city"] = city
    if country:
        params["country"] = country
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get("fac", params=params, headers=_headers(api_key))
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    _check_status(resp)
    return resp.json().get("data", [])


async def get_facility_networks(
    api_key: str, fac_id: int, limit: int = 100, skip: int = 0
) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                "netfac",
                params={"fac_id": fac_id, "depth": 1, "limit": limit, "skip": skip},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    _check_status(resp)
    return resp.json().get("data", [])


async def get_facility_exchanges(
    api_key: str, fac_id: int, limit: int = 50
) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                "ixfac",
                params={"fac_id": fac_id, "depth": 1, "limit": limit},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    _check_status(resp)
    return resp.json().get("data", [])


# ── Cross-object / intelligence tools ─────────────────────────────────────────

async def find_common_exchanges(api_key: str, asn_a: int, asn_b: int) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        # Step 1: netixlan for ASN A
        async with _RATE_LIMIT:
            try:
                resp_a = await client.get(
                    "netixlan",
                    params={"asn": asn_a, "depth": 0, "limit": 500},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_a)
        netixlans_a = resp_a.json().get("data", [])

        # Step 2: netixlan for ASN B
        async with _RATE_LIMIT:
            try:
                resp_b = await client.get(
                    "netixlan",
                    params={"asn": asn_b, "depth": 0, "limit": 500},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_b)
        netixlans_b = resp_b.json().get("data", [])

        # Step 3: intersect ix_ids
        by_ix_a: dict[int, list] = {}
        for r in netixlans_a:
            by_ix_a.setdefault(r["ix_id"], []).append(r)

        by_ix_b: dict[int, list] = {}
        for r in netixlans_b:
            by_ix_b.setdefault(r["ix_id"], []).append(r)

        common_ix_ids = set(by_ix_a.keys()) & set(by_ix_b.keys())
        if not common_ix_ids:
            return []

        # Step 4: fetch IX data at depth=2 to get name and ixfac_set (for scope detection)
        async with _RATE_LIMIT:
            try:
                resp_ix = await client.get(
                    "ix",
                    params={
                        "id__in": ",".join(str(i) for i in sorted(common_ix_ids)),
                        "depth": 2,
                    },
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_ix)
        ix_by_id = {r["id"]: r for r in resp_ix.json().get("data", [])}

    # Step 5: build result; include ixfac_set so server.py can annotate scope.
    # Top-level asn_a/asn_b and name fields make it unambiguous which entries
    # belong to which network when the result is rendered as a table.
    result = []
    for ix_id in sorted(common_ix_ids):
        ix = ix_by_id.get(ix_id, {})
        entries_a = by_ix_a[ix_id]
        entries_b = by_ix_b[ix_id]
        result.append({
            "ix_id": ix_id,
            "ix_name": ix.get("name", ""),
            "ixfac_set": ix.get("ixfac_set", []),
            "asn_a": asn_a,
            "network_a_name": entries_a[0].get("name", "") if entries_a else "",
            "network_a_entries": entries_a,
            "asn_b": asn_b,
            "network_b_name": entries_b[0].get("name", "") if entries_b else "",
            "network_b_entries": entries_b,
        })
    return result


def _netfac_net_name(record: dict) -> str:
    """Extract network name from a netfac record (depth=1 includes nested net dict)."""
    net = record.get("net") or {}
    return net.get("name", "") if isinstance(net, dict) else ""


def _netfac_fac_name(record: dict) -> str:
    """Extract facility name from a netfac record (depth=1 includes nested fac dict)."""
    fac = record.get("fac") or {}
    return fac.get("name", "") if isinstance(fac, dict) else ""


async def find_common_facilities(api_key: str, asn_a: int, asn_b: int) -> list:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        # Step 1: resolve ASN A → net_id
        async with _RATE_LIMIT:
            try:
                resp_a = await client.get(
                    "net",
                    params={"asn": asn_a, "fields": "id"},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_a)
        nets_a = resp_a.json().get("data", [])
        if not nets_a:
            return []
        net_id_a = nets_a[0]["id"]

        # Step 2: resolve ASN B → net_id
        async with _RATE_LIMIT:
            try:
                resp_b = await client.get(
                    "net",
                    params={"asn": asn_b, "fields": "id"},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_b)
        nets_b = resp_b.json().get("data", [])
        if not nets_b:
            return []
        net_id_b = nets_b[0]["id"]

        # Step 3: netfac for network A
        async with _RATE_LIMIT:
            try:
                resp_fa = await client.get(
                    "netfac",
                    params={"net_id": net_id_a, "depth": 1},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_fa)
        netfacs_a = resp_fa.json().get("data", [])

        # Step 4: netfac for network B
        async with _RATE_LIMIT:
            try:
                resp_fb = await client.get(
                    "netfac",
                    params={"net_id": net_id_b, "depth": 1},
                    headers=_headers(api_key),
                )
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
            await asyncio.sleep(1)

        _check_status(resp_fb)
        netfacs_b = resp_fb.json().get("data", [])

    # Step 5: intersect on fac_id.
    # Top-level asn/name fields make it unambiguous which entry belongs to which
    # network when the result is rendered as a table.
    by_fac_a = {r["fac_id"]: r for r in netfacs_a}
    by_fac_b = {r["fac_id"]: r for r in netfacs_b}
    common_fac_ids = set(by_fac_a.keys()) & set(by_fac_b.keys())

    return [
        {
            "fac_id": fac_id,
            "facility_name": _netfac_fac_name(by_fac_a[fac_id]),
            "asn_a": asn_a,
            "network_a_name": _netfac_net_name(by_fac_a[fac_id]),
            "network_a_entry": by_fac_a[fac_id],
            "asn_b": asn_b,
            "network_b_name": _netfac_net_name(by_fac_b[fac_id]),
            "network_b_entry": by_fac_b[fac_id],
        }
        for fac_id in sorted(common_fac_ids)
    ]


async def get_organisation(api_key: str, id: int) -> dict | None:
    async with httpx.AsyncClient(base_url=_BASE_URL) as client:
        try:
            resp = await client.get(
                f"org/{id}",
                params={"depth": 1},
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    if resp.status_code == 404:
        return None
    _check_status(resp)
    return _unwrap_single(resp.json().get("data"))


async def get_my_profile(api_key: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                _AUTH_PROFILE_URL,
                headers=_headers(api_key),
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach PeeringDB: {exc}") from exc
    if resp.status_code == 404:
        return None
    _check_status(resp)
    return resp.json()


# ── IXPDB enrichment tools ─────────────────────────────────────────────────────

def _traffic_json_url(raw_url: str, period: str, category: str) -> str:
    """Transform an IXPDB traffic URL to request IXP Manager JSON aggregate stats.

    Replaces the type parameter with 'json' (IXP Manager's machine-readable
    output format) and sets period and category query parameters.
    """
    parsed = urlparse(raw_url.strip())
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["type"] = ["json"]
    params["period"] = [period]
    params["category"] = [category]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


async def get_ix_enrichment(api_key: str, ix_id: int) -> dict | None:
    """Fetch real-time IXPDB data for a PeeringDB exchange ID.

    Returns MANRS status, looking glass URLs, traffic API URL, and association
    membership. Returns None if the IXP is not registered in IXPDB.
    api_key is accepted for interface consistency but IXPDB is public.
    """
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(f"{_IXPDB_BASE}/provider/list")
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach IXPDB: {exc}") from exc
    if resp.status_code != 200:
        raise ValueError(f"IXPDB returned HTTP {resp.status_code}")

    provider = None
    for p in resp.json():
        if p.get("pdb_id") == ix_id:
            provider = p
            break

    if provider is None:
        return None

    # looking_glass may be a list of strings or a list of objects with a 'url' key
    raw_lg = provider.get("looking_glass") or []
    lg_urls: list[str] = []
    for lg in raw_lg:
        if isinstance(lg, str) and lg:
            lg_urls.append(lg)
        elif isinstance(lg, dict) and lg.get("url"):
            lg_urls.append(lg["url"])

    apis = provider.get("apis") or {}
    traffic_url = apis.get("traffic") or None

    org = provider.get("organization") or {}

    return {
        "ixpdb_id": provider.get("id"),
        "pdb_id": ix_id,
        "name": provider.get("name"),
        "manrs": provider.get("manrs", False),
        "looking_glass_urls": lg_urls,
        "traffic_api_url": traffic_url,
        "association": org.get("association"),
        "participant_count": provider.get("participant_count"),
        "location_count": provider.get("location_count"),
    }


async def get_ix_traffic(
    api_key: str,
    ix_id: int,
    period: str = "day",
    category: str = "bits",
) -> dict:
    """Fetch live aggregate traffic stats for an IXP from its IXP Manager instance.

    Step 1: Queries IXPDB in real time to discover the traffic API URL.
    Step 2: Transforms the URL to request JSON output for the requested period.
    Step 3: Fetches live stats from the IXP's own IXP Manager instance.

    period: day | week | month | year
    category: bits | pkts
    api_key is accepted for interface consistency but IXPDB is public.
    """
    enrichment = await get_ix_enrichment(api_key, ix_id)
    if enrichment is None:
        raise ValueError(f"IXP with PeeringDB ID {ix_id} not found in IXPDB")

    traffic_url = enrichment.get("traffic_api_url")
    if not traffic_url:
        raise ValueError(
            f"No traffic API URL registered in IXPDB for IXP {ix_id} "
            f"({enrichment.get('name', 'unknown')})"
        )

    json_url = _traffic_json_url(traffic_url, period, category)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(json_url)
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach traffic API: {exc}") from exc

    if resp.status_code != 200:
        raise ValueError(
            f"Traffic API returned HTTP {resp.status_code} for IXP {ix_id}"
        )

    raw = resp.json()
    # IXP Manager v5+ wraps stats under a "statistics" key; older versions return them flat.
    stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else raw

    return {
        "ix_id": ix_id,
        "ixpdb_name": enrichment.get("name"),
        "period": period,
        "category": category,
        "traffic_url": json_url,
        "current_in_bps": stats.get("curin"),
        "current_out_bps": stats.get("curout"),
        "average_in_bps": stats.get("averagein"),
        "average_out_bps": stats.get("averageout"),
        "peak_in_bps": stats.get("maxin"),
        "peak_out_bps": stats.get("maxout"),
        "peak_in_at": stats.get("maxinat"),
        "peak_out_at": stats.get("maxoutat"),
        "total_in_bits": stats.get("totalin"),
        "total_out_bits": stats.get("totalout"),
    }
