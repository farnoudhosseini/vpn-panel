"""System, settings, logs and reporting."""

from __future__ import annotations

import platform
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from ... import PRODUCT, __version__
from ...config import get_settings
from ...core import audit
from ...core.logging import get_logger
from ...db import get_engine
from ...models import (
    AuditLog,
    Client,
    ClientStatus,
    Event,
    Inbound,
    Node,
    NodeSample,
    NodeStatus,
    SystemSetting,
    Tunnel,
    TunnelStatus,
    utcnow,
)
from ...services import certbot, node_ops, scheduler, traffic
from ..deps import AdminOnly, Context, DbSession, OperatorUp, ViewerUp
from ..schemas import (
    AuditPage,
    CertificateRequest,
    CertificateResult,
    EventOut,
    EventPage,
    JobRunResult,
    SettingOut,
    SettingUpdate,
    SystemInfoOut,
)

log = get_logger("api.system")

router = APIRouter(prefix="/system", tags=["system"])

_STARTED_AT = time.time()

DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "branding": {
        "panel_title": "Panah",
        "accent": "#E11D2E",
        "language": "fa",
    },
    "notifications": {"email": "", "webhook": "", "events": ["node.offline", "client.limited"]},
    "retention": {"usage_days": 30, "node_sample_days": 14, "audit_days": 90},
}


@router.get("/health")
def health() -> dict[str, Any]:
    from ...db import healthcheck

    return {
        "status": "ok" if healthcheck() else "degraded",
        "product": PRODUCT,
        "version": __version__,
        "uptime_seconds": int(time.time() - _STARTED_AT),
    }


@router.get("/info", response_model=SystemInfoOut)
def info(session: DbSession, principal: ViewerUp) -> SystemInfoOut:
    settings = get_settings()

    nodes_total = int(session.scalar(select(func.count(Node.id))) or 0)
    nodes_online = int(
        session.scalar(
            select(func.count(Node.id)).where(Node.status == NodeStatus.ONLINE.value)
        )
        or 0
    )
    inbounds_total = int(session.scalar(select(func.count(Inbound.id))) or 0)
    clients_total = int(session.scalar(select(func.count(Client.id))) or 0)
    clients_active = int(
        session.scalar(
            select(func.count(Client.id)).where(Client.status == ClientStatus.ACTIVE.value)
        )
        or 0
    )
    tunnels_total = int(session.scalar(select(func.count(Tunnel.id))) or 0)
    tunnels_up = int(
        session.scalar(
            select(func.count(Tunnel.id)).where(Tunnel.status == TunnelStatus.UP.value)
        )
        or 0
    )
    bandwidth_in = int(session.scalar(select(func.coalesce(func.sum(Client.up_bytes), 0))) or 0)
    bandwidth_out = int(session.scalar(select(func.coalesce(func.sum(Client.down_bytes), 0))) or 0)
    billed = float(session.scalar(select(func.coalesce(func.sum(Client.billed_bytes), 0.0))) or 0.0)

    db_path = settings.database_path
    try:
        db_size = db_path.stat().st_size
    except OSError:
        db_size = 0

    return SystemInfoOut(
        product=PRODUCT,
        version=__version__,
        mode=settings.mode,
        domain=settings.domain,
        uptime_seconds=int(time.time() - _STARTED_AT),
        python=platform.python_version(),
        database_path=str(db_path),
        database_size_bytes=db_size,
        nodes_total=nodes_total,
        nodes_online=nodes_online,
        inbounds_total=inbounds_total,
        clients_total=clients_total,
        clients_active=clients_active,
        tunnels_total=tunnels_total,
        tunnels_up=tunnels_up,
        bandwidth_in_bytes=bandwidth_in,
        bandwidth_out_bytes=bandwidth_out,
        billed_bytes=round(billed, 2),
        scheduler={"running": scheduler.is_running(), "jobs": scheduler.job_status()},
        capabilities=node_ops.capabilities(),
        last_accounting=scheduler.last_report(),
    )


