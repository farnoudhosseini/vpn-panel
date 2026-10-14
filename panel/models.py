"""ORM models.

The schema mirrors the concepts the panel exposes: nodes, tunnels between
nodes, inbounds that listen on a node, and clients that belong to an inbound
with their own quota, multiplier and subscription identity.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Enumerations (stored as text so migrations stay trivial)
# ---------------------------------------------------------------------------


class NodeStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ClientStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    LIMITED = "limited"
    ON_HOLD = "on_hold"


class TunnelStatus(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    PENDING = "pending"
    ERROR = "error"


class ResetPolicy(str, enum.Enum):
    NEVER = "never"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Role(str, enum.Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.ADMIN.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    totp_secret: Mapped[str | None] = mapped_column(String(255))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def is_locked(self, now: datetime | None = None) -> bool:
        if not self.locked_until:
            return False
        reference = now or utcnow()
        locked = self.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=UTC)
        return locked > reference


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="tokens")


class ApiToken(Base, TimestampMixin):
    """Long-lived machine token for automation and subscriptions."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.VIEWER.value)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


class Node(Base, TimestampMixin):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    address: Mapped[str] = mapped_column(String(255))
    agent_port: Mapped[int] = mapped_column(Integer, default=8787)
    api_key_enc: Mapped[str] = mapped_column(Text, default="")

    # The panel host doubles as a node so inbounds can run locally.
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # When true the agent certificate is not verified (useful for private IPs).
    insecure_tls: Mapped[bool] = mapped_column(Boolean, default=False)
    # "https" for a direct agent, "http" when a reverse proxy terminates TLS.
    scheme: Mapped[str] = mapped_column(String(8), default="https")
    # PEM of the agent's self-signed certificate, used to pin the node identity.
    tls_cert: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default=NodeStatus.UNKNOWN.value)
    version: Mapped[str | None] = mapped_column(String(32))
    os_info: Mapped[str | None] = mapped_column(String(160))
    note: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    # Last observed host snapshot (cheap reads for the dashboard).
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    memory_percent: Mapped[float] = mapped_column(Float, default=0.0)
    disk_percent: Mapped[float] = mapped_column(Float, default=0.0)
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    openvpn_installed: Mapped[bool] = mapped_column(Boolean, default=False)
    backpack_installed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cached capability payload so the UI can render without a round trip.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_nodes_status", "status"),)


class NodeSample(Base):
    """Time series used by the dashboard charts (pruned periodically)."""

    __tablename__ = "node_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    memory_percent: Mapped[float] = mapped_column(Float, default=0.0)
    disk_percent: Mapped[float] = mapped_column(Float, default=0.0)
    net_rx_bytes: Mapped[int] = mapped_column(Integer, default=0)
    net_tx_bytes: Mapped[int] = mapped_column(Integer, default=0)
    connections: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------


class Tunnel(Base, TimestampMixin):
    """A link between two nodes, created from the blueprint editor."""

    __tablename__ = "tunnels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    engine: Mapped[str] = mapped_column(String(16), default="native")
    transport: Mapped[str] = mapped_column(String(24), default="tcp")
    mode: Mapped[str] = mapped_column(String(16), default="reverse")
    carrier: Mapped[str | None] = mapped_column(String(24))

    local_node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    remote_node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))

    listen_port: Mapped[int] = mapped_column(Integer)
    remote_address: Mapped[str | None] = mapped_column(String(255))
    interface_name: Mapped[str | None] = mapped_column(String(24))
    mtu: Mapped[int] = mapped_column(Integer, default=1420)

    token_enc: Mapped[str] = mapped_column(Text, default="")
    forwarded_ports: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(16), default=TunnelStatus.PENDING.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_handshake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rx_bytes: Mapped[int] = mapped_column(Integer, default=0)
    tx_bytes: Mapped[int] = mapped_column(Integer, default=0)

    local_node: Mapped[Node] = relationship(foreign_keys=[local_node_id])
    remote_node: Mapped[Node] = relationship(foreign_keys=[remote_node_id])


# ---------------------------------------------------------------------------
# Inbounds
# ---------------------------------------------------------------------------


class Inbound(Base, TimestampMixin):
    __tablename__ = "inbounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    tag: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    protocol: Mapped[str] = mapped_column(String(24), default="openvpn")
    transport: Mapped[str] = mapped_column(String(8), default="udp")
    listen_port: Mapped[int] = mapped_column(Integer)
    network_cidr: Mapped[str] = mapped_column(String(48), default="10.8.0.0/24")
    domain: Mapped[str | None] = mapped_column(String(255))
    listen_host: Mapped[str] = mapped_column(String(64), default="0.0.0.0")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Nginx front: several inbounds can share one port with a fake website.
    nginx_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    nginx_port: Mapped[int | None] = mapped_column(Integer)
    sni: Mapped[str | None] = mapped_column(String(255))
    fake_site_domain: Mapped[str | None] = mapped_column(String(255))
    fake_site_title: Mapped[str | None] = mapped_column(String(160))

    # Aggregate counters (kept in sync by the accounting task).
    up_bytes: Mapped[int] = mapped_column(Integer, default=0)
    down_bytes: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    active_connections: Mapped[int] = mapped_column(Integer, default=0)

    node: Mapped[Node] = relationship()
    clients: Mapped[list[Client]] = relationship(
        back_populates="inbound", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("node_id", "listen_port", "protocol", name="uq_inbound_bind"),
    )


