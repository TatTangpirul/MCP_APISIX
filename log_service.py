"""
EKS log middleware — sits between OpenWebUI and the Kubernetes API.
Uses the official kubernetes python client (no kubectl subprocess).
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from kubernetes import client, config
import subprocess

# Namespaces this middleware is allowed to touch. RBAC (RoleBinding per
# namespace) should mirror this list — do not widen this to a ClusterRole.
NAMESPACE_ALLOWLIST: list[str] = [
    "seven-deli-chat-stg",
    "seven-deli-chat-dev",
    "scp-cpall-rag-edge-stg",
    "scp-cpall-rag-edge-dev",
    "scp-cpall-rag-data-stg",
    "scp-cpall-rag-data-dev",
    "scp-cpall-rag-app-stg",
    "scp-cpall-rag-app-dev",
]

@dataclass
class DiscoverySnapshot:
    namespaces: dict[str, list[str]]  # namespace -> list of pod names
    updated_at: datetime


# Epoch updated_at means "never refreshed" -> any TTL check treats it as stale.
_discovery_cache = DiscoverySnapshot(
    namespaces={},
    updated_at=datetime.fromtimestamp(0, tz=timezone.utc),
)


def is_stale(snapshot: DiscoverySnapshot, max_age: timedelta = timedelta(days=1)) -> bool:
    return datetime.now(timezone.utc) - snapshot.updated_at > max_age


def _load_k8s_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


_load_k8s_config()
core_v1 = client.CoreV1Api()


def list_pods(namespace: str) -> list[str]:
    """
    List service names discoverable inside a single allow-listed namespace.
    Used to build/refresh a DiscoverySnapshot.

    :param namespace: must be a member of NAMESPACE_ALLOWLIST
    :return: service names found in that namespace
    """
    result = subprocess.run(["cmd/list_pods.sh", namespace], capture_output=True, text=True)
    pods = result.stdout.splitlines()
    _discovery_cache.namespaces[namespace] = pods
    _discovery_cache.updated_at = datetime.now(timezone.utc)
    return pods


# Flat per-pod cap so a wide/long query can't blow past the LLM context window.
MAX_LINES_PER_POD = 50


def _cap_output(text: str) -> str:
    """Keep only the last MAX_LINES_PER_POD lines, labeling any truncation."""
    lines = text.splitlines()
    if len(lines) <= MAX_LINES_PER_POD:
        return text
    kept = lines[-MAX_LINES_PER_POD:]
    note = f"[truncated: showing last {MAX_LINES_PER_POD} of {len(lines)} lines]"
    return note + "\n" + "\n".join(kept)


def get_namespace_logs(namespace: str, minutes: int = 60) -> dict[str, Any]:
    """
    Fetch recent logs for every service/pod inside a single namespace
    ("system" with no specific service named).

    :param namespace: must be a member of NAMESPACE_ALLOWLIST
    :param minutes: lookback window
    :return: per-pod/service result, e.g.
        {"ok": True, "namespace": namespace, "results": {...}}
        or {"ok": False, "error": "..."}
    """
    if namespace not in _discovery_cache.namespaces or is_stale(_discovery_cache):
        list_pods(namespace)

    pods = _discovery_cache.namespaces[namespace]

    def fetch(pod: str) -> tuple[str, str]:
        result = subprocess.run(
            ["cmd/get_log.sh", pod, namespace, str(minutes)], capture_output=True, text=True
        )
        return pod, _cap_output(result.stdout)

    logs: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(len(pods), 8) or 1) as pool:
        for pod, output in pool.map(fetch, pods):
            logs[pod] = output

    return {"ok": True, "namespace": namespace, "results": logs}


def get_pod_logs(namespace: str, service: str, minutes: int = 60) -> dict[str, Any]:
    """
    Fetch recent logs for one specific service (label-selected deployment)
    inside a namespace.

    :param namespace: must be a member of NAMESPACE_ALLOWLIST
    :param service: used to build the pod label selector, e.g. app=<service>
    :param minutes: lookback window
    :return: per-pod result, e.g.
        {"ok": True, "namespace": namespace, "service": service, "results": {...}}
        or {"ok": False, "error": "..."}
    """
    raise NotImplementedError
