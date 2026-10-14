/** API contracts. Kept in sync with `panel/api/schemas.py`. */

export type Role = "admin" | "operator" | "viewer";

export type NodeStatus = "online" | "offline" | "degraded" | "unknown";
export type ClientStatus = "active" | "disabled" | "expired" | "limited" | "on_hold";
export type TunnelStatus = "up" | "down" | "pending" | "error";
export type ResetPolicy = "never" | "daily" | "weekly" | "monthly";

export interface Account {
  id: number;
  username: string;
  email: string | null;
  role: Role;
  is_active: boolean;
  must_change_password: boolean;
  totp_enabled: boolean;
  last_login_at: string | null;
  last_login_ip: string | null;
  created_at: string | null;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  username: string;
  role: Role;
  must_change_password: boolean;
  totp_enabled: boolean;
}

export interface NodeInfo {
  id: number;
  name: string;
  address: string;
  agent_port: number;
  scheme: "http" | "https";
  is_local: boolean;
  enabled: boolean;
  insecure_tls: boolean;
  status: NodeStatus;
  version: string | null;
  os_info: string | null;
  note: string | null;
  tags: string[];
  last_seen_at: string | null;
  last_error: string | null;
  cpu_percent: number;
  memory_percent: number;
  disk_percent: number;
  uptime_seconds: number;
  openvpn_installed: boolean;
  backpack_installed: boolean;
}

export interface NodeDetail extends NodeInfo {
  capabilities: {
    items?: Capability[];
    hostname?: string;
    kernel?: string;
    is_root?: boolean;
    ports?: number[];
  };
  inbounds: number;
  tunnels: number;
  clients: number;
  has_tls_cert: boolean;
}

export interface Capability {
  key: string;
  installed: boolean;
  version?: string | null;
  detail?: string;
}

export interface NodeSample {
  ts: string;
  cpu_percent: number;
  memory_percent: number;
  disk_percent: number;
  net_rx_bytes: number;
  net_tx_bytes: number;
  connections: number;
}

export interface InboundSettings {
  mtu?: number | null;
  max_clients?: number | null;
  keepalive_interval?: number | null;
  keepalive_timeout?: number | null;
  data_ciphers?: string | null;
  data_ciphers_fallback?: string | null;
  auth_digest?: string | null;
  tls_version_min?: "1.2" | "1.3" | null;
  reneg_sec?: number | null;
  verbosity?: number | null;
  compress?: string | null;
  push_routes?: string[] | null;
  redirect_gateway?: boolean | null;
  dns_servers?: string[] | null;
  port_share?: boolean | null;
  port_share_target?: string | null;
  static_addresses?: boolean | null;
  duplicate_cn?: boolean | null;
  sndbuf?: number | null;
  rcvbuf?: number | null;
  txqueuelen?: number | null;
}

export interface Inbound {
  id: number;
  node_id: number;
  tag: string;
  protocol: string;
  transport: "udp" | "tcp";
  listen_port: number;
  listen_host: string;
  network_cidr: string;
  domain: string | null;
  enabled: boolean;
  settings: InboundSettings;
  nginx_enabled: boolean;
  nginx_port: number | null;
  sni: string | null;
  fake_site_domain: string | null;
  fake_site_title: string | null;
  up_bytes: number;
  down_bytes: number;
  total_bytes: number;
  active_connections: number;
  created_at: string | null;
}

export interface InboundDetail extends Inbound {
  node_name: string | null;
  service_active: boolean | null;
  service_state: string | null;
  client_count: number;
  has_pki: boolean;
  server_config: string | null;
  effective_settings: Record<string, unknown>;
}

export interface Client {
  id: number;
  inbound_id: number;
  node_id: number;
  name: string;
  email: string | null;
  common_name: string;
  sub_id: string;
  sub_enabled: boolean;
  status: ClientStatus;
  enabled: boolean;
  up_bytes: number;
  down_bytes: number;
  billed_bytes: number;
  data_limit_bytes: number;
  multiplier: number;
  expire_at: string | null;
  reset_policy: ResetPolicy;
  reset_at: string | null;
  ip_limit: number;
  device_limit: number;
  last_online_at: string | null;
  note: string | null;
  created_at: string | null;
}

export interface ClientDetail extends Client {
  inbound_tag: string | null;
  node_name: string | null;
  certificate_serial: string | null;
  certificate_expires_at: string | null;
  certificate_revoked: boolean;
  online: boolean;
  real_address: string | null;
  virtual_address: string | null;
  subscription_url: string | null;
}

export interface ClientConfig {
  filename: string;
  content: string;
  remote_host: string;
  remote_port: number;
  protocol: string;
}

export interface SubscriptionInfo {
  client_id: number;
  name: string;
  status: ClientStatus;
  sub_id: string;
  sub_enabled: boolean;
  used_bytes: number;
  data_limit_bytes: number;
  remaining_bytes: number | null;
  expire_at: string | null;
  days_remaining: number | null;
  subscription_url: string | null;
}

