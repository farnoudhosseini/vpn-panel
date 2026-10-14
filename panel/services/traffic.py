"""Traffic accounting and enforcement.

OpenVPN publishes cumulative counters per live session, so the panel converts
them into deltas, applies the client's multiplier and bills the result. When a
client passes its quota or its expiry, the session is closed through the
OpenVPN management interface and the client is marked accordingly — a disabled
client is also dropped from the static address map so it cannot reconnect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..core import audit
from ..core.logging import get_logger
from ..models import (
    Client,
    ClientStatus,
    Inbound,
    Node,
    NodeSample,
    ResetPolicy,
    SessionRecord,
    TrafficCounter,
    UsageSample,
    utcnow,
)
from . import node_client

log = get_logger("traffic")

#: Closing a session is best effort and happens after the commit, so it must
#: never block accounting for long.
DISCONNECT_TIMEOUT = 10.0


@dataclass(slots=True)
class SessionDelta:
    common_name: str
    up_delta: int
    down_delta: int
    billed_delta: float


@dataclass(slots=True)
class AccountingReport:
    inbounds: int = 0
    sessions: int = 0
    clients_updated: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    billed: float = 0.0
    limited: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Sessions to close on their node. Collected during accounting and applied
    #: by the caller *after* the transaction commits, so no network call ever
    #: holds the SQLite write lock.
    disconnects: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "inbounds": self.inbounds,
            "sessions": self.sessions,
            "clients_updated": self.clients_updated,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "billed": round(self.billed, 2),
            "limited": list(self.limited),
            "expired": list(self.expired),
            "errors": list(self.errors),
            "disconnected": len(self.disconnects),
        }


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def compute_delta(previous: int, current: int) -> int:
    """Byte delta for a cumulative counter.

    A decrease means the client reconnected (OpenVPN restarted the session
    counter), so everything observed so far in the new session counts.
    """
    if current < 0:
        return 0
    if current < previous:
        return current
    return current - previous


def bill(up_delta: int, down_delta: int, multiplier: float) -> float:
    """Billed traffic for a sample, honouring the client's multiplier."""
    factor = multiplier if multiplier and multiplier > 0 else 1.0
    return round((up_delta + down_delta) * factor, 2)