class PkiMaterial(Base, TimestampMixin):
    """Certificate authority and server material owned by an inbound."""

    __tablename__ = "pki_material"

    id: Mapped[int] = mapped_column(primary_key=True)
    inbound_id: Mapped[int] = mapped_column(
        ForeignKey("inbounds.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("inbound_id", "kind", name="uq_pki_kind"),)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class Client(Base, TimestampMixin):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    inbound_id: Mapped[int] = mapped_column(
        ForeignKey("inbounds.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(String(80), index=True)
    email: Mapped[str | None] = mapped_column(String(160))
    # OpenVPN certificate common name — must be unique per inbound.
    common_name: Mapped[str] = mapped_column(String(80), index=True)
    password_enc: Mapped[str | None] = mapped_column(Text)

    sub_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sub_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(16), default=ClientStatus.ACTIVE.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    up_bytes: Mapped[int] = mapped_column(Integer, default=0)
    down_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # Counter of billed traffic (already multiplied); drives quota checks.
    billed_bytes: Mapped[float] = mapped_column(Float, default=0.0)
    data_limit_bytes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = unlimited
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_policy: Mapped[str] = mapped_column(String(16), default=ResetPolicy.NEVER.value)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ip_limit: Mapped[int] = mapped_column(Integer, default=0)
    device_limit: Mapped[int] = mapped_column(Integer, default=0)

    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(255))

    inbound: Mapped[Inbound] = relationship(back_populates="clients")

    __table_args__ = (
        UniqueConstraint("inbound_id", "common_name", name="uq_client_cn"),
        Index("ix_clients_status", "status"),
    )

    @property
    def used_bytes(self) -> float:
        """Billed usage, honouring the per-client multiplier."""
        return round(self.billed_bytes, 2)

    @property
    def remaining_bytes(self) -> int | None:
        if not self.data_limit_bytes:
            return None
        return max(0, int(self.data_limit_bytes - self.used_bytes))


class ClientCredential(Base, TimestampMixin):
    """Per-client material such as an OpenVPN certificate/key pair."""

    __tablename__ = "client_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    serial: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("client_id", "kind", name="uq_client_cred_kind"),)


class UsageSample(Base):
    """Raw accounting deltas; aggregated for charts and pruned on a schedule."""

    __tablename__ = "usage_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    up_delta: Mapped[int] = mapped_column(Integer, default=0)
    down_delta: Mapped[int] = mapped_column(Integer, default=0)
    billed_delta: Mapped[float] = mapped_column(Float, default=0.0)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)


class TrafficCounter(Base):
    """Last raw counter seen per live session.

    OpenVPN reports cumulative bytes for the life of a session and resets them
    when a client reconnects, so deltas are computed against this row and a
    decrease is treated as a new session rather than negative traffic.
    """

    __tablename__ = "traffic_counters"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(Integer, index=True)
    inbound_tag: Mapped[str] = mapped_column(String(80), index=True)
    common_name: Mapped[str] = mapped_column(String(80), index=True)
    raw_received: Mapped[int] = mapped_column(Integer, default=0)
    raw_sent: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("node_id", "inbound_tag", "common_name", name="uq_counter_session"),
    )


class SessionRecord(Base):
    """OpenVPN session history for the connection log view."""

    __tablename__ = "session_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), index=True
    )
    inbound_id: Mapped[int | None] = mapped_column(Integer, index=True)
    node_id: Mapped[int | None] = mapped_column(Integer, index=True)
    common_name: Mapped[str] = mapped_column(String(80), index=True)
    real_address: Mapped[str | None] = mapped_column(String(64))
    virtual_address: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bytes_received: Mapped[int] = mapped_column(Integer, default=0)
    bytes_sent: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Platform state
# ---------------------------------------------------------------------------


class SystemSetting(Base, TimestampMixin):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Event(Base):
    """Operator-facing notifications (node down, quota hit, tunnel error)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    source: Mapped[str] = mapped_column(String(32), default="panel")
    message: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


__all__ = [
    "ApiToken",
    "AuditLog",
    "Base",
    "Client",
    "ClientCredential",
    "ClientStatus",
    "Event",
    "Inbound",
    "Node",
    "NodeSample",
    "NodeStatus",
    "PkiMaterial",
    "RefreshToken",
    "ResetPolicy",
    "Role",
    "SessionRecord",
    "SystemSetting",
    "TrafficCounter",
    "Tunnel",
    "TunnelStatus",
    "UsageSample",
    "User",
    "utcnow",
]
