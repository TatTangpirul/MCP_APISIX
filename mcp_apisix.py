"""
MCP server exposing read-only APISIX admin API tools to Claude Code / OpenWebUI
over Streamable HTTP.
Run with: python mcp_apisix.py
"""
from mcp.server.fastmcp import FastMCP

import apisix_service

mcp = FastMCP("apisix-admin-server", host="0.0.0.0", port=8002)


@mcp.tool()
def list_environments() -> list[str]:
    """List environment names this server can query (e.g. rag-edge-dev, rag-edge-stg)."""
    return apisix_service.list_environments()


@mcp.tool()
def list_routes(env: str) -> dict:
    """List all routes configured in APISIX for an environment."""
    return apisix_service.list_routes(env)


@mcp.tool()
def get_route(env: str, route_id: str) -> dict:
    """Get the full configuration of one route by id."""
    return apisix_service.get_route(env, route_id)


@mcp.tool()
def list_upstreams(env: str) -> dict:
    """List all upstreams and their load-balancing/health-check config for an environment."""
    return apisix_service.list_upstreams(env)


@mcp.tool()
def get_upstream(env: str, upstream_id: str) -> dict:
    """Get the full configuration of one upstream by id."""
    return apisix_service.get_upstream(env, upstream_id)


@mcp.tool()
def get_upstream_health(env: str, upstream_id: str) -> dict:
    """Get the health status of one upstream by id."""
    return apisix_service.get_upstream_health(env, upstream_id)


@mcp.tool()
def list_services(env: str) -> dict:
    """List all APISIX services (reusable route/upstream/plugin bundles) for an environment."""
    return apisix_service.list_services(env)


@mcp.tool()
def list_plugins(env: str) -> dict:
    """List all plugins available/enabled on this APISIX instance."""
    return apisix_service.list_plugins(env)


@mcp.tool()
def get_plugin_config(env: str, plugin_name: str) -> dict:
    """Get the schema/config for one named plugin."""
    return apisix_service.get_plugin_config(env, plugin_name)


@mcp.tool()
def test_route_matching(env: str, route_id: str, method: str = "GET", extra_path: str = "") -> dict:
    """
    Safely test whether a route matches (and which plugins fire) by sending
    one live GET/HEAD/OPTIONS request through the environment's gateway,
    without modifying any configuration. method is restricted to idempotent
    verbs; extra_path is appended to the route's base uri to probe a
    specific sub-path of a wildcard route.
    """
    return apisix_service.test_route_matching(env, route_id, method, extra_path)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
