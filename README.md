# peeringdb-mcp

An MCP server that exposes the [PeeringDB REST API](https://www.peeringdb.com/apidocs/) as conversational tools, allowing Claude (and other MCP clients) to answer peering and interconnection questions using live PeeringDB data.

The server has no authentication layer of its own. Every tool call requires a `peeringdb_api_key` argument, which is forwarded directly to PeeringDB and discarded after the request. Network-level access control can be added via an nginx IP allowlist.

## Tools

| Tool | Description |
|------|-------------|
| `get_network_by_asn` | Look up a network by AS number |
| `get_network` | Look up a network by PeeringDB network ID |
| `search_networks` | Search networks by name, policy, type, or country |
| `get_network_peering_points` | List all IX peering points for a network (by ASN) |
| `get_network_facilities` | List all facilities where a network is present (by ASN) |
| `get_exchange` | Retrieve an internet exchange by ID |
| `search_exchanges` | Search exchanges by name, country, or continent |
| `get_exchange_members` | List all networks present at an exchange |
| `get_facility` | Retrieve a colocation facility by ID |
| `search_facilities` | Search facilities by name, city, or country |
| `get_facility_networks` | List all networks present at a facility |
| `get_facility_exchanges` | List all exchanges present at a facility |
| `find_common_exchanges` | Find exchanges where two networks are both present |
| `find_common_facilities` | Find facilities where two networks both have a presence |
| `get_organisation` | Retrieve an organisation record by ID |
| `get_my_profile` | Return the authenticated user's PeeringDB profile |

## Requirements

- Python 3.11+
- A [PeeringDB API key](https://www.peeringdb.com/profile/) (supplied per tool call — not configured on the server)

## Installation

```bash
git clone <repo-url>
cd peeringdb-mcp
pip install -e .
```

Or with a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Running

```bash
python -m peeringdb_mcp
```

The server listens on `http://127.0.0.1:8002` by default.

### Smoke test

```bash
curl http://localhost:8002/
```

## Connecting to Claude

### Claude Desktop / Claude.ai

1. Settings → Model Context Protocol → Add remote server
2. URL: `http://127.0.0.1:8002/` (local) or `https://<your-domain>/mcp/` (production)
3. No custom headers or auth tokens needed at the MCP level

Then add your PeeringDB API key to your Claude project instructions:

```
When using PeeringDB MCP tools, always pass peeringdb_api_key: <your-key-here>
```

Claude will include it automatically on every tool call.

### Claude API

```python
import anthropic

client = anthropic.Anthropic()
response = client.beta.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    tools=[{
        "type": "mcp",
        "server": {
            "type": "url",
            "url": "http://127.0.0.1:8002/",
        },
    }],
    messages=[{
        "role": "user",
        "content": "What exchanges does AS15169 peer at? peeringdb_api_key: <your-key>",
    }],
    betas=["mcp-client-2025-04-04"],
)
```

## Production deployment

### nginx

Copy `deploy/nginx-peeringdb-mcp.conf` into your nginx server block. It configures the `/mcp/` location with correct SSE settings (buffering off, long timeouts). Uncomment the `allow`/`deny` directives to restrict access by IP.

### systemd

```bash
# Copy the unit file
sudo cp deploy/peeringdb-mcp.service /etc/systemd/system/

# Install the package into /opt/peeringdb-mcp/venv
sudo python -m venv /opt/peeringdb-mcp/venv
sudo /opt/peeringdb-mcp/venv/bin/pip install -e .

# Enable and start
sudo systemctl enable --now peeringdb-mcp
sudo journalctl -u peeringdb-mcp -f
```

## Project layout

```
src/peeringdb_mcp/
├── __init__.py
├── __main__.py   # uvicorn entry point (port 8002)
├── server.py     # MCP tool definitions, dispatch, app factory
└── queries.py    # async PeeringDB API client functions

deploy/
├── nginx-peeringdb-mcp.conf
└── peeringdb-mcp.service
```
