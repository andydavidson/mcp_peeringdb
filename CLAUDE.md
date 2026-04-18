# PeeringDB MCP Server — CLAUDE.md

## What this project is

An MCP server that exposes the PeeringDB REST API as conversational tools. The server has
**no authentication layer of its own** — there is no token file, no bearer token middleware,
and no OAuth flow. Access control is delegated entirely to PeeringDB: every tool call
requires a `peeringdb_api_key` argument, which is forwarded directly to PeeringDB and
discarded after the request. Anyone who connects without a valid PeeringDB key gets a 401
from PeeringDB on their first tool call.

If you need network-level access control, use an nginx `allow`/`deny` block. Do not
reintroduce an MCP token layer.

## Read this before writing any code

1. **Read `MCP_SERVER_BLUEPRINT.md`** in the project root. Follow its patterns for
   `server.py`, `__main__.py`, TOML serialisation, nginx, and systemd. **Ignore** the
   sections on `auth.py`, `oauth.py`, and `mcp_tokens.toml` — none of those are used here.

2. **Read `SPECIFICATION.md`** for the full tool list and data model.

## Project layout

```
src/
└── peeringdb_mcp/
    ├── __init__.py
    ├── __main__.py   # uvicorn entry point
    ├── server.py     # Tool list, _dispatch, _clean, _dump, create_app
    └── queries.py    # PeeringDB API client functions

deploy/
├── nginx-peeringdb-mcp.conf  # nginx location blocks + IP allowlist
└── peeringdb-mcp.service     # systemd unit
```

There is no `config/` directory, no `auth.py`, no `oauth.py`, and no token file of any
kind. Do not create them.

## Credential pass-through

The user's PeeringDB API key is supplied as a `peeringdb_api_key` parameter on **every
tool call**. `call_tool()` in `server.py` extracts it from `arguments` before dispatch and
passes it as the first positional argument to every function in `queries.py`. It is
discarded when the function returns.

In `call_tool()`:
1. Extract `peeringdb_api_key = args.get("peeringdb_api_key", "").strip()`
2. If empty, return immediately: `{"error": "peeringdb_api_key is required", "tool": name}`
3. Pass it as the first positional argument into `_dispatch` and on to `queries.py`
4. Never assign it to any module-level variable or cache

## queries.py rules

- Use `httpx.AsyncClient` with `base_url = "https://www.peeringdb.com/api/"`.
- Every function is `async def`.
- Every function accepts `api_key: str` as its **first positional argument**.
- Pass `headers={"Authorization": f"Api-Key {api_key}"}` on every request.
- List endpoints: `depth=0` default. Single-record endpoints: `depth=2` default.
- All list endpoints accept `limit: int` and `skip: int`.
- Return raw parsed JSON (`dict` or `list`) — serialisation to TOML happens in `server.py`.
- Return `None` on 404; do not raise. `_dispatch` turns `None` into `{"error": "not found"}`.
- Rate limit: ~1 req/s per PeeringDB key. Use a module-level `asyncio.Semaphore(1)` with a
  1-second sleep inside it if any tool issues more than one sequential PeeringDB request.
- **Never log `api_key`** — not at DEBUG, not in exception messages, not anywhere.

## server.py rules

- Use the blueprint's `_clean` and `_dump` functions verbatim.
- `create_app()` returns a plain `Starlette` application — **no** `BearerAuthMiddleware`
  wrapper. The `routes` list contains only the `Mount("/", app=handle_mcp)` entry plus the
  lifespan. There are no OAuth routes.
- Tool results are TOML strings in a `TextContent` block.
- Wrap list results: `{"networks": [...]}`. Wrap single records: `{"network": {...}}`.
- Error shape: `{"error": "...", "tool": name}`.
- `_dispatch` is a single `if / elif` function — no dynamic dispatch.
- Import `queries` at the top of `server.py`; do not import inside `call_tool`.

## __main__.py

Exactly as the blueprint. Port 8001. `factory=True`. `workers=1`.

## Dependencies

```toml
[project]
name = "peeringdb-mcp"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.2.0",
    "tomli-w>=1.0.0",
    "httpx>=0.27.0",
    "starlette>=0.40.0",
    "uvicorn>=0.29.0",
]
```

`tomllib` is stdlib since Python 3.11 — no extra dependency needed, but note there is no
TOML config file to read in this project anyway.

## nginx: IP allowlist instead of bearer tokens

Since there is no MCP-level auth, restrict access at the nginx layer if needed:

```nginx
location /mcp/ {
    allow 192.0.2.10;   # your office/home IP
    allow 10.0.0.0/8;   # internal network
    deny all;

    proxy_pass         http://peeringdb_mcp/;
    proxy_buffering    off;
    # ... rest of SSE settings as per blueprint
}
```

The OAuth and `/.well-known/` location blocks from the blueprint are **not** needed and
should not be added.

## Testing locally

```bash
# Run
python -m peeringdb_mcp

# Smoke test — server should respond (no 401, no auth required at MCP level)
curl http://localhost:8001/

# Test a tool call via mcp CLI or Claude Desktop
# Supply your PeeringDB API key as the peeringdb_api_key tool argument
```

## Common mistakes to avoid

See `MCP_SERVER_BLUEPRINT.md` §Common mistakes. In addition:

- **Do not add `BearerAuthMiddleware` or any auth middleware.** PeeringDB is the auth.
- **Do not create `config/mcp_tokens.toml` or any token file.** There are no MCP tokens.
- **Do not add OAuth routes.** There is nothing to issue tokens for.
- **Never log `api_key`.** Not at DEBUG, not in tracebacks. Log the URL and tool name only.
- **Never cache `api_key` at module level.** It must not outlive the request.
- **`depth=0` on list endpoints** — depth=2 responses can be very large. Default low.
- **PeeringDB rate limit ~1 req/s.** Use the semaphore guard for any multi-request tool.
- **Never use Basic Auth to PeeringDB.** Use `Authorization: Api-Key <key>` only.

## Deployment checklist

- [ ] `queries.py` written — `api_key: str` first arg on every function, never logged
- [ ] `server.py` complete — no auth middleware, `create_app` returns bare Starlette app
- [ ] `__main__.py` written
- [ ] `deploy/nginx-peeringdb-mcp.conf` written — SSE settings + IP allowlist if needed
- [ ] `deploy/peeringdb-mcp.service` written — `--workers 1`
- [ ] `systemctl enable --now peeringdb-mcp`
- [ ] Smoke test: `curl http://localhost:8001/` returns MCP response (not 401)
- [ ] Tool call succeeds with valid `peeringdb_api_key` in arguments
- [ ] Tool call returns clean error when `peeringdb_api_key` is omitted
- [ ] Tool call returns clean error when `peeringdb_api_key` is invalid (PeeringDB 401)
