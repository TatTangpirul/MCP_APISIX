"""
APISIX admin API service — read-only queries against the APISIX Admin API
in each environment, reached via an on-demand `kubectl port-forward`
(same subprocess-based approach as log_service.py's cmd/*.sh scripts).

Write operations (create/update/delete route) are intentionally NOT
implemented here yet — see the agile card's acceptance criteria for the
approval-gated write path that should be added as a follow-up.
"""
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

# Data-plane (gateway) Service port — the apisix helm chart's default
# `service.http.servicePort`. Confirm against each release's values.yaml
# before trusting this for a newly-added environment.
# Namespace + admin/gateway Service names per environment, plus the local
# ports used for their dedicated port-forwards. Keep this in lockstep with
# log_service.NAMESPACE_ALLOWLIST — every env here must also be RBAC-allowed
# for the identity running this MCP server.
ENVIRONMENTS: dict[str, dict[str, Any]] = {
    "rag-edge-dev": {
        "namespace": "scp-cpall-rag-edge-dev",
        "service": "scp-cpall-rag-edge-dev-apisix-admin",
        "local_port": 19180,
        "gateway_service": "scp-cpall-rag-edge-dev-apisix-gateway",
        "gateway_local_port": 19280,
        "deployment": "scp-cpall-rag-edge-dev-apisix",
        "control_local_port": 19380,
    },
    "rag-edge-stg": {
        "namespace": "scp-cpall-rag-edge-stg",
        "service": "scp-cpall-rag-edge-stg-apisix-admin",
        "local_port": 19181,
        "gateway_service": "scp-cpall-rag-edge-stg-apisix-gateway",
        "gateway_local_port": 19281,
        "deployment": "scp-cpall-rag-edge-stg-apisix",
        "control_local_port": 19381,
    },
    "seven-deli-dev": {
        "namespace": "seven-deli-chat-dev",
        "service": "seven-deli-chat-dev-apisix-admin",
        "local_port": 19182,
        "gateway_service": "seven-deli-chat-dev-apisix-gateway",
        "gateway_local_port": 19282,
        "deployment": "seven-deli-chat-dev-apisix",
        "control_local_port": 19382,
    },
    "seven-deli-stg": {
        "namespace": "seven-deli-chat-stg",
        "service": "seven-deli-chat-stg-apisix-admin",
        "local_port": 19183,
        "gateway_service": "seven-deli-chat-stg-apisix-gateway",
        "gateway_local_port": 19283,
        "deployment": "seven-deli-chat-stg-apisix",
        "control_local_port": 19383,
    },
    "allchat-edge-stg": {
        "namespace": "scp-cpall-allchat-edge-stg",
        "service": "scp-cpall-allchat-edge-stg-apisix-admin",
        "local_port": 19184,
        "gateway_service": "scp-cpall-allchat-edge-stg-apisix-gateway",
        "gateway_local_port": 19284,
        "deployment": "scp-cpall-allchat-edge-stg-apisix",
        "control_local_port": 19384,
    },
}


@dataclass
class PortForward:
    process: subprocess.Popen
    local_port: int

ADMIN_PORT = int(os.environ.get("ADMIN_PORT", 9180))
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", 80))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", 9090))


# One persistent port-forward process per (environment, target) pair,
# started lazily on first use and reused across calls. "target" is "admin"
# (the Admin API, ADMIN_PORT) or "gateway" (the data plane, GATEWAY_PORT).
_port_forwards: dict[tuple[str, str], PortForward] = {}


