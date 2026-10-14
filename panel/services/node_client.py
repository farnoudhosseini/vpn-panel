"""Talking to a node.

A node is either local (the panel host runs the agent code in-process) or
remote (a small agent service). Both expose exactly the same operation set, so
the rest of the panel never has to care which one it is.

Remote calls are authenticated with a shared key and, when the agent's
certificate is known, pinned to that certificate — an attacker who can spoof
DNS or the address cannot impersonate a node.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping
from datetime import UTC
from typing import Any

import httpx
from sqlalchemy.orm import Session as OrmSession

from ..core.logging import get_logger
from ..models import Node, NodeStatus
from . import node_ops, tunnels
from .plan import SidePlan

log = get_logger("node_client")

AGENT_VERSION = "1.0.0"
DEFAULT_TIMEOUT = 30.0
APPLY_TIMEOUT = 180.0
#: A node that has just been registered is probed from inside the request that
#: created it, so the probe must fail fast: an operator typing a wrong address
#: should get an answer, not a spinner.
PROBE_TIMEOUT = 6.0
#: Status-style reads ("is this service up", "who is connected") are rendered in
#: list and detail views, so they must fail fast: a dead node should show as
#: unreachable in a few seconds, not freeze the page for the full default.
STATUS_TIMEOUT = 6.0
#: Tearing a tunnel down is quick on a healthy node; the cap matters when a
#: node is gone, because the operator is waiting to delete the record and must
#: not be held for minutes by an address that no longer answers.
TEARDOWN_TIMEOUT = 20.0


class NodeUnreachable(RuntimeError):
    """The node did not answer, or answered with an error."""


Transport = Callable[[Mapping[str, Any]], dict[str, Any]]


def _local_transport_handlers() -> dict[str, Transport]:
    return {
        "capabilities": lambda payload: node_ops.capabilities(),
        "metrics": lambda payload: node_ops.metrics(),
        "openvpn.status": lambda payload: {
            "sessions": node_ops.openvpn_sessions(str(payload["tag"]))
        },
        "openvpn.disconnect": lambda payload: node_ops.openvpn_disconnect(
            str(payload["tag"]), str(payload["common_name"])
        ),
        "plan.apply": lambda payload: node_ops.apply_plan(
            SidePlan.from_dict(payload["plan"])
        ),
        "inbound.remove": lambda payload: node_ops.remove_inbound(payload["inbound"]),
        "tunnel.plan": lambda payload: {"plan": node_ops.plan_tunnel_side(payload).to_dict()},
        "tunnel.teardown": lambda payload: node_ops.teardown_tunnel(payload),
        "nginx.apply": lambda payload: node_ops.apply_nginx(payload),
        "nginx.remove": lambda payload: node_ops.remove_nginx(payload),
        "nginx.status": lambda payload: node_ops.nginx_status(),
        "backpack.info": lambda payload: tunnels.backpack_info(),
        "service.state": lambda payload: node_ops.service_status(str(payload["unit"])),
    }


LOCAL_OPS = _local_transport_handlers()

#: Operations the agent is allowed to run, with their worst-case duration.
KNOWN_OPS: dict[str, float] = {
    "capabilities": DEFAULT_TIMEOUT,
    "metrics": DEFAULT_TIMEOUT,
    "openvpn.status": DEFAULT_TIMEOUT,
    "openvpn.disconnect": DEFAULT_TIMEOUT,
    "plan.apply": APPLY_TIMEOUT,
    "inbound.remove": APPLY_TIMEOUT,
    "tunnel.plan": DEFAULT_TIMEOUT,
    "tunnel.teardown": APPLY_TIMEOUT,
    "nginx.apply": APPLY_TIMEOUT,
    "nginx.remove": APPLY_TIMEOUT,
    "nginx.status": DEFAULT_TIMEOUT,
    "backpack.info": DEFAULT_TIMEOUT,
    "service.state": DEFAULT_TIMEOUT,
}


def call(node: Node, op: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
    """Run ``op`` on ``node`` and return its result."""
    if op not in KNOWN_OPS:
        raise NodeUnreachable(f"unknown operation {op!r}")
    body = dict(payload or {})
    if node.is_local:
        try:
            return LOCAL_OPS[op](body)
        except Exception as exc:  # surfaced to the caller with context
            raise NodeUnreachable(f"local operation {op} failed: {exc}") from exc
    return _remote(node, op, body, timeout=timeout or KNOWN_OPS[op])


def _tls_context(node: Node) -> ssl.SSLContext | bool:
    """Build a verification context, preferring certificate pinning."""
    if node.insecure_tls:
        return False
    if node.tls_cert:
        context = ssl.create_default_context(cadata=node.tls_cert)
        # The agent is addressed by IP, so hostname matching is not meaningful;
        # the pinned certificate is what proves identity.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        return context
    # No pinned certificate yet: fall back to the system trust store.
    return True


def _remote(node: Node, op: str, payload: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
    from ..config import get_settings

    settings = get_settings()
    api_key = ""
    if node.api_key_enc:
        from ..core.crypto import decrypt

        api_key = decrypt(node.api_key_enc)
    elif settings.agent_key:
        api_key = settings.agent_key

    scheme = node.scheme if node.scheme in {"http", "https"} else "https"
    url = f"{scheme}://{node.address}:{node.agent_port}/agent/v1/ops"
    headers = {
        "X-Agent-Key": api_key,
        "X-Agent-Version": AGENT_VERSION,
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(verify=_tls_context(node), timeout=timeout) as client:
            response = client.post(url, json={"op": op, "payload": payload}, headers=headers)
    except httpx.HTTPError as exc:
        raise NodeUnreachable(f"{node.name}: {exc}") from exc

    if response.status_code == 401:
        raise NodeUnreachable(f"{node.name}: agent rejected the API key")
    if response.status_code >= 400:
        raise NodeUnreachable(
            f"{node.name}: agent returned {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise NodeUnreachable(f"{node.name}: agent returned invalid JSON") from exc
    if isinstance(data, dict) and data.get("error"):
        raise NodeUnreachable(f"{node.name}: {data['error']}")
    return data.get("result", {}) if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Higher level helpers
# ---------------------------------------------------------------------------


def probe(node: Node, *, timeout: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask a node for its capabilities and metrics. Network only — no session."""
    window = timeout or KNOWN_OPS["capabilities"]
    return call(node, "capabilities", timeout=window), call(node, "metrics", timeout=window)