def next_reset_at(policy: str, reference: datetime | None = None) -> datetime | None:
    now = reference or utcnow()
    if policy == ResetPolicy.DAILY.value:
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if policy == ResetPolicy.WEEKLY.value:
        # Persian weeks start on Saturday; Python's weekday() is Monday based.
        days_ahead = (5 - now.weekday()) % 7 or 7
        target = now + timedelta(days=days_ahead)
        return target.replace(hour=0, minute=0, second=0, microsecond=0)
    if policy == ResetPolicy.MONTHLY.value:
        year, month = now.year, now.month + 1
        if month > 12:
            year, month = year + 1, 1
        return now.replace(
            year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return None


def evaluate_client(client: Client, now: datetime | None = None) -> str | None:
    """Return the status a client *should* have, or ``None`` to leave as is."""
    reference = now or utcnow()
    if not client.enabled:
        return ClientStatus.DISABLED.value
    if client.status == ClientStatus.ON_HOLD.value:
        # Held by an operator; only a manual change releases it.
        return None
    if client.expire_at:
        expires = client.expire_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= reference:
            return ClientStatus.EXPIRED.value
    if client.data_limit_bytes and client.billed_bytes >= client.data_limit_bytes:
        return ClientStatus.LIMITED.value
    return ClientStatus.ACTIVE.value


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect(session: OrmSession, *, enforce: bool = True) -> AccountingReport:
    """Poll every enabled inbound, bill the deltas and enforce limits.

    The node calls happen in a first pass that reads only: a slow or
    unreachable node must never hold the database write lock, because SQLite
    allows a single writer and the API would otherwise fail with "database is
    locked" while the accounting job waits on a socket.
    """
    report = AccountingReport()
    inbounds = session.scalars(
        select(Inbound).where(Inbound.enabled.is_(True)).order_by(Inbound.id)
    ).all()

    payloads: list[tuple[Inbound, Node, list[dict[str, object]]]] = []
    for inbound in inbounds:
        report.inbounds += 1
        node = session.get(Node, inbound.node_id)
        if node is None or not node.enabled:
            continue
        try:
            sessions = node_client.fetch_sessions(node, inbound.tag)
        except node_client.NodeUnreachable as exc:
            report.errors.append(f"{inbound.tag}: {exc}")
            continue
        payloads.append((inbound, node, sessions))

    # Every node has answered; the writes below are quick and local.
    for inbound, node, sessions in payloads:
        report.sessions += len(sessions)
        _process_inbound(session, inbound, node, sessions, report)

    if enforce:
        _enforce(session, report)
    session.flush()
    return report


def apply_disconnects(report: AccountingReport) -> int:
    """Close the sessions collected by :func:`collect`. Safe to call late."""
    closed = 0
    for item in report.disconnects:
        try:
            ok = node_client.disconnect_by_id(
                int(item["node_id"]),  # type: ignore[arg-type]
                str(item["tag"]),
                str(item["common_name"]),
                timeout=DISCONNECT_TIMEOUT,
            )
        except Exception as exc:
            log.warning("could not close %s: %s", item.get("common_name"), exc)
            continue
        closed += 1 if ok else 0
    return closed


def _process_inbound(
    session: OrmSession,
    inbound: Inbound,
    node: Node,
    sessions: list[dict[str, object]],
    report: AccountingReport,
) -> None:
    seen: set[str] = set()
    up_total = 0
    down_total = 0

    for raw in sessions:
        common_name = str(raw.get("common_name") or "").strip()
        if not common_name:
            continue
        seen.add(common_name)
        received = int(raw.get("bytes_received") or 0)
        sent = int(raw.get("bytes_sent") or 0)
        up_total += received
        down_total += sent

        client = _match_client(session, inbound.id, common_name, raw)
        if client is None:
            continue

        counter = session.scalar(
            select(TrafficCounter).where(
                TrafficCounter.node_id == node.id,
                TrafficCounter.inbound_tag == inbound.tag,
                TrafficCounter.common_name == common_name,
            )
        )
        previous_rx = counter.raw_received if counter else 0
        previous_tx = counter.raw_sent if counter else 0

        rx_delta = compute_delta(previous_rx, received)
        tx_delta = compute_delta(previous_tx, sent)

        if counter is None:
            counter = TrafficCounter(
                node_id=node.id,
                inbound_tag=inbound.tag,
                common_name=common_name,
                raw_received=received,
                raw_sent=sent,
            )
            session.add(counter)
        else:
            counter.raw_received = received
            counter.raw_sent = sent

        if rx_delta == 0 and tx_delta == 0:
            client.last_online_at = utcnow()
            continue

        billed_delta = bill(rx_delta, tx_delta, client.multiplier)
        client.up_bytes += rx_delta
        client.down_bytes += tx_delta
        client.billed_bytes = round(client.billed_bytes + billed_delta, 2)
        client.last_online_at = utcnow()

        session.add(
            UsageSample(
                client_id=client.id,
                node_id=node.id,
                up_delta=rx_delta,
                down_delta=tx_delta,
                billed_delta=billed_delta,
                multiplier=client.multiplier,
            )
        )
        report.clients_updated += 1
        report.bytes_up += rx_delta
        report.bytes_down += tx_delta
        report.billed += billed_delta

    inbound.up_bytes = up_total
    inbound.down_bytes = down_total
    inbound.total_bytes = up_total + down_total
    inbound.active_connections = len(seen)

    _sync_session_history(session, inbound, node, sessions, seen)


def _match_client(
    session: OrmSession,
    inbound_id: int,
    common_name: str,
    raw: dict[str, object],
) -> Client | None:
    client = session.scalar(
        select(Client).where(
            Client.inbound_id == inbound_id, Client.common_name == common_name
        )
    )
    if client is not None:
        return client
    # A certificate that exists on the node but not in the panel (for example
    # restored from a backup) is reported once and then ignored.
    log.warning("unmanaged session %s on inbound %s", common_name, inbound_id)
    return None


def _sync_session_history(
    session: OrmSession,
    inbound: Inbound,
    node: Node,
    sessions: list[dict[str, object]],
    seen: set[str],
) -> None:
    open_records = session.scalars(
        select(SessionRecord).where(
            SessionRecord.inbound_id == inbound.id, SessionRecord.ended_at.is_(None)
        )
    ).all()
    open_by_name = {record.common_name: record for record in open_records}

    for raw in sessions:
        common_name = str(raw.get("common_name") or "").strip()
        if not common_name:
            continue
        record = open_by_name.get(common_name)
        started = raw.get("connected_since")
        started_at = _parse_iso(started) if isinstance(started, str) else None
        if record is None:
            client = session.scalar(
                select(Client).where(
                    Client.inbound_id == inbound.id, Client.common_name == common_name
                )
            )
            session.add(
                SessionRecord(
                    client_id=client.id if client else None,
                    inbound_id=inbound.id,
                    node_id=node.id,
                    common_name=common_name,
                    real_address=str(raw.get("real_address") or "")[:64] or None,
                    virtual_address=str(raw.get("virtual_address") or "")[:64] or None,
                    started_at=started_at or utcnow(),
                )
            )
        else:
            record.bytes_received = int(raw.get("bytes_received") or 0)
            record.bytes_sent = int(raw.get("bytes_sent") or 0)

    for name, record in open_by_name.items():
        if name not in seen:
            record.ended_at = utcnow()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_aware(parsed)


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def _enforce(session: OrmSession, report: AccountingReport) -> None:
    now = utcnow()
    clients = session.scalars(select(Client).order_by(Client.id)).all()

    for client in clients:
        _apply_reset(session, client, now)

        desired = evaluate_client(client, now)
        if desired is None or desired == client.status:
            continue

        previous = client.status
        client.status = desired
        reason = "quota reached" if desired == ClientStatus.LIMITED.value else (
            "subscription expired" if desired == ClientStatus.EXPIRED.value else "disabled"
        )

        # A client that may no longer connect must not keep an open session.
        _disconnect(session, client, report)

        if desired == ClientStatus.LIMITED.value:
            report.limited.append(client.name)
        elif desired == ClientStatus.EXPIRED.value:
            report.expired.append(client.name)

        action = {
            ClientStatus.LIMITED.value: "client.limited",
            ClientStatus.EXPIRED.value: "client.expired",
            ClientStatus.DISABLED.value: "client.disabled",
        }.get(desired, "client.updated")

        if action in audit.NOTIFY_ACTIONS:
            audit.record(
                session,
                action=action,
                username="system",
                target_type="client",
                target_id=client.id,
                level="warning",
                detail={"reason": reason, "from": previous, "to": desired},
            )


def _apply_reset(session: OrmSession, client: Client, now: datetime) -> None:
    if client.reset_policy == ResetPolicy.NEVER.value:
        return
    due = _as_aware(client.reset_at)
    if due is None:
        client.reset_at = next_reset_at(client.reset_policy, now)
        return
    if due > now:
        return

    client.up_bytes = 0
    client.down_bytes = 0
    client.billed_bytes = 0.0
    client.reset_at = next_reset_at(client.reset_policy, now)
    if client.status == ClientStatus.LIMITED.value and client.enabled:
        client.status = ClientStatus.ACTIVE.value
    audit.record(
        session,
        action="client.reset",
        username="system",
        target_type="client",
        target_id=client.id,
        detail={"policy": client.reset_policy},
    )


def _disconnect(session: OrmSession, client: Client, report: AccountingReport) -> None:
    """Queue a session close; the network call happens after the commit."""
    node = session.get(Node, client.node_id)
    inbound = session.get(Inbound, client.inbound_id)
    if node is None or inbound is None:
        return
    report.disconnects.append(
        {"node_id": node.id, "tag": inbound.tag, "common_name": client.common_name}
    )


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def prune(session: OrmSession, *, sample_days: int = 30, node_sample_days: int = 14) -> dict[str, int]:
    """Drop old time-series rows so the database stays small and fast."""
    usage_cutoff = utcnow() - timedelta(days=sample_days)
    node_cutoff = utcnow() - timedelta(days=node_sample_days)

    usage = session.query(UsageSample).filter(UsageSample.ts < usage_cutoff).delete(
        synchronize_session=False
    )
    node_samples = session.query(NodeSample).filter(NodeSample.ts < node_cutoff).delete(
        synchronize_session=False
    )
    stale_counters = (
        session.query(TrafficCounter)
        .filter(TrafficCounter.updated_at < usage_cutoff)
        .delete(synchronize_session=False)
    )
    session.flush()
    result = {
        "usage_samples": int(usage or 0),
        "node_samples": int(node_samples or 0),
        "traffic_counters": int(stale_counters or 0),
    }
    if any(result.values()):
        log.info("pruned %s", result)
    return result


def usage_series(
    session: OrmSession,
    *,
    client_id: int | None = None,
    hours: int = 24,
    limit: int = 5000,
) -> list[dict[str, object]]:
    """Hourly billed totals for the charts."""
    since = utcnow() - timedelta(hours=hours)
    query = select(UsageSample).where(UsageSample.ts >= since)
    if client_id is not None:
        query = query.where(UsageSample.client_id == client_id)
    query = query.order_by(UsageSample.ts).limit(limit)

    buckets: dict[str, dict[str, float]] = {}
    for sample in session.scalars(query):
        ts = _as_aware(sample.ts)
        if ts is None:
            continue
        key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
        bucket = buckets.setdefault(key, {"up": 0.0, "down": 0.0, "billed": 0.0})
        bucket["up"] += sample.up_delta
        bucket["down"] += sample.down_delta
        bucket["billed"] += sample.billed_delta
    return [
        {"ts": key, **values}
        for key, values in sorted(buckets.items())
    ]


def top_clients(session: OrmSession, *, limit: int = 10) -> list[dict[str, object]]:
    rows = session.scalars(
        select(Client).order_by(Client.billed_bytes.desc()).limit(limit)
    ).all()
    return [
        {
            "id": client.id,
            "name": client.name,
            "common_name": client.common_name,
            "status": client.status,
            "billed_bytes": round(client.billed_bytes, 2),
            "multiplier": client.multiplier,
            "data_limit_bytes": client.data_limit_bytes,
        }
        for client in rows
    ]