def _port_open(port: int, timeout: float = 0.2) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _ensure_port_forward(env: str, target: str = "admin") -> int:
    """
    Start (or reuse) the kubectl port-forward for an environment's admin
    service, gateway service, or apisix Deployment (control API).

    "control" targets `deployment/<name>` rather than a Service on purpose:
    APISIX's Control API is bound to 127.0.0.1 *inside the pod*
    (enable_control/control.ip in its config.yaml) precisely so it isn't
    network-exposed — a ClusterIP/NodePort Service can never reach a
    loopback-bound port, since Service traffic arrives at the pod's IP, not
    its loopback interface. `kubectl port-forward` against a Deployment (or
    a pod) tunnels into the pod's own network namespace instead, which can
    reach 127.0.0.1, and letting kubectl resolve the Deployment -> live pod
    means this doesn't break across pod restarts.
    """
    cfg = ENVIRONMENTS[env]
    if target == "admin":
        kube_target, local_port, remote_port = f"svc/{cfg['service']}", cfg["local_port"], ADMIN_PORT
    elif target == "gateway":
        kube_target, local_port, remote_port = f"svc/{cfg['gateway_service']}", cfg["gateway_local_port"], GATEWAY_PORT
    elif target == "control":
        kube_target, local_port, remote_port = f"deployment/{cfg['deployment']}", cfg["control_local_port"], CONTROL_PORT
    else:
        raise ValueError(f"unknown port-forward target '{target}', must be 'admin', 'gateway', or 'control'")

    key = (env, target)
    existing = _port_forwards.get(key)
    if existing is not None and existing.process.poll() is None:
        return local_port

    proc = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            kube_target,
            f"{local_port}:{remote_port}",
            "-n",
            cfg["namespace"],
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _port_forwards[key] = PortForward(process=proc, local_port=local_port)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _port_open(local_port):
            return local_port
        if proc.poll() is not None:
            raise RuntimeError(f"kubectl port-forward for '{env}' ({target}) exited before binding {local_port}")
        time.sleep(0.2)
    raise RuntimeError(f"kubectl port-forward for '{env}' ({target}) did not bind {local_port} in time")


AUDIT_LOG_PATH = os.environ.get(
    "AUDIT_LOG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.log")
)

# Cached AWS caller identity for audit attribution. Deliberately NOT cached
# on failure (e.g. expired SSO session) so a later `aws sso login` heals it
# on the next call, without restarting the server.
_actor_identity_cache: dict[str, Any] | None = None


def _actor_identity() -> dict[str, Any]:
    """
    Resolve the local operator's AWS identity via `aws sts get-caller-identity`
    for audit attribution. Developers run this server locally under their own
    AWS SSO session (the same credentials used for kubectl/EKS auth), so this
    identifies "who" made a call without any separate auth system.
    """
    global _actor_identity_cache
    if _actor_identity_cache is not None:
        return _actor_identity_cache

    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        identity = json.loads(result.stdout)
        _actor_identity_cache = {"account": identity.get("Account"), "arn": identity.get("Arn")}
        return _actor_identity_cache
    except Exception as e:
        return {"account": "unknown", "arn": "unknown", "error": str(e)}


def _audit(action: str, env: str, detail: dict[str, Any], ok: bool) -> None:
    """
    Append one JSON-line audit record. Best-effort only — a logging failure
    must never break the underlying tool call, so all errors are swallowed.
    """
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": _actor_identity(),
        "action": action,
        "env": env,
        "ok": ok,
        **detail,
    }
    try:
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _api_key_for(env: str) -> str:
    """
    Look up the viewer (read-only) admin key for an environment.

    Falls back to APISIX_VIEWER_KEY (shared default) if no per-env
    override is set. NOTE: as of 2026-07-30 every discovered environment
    still uses APISIX's stock demo viewer key — rotate this before
    treating any environment as sensitive.
    """
    override = os.environ.get(f"APISIX_VIEWER_KEY__{env.upper().replace('-', '_')}")
    if override:
        return override
    shared = os.environ.get("APISIX_VIEWER_KEY")
    if not shared:
        raise RuntimeError(
            f"no APISIX viewer key configured for '{env}' "
            f"(set APISIX_VIEWER_KEY or APISIX_VIEWER_KEY__{env.upper().replace('-', '_')})"
        )
    return shared