def refresh_node(
    session: OrmSession,
    node: Node,
    *,
    collect_sample: bool = True,
    timeout: float | None = None,
    ready: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> Node:
    """Update a node row from its live capability and metric payloads.

    Pass ``ready`` with the result of an earlier :func:`probe` to keep the
    network round trip outside the caller's transaction.
    """
    from datetime import datetime

    from ..models import NodeSample

    if ready is not None:
        caps, metrics = ready
    else:
        try:
            caps, metrics = probe(node, timeout=timeout)
        except NodeUnreachable as exc:
            return mark_offline(session, node, exc)

    node.status = NodeStatus.ONLINE.value
    node.last_error = None
    node.last_seen_at = datetime.now(UTC)
    node.version = str(caps.get("python") or caps.get("platform") or "")[:32] or None
    node.os_info = str(caps.get("platform") or "")[:160]

    available = {
        item["key"]: bool(item.get("installed"))
        for item in caps.get("capabilities", [])
    }
    node.openvpn_installed = available.get("openvpn", False)
    node.backpack_installed = available.get("backpack", False)
    node.capabilities = {
        "items": caps.get("capabilities", []),
        "hostname": caps.get("hostname"),
        "kernel": caps.get("kernel"),
        "is_root": caps.get("is_root"),
        "ports": caps.get("ports", []),
    }

    node.cpu_percent = float(metrics.get("cpu_percent") or 0.0)
    node.memory_percent = float(metrics.get("memory_percent") or 0.0)
    node.disk_percent = float(metrics.get("disk_percent") or 0.0)
    node.uptime_seconds = int(metrics.get("uptime_seconds") or 0)

    if collect_sample:
        session.add(
            NodeSample(
                node_id=node.id,
                cpu_percent=node.cpu_percent,
                memory_percent=node.memory_percent,
                disk_percent=node.disk_percent,
                net_rx_bytes=int(metrics.get("net_rx_bytes") or 0),
                net_tx_bytes=int(metrics.get("net_tx_bytes") or 0),
                connections=int(metrics.get("connections") or 0),
            )
        )
    session.flush()
    return node


def mark_offline(session: OrmSession, node: Node, reason: object) -> Node:
    """Record that a node did not answer. Writes only — no network call."""
    node.status = NodeStatus.OFFLINE.value
    node.last_error = str(reason)[:500]
    log.warning("node %s is unreachable: %s", node.name, reason)
    session.flush()
    return node


def apply_side(session: OrmSession, node: Node, side: SidePlan) -> dict[str, Any]:
    """Send one side of a plan to the node that owns it."""
    if node.is_local:
        return node_ops.apply_plan(side)
    return call(node, "plan.apply", {"plan": side.to_dict()})


def teardown_tunnel(
    node: Node, payload: Mapping[str, Any], *, timeout: float | None = None
) -> dict[str, Any]:
    return call(
        node, "tunnel.teardown", dict(payload), timeout=timeout or TEARDOWN_TIMEOUT
    )


def fetch_sessions(node: Node, tag: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
    data = call(
        node,
        "openvpn.status",
        {"tag": tag},
        timeout=timeout or KNOWN_OPS["openvpn.status"],
    )
    sessions = data.get("sessions") or []
    return sessions if isinstance(sessions, list) else []


def disconnect(
    node: Node, tag: str, common_name: str, *, timeout: float | None = None
) -> bool:
    data = call(
        node,
        "openvpn.disconnect",
        {"tag": tag, "common_name": common_name},
        timeout=timeout or KNOWN_OPS["openvpn.disconnect"],
    )
    return bool(data.get("ok"))


def disconnect_by_id(
    node_id: int, tag: str, common_name: str, *, timeout: float | None = None
) -> bool:
    """Close a session using its node id.

    Used by enforcement *after* the accounting transaction has committed: the
    panel must not keep a SQLite write transaction open while it waits for a
    node to answer, so the work is deferred to a short session of its own.
    """
    from ..db import session_scope

    with session_scope() as session:
        node = session.get(Node, node_id)
        if node is None:
            return False
        return disconnect(node, tag, common_name, timeout=timeout)


def apply_nginx(node: Node, payload: Mapping[str, Any]) -> dict[str, Any]:
    return call(node, "nginx.apply", dict(payload))
