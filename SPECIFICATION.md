# PeeringDB MCP Server — Specification

## Purpose

Expose the [PeeringDB REST API](https://www.peeringdb.com/apidocs/) as a set of MCP tools,
allowing Claude (and other MCP clients) to answer peering and interconnection questions
using live PeeringDB data. Each user supplies their own PeeringDB API key as a tool
argument on every call; the server forwards it to PeeringDB and discards it immediately
after. The server has no authentication layer of its own.

---

## Architecture overview

```
Claude.ai / MCP client
        │
        │  HTTPS  (no MCP-level auth — open at MCP layer)
        ▼
nginx  (:443)
  /mcp/  → proxy to uvicorn :8002
           (optional: IP allowlist in nginx for network-level restriction)
        │
        ▼
peeringdb_mcp (uvicorn, 1 worker)
  Starlette router
    StreamableHTTPSessionManager  ← mcp.Server tools
        │
        │  HTTPS  (Api-Key <peeringdb_api_key from tool arguments>)
        ▼
https://www.peeringdb.com/api/
```

---

## Authentication design

There is no MCP-level authentication. No token file, no bearer token middleware, no OAuth
flow. The server is open at the MCP transport layer.

Access control is provided by two mechanisms:

1. **PeeringDB API key — per tool call.** Every tool requires a `peeringdb_api_key`
   argument. The server forwards it to PeeringDB as `Authorization: Api-Key <key>`. A
   missing or invalid key produces a clean error from the tool rather than from the MCP
   transport. The key is held in memory only for the duration of the request.

2. **nginx IP allowlist — optional network-level restriction.** If the server should only
   be reachable from known IPs (office, VPN, trusted hosts), add `allow`/`deny` directives
   to the nginx `/mcp/` location block. This is the appropriate place for access control,
   not the MCP layer.

### Why no MCP auth layer?

The MCP bearer token pattern (from the blueprint) exists to gate access to a server that
holds shared secrets or has something of its own to protect. This server holds nothing. It
is a stateless proxy to PeeringDB. Every request that reaches PeeringDB without a valid
API key is rejected by PeeringDB with a 401. Adding an MCP token layer would provide no
additional security while adding operational overhead (token management, OAuth flow, config
files on disk).

---

## PeeringDB API reference

Base URL: `https://www.peeringdb.com/api/`
Auth header: `Authorization: Api-Key <key>`
Response format: `{"meta": {...}, "data": [...]}`
Rate limit: ~1 request/second per key

### Core object types

| Tag | Endpoint | Description |
|-----|----------|-------------|
| `net` | `/api/net` | Networks (ASN, policy, NOC contact refs) |
| `ix` | `/api/ix` | Internet Exchanges |
| `ixlan` | `/api/ixlan` | IX LAN segments |
| `netixlan` | `/api/netixlan` | Network presence at an IX LAN (peering point) |
| `fac` | `/api/fac` | Facilities / datacentres |
| `org` | `/api/org` | Organisations |
| `poc` | `/api/poc` | Points of contact (auth required; respects visibility settings) |
| `netfac` | `/api/netfac` | Network presence at a facility |
| `ixfac` | `/api/ixfac` | IX presence at a facility |
| `carrier` | `/api/carrier` | Carriers |
| `carrierfac` | `/api/carrierfac` | Carrier presence at a facility |
| `campus` | `/api/campus` | Campus groupings of facilities |

### Query parameters (all GET list endpoints)

| Parameter | Type | Description |
|-----------|------|-------------|
| `limit` | int | Max records to return |
| `skip` | int | Records to skip (offset pagination) |
| `depth` | int | 0=no expansion (list default), 2=full expansion (single-record default) |
| `fields` | str | Comma-separated field names to return |
| `since` | int | Unix timestamp — return only records updated since |
| `<field>` | str/int | Exact match filter |
| `<field>__contains` | str | Substring filter |
| `<field>__startswith` | str | Prefix filter |
| `<field>__in` | str | Comma-separated values (OR match) |
| `<field>__lt/lte/gt/gte` | int | Numeric comparison |

### Key field notes

**net** (network):
- `asn` — AS number (integer)
- `name` — network name
- `policy_general` — `Open`, `Selective`, `Restrictive`, `No`
- `info_prefixes4`, `info_prefixes6` — announced prefix counts
- `netixlan_set` — peering points (expand with depth≥1)
- `netfac_set` — facility presences (expand with depth≥1)
- `status` — `ok`, `pending`, `deleted`

**ix** (internet exchange):
- `name`, `name_long` — short and full name
- `country` — ISO 3166-1 alpha-2
- `region_continent` — `Africa`, `Asia Pacific`, `Australia`, `Europe`, `Middle East`,
  `North America`, `South America`
- `net_count` — number of connected networks
- `ixlan_set` — LAN segments

**netixlan** (peering point):
- `net_id`, `ix_id`, `ixlan_id`
- `asn` — ASN of the network
- `ipaddr4`, `ipaddr6` — peering IPs
- `speed` — port speed in Mbps
- `is_rs_peer` — true if route-server peer

**fac** (facility):
- `name`, `city`, `country`, `region_continent`
- `net_count`, `ix_count`
- `org_id`

---

## Tool catalogue

### Universal parameter: `peeringdb_api_key`

Every tool includes this parameter in its `inputSchema` and it is always **required**:

```json
"peeringdb_api_key": {
  "type": "string",
  "description": "Your PeeringDB API key (from peeringdb.com/profile/). Used only for this request — never stored."
}
```

`call_tool()` extracts it before dispatch. If absent or empty, the tool returns immediately:
`{"error": "peeringdb_api_key is required", "tool": name}`

It is not documented individually under each tool below — assume it is always present and
required.

### Tool naming convention

`search_*` / `list_*` — return lists. `get_*` — return a single record. `find_*` — cross-object lookups.

---

### Network tools

#### `get_network_by_asn`

Look up a network by AS number. Returns the full network record including peering policy,
NOC info, and prefix counts.

Input:
```json
{
  "asn": { "type": "integer", "description": "AS number (without 'AS' prefix)" }
}
```
Required: `asn`

Implementation: `GET /api/net?asn=<asn>&depth=2`

Output key: `network`

---

#### `get_network`

Look up a network by its PeeringDB network ID.

Input:
```json
{
  "id":    { "type": "integer", "description": "PeeringDB network ID" },
  "depth": { "type": "integer", "description": "Expansion depth 0–2 (default 2)", "default": 2 }
}
```
Required: `id`

Implementation: `GET /api/net/<id>?depth=<depth>`

Output key: `network`

---

#### `search_networks`

Search for networks by name, policy, type, or country.

Input:
```json
{
  "name":           { "type": "string",  "description": "Partial network name (contains match)" },
  "policy_general": { "type": "string",  "description": "Open, Selective, Restrictive, No" },
  "info_type":      { "type": "string",  "description": "NSP, Content, Cable/DSL/ISP, Enterprise, Educational, Non-Profit, Route Server, Network Services, Route Collector, Government" },
  "country":        { "type": "string",  "description": "ISO 3166-1 alpha-2 country code" },
  "limit":          { "type": "integer", "description": "Max results (default 20, max 250)", "default": 20 },
  "skip":           { "type": "integer", "description": "Offset for pagination (default 0)", "default": 0 }
}
```
Required: at least one of `name`, `policy_general`, `info_type`, `country`

Implementation: `GET /api/net?<filters>&depth=0&limit=<limit>&skip=<skip>`

Output key: `networks`

---

#### `get_network_peering_points`

Return all IX peering points for a network identified by ASN. Each record includes
exchange name, peering IPs, port speed, and route-server flag.

Input:
```json
{
  "asn":   { "type": "integer", "description": "AS number" },
  "limit": { "type": "integer", "description": "Max results (default 100)", "default": 100 },
  "skip":  { "type": "integer", "description": "Offset (default 0)", "default": 0 }
}
```
Required: `asn`

Implementation: `GET /api/netixlan?asn=<asn>&depth=0&limit=<limit>&skip=<skip>`

Output key: `peering_points`

---

#### `get_network_facilities`

Return all facilities where a network is present, identified by ASN.

Input:
```json
{
  "asn":   { "type": "integer", "description": "AS number" },
  "limit": { "type": "integer", "description": "Max results (default 50)", "default": 50 }
}
```
Required: `asn`

Implementation: Resolve `net_id` via `GET /api/net?asn=<asn>&fields=id`, then
`GET /api/netfac?net_id=<net_id>&depth=1&limit=<limit>`

Output key: `facilities`

---

### Internet Exchange tools

#### `get_exchange`

Retrieve a single internet exchange by ID.

Input:
```json
{
  "id":    { "type": "integer", "description": "PeeringDB IX ID" },
  "depth": { "type": "integer", "description": "Expansion depth 0–2 (default 2)", "default": 2 }
}
```
Required: `id`

Implementation: `GET /api/ix/<id>?depth=<depth>`

Output key: `exchange`

---

#### `search_exchanges`

Search internet exchanges by name, country, or continent.

Input:
```json
{
  "name":             { "type": "string",  "description": "Partial IX name" },
  "country":          { "type": "string",  "description": "ISO 3166-1 alpha-2 country code" },
  "region_continent": { "type": "string",  "description": "Africa, Asia Pacific, Australia, Europe, Middle East, North America, South America" },
  "city":             { "type": "string",  "description": "City name" },
  "limit":            { "type": "integer", "description": "Max results (default 20)", "default": 20 },
  "skip":             { "type": "integer", "description": "Offset (default 0)", "default": 0 }
}
```
Required: at least one filter

Implementation: `GET /api/ix?<filters>&depth=0&limit=<limit>&skip=<skip>`

Output key: `exchanges`

---

#### `get_exchange_members`

Return all networks present at an internet exchange, with peering IPs and port speeds.

Input:
```json
{
  "ix_id": { "type": "integer", "description": "PeeringDB IX ID" },
  "limit": { "type": "integer", "description": "Max results (default 200)", "default": 200 },
  "skip":  { "type": "integer", "description": "Offset (default 0)", "default": 0 }
}
```
Required: `ix_id`

Implementation: `GET /api/netixlan?ix_id=<ix_id>&depth=0&limit=<limit>&skip=<skip>`

Output key: `members`

---

### Facility tools

#### `get_facility`

Retrieve a single facility by ID.

Input:
```json
{
  "id":    { "type": "integer", "description": "PeeringDB facility ID" },
  "depth": { "type": "integer", "description": "Expansion depth 0–2 (default 2)", "default": 2 }
}
```
Required: `id`

Implementation: `GET /api/fac/<id>?depth=<depth>`

Output key: `facility`

---

#### `search_facilities`

Search for colocation facilities by name, city, or country.

Input:
```json
{
  "name":    { "type": "string",  "description": "Partial facility name" },
  "city":    { "type": "string",  "description": "City name" },
  "country": { "type": "string",  "description": "ISO 3166-1 alpha-2 country code" },
  "limit":   { "type": "integer", "description": "Max results (default 20)", "default": 20 },
  "skip":    { "type": "integer", "description": "Offset (default 0)", "default": 0 }
}
```
Required: at least one filter

Implementation: `GET /api/fac?<filters>&depth=0&limit=<limit>&skip=<skip>`

Output key: `facilities`

---

#### `get_facility_networks`

List all networks present at a facility.

Input:
```json
{
  "fac_id": { "type": "integer", "description": "PeeringDB facility ID" },
  "limit":  { "type": "integer", "description": "Max results (default 100)", "default": 100 },
  "skip":   { "type": "integer", "description": "Offset (default 0)", "default": 0 }
}
```
Required: `fac_id`

Implementation: `GET /api/netfac?fac_id=<fac_id>&depth=1&limit=<limit>&skip=<skip>`

Output key: `networks`

---

#### `get_facility_exchanges`

List all internet exchanges present at a facility.

Input:
```json
{
  "fac_id": { "type": "integer", "description": "PeeringDB facility ID" },
  "limit":  { "type": "integer", "description": "Max results (default 50)", "default": 50 }
}
```
Required: `fac_id`

Implementation: `GET /api/ixfac?fac_id=<fac_id>&depth=1&limit=<limit>`

Output key: `exchanges`

---

### Cross-object / intelligence tools

#### `find_common_exchanges`

Find internet exchanges where two networks are both present. Useful for identifying
potential peering locations. Returns matched exchange records with both networks' peering
IPs and port speeds.

Input:
```json
{
  "asn_a": { "type": "integer", "description": "First AS number" },
  "asn_b": { "type": "integer", "description": "Second AS number" }
}
```
Required: `asn_a`, `asn_b`

Implementation:
1. `GET /api/netixlan?asn=<asn_a>&depth=0&limit=500` → set A of `ix_id` values
2. `GET /api/netixlan?asn=<asn_b>&depth=0&limit=500` → set B of `ix_id` values
3. Intersect `ix_id` sets
4. Fetch IX names: `GET /api/ix?id__in=<comma-separated-ix-ids>&fields=id,name`
5. For each common `ix_id`, return both networks' `netixlan` records with IX name

Output key: `common_exchanges`

---

#### `find_common_facilities`

Find facilities where two networks both have a presence.

Input:
```json
{
  "asn_a": { "type": "integer", "description": "First AS number" },
  "asn_b": { "type": "integer", "description": "Second AS number" }
}
```
Required: `asn_a`, `asn_b`

Implementation:
1. Resolve both ASNs to `net_id` values via `GET /api/net?asn=<asn>&fields=id`
2. `GET /api/netfac?net_id=<net_id_a>&depth=1` and `GET /api/netfac?net_id=<net_id_b>&depth=1`
3. Intersect on `fac_id`

Output key: `common_facilities`

---

#### `get_organisation`

Retrieve an organisation record by ID.

Input:
```json
{
  "id": { "type": "integer", "description": "PeeringDB organisation ID" }
}
```
Required: `id`

Implementation: `GET /api/org/<id>?depth=1`

Output key: `organisation`

---

#### `get_my_profile`

Return the authenticated user's PeeringDB profile, including their networks and permission
bitmasks. Useful for confirming a key is working and checking what networks the user manages.

Input: _(peeringdb_api_key only)_

Implementation: `GET https://auth.peeringdb.com/profile/v1` with
`Authorization: Api-Key <key>`. Note: this uses the auth subdomain, not the main API.

Fields of interest: `id`, `name`, `verified_user`, `verified_email`, `networks`
(array with `asn`, `name`, `perms` bitmask — bitmask is CRUD as low 4 bits).

Output key: `profile`

---

## TOML output conventions

```python
# Single record
{"network": {network_dict}}

# List of records
{"networks": [record, record, ...]}

# Empty list
{"networks": []}

# Error
{"error": "not found", "tool": "get_network"}

# Pagination hint (when result may be truncated)
{"networks": [...], "limit": 20, "skip": 0, "note": "Use skip to paginate"}
```

`None` values are stripped by `_clean()` before serialisation.

---

## Error handling

| PeeringDB HTTP status | Tool behaviour |
|-----------------------|----------------|
| 200 | Return `data[0]` (single) or `data` (list) |
| 401 | `{"error": "PeeringDB authentication failed — check your API key", "tool": name}` |
| 403 | `{"error": "PeeringDB API key lacks permission for this data", "tool": name}` |
| 404 | `{"error": "Record not found", "tool": name}` |
| 429 | `{"error": "PeeringDB rate limit exceeded — retry in 1 second", "tool": name}` |
| 5xx | `{"error": "PeeringDB server error: <status>", "tool": name}` |
| Network error | `{"error": "Could not reach PeeringDB: <exc>", "tool": name}` |

Never surface raw PeeringDB error bodies. Extract the useful part only.

---

## Rate limiting

PeeringDB enforces approximately 1 request/second per API key. `queries.py` uses a
module-level `asyncio.Semaphore(1)` with a 1-second `asyncio.sleep` inside it to serialise
any tool that makes more than one sequential PeeringDB request (e.g. `find_common_exchanges`
and `find_common_facilities`). Single-request tools do not need the semaphore.

---

## nginx deployment config

```nginx
# deploy/nginx-peeringdb-mcp.conf — include from your server block

upstream peeringdb_mcp {
    server 127.0.0.1:8002;
    keepalive 8;
}

# No OAuth location blocks — they are not needed

location = /mcp {
    return 308 https://$host/mcp/;
}

location /mcp/ {
    # Optional: restrict to known IPs
    # allow 203.0.113.10;
    # deny all;

    proxy_pass         http://peeringdb_mcp/;
    proxy_buffering    off;
    proxy_cache        off;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
    chunked_transfer_encoding on;
    proxy_http_version 1.1;
    proxy_set_header   Connection "";
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-Proto $scheme;
}
```

---

## systemd unit

```ini
# deploy/peeringdb-mcp.service
[Unit]
Description=PeeringDB MCP Server
After=network.target

[Service]
Type=simple
User=peeringdb-mcp
WorkingDirectory=/opt/peeringdb-mcp
ExecStart=/opt/peeringdb-mcp/venv/bin/uvicorn \
    peeringdb_mcp.server:create_app \
    --factory \
    --host 127.0.0.1 \
    --port 8002 \
    --workers 1
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=peeringdb-mcp

[Install]
WantedBy=multi-user.target
```

---

## Connecting from Claude.ai

**Add the MCP server:**
1. Settings → Model Context Protocol → Add remote server
2. URL: `https://<your-domain>/mcp/`
3. No custom headers or auth tokens needed at the MCP level

**Configure the PeeringDB API key:**

Add to your Claude project instructions or system prompt:

```
When using PeeringDB MCP tools, always pass your PeeringDB API key as the
peeringdb_api_key argument. PeeringDB API key: <your key here>
```

Claude will then include it automatically on every tool call.

---

## Out of scope

- **Write operations** (`POST`, `PUT`, `DELETE`) — read-only tools only.
- **Local replication / bulk sync** — use `peeringdb sync` for bulk analysis.
- **Unauthenticated PeeringDB access** — the server always requires `peeringdb_api_key`;
  unauthenticated PeeringDB calls return reduced data and tighter rate limits.
- **Multi-worker deployment** — `--workers 1` is required for SSE. Do not scale
  horizontally without a shared session store.