@router.get("/overview")
def overview(
    session: DbSession,
    principal: ViewerUp,
    minutes: int = Query(default=30, ge=1, le=1440),
) -> dict[str, Any]:
    """Lean snapshot for the always-visible node strip.

    Deliberately smaller than :func:`dashboard` so polling it every few seconds
    is cheap: one query for the nodes and one for the samples.
    """
    since = utcnow() - timedelta(minutes=minutes)
    nodes = list(session.scalars(select(Node).order_by(Node.is_local.desc(), Node.name)))
    rows = session.scalars(
        select(NodeSample)
        .where(NodeSample.ts >= since)
        .order_by(NodeSample.ts)
        .limit(4000)
    ).all()

    connections: dict[int, list[int]] = {}
    counters: dict[int, list[tuple[Any, int]]] = {}
    for row in rows:
        connections.setdefault(row.node_id, []).append(int(row.connections))
        counters.setdefault(row.node_id, []).append(
            (row.ts, int(row.net_rx_bytes) + int(row.net_tx_bytes))
        )

    def throughput(node_id: int) -> int:
        """Bytes per second between the two most recent samples."""
        samples = counters.get(node_id) or []
        if len(samples) < 2:
            return 0
        (previous_ts, previous_total) = samples[-2]
        (last_ts, last_total) = samples[-1]
        elapsed = (last_ts - previous_ts).total_seconds() if last_ts and previous_ts else 0
        if elapsed <= 0:
            return 0
        return max(0, int((last_total - previous_total) / elapsed))

    return {
        "ts": utcnow(),
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "status": node.status,
                "is_local": node.is_local,
                "enabled": node.enabled,
                "address": node.address,
                "cpu": node.cpu_percent,
                "memory": node.memory_percent,
                "disk": node.disk_percent,
                "connections": (connections.get(node.id) or [0])[-1],
                "series": (connections.get(node.id) or [])[-24:],
                "throughput": throughput(node.id),
            }
            for node in nodes
        ],
        "scheduler": scheduler.is_running(),
    }


@router.get("/dashboard")
def dashboard(
    session: DbSession,
    principal: ViewerUp,
    hours: int = Query(default=24, ge=1, le=720),
) -> dict[str, Any]:
    """Everything the landing page needs in one round trip."""
    since = utcnow() - timedelta(hours=hours)
    nodes = list(session.scalars(select(Node).order_by(Node.name)))

    node_series: dict[int, list[dict[str, Any]]] = {}
    rows = session.scalars(
        select(NodeSample).where(NodeSample.ts >= since).order_by(NodeSample.ts).limit(5000)
    ).all()
    for row in rows:
        node_series.setdefault(row.node_id, []).append(
            {
                "ts": row.ts,
                "cpu": row.cpu_percent,
                "memory": row.memory_percent,
                "disk": row.disk_percent,
                "rx": row.net_rx_bytes,
                "tx": row.net_tx_bytes,
                "connections": row.connections,
            }
        )

    events = session.scalars(
        select(Event).order_by(Event.ts.desc()).limit(12)
    ).all()

    return {
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "status": node.status,
                "is_local": node.is_local,
                "cpu": node.cpu_percent,
                "memory": node.memory_percent,
                "disk": node.disk_percent,
                "address": node.address,
            }
            for node in nodes
        ],
        "node_series": node_series,
        "traffic": traffic.usage_series(session, hours=hours),
        "top_clients": traffic.top_clients(session, limit=8),
        "events": [
            {
                "id": event.id,
                "ts": event.ts,
                "level": event.level,
                "source": event.source,
                "message": event.message,
                "acknowledged": event.acknowledged,
            }
            for event in events
        ],
    }


@router.get("/traffic/series")
def traffic_series(
    session: DbSession,
    principal: ViewerUp,
    hours: int = Query(default=24, ge=1, le=720),
    client_id: int | None = Query(default=None),
) -> dict[str, Any]:
    return {
        "hours": hours,
        "series": traffic.usage_series(session, client_id=client_id, hours=hours),
    }


@router.get("/traffic/top")
def traffic_top(
    session: DbSession, principal: ViewerUp, limit: int = Query(default=10, ge=1, le=100)
) -> list[dict[str, Any]]:
    return traffic.top_clients(session, limit=limit)


@router.post("/accounting/run")
def run_accounting(session: DbSession, principal: OperatorUp) -> dict[str, Any]:
    report = traffic.collect(session)
    return report.as_dict()


@router.get("/settings", response_model=list[SettingOut])
def list_settings(session: DbSession, principal: AdminOnly) -> list[SettingOut]:
    stored = {row.key: row.value for row in session.scalars(select(SystemSetting))}
    merged = [
        SettingOut(key=key, value={**default, **stored.get(key, {})})
        for key, default in DEFAULT_SETTINGS.items()
    ]
    extra = [
        SettingOut(key=key, value=value)
        for key, value in stored.items()
        if key not in DEFAULT_SETTINGS and key != "pending_cleanup"
    ]
    return merged + extra


@router.put("/settings/{key}", response_model=SettingOut)
def update_setting(
    key: str,
    payload: SettingUpdate,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> SettingOut:
    if len(key) > 80 or not key.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid key")
    row = session.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=payload.value)
        session.add(row)
    else:
        row.value = payload.value
    audit.record(
        session,
        action="settings.update",
        username=principal.username,
        target_type="setting",
        target_id=key,
        ip=ctx.ip,
    )
    return SettingOut(key=key, value=row.value)


