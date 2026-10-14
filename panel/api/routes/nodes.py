"""Node management."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ...config import get_settings
from ...core import audit
from ...core.crypto import encrypt
from ...core.logging import get_logger
from ...models import Client, Inbound, Node, NodeSample, NodeStatus, Tunnel, utcnow
from ...services import node_client, openvpn
from ..deps import AdminOnly, Context, DbSession, OperatorUp, ViewerUp
from ..schemas import (
    NodeCreate,
    NodeDetail,
    NodeOut,
    NodeSampleOut,
    NodeTestResult,
    NodeUpdate,
)

log = get_logger("api.nodes")

router = APIRouter(prefix="/nodes", tags=["nodes"])


def _require_node(session, node_id: int) -> Node:
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    return node


def _counts(session, node_id: int) -> dict[str, int]:
    inbounds = session.scalar(
        select(func.count(Inbound.id)).where(Inbound.node_id == node_id)
    )
    tunnels = session.scalar(
        select(func.count(Tunnel.id)).where(
            (Tunnel.local_node_id == node_id) | (Tunnel.remote_node_id == node_id)
        )
    )
    clients = session.scalar(select(func.count(Client.id)).where(Client.node_id == node_id))
    return {
        "inbounds": int(inbounds or 0),
        "tunnels": int(tunnels or 0),
        "clients": int(clients or 0),
    }


@router.get("", response_model=list[NodeOut])
def list_nodes(session: DbSession, principal: ViewerUp) -> list[Node]:
    return list(session.scalars(select(Node).order_by(Node.is_local.desc(), Node.name)))


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
def create_node(
    payload: NodeCreate,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> Node:
    if session.scalar(select(Node).where(Node.name == payload.name)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="node name already exists")

    settings = get_settings()
    api_key = payload.api_key or settings.agent_key or ""
    node = Node(
        name=payload.name,
        address=payload.address,
        agent_port=payload.agent_port,
        api_key_enc=encrypt(api_key) if api_key else "",
        is_local=False,
        insecure_tls=payload.insecure_tls,
        scheme=payload.scheme,
        note=payload.note,
        tags=payload.tags,
        tls_cert=payload.tls_cert,
        status=NodeStatus.UNKNOWN.value,
    )
    # Probe before the INSERT: an unreachable host must not leave a write
    # transaction (and therefore the whole API) waiting on a socket.
    ready: tuple[dict[str, Any], dict[str, Any]] | None = None
    failure: Exception | None = None
    try:
        ready = node_client.probe(node, timeout=node_client.PROBE_TIMEOUT)
    except node_client.NodeUnreachable as exc:
        failure = exc

    session.add(node)
    session.flush()

    audit.record(
        session,
        action="node.create",
        username=principal.username,
        target_type="node",
        target_id=node.id,
        ip=ctx.ip,
        detail={"address": node.address, "port": node.agent_port},
    )

    # Best effort: record what the first probe learned; the scheduler polls
    # again every 30 seconds anyway.
    if failure is not None:
        node_client.mark_offline(session, node, failure)
    elif ready is not None:
        node_client.refresh_node(session, node, collect_sample=False, ready=ready)
    return node


@router.get("/{node_id}", response_model=NodeDetail)
def get_node(node_id: int, session: DbSession, principal: ViewerUp) -> NodeDetail:
    node = _require_node(session, node_id)
    detail = NodeDetail.model_validate(node)
    detail.capabilities = node.capabilities or {}
    detail.has_tls_cert = bool(node.tls_cert)
    counts = _counts(session, node_id)
    detail.inbounds = counts["inbounds"]
    detail.tunnels = counts["tunnels"]
    detail.clients = counts["clients"]
    return detail


@router.patch("/{node_id}", response_model=NodeOut)
def update_node(
    node_id: int,
    payload: NodeUpdate,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> Node:
    node = _require_node(session, node_id)

    if payload.name and payload.name != node.name:
        if session.scalar(select(Node).where(Node.name == payload.name, Node.id != node_id)):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="name already used")
        node.name = payload.name
    if payload.address is not None:
        node.address = payload.address
    if payload.agent_port is not None:
        node.agent_port = payload.agent_port
    if payload.api_key:
        node.api_key_enc = encrypt(payload.api_key)
    if payload.enabled is not None:
        node.enabled = payload.enabled
    if payload.insecure_tls is not None:
        node.insecure_tls = payload.insecure_tls
    if payload.scheme is not None:
        node.scheme = payload.scheme
    if payload.note is not None:
        node.note = payload.note
    if payload.tags is not None:
        node.tags = [tag.strip()[:24] for tag in payload.tags if tag.strip()][:16]
    if payload.tls_cert is not None:
        node.tls_cert = payload.tls_cert or None

    audit.record(
        session,
        action="node.update",
        username=principal.username,
        target_type="node",
        target_id=node.id,
        ip=ctx.ip,
    )
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    node_id: int,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
    force: bool = Query(default=False),
) -> None:
    from fastapi import Response

    node = _require_node(session, node_id)
    if node.is_local:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the panel node cannot be removed",
        )

    counts = _counts(session, node_id)
    if not force and any(counts.values()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"node still owns {counts['inbounds']} inbound(s), "
                f"{counts['tunnels']} tunnel(s) and {counts['clients']} client(s)"
            ),
        )

    session.delete(node)
    audit.record(
        session,
        action="node.delete",
        username=principal.username,
        target_type="node",
        target_id=node_id,
        level="warning",
        ip=ctx.ip,
        detail=counts,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{node_id}/test", response_model=NodeTestResult)
def test_node(node_id: int, session: DbSession, principal: OperatorUp) -> NodeTestResult:
    node = _require_node(session, node_id)
    try:
        caps = node_client.call(node, "capabilities", timeout=15)
        metrics = node_client.call(node, "metrics", timeout=15)
    except node_client.NodeUnreachable as exc:
        node.status = NodeStatus.OFFLINE.value
        node.last_error = str(exc)[:500]
        return NodeTestResult(ok=False, message=str(exc))

    node.status = NodeStatus.ONLINE.value
    node.last_error = None
    node.last_seen_at = utcnow()
    node.capabilities = {
        "items": caps.get("capabilities", []),
        "hostname": caps.get("hostname"),
        "kernel": caps.get("kernel"),
        "is_root": caps.get("is_root"),
        "ports": caps.get("ports", []),
    }
    return NodeTestResult(
        ok=True,
        message=f"connected to {caps.get('hostname') or node.address}",
        capabilities=caps,
        metrics=metrics,
    )


@router.post("/{node_id}/refresh", response_model=NodeOut)
def refresh_node(node_id: int, session: DbSession, principal: OperatorUp) -> Node:
    node = _require_node(session, node_id)
    node_client.refresh_node(session, node)
    return node


@router.get("/{node_id}/samples", response_model=list[NodeSampleOut])
def node_samples(
    node_id: int,
    session: DbSession,
    principal: ViewerUp,
    hours: int = Query(default=6, ge=1, le=168),
    limit: int = Query(default=500, ge=10, le=5000),
) -> list[NodeSample]:
    _require_node(session, node_id)
    since = utcnow() - timedelta(hours=hours)
    return list(
        session.scalars(
            select(NodeSample)
            .where(NodeSample.node_id == node_id, NodeSample.ts >= since)
            .order_by(NodeSample.ts.desc())
            .limit(limit)
        )
    )[::-1]


@router.get("/{node_id}/sessions")
def node_sessions(
    node_id: int,
    session: DbSession,
    principal: ViewerUp,
    tag: str | None = Query(default=None, max_length=80),
) -> dict[str, Any]:
    """Live OpenVPN sessions for every inbound on this node."""
    node = _require_node(session, node_id)
    inbounds = session.scalars(
        select(Inbound).where(Inbound.node_id == node_id).order_by(Inbound.tag)
    ).all()
    if tag:
        inbounds = [item for item in inbounds if item.tag == tag]

    result: dict[str, Any] = {"node": node.name, "inbounds": []}
    for inbound in inbounds:
        entry: dict[str, Any] = {"tag": inbound.tag, "sessions": [], "error": None}
        try:
            entry["sessions"] = node_client.fetch_sessions(
                node, inbound.tag, timeout=node_client.STATUS_TIMEOUT
            )
        except node_client.NodeUnreachable as exc:
            entry["error"] = str(exc)
        result["inbounds"].append(entry)
    return result


@router.get("/{node_id}/openvpn/capabilities")
def openvpn_capabilities(node_id: int, session: DbSession, principal: ViewerUp) -> dict[str, Any]:
    node = _require_node(session, node_id)
    if node.is_local:
        return openvpn.detect().as_dict()
    caps = node.capabilities or {}
    for item in caps.get("items", []):
        if item.get("key") == "openvpn":
            return item
    return {"installed": False, "detail": "not reported"}
