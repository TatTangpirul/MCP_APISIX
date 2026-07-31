# APISIX Admin MCP Server

Read-only MCP tools for querying APISIX routes, upstreams, services, and
plugins across the Nila project's environments, without giving Claude Code
direct access to the APISIX admin API.

## How it works

- `apisix_service.py` — business logic. For each call, it lazily starts (and
  reuses) a `kubectl port-forward` to that environment's `*-apisix-admin`
  Service (port 9180), then makes an authenticated HTTP GET to the admin API.
  `test_route_matching` additionally port-forwards to the `*-apisix-gateway`
  Service (port 80, the data plane) to send one live, idempotent
  (GET/HEAD/OPTIONS-only) request and observe the real routing/plugin
  decision — the Admin API has no "would this match" endpoint, so this is
  the only way to test matching without guessing. `get_upstream_health`
  port-forwards to the apisix `Deployment` (not a Service — the Control API
  is bound to `127.0.0.1` inside the pod, which only a Deployment/pod-based
  port-forward can reach) to query `/v1/healthcheck` for live node status,
  since the Admin API only exposes the *configured* health-check policy, not
  observed health.
- `mcp_apisix.py` — wraps `apisix_service.py` functions as MCP tools using
  `FastMCP`, served over Streamable HTTP (same pattern as
  `openwebui_log/middleware/mcp_server.py`).

Only read (`GET`) Admin API operations, plus safe idempotent gateway probes
for route-matching tests, are implemented. Write operations to the Admin API
(create/update/delete route) are intentionally out of scope for this first
pass — see the agile card for the approval-gated write path to add next.

## Setup

```bash
cd MCP_Server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in APISIX_VIEWER_KEY
```

Requires:
- `kubectl` configured with a context that can reach the target cluster
  (`kubectl config current-context`)
