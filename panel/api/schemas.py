"""Request and response models.

Validation happens here rather than in the handlers so every entry point gets
the same bounds, and so the OpenAPI document is accurate.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Protocol = Literal["openvpn"]
Transport = Literal["udp", "tcp"]
RoleName = Literal["admin", "operator", "viewer"]
ClientStatusName = Literal["active", "disabled", "expired", "limited", "on_hold"]
ResetPolicyName = Literal["never", "daily", "weekly", "monthly"]
TunnelMode = Literal["reverse", "direct"]

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _validate_host(value: str | None, *, allow_any: bool = False) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if allow_any and candidate in {"_", "*", "any"}:
        return candidate
    if _HOSTNAME_RE.match(candidate):
        return candidate.lower()
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError as exc:
        raise ValueError(f"invalid host {value!r}") from exc


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=16)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    username: str
    role: str
    must_change_password: bool = False
    totp_enabled: bool = False


class AccountOut(ORMModel):
    id: int
    username: str
    email: str | None = None
    role: str
    is_active: bool
    must_change_password: bool
    totp_enabled: bool
    last_login_at: datetime | None = None
    last_login_ip: str | None = None
    created_at: datetime | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class TotpEnableRequest(BaseModel):
    secret: str = Field(min_length=16, max_length=64)
    code: str = Field(min_length=6, max_length=10)


class TotpDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str | None = Field(default=None, max_length=10)


class TotpSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str
    qr_svg: str


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str | None = Field(default=None, min_length=10, max_length=256)
    email: str | None = Field(default=None, max_length=255)
    role: RoleName = "viewer"


class UserUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=255)
    role: RoleName | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    role: RoleName = "viewer"
    scopes: list[str] = Field(default_factory=list, max_length=32)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiTokenOut(BaseModel):
    id: int
    name: str
    role: str
    scopes: list[str]
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool


class ApiTokenCreated(ApiTokenOut):
    token: str


class SessionOut(BaseModel):
    id: int
    jti: str
    ip: str | None = None
    user_agent: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


class NodeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9._ -]+$")
    address: str = Field(min_length=1, max_length=255)
    agent_port: int = Field(default=8787, ge=1, le=65535)
    api_key: str | None = Field(default=None, max_length=256)
    is_local: bool = False
    insecure_tls: bool = False
    scheme: Literal["http", "https"] = "https"
    note: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list, max_length=16)
    tls_cert: str | None = None

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        validated = _validate_host(value, allow_any=True)
        if not validated:
            raise ValueError("address is required")
        host, sep, port = validated.rpartition(":")
        if sep and port.isdigit():
            return host
        return validated

    @field_validator("tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip()[:24] for v in values if v.strip()]
        return cleaned[:16]


class NodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    address: str | None = Field(default=None, max_length=255)
    agent_port: int | None = Field(default=None, ge=1, le=65535)
    api_key: str | None = Field(default=None, max_length=256)
    enabled: bool | None = None
    insecure_tls: bool | None = None
    scheme: Literal["http", "https"] | None = None
    note: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = Field(default=None, max_length=16)
    tls_cert: str | None = None


class NodeOut(ORMModel):
    id: int
    name: str
    address: str
    agent_port: int
    scheme: str
    is_local: bool
    enabled: bool
    insecure_tls: bool
    status: str
    version: str | None = None
    os_info: str | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    last_error: str | None = None
    cpu_percent: float = 0
    memory_percent: float = 0
    disk_percent: float = 0
    uptime_seconds: int = 0
    openvpn_installed: bool = False
    backpack_installed: bool = False


class NodeDetail(NodeOut):
    capabilities: dict[str, Any] = Field(default_factory=dict)
    inbounds: int = 0
    tunnels: int = 0
    clients: int = 0
    has_tls_cert: bool = False


class NodeSampleOut(ORMModel):
    ts: datetime
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    net_rx_bytes: int
    net_tx_bytes: int
    connections: int


class NodeTestResult(BaseModel):
    ok: bool
    message: str
    capabilities: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Inbounds
# ---------------------------------------------------------------------------


class InboundSettings(BaseModel):
    mtu: int | None = Field(default=None, ge=576, le=1500)
    max_clients: int | None = Field(default=None, ge=1, le=10000)
    keepalive_interval: int | None = Field(default=None, ge=5, le=120)
    keepalive_timeout: int | None = Field(default=None, ge=10, le=600)
    data_ciphers: str | None = Field(default=None, max_length=255)
    data_ciphers_fallback: str | None = Field(default=None, max_length=64)
    auth_digest: str | None = Field(default=None, max_length=16)
    tls_version_min: Literal["1.2", "1.3"] | None = None
    reneg_sec: int | None = Field(default=None, ge=0, le=86400)
    verbosity: int | None = Field(default=None, ge=0, le=6)
    compress: str | None = Field(default=None, max_length=16)
    push_routes: list[str] | str | None = None
    redirect_gateway: bool | None = None
    dns_servers: list[str] | str | None = None
    port_share: bool | None = None
    port_share_target: str | None = Field(default=None, max_length=64)
    static_addresses: bool | None = None
    duplicate_cn: bool | None = None
    sndbuf: int | None = Field(default=None, ge=0, le=8 * 1024 * 1024)
    rcvbuf: int | None = Field(default=None, ge=0, le=8 * 1024 * 1024)
    txqueuelen: int | None = Field(default=None, ge=0, le=20000)


class InboundCreate(BaseModel):
    tag: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    node_id: int = Field(ge=1)
    protocol: Protocol = "openvpn"
    transport: Transport = "udp"
    listen_port: int = Field(ge=1, le=65535)
    listen_host: str = Field(default="0.0.0.0", max_length=64)
    network_cidr: str = Field(default="10.8.0.0/24", max_length=48)
    domain: str | None = Field(default=None, max_length=255)
    enabled: bool = True
    settings: InboundSettings = Field(default_factory=InboundSettings)
    nginx_enabled: bool = False
    nginx_port: int | None = Field(default=None, ge=1, le=65535)
    sni: str | None = Field(default=None, max_length=255)
    fake_site_domain: str | None = Field(default=None, max_length=255)
    fake_site_title: str | None = Field(default=None, max_length=160)

    @field_validator("domain", "sni", "fake_site_domain")
    @classmethod
    def _hosts(cls, value: str | None) -> str | None:
        return _validate_host(value)

    @field_validator("network_cidr")
    @classmethod
    def _network(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid network {value!r}") from exc
        if network.version != 4:
            raise ValueError("only IPv4 networks are supported for OpenVPN pools")
        if network.prefixlen > 30:
            raise ValueError("network is too small to assign client addresses")
        return str(network)

    @field_validator("listen_host")
    @classmethod
    def _listen(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"invalid listen address {value!r}") from exc
        return value


class InboundUpdate(BaseModel):
    transport: Transport | None = None
    listen_port: int | None = Field(default=None, ge=1, le=65535)
    listen_host: str | None = Field(default=None, max_length=64)
    network_cidr: str | None = Field(default=None, max_length=48)
    domain: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None
    settings: InboundSettings | None = None
    nginx_enabled: bool | None = None
    nginx_port: int | None = Field(default=None, ge=1, le=65535)
    sni: str | None = Field(default=None, max_length=255)
    fake_site_domain: str | None = Field(default=None, max_length=255)
    fake_site_title: str | None = Field(default=None, max_length=160)


class InboundOut(ORMModel):
    id: int
    node_id: int
    tag: str
    protocol: str
    transport: str
    listen_port: int
    listen_host: str
    network_cidr: str
    domain: str | None = None
    enabled: bool
    settings: dict[str, Any] = Field(default_factory=dict)
    nginx_enabled: bool = False
    nginx_port: int | None = None
    sni: str | None = None
    fake_site_domain: str | None = None
    fake_site_title: str | None = None
    up_bytes: int = 0
    down_bytes: int = 0
    total_bytes: int = 0
    active_connections: int = 0
    created_at: datetime | None = None


class InboundDetail(InboundOut):
    node_name: str | None = None
    service_active: bool | None = None
    service_state: str | None = None
    client_count: int = 0
    has_pki: bool = False
    server_config: str | None = None
    effective_settings: dict[str, Any] = Field(default_factory=dict)


class InboundApplyResult(BaseModel):
    ok: bool
    message: str
    steps: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class ClientCreate(BaseModel):
    inbound_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=160)
    common_name: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]*$")
    data_limit_bytes: int = Field(default=0, ge=0, le=10**14)
    multiplier: float = Field(default=1.0, ge=0.1, le=100.0)
    expire_at: datetime | None = None
    reset_policy: ResetPolicyName = "never"
    ip_limit: int = Field(default=0, ge=0, le=1000)
    device_limit: int = Field(default=0, ge=0, le=1000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)
    sub_enabled: bool = True


class ClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=160)
    data_limit_bytes: int | None = Field(default=None, ge=0, le=10**14)
    multiplier: float | None = Field(default=None, ge=0.1, le=100.0)
    expire_at: datetime | None = None
    reset_policy: ResetPolicyName | None = None
    ip_limit: int | None = Field(default=None, ge=0, le=1000)
    device_limit: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool | None = None
    status: ClientStatusName | None = None
    note: str | None = Field(default=None, max_length=255)
    sub_enabled: bool | None = None


class ClientOut(ORMModel):
    id: int
    inbound_id: int
    node_id: int
    name: str
    email: str | None = None
    common_name: str
    sub_id: str
    sub_enabled: bool
    status: str
    enabled: bool
    up_bytes: int = 0
    down_bytes: int = 0
    billed_bytes: float = 0
    data_limit_bytes: int = 0
    multiplier: float = 1.0
    expire_at: datetime | None = None
    reset_policy: str = "never"
    reset_at: datetime | None = None
    ip_limit: int = 0
    device_limit: int = 0
    last_online_at: datetime | None = None
    note: str | None = None
    created_at: datetime | None = None


class ClientDetail(ClientOut):
    inbound_tag: str | None = None
    node_name: str | None = None
    certificate_serial: str | None = None
    certificate_expires_at: datetime | None = None
    certificate_revoked: bool = False
    online: bool = False
    real_address: str | None = None
    virtual_address: str | None = None
    subscription_url: str | None = None


class ClientBulkAction(BaseModel):
    action: Literal["enable", "disable", "reset", "disconnect", "delete"]
    client_ids: list[int] = Field(min_length=1, max_length=500)


class ClientResetRequest(BaseModel):
    keep_expiry: bool = True


class ClientConfigOut(BaseModel):
    filename: str
    content: str
    remote_host: str
    remote_port: int
    protocol: str


class SubscriptionInfoOut(BaseModel):
    client_id: int
    name: str
    status: str
    sub_id: str
    sub_enabled: bool
    used_bytes: float
    data_limit_bytes: int
    remaining_bytes: int | None = None
    expire_at: datetime | None = None
    days_remaining: int | None = None
    subscription_url: str | None = None


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------


class ForwardedPort(BaseModel):
    expose: int = Field(ge=1, le=65535)
    target: int | str
    protocol: Literal["tcp", "udp"] = "tcp"

    @field_validator("target")
    @classmethod
    def _target(cls, value: int | str) -> int | str:
        if isinstance(value, int):
            if not 1 <= value <= 65535:
                raise ValueError("target port out of range")
            return value
        candidate = value.strip()
        if re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}:\d{1,5}", candidate):
            return candidate
        if candidate.isdigit() and 1 <= int(candidate) <= 65535:
            return int(candidate)
        raise ValueError("target must be a port or host:port")


class TunnelCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9._ -]+$")
    transport: str = Field(min_length=1, max_length=24)
    mode: TunnelMode = "reverse"
    local_node_id: int = Field(ge=1)
    remote_node_id: int = Field(ge=1)
    listen_port: int = Field(ge=1, le=65535)
    interface_name: str | None = Field(default=None, max_length=15)
    mtu: int = Field(default=1420, ge=576, le=1500)
    forwarded_ports: list[ForwardedPort] = Field(default_factory=list, max_length=64)
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TunnelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    transport: str | None = Field(default=None, min_length=1, max_length=24)
    mode: TunnelMode | None = None
    listen_port: int | None = Field(default=None, ge=1, le=65535)
    interface_name: str | None = Field(default=None, max_length=15)
    mtu: int | None = Field(default=None, ge=576, le=1500)
    forwarded_ports: list[ForwardedPort] | None = Field(default=None, max_length=64)
    options: dict[str, Any] | None = None
    enabled: bool | None = None


class TunnelOut(ORMModel):
    id: int
    name: str
    engine: str
    transport: str
    mode: str
    local_node_id: int
    remote_node_id: int
    listen_port: int
    interface_name: str | None = None
    mtu: int = 1420
    forwarded_ports: list[dict[str, Any]] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    status: str
    enabled: bool
    last_error: str | None = None
    last_handshake_at: datetime | None = None
    created_at: datetime | None = None


class TunnelDetail(TunnelOut):
    local_node_name: str | None = None
    remote_node_name: str | None = None
    local_service_state: str | None = None
    remote_service_state: str | None = None
    notes: list[str] = Field(default_factory=list)


class TunnelPlanOut(BaseModel):
    engine: str
    transport: str
    mode: str
    notes: list[str] = Field(default_factory=list)
    local: dict[str, Any]
    remote: dict[str, Any]


class TunnelApplyResult(BaseModel):
    ok: bool
    message: str
    local: dict[str, Any] | None = None
    remote: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


class TransportOut(BaseModel):
    key: str
    label: str
    engine: str
    layer: str
    encrypted: bool
    requirements: list[str] = Field(default_factory=list)
    notes: str = ""
    backpack_mode: str | None = None


# ---------------------------------------------------------------------------
# System, logs and settings
# ---------------------------------------------------------------------------


class SystemInfoOut(BaseModel):
    product: str
    version: str
    mode: str
    domain: str
    uptime_seconds: int
    python: str
    database_path: str
    database_size_bytes: int
    nodes_total: int
    nodes_online: int
    inbounds_total: int
    clients_total: int
    clients_active: int
    tunnels_total: int
    tunnels_up: int
    bandwidth_in_bytes: int
    bandwidth_out_bytes: int
    billed_bytes: float
    scheduler: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    last_accounting: dict[str, Any] = Field(default_factory=dict)


class SettingOut(BaseModel):
    key: str
    value: dict[str, Any]


class SettingUpdate(BaseModel):
    value: dict[str, Any]


class AuditOut(ORMModel):
    id: int
    ts: datetime
    username: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    level: str
    success: bool
    ip: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EventOut(ORMModel):
    id: int
    ts: datetime
    level: str
    source: str
    message: str
    meta: dict[str, Any] = Field(default_factory=dict)
    acknowledged: bool


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class AuditPage(Page):
    items: list[AuditOut]


class EventPage(Page):
    items: list[EventOut]


class CertificateRequest(BaseModel):
    domain: str = Field(min_length=4, max_length=255)
    email: str = Field(min_length=5, max_length=255)
    staging: bool = False


class CertificateResult(BaseModel):
    ok: bool
    domain: str
    message: str
    certificate: dict[str, Any] = Field(default_factory=dict)


class JobRunResult(BaseModel):
    job_id: str
    result: dict[str, Any]


class BackupOut(BaseModel):
    filename: str
    created_at: datetime
    size_bytes: int