def _admin_get(env: str, path: str) -> dict[str, Any]:
    if env not in ENVIRONMENTS:
        return {"ok": False, "error": f"unknown environment '{env}', must be one of {list(ENVIRONMENTS)}"}

    try:
        local_port = _ensure_port_forward(env, "admin")
        resp = requests.get(
            f"http://127.0.0.1:{local_port}{path}",
            headers={"X-API-KEY": _api_key_for(env)},
            timeout=10,
        )
        resp.raise_for_status()
        _audit("admin_get", env, {"path": path, "status": resp.status_code}, True)
        return {"ok": True, "env": env, "data": resp.json()}
    except Exception as e:
        _audit("admin_get", env, {"path": path, "error": str(e)}, False)
        return {"ok": False, "env": env, "error": str(e)}


# Methods allowed for live route-matching tests. Deliberately excludes
# POST/PUT/PATCH/DELETE — a route-matching test must never risk mutating
# whatever is behind the upstream.
_SAFE_TEST_METHODS = {"GET", "HEAD", "OPTIONS"}


def _gateway_request(env: str, method: str, path: str, host: str | None = None) -> dict[str, Any]:
    """
    Issue one idempotent request against an environment's data-plane gateway
    (never the Admin API) and report exactly what came back, without
    following redirects — a 302 to a login page is itself the result we
    want to observe, not something to chase.
    """
    if env not in ENVIRONMENTS:
        return {"ok": False, "error": f"unknown environment '{env}', must be one of {list(ENVIRONMENTS)}"}
    if method not in _SAFE_TEST_METHODS:
        return {"ok": False, "error": f"method '{method}' not allowed for live route tests, must be one of {sorted(_SAFE_TEST_METHODS)}"}

    try:
        local_port = _ensure_port_forward(env, "gateway")
        resp = requests.request(
            method,
            f"http://127.0.0.1:{local_port}{path}",
            headers={"Host": host} if host else {},
            timeout=10,
            allow_redirects=False,
        )
        _audit("gateway_request", env, {"method": method, "path": path, "host": host, "status": resp.status_code}, True)
        return {
            "ok": True,
            "env": env,
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body_preview": resp.text[:500],
        }
    except Exception as e:
        _audit("gateway_request", env, {"method": method, "path": path, "host": host, "error": str(e)}, False)
        return {"ok": False, "env": env, "error": str(e)}


def list_environments() -> list[str]:
    """List environment names this server can query."""
    return list(ENVIRONMENTS)


def list_routes(env: str) -> dict[str, Any]:
    """List all routes configured in APISIX for an environment."""
    return _admin_get(env, "/apisix/admin/routes")


def get_route(env: str, route_id: str) -> dict[str, Any]:
    """Get the full configuration of one route by id."""
    return _admin_get(env, f"/apisix/admin/routes/{route_id}")


def list_upstreams(env: str) -> dict[str, Any]:
    """List all upstreams (and their load-balancing/health-check config) for an environment."""
    return _admin_get(env, "/apisix/admin/upstreams")


def get_upstream(env: str, upstream_id: str) -> dict[str, Any]:
    """Get the full configuration of one upstream by id."""
    return _admin_get(env, f"/apisix/admin/upstreams/{upstream_id}")


def _control_get(env: str, path: str) -> dict[str, Any]:
    """
    GET against an environment's Control API (port 9090) via the
    Deployment-based port-forward — see _ensure_port_forward's docstring
    for why this can't go through a Service. No API key: the Control API
    has no auth of its own (matches upstream APISIX behavior).
    """
    if env not in ENVIRONMENTS:
        return {"ok": False, "error": f"unknown environment '{env}', must be one of {list(ENVIRONMENTS)}"}

    try:
        local_port = _ensure_port_forward(env, "control")
        resp = requests.get(f"http://127.0.0.1:{local_port}{path}", timeout=10)
        resp.raise_for_status()
        _audit("control_get", env, {"path": path, "status": resp.status_code}, True)
        return {"ok": True, "env": env, "data": resp.json()}
    except Exception as e:
        _audit("control_get", env, {"path": path, "error": str(e)}, False)
        return {"ok": False, "env": env, "error": str(e)}