- RBAC access to `get`/port-forward the `*-apisix-admin` and
  `*-apisix-gateway` Services, **and** the `*-apisix` Deployment (used for
  `get_upstream_health`'s Control API access), in the namespaces listed in
  `apisix_service.ENVIRONMENTS`
- `aws` CLI with an active SSO session (`aws sso login`) — used only to
  attribute audit log entries to your AWS identity, not for cluster access

## Run

```bash
source .env && ./run.sh
```

This starts:
- `mcp_apisix.py` on `:8002` (the actual MCP server)
- `mcpo` on `:8000` (OpenAPI proxy in front of it, for OpenWebUI/tool-calling
  clients that expect an OpenAPI tool server)

Don't run this at the same time as `openwebui_log/middleware/run.sh` without
changing one of the ports — both scripts currently default `mcpo` to `:8000`.

## Available tools

| Tool | Description |
|---|---|
| `list_environments()` | List queryable environment names |
| `list_routes(env)` | List all routes |
| `get_route(env, route_id)` | Get one route's full config |
| `list_upstreams(env)` | List all upstreams (LB/health-check config) |
| `get_upstream(env, upstream_id)` | Get one upstream's full config |
| `list_services(env)` | List all APISIX services |
| `list_plugins(env)` | List available/enabled plugins |
| `get_plugin_config(env, plugin_name)` | Get one plugin's schema/config |
| `test_route_matching(env, route_id, method="GET", extra_path="")` | Safely test whether a route matches (and which plugins fire) via one live idempotent gateway request — no config changes |
| `get_upstream_health(env, upstream_id)` | Live health-check status (per-node, from the Control API) for one upstream — `found: false` means it has no health-check policy configured, not an error |

`env` is one of: `rag-edge-dev`, `rag-edge-stg`, `seven-deli-dev`,
`seven-deli-stg`, `allchat-edge-stg` (see `apisix_service.ENVIRONMENTS`).

## Usage examples

These call the tools through the `mcpo` OpenAPI proxy (`:8000`, started by
`run.sh`) — the same way OpenWebUI or any HTTP tool-calling client would.
From Claude Code / another MCP client, call the equivalent tool directly
instead, e.g. `get_route(env="rag-edge-dev", route_id="c524ad0a")`.

**List routes, then inspect one:**
```bash
curl -s -X POST http://localhost:8000/list_routes \
  -H 'content-type: application/json' -d '{"env": "rag-edge-dev"}'

curl -s -X POST http://localhost:8000/get_route \
  -H 'content-type: application/json' \
  -d '{"env": "rag-edge-dev", "route_id": "c524ad0a"}'
```

**Test whether a route matches, without changing anything** — here, an
auth-protected route (`route_plugins` in the response confirms
`openid-connect` actually fired, `status: 302` shows it redirected to the
IdP login rather than letting the request through):
```bash
curl -s -X POST http://localhost:8000/test_route_matching \
  -H 'content-type: application/json' \
  -d '{"env": "rag-edge-dev", "route_id": "6abcc371", "method": "GET"}'
# -> {"ok": true, "matched": true, "status": 302,
#     "headers": {"Location": "https://.../oauth2/authorize?...", ...},
#     "route_plugins": ["limit-req", "openid-connect"], ...}
```
Probing a sub-path of a wildcard route with `extra_path`, and confirming a
proxy-rewrite route actually reached its upstream (a real 404 *from* the
backend, not APISIX's own route-not-found page):
```bash
curl -s -X POST http://localhost:8000/test_route_matching \
  -H 'content-type: application/json' \
  -d '{"env": "rag-edge-dev", "route_id": "a5c96322", "extra_path": "/ping"}'
# -> {"ok": true, "matched": true, "status": 404,
#     "body_preview": "{\"detail\":\"Not Found\"}", "route_plugins": ["proxy-rewrite"], ...}
```
Only `GET`/`HEAD`/`OPTIONS` are accepted — anything else is rejected before
any request is sent:
```bash
curl -s -X POST http://localhost:8000/test_route_matching \
  -H 'content-type: application/json' \
  -d '{"env": "rag-edge-dev", "route_id": "6abcc371", "method": "POST"}'
# -> {"ok": false, "error": "method 'POST' not allowed for live route tests, ..."}
```

**Check live upstream health:**
```bash
curl -s -X POST http://localhost:8000/get_upstream_health \
  -H 'content-type: application/json' \
  -d '{"env": "rag-edge-dev", "upstream_id": "9dff8669"}'
# -> {"ok": true, "found": false, "nodes": []}
# ("found": false here is correct, not a bug — this upstream has no
# `checks` policy configured at all, confirmed via get_upstream)
```

## Audit logging

Every Admin API, gateway, and Control API call is appended as one JSON line
to `audit.log` (path overridable via `AUDIT_LOG_PATH`), e.g.:
```json
{"ts": "2026-07-31T03:22:25Z", "actor": {"account": "971022779850", "arn": "arn:aws:sts::971022779850:assumed-role/AWSGSDevRole/you@example.com"}, "action": "admin_get", "env": "rag-edge-dev", "ok": true, "path": "/apisix/admin/routes", "status": 200}
```
The `actor` is resolved once per process via `aws sts get-caller-identity` —
since each developer runs this server locally under their own AWS SSO
session (the same credentials used for `kubectl`/EKS auth), this attributes
every call without any separate auth system. If the SSO session is expired
when the server starts, `actor` falls back to `{"account": "unknown", "arn":
"unknown", "error": "..."}` rather than failing the call — it retries on the
next call, so running `aws sso login` mid-session self-heals it. Logging
itself is best-effort: a write failure is swallowed and never breaks the
underlying tool call.

## Security note

Every environment currently uses APISIX's stock demo admin/viewer keys
(publicly known, from APISIX's own docs). This server only uses the
viewer key and only issues GET requests, but the underlying admin API
itself is not meaningfully protected — rotate the keys in each
environment's `config.yaml` ConfigMap independent of this tool.