export interface ForwardedPort {
  expose: number;
  target: number | string;
  protocol: "tcp" | "udp";
}

export interface Tunnel {
  id: number;
  name: string;
  engine: "native" | "backpack";
  transport: string;
  mode: "reverse" | "direct";
  local_node_id: number;
  remote_node_id: number;
  listen_port: number;
  interface_name: string | null;
  mtu: number;
  forwarded_ports: ForwardedPort[];
  options: Record<string, unknown>;
  status: TunnelStatus;
  enabled: boolean;
  last_error: string | null;
  last_handshake_at: string | null;
  created_at: string | null;
}

export interface TunnelDetail extends Tunnel {
  local_node_name: string | null;
  remote_node_name: string | null;
  local_service_state: string | null;
  remote_service_state: string | null;
  notes: string[];
}

export interface Transport {
  key: string;
  label: string;
  engine: "native" | "backpack";
  layer: "l3" | "l4";
  encrypted: boolean;
  requirements: string[];
  notes: string;
  backpack_mode: string | null;
}

export interface PlanFile {
  path: string;
  content: string;
  mode: number;
}

export interface PlanSide {
  role: string;
  node_id: number;
  node_name: string;
  files: PlanFile[];
  units: { name: string; content: string; reload?: boolean }[];
  commands: { args: string[]; ignore_failure?: boolean }[];
  removals?: string[];
  service: string | null;
  summary: Record<string, unknown>;
}

export interface TunnelPlan {
  engine: string;
  transport: string;
  mode: string;
  notes: string[];
  local: PlanSide;
  remote: PlanSide;
}

export interface ApplyReport {
  role?: string;
  node?: string;
  ok: boolean;
  files?: { path: string; ok: boolean; error?: string }[];
  units?: { name: string; ok: boolean; error?: string }[];
  commands?: { args: string[]; ok: boolean; message?: string }[];
}

export interface TunnelApplyResult {
  ok: boolean;
  message: string;
  local: ApplyReport | null;
  remote: ApplyReport | null;
  notes: string[];
}

export interface AuditEntry {
  id: number;
  ts: string;
  username: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  level: string;
  success: boolean;
  ip: string | null;
  detail: Record<string, unknown>;
}

export interface PanelEvent {
  id: number;
  ts: string;
  level: string;
  source: string;
  message: string;
  meta: Record<string, unknown>;
  acknowledged: boolean;
}

export interface SystemInfo {
  product: string;
  version: string;
  mode: string;
  domain: string;
  uptime_seconds: number;
  python: string;
  database_path: string;
  database_size_bytes: number;
  nodes_total: number;
  nodes_online: number;
  inbounds_total: number;
  clients_total: number;
  clients_active: number;
  tunnels_total: number;
  tunnels_up: number;
  bandwidth_in_bytes: number;
  bandwidth_out_bytes: number;
  billed_bytes: number;
  scheduler: { running: boolean; jobs: { id: string; next_run: string | null }[] };
  capabilities: {
    hostname?: string;
    platform?: string;
    kernel?: string;
    is_root?: boolean;
    capabilities?: Capability[];
  };
  last_accounting: Record<string, unknown>;
}

export interface OverviewNode {
  id: number;
  name: string;
  status: NodeStatus;
  is_local: boolean;
  enabled: boolean;
  address: string;
  cpu: number;
  memory: number;
  disk: number;
  connections: number;
  series: number[];
  throughput: number;
}

export interface Overview {
  ts: string;
  nodes: OverviewNode[];
  scheduler: boolean;
}

export interface DashboardData {
  nodes: {
    id: number;
    name: string;
    status: NodeStatus;
    is_local: boolean;
    cpu: number;
    memory: number;
    disk: number;
    address: string;
  }[];
  node_series: Record<
    string,
    { ts: string; cpu: number; memory: number; disk: number; rx: number; tx: number; connections: number }[]
  >;
  traffic: { ts: string; up: number; down: number; billed: number }[];
  top_clients: {
    id: number;
    name: string;
    common_name: string;
    status: ClientStatus;
    billed_bytes: number;
    multiplier: number;
    data_limit_bytes: number;
  }[];
  events: {
    id: number;
    ts: string;
    level: string;
    source: string;
    message: string;
    acknowledged: boolean;
  }[];
}

export interface SessionView {
  common_name: string;
  real_address: string;
  virtual_address: string;
  bytes_received: number;
  bytes_sent: number;
  connected_since: string | null;
  username: string;
  client_id: string;
}

export interface AuditPage {
  total: number;
  limit: number;
  offset: number;
  items: AuditEntry[];
}

export interface EventPage {
  total: number;
  limit: number;
  offset: number;
  items: PanelEvent[];
}

export interface ApiToken {
  id: number;
  name: string;
  role: Role;
  scopes: string[];
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  is_active: boolean;
}

export interface ApiTokenCreated extends ApiToken {
  token: string;
}

export interface Paged<T> {
  total: number;
  limit: number;
  offset: number;
  items: T[];
}