def get_upstream_health(env: str, upstream_id: str) -> dict[str, Any]:
    """
    Get the *live* health-check status of one upstream's nodes, via the
    Control API's /v1/healthcheck (not the Admin API — there is no
    per-upstream health endpoint there; Admin only holds the configured
    check *policy*, not observed node status).

    Only upstreams with an active/passive health-check policy configured
    report anything here — an upstream with no `checks` config has no live
    status to observe (APISIX always considers all of its nodes live).

    :param env: one of ENVIRONMENTS
    :param upstream_id: id of the upstream, as returned by list_upstreams
    :return: {"ok": True, "upstream_id": ..., "found": bool, "nodes": [...]}
        ("found": False means this upstream has no health-check policy
        configured, so Control API has nothing to report) or
        {"ok": False, "error": str}
    """
    result = _control_get(env, "/v1/healthcheck")
    if not result["ok"]:
        return result

    for entry in result["data"]:
        # APISIX names each entry "upstream#/apisix/upstreams/<id>" (routes
        # bound to a service or plugin get their own prefix instead), so a
        # suffix match on the id is robust across those naming variants.
        if entry.get("name", "").endswith(f"/{upstream_id}"):
            return {
                "ok": True,
                "env": env,
                "upstream_id": upstream_id,
                "found": True,
                "nodes": entry.get("nodes", []),
            }

    return {"ok": True, "env": env, "upstream_id": upstream_id, "found": False, "nodes": []}


def list_services(env: str) -> dict[str, Any]:
    """List all APISIX services (reusable route/upstream/plugin bundles) for an environment."""
    return _admin_get(env, "/apisix/admin/services")


def list_plugins(env: str) -> dict[str, Any]:
    """List all plugins available/enabled on this APISIX instance."""
    return _admin_get(env, "/apisix/admin/plugins/list")


def get_plugin_config(env: str, plugin_name: str) -> dict[str, Any]:
    """Get the schema/config for one named plugin."""
    return _admin_get(env, f"/apisix/admin/plugins/{plugin_name}")


def test_route_matching(env: str, route_id: str, method: str = "GET", extra_path: str = "") -> dict[str, Any]:
    """
    Safely test whether a route matches, and which plugins fired, without
    touching any configuration. Fetches the route's own match conditions
    (uri/host) via the read-only Admin API, then sends one live idempotent
    request through the environment's data-plane gateway — APISIX has no
    admin-side "would this match" endpoint, so exercising the real router
    is the only way to observe an actual match/plugin decision.

    :param env: one of ENVIRONMENTS
    :param route_id: id of the route to test, as returned by list_routes
    :param method: GET/HEAD/OPTIONS only — no method that could mutate
        whatever sits behind the upstream is permitted
    :param extra_path: appended after the route's base uri, e.g. "/health",
        to probe a specific sub-path of a wildcard route
    :return: {"ok": True, "matched": bool, "status": int, "headers": {...},
        "body_preview": str, "route": {...}} or {"ok": False, "error": str}
    """
    route_result = get_route(env, route_id)
    if not route_result["ok"]:
        return route_result

    route = route_result["data"].get("value")
    if route is None:
        return {"ok": False, "env": env, "error": f"route '{route_id}' has no 'value' in its Admin API response"}

    uris = route.get("uris") or ([route["uri"]] if "uri" in route else [])
    if not uris:
        return {"ok": False, "env": env, "error": f"route '{route_id}' has no uri/uris to test"}

    base_uri = uris[0].rstrip("*").rstrip("/") or "/"
    path = base_uri + extra_path
    host = (route.get("hosts") or [None])[0]

    result = _gateway_request(env, method, path, host=host)
    if not result["ok"]:
        return result

    # APISIX's own "no route matched at all" response is a 404 with this
    # specific body — distinct from a legitimate 404 returned *by* the
    # matched upstream, which means the route matched just fine.
    not_found = result["status"] == 404 and '"error_msg"' in result["body_preview"] and "Route Not Found" in result["body_preview"]

    return {
        "ok": True,
        "env": env,
        "route_id": route_id,
        "tested_path": path,
        "tested_host": host,
        "matched": not not_found,
        "status": result["status"],
        "headers": result["headers"],
        "body_preview": result["body_preview"],
        "route_plugins": list(route.get("plugins", {})),
    }