@router.get("/logs", response_model=AuditPage)
def logs(
    session: DbSession,
    principal: ViewerUp,
    action: str | None = Query(default=None, max_length=64),
    username: str | None = Query(default=None, max_length=64),
    level: str | None = Query(default=None, max_length=16),
    search: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AuditPage:
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if username:
        conditions.append(AuditLog.username == username)
    if level:
        conditions.append(AuditLog.level == level)
    if search:
        pattern = f"%{search}%"
        conditions.append(or_(AuditLog.action.ilike(pattern), AuditLog.username.ilike(pattern)))

    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = int(session.scalar(count_query) or 0)
    items = list(
        session.scalars(
            query.order_by(AuditLog.ts.desc()).limit(limit).offset(offset)
        )
    )
    return AuditPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/events", response_model=EventPage)
def events(
    session: DbSession,
    principal: ViewerUp,
    level: str | None = Query(default=None, max_length=16),
    unacknowledged: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> EventPage:
    query = select(Event)
    count_query = select(func.count(Event.id))
    conditions = []
    if level:
        conditions.append(Event.level == level)
    if unacknowledged:
        conditions.append(Event.acknowledged.is_(False))
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = int(session.scalar(count_query) or 0)
    items = list(
        session.scalars(query.order_by(Event.ts.desc()).limit(limit).offset(offset))
    )
    return EventPage(total=total, limit=limit, offset=offset, items=items)


@router.post("/events/{event_id}/ack", response_model=EventOut)
def acknowledge_event(
    event_id: int, session: DbSession, principal: OperatorUp
) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    event.acknowledged = True
    return event


@router.get("/jobs")
def jobs(principal: ViewerUp) -> dict[str, Any]:
    return {"running": scheduler.is_running(), "jobs": scheduler.job_status()}


@router.post("/jobs/{job_id}/run", response_model=JobRunResult)
def run_job(job_id: str, principal: OperatorUp) -> JobRunResult:
    try:
        result = scheduler.run_job_now(job_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown job {job_id}"
        ) from exc
    return JobRunResult(job_id=job_id, result=result)


@router.post("/certificates", response_model=CertificateResult)
def issue_certificate(
    payload: CertificateRequest,
    session: DbSession,
    principal: AdminOnly,
    ctx: Context,
) -> CertificateResult:
    try:
        outcome = certbot.issue(payload.domain, payload.email, staging=payload.staging)
    except certbot.CertificateError as exc:
        if payload.staging:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        # Fall back to a self-signed certificate so the panel still serves TLS.
        log.warning("falling back to a self-signed certificate: %s", exc)
        certificate = certbot.self_signed(payload.domain)
        outcome = {
            "ok": True,
            "domain": certificate.domain,
            "message": f"issued a self-signed certificate ({exc})",
            "certificate": certificate.as_dict(),
        }

    audit.record(
        session,
        action="certificate.issue",
        username=principal.username,
        target_type="certificate",
        target_id=payload.domain,
        success=bool(outcome.get("ok")),
        ip=ctx.ip,
        detail={"staging": payload.staging},
    )
    return CertificateResult(**outcome)


@router.post("/certificates/renew")
def renew_certificates(session: DbSession, principal: AdminOnly) -> dict[str, Any]:
    try:
        return certbot.renew()
    except certbot.CertificateError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/database")
def database_info(session: DbSession, principal: AdminOnly) -> dict[str, Any]:
    from sqlalchemy import inspect
    from sqlalchemy import text as sql_text

    engine = get_engine()
    inspector = inspect(engine)
    tables: dict[str, int] = {}
    for table in sorted(inspector.get_table_names()):
        if table == "schema_migrations":
            continue
        # Table names come from SQLite's own catalogue, not from user input.
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            tables[table] = int(
                session.execute(sql_text(f"SELECT COUNT(*) FROM {quoted}")).scalar() or 0
            )
        except Exception:  # pragma: no cover - defensive
            tables[table] = -1

    with engine.connect() as conn:
        mode = conn.execute(sql_text("PRAGMA journal_mode")).scalar()
        page_size = conn.execute(sql_text("PRAGMA page_size")).scalar()
        page_count = conn.execute(sql_text("PRAGMA page_count")).scalar()

    return {
        "tables": tables,
        "journal_mode": mode,
        "page_size": page_size,
        "page_count": page_count,
        "size_bytes": (page_size or 0) * (page_count or 0),
    }


@router.post("/database/vacuum")
def vacuum(session: DbSession, principal: AdminOnly) -> dict[str, Any]:
    from sqlalchemy import text as sql_text

    engine = get_engine()
    session.commit()
    with engine.connect() as conn:
        conn.execute(sql_text("PRAGMA wal_checkpoint(TRUNCATE)"))
        conn.execute(sql_text("VACUUM"))
        conn.execute(sql_text("PRAGMA optimize"))
    return {"ok": True, "message": "database compacted"}
