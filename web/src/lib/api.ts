/**
 * API client.
 *
 * The access token lives in a module variable — never in localStorage — so a
 * cross-site scripting bug cannot read a long-lived credential. The refresh
 * token is an HttpOnly cookie the browser sends only to the auth routes, and
 * refresh is single-flight so parallel 401s do not rotate the cookie twice.
 */

import type {
  Account,
  ApiToken,
  ApiTokenCreated,
  AuditPage,
  Client,
  ClientConfig,
  ClientDetail,
  DashboardData,
  EventPage,
  Inbound,
  InboundDetail,
  LoginResponse,
  NodeDetail,
  NodeInfo,
  NodeSample,
  Overview,
  PlanSide,
  SessionView,
  SubscriptionInfo,
  SystemInfo,
  Transport,
  Tunnel,
  TunnelApplyResult,
  TunnelDetail,
  TunnelPlan,
} from "./types";
import { errorMessage } from "./format";

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(status: number, payload: unknown) {
    super(errorMessage(payload, `HTTP ${status}`));
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

const BASE = "/api";

let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

/** Notified when the session can no longer be renewed. */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

function notifyUnauthorized(): void {
  accessToken = null;
  for (const listener of unauthorizedListeners) listener();
}

async function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return false;
      const data = (await response.json()) as LoginResponse;
      accessToken = data.access_token;
      return true;
    } catch {
      return false;
    } finally {
      // Cleared on the next tick so concurrent callers share this attempt.
      setTimeout(() => {
        refreshInFlight = null;
      }, 0);
    }
  })();
  return refreshInFlight;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  /** Skip the refresh-retry cycle (used by the auth routes themselves). */
  noRetry?: boolean;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = `${BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const serialised = params.toString();
  return serialised ? `${url}?${serialised}` : url;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(buildUrl(path, options.query), {
      method: options.method ?? "GET",
      credentials: "same-origin",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  };

  let response: Response;
  try {
    response = await send();
  } catch (error) {
    throw new ApiError(0, { detail: error instanceof Error ? error.message : "network error" });
  }

  if (response.status === 401 && !options.noRetry) {
    const renewed = await refreshSession();
    if (renewed) {
      try {
        response = await send();
      } catch (error) {
        throw new ApiError(0, { detail: error instanceof Error ? error.message : "network error" });
      }
    } else {
      notifyUnauthorized();
    }
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text.slice(0, 400) };
    }
  }

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      if (response.status === 401) notifyUnauthorized();
    }
    throw new ApiError(response.status, payload);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"], signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: RequestOptions["query"]) =>
    request<T>(path, { method: "POST", body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body }),
  del: <T>(path: string, query?: RequestOptions["query"]) =>
    request<T>(path, { method: "DELETE", query }),
};

/* ------------------------------------------------------------------ */
/* Endpoint helpers                                                    */
/* ------------------------------------------------------------------ */

export const endpoints = {
  login: (username: string, password: string, totp?: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: { username, password, totp: totp || null },
      noRetry: true,
    }),
  logout: () => request<void>("/auth/logout", { method: "POST", noRetry: true }),
  me: () => api.get<Account>("/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/auth/password", {
      method: "POST",
      body: { current_password, new_password },
    }),
  totpSetup: () =>
    api.post<{ secret: string; provisioning_uri: string; qr_svg: string }>("/auth/totp/setup"),
  totpEnable: (secret: string, code: string) =>
    api.post<void>("/auth/totp/enable", { secret, code }),
  totpDisable: (password: string, code?: string) =>
    api.post<void>("/auth/totp/disable", { password, code: code || null }),
  sessions: () =>
    api.get<{ id: number; jti: string; ip: string | null; user_agent: string | null; created_at: string | null; expires_at: string | null }[]>(
      "/auth/sessions",
    ),
  revokeSessions: () => api.del<void>("/auth/sessions"),
  users: () => api.get<Account[]>("/auth/users"),
  createUser: (body: { username: string; password?: string; email?: string; role: string }) =>
    api.post<Account>("/auth/users", body),
  updateUser: (id: number, body: Record<string, unknown>) =>
    api.patch<Account>(`/auth/users/${id}`, body),
  deleteUser: (id: number) => api.del<void>(`/auth/users/${id}`),
  tokens: () => api.get<ApiToken[]>("/auth/tokens"),
  createToken: (body: { name: string; role: string; expires_in_days?: number }) =>
    api.post<ApiTokenCreated>("/auth/tokens", body),
  deleteToken: (id: number) => api.del<void>(`/auth/tokens/${id}`),

  systemInfo: () => api.get<SystemInfo>("/system/info"),
  overview: (minutes: number) => api.get<Overview>("/system/overview", { minutes }),
  dashboard: (hours: number) => api.get<DashboardData>("/system/dashboard", { hours }),
  trafficSeries: (hours: number, clientId?: number) =>
    api.get<{ hours: number; series: { ts: string; up: number; down: number; billed: number }[] }>(
      "/system/traffic/series",
      { hours, client_id: clientId },
    ),
  logs: (query: Record<string, string | number | undefined>) =>
    api.get<AuditPage>("/system/logs", query),
  events: (query: Record<string, string | number | boolean | undefined>) =>
    api.get<EventPage>("/system/events", query),
  acknowledgeEvent: (id: number) => api.post<unknown>(`/system/events/${id}/ack`),
  jobs: () => api.get<{ running: boolean; jobs: { id: string; next_run: string | null }[] }>("/system/jobs"),
  runJob: (id: string) => api.post<{ job_id: string; result: Record<string, unknown> }>(`/system/jobs/${id}/run`),
  settings: () => api.get<{ key: string; value: Record<string, unknown> }[]>("/system/settings"),
  saveSetting: (key: string, value: Record<string, unknown>) =>
    api.put<{ key: string; value: Record<string, unknown> }>(`/system/settings/${key}`, { value }),
  issueCertificate: (domain: string, email: string, staging: boolean) =>
    api.post<{ ok: boolean; domain: string; message: string }>("/system/certificates", {
      domain,
      email,
      staging,
    }),
  database: () =>
    api.get<{ tables: Record<string, number>; journal_mode: string; size_bytes: number }>(
      "/system/database",
    ),
  vacuum: () => api.post<{ ok: boolean; message: string }>("/system/database/vacuum"),

  nodes: () => api.get<NodeInfo[]>("/nodes"),
  node: (id: number) => api.get<NodeDetail>(`/nodes/${id}`),
  createNode: (body: Record<string, unknown>) => api.post<NodeInfo>("/nodes", body),
  updateNode: (id: number, body: Record<string, unknown>) =>
    api.patch<NodeInfo>(`/nodes/${id}`, body),
  deleteNode: (id: number, force: boolean) => api.del<void>(`/nodes/${id}`, { force }),
  testNode: (id: number) =>
    api.post<{ ok: boolean; message: string; capabilities?: Record<string, unknown> }>(
      `/nodes/${id}/test`,
    ),
  refreshNode: (id: number) => api.post<NodeInfo>(`/nodes/${id}/refresh`),
  nodeSamples: (id: number, hours: number) =>
    api.get<NodeSample[]>(`/nodes/${id}/samples`, { hours }),
  nodeSessions: (id: number) =>
    api.get<{ node: string; inbounds: { tag: string; sessions: SessionView[]; error: string | null }[] }>(
      `/nodes/${id}/sessions`,
    ),

  inbounds: (query?: Record<string, string | number | boolean | undefined>) =>
    api.get<Inbound[]>("/inbounds", query),
  inbound: (id: number) => api.get<InboundDetail>(`/inbounds/${id}`),
  createInbound: (body: Record<string, unknown>) => api.post<InboundDetail>("/inbounds", body),
  updateInbound: (id: number, body: Record<string, unknown>) =>
    api.patch<InboundDetail>(`/inbounds/${id}`, body),
  deleteInbound: (id: number) => api.del<void>(`/inbounds/${id}`),
  applyInbound: (id: number) => api.post<unknown>(`/inbounds/${id}/apply`),
  restartInbound: (id: number) => api.post<unknown>(`/inbounds/${id}/restart`),
  stopInbound: (id: number) => api.post<unknown>(`/inbounds/${id}/stop`),
  rotatePki: (id: number) => api.post<unknown>(`/inbounds/${id}/rotate-pki`),
  inboundServerConfig: (id: number) =>
    api.get<{ config: string }>(`/inbounds/${id}/server-config`),
  inboundStatus: (id: number) =>
    api.get<{ tag: string; sessions: SessionView[]; error?: string }>(`/inbounds/${id}/status`),
  applyNginxFront: (id: number) => api.post<unknown>(`/inbounds/${id}/nginx`),
  protocols: () =>
    api.get<{
      protocols: { key: string; label: string; transports: string[]; status: string; notes: string }[];
    }>("/inbounds/protocols"),

  clients: (query: Record<string, string | number | undefined>) =>
    api.get<Client[]>("/clients", query),
  client: (id: number) => api.get<ClientDetail>(`/clients/${id}`),
  createClient: (body: Record<string, unknown>) => api.post<ClientDetail>("/clients", body),
  updateClient: (id: number, body: Record<string, unknown>) =>
    api.patch<ClientDetail>(`/clients/${id}`, body),
  deleteClient: (id: number) => api.del<void>(`/clients/${id}`),
  resetClient: (id: number, keepExpiry: boolean) =>
    api.post<ClientDetail>(`/clients/${id}/reset`, undefined, { keep_expiry: keepExpiry }),
  disconnectClient: (id: number) =>
    api.post<{ ok: boolean; message: string }>(`/clients/${id}/disconnect`),
  rotateClientKeys: (id: number) => api.post<ClientConfig>(`/clients/${id}/rotate-keys`),
  clientConfig: (id: number) => api.get<ClientConfig>(`/clients/${id}/config`),
  clientUsage: (id: number, hours: number) =>
    api.get<{
      client: string;
      multiplier: number;
      used_bytes: number;
      data_limit_bytes: number;
      series: { ts: string; up: number; down: number; billed: number }[];
    }>(`/clients/${id}/usage`, { hours }),
  clientSessions: (id: number) =>
    api.get<
      {
        id: number;
        started_at: string;
        ended_at: string | null;
        real_address: string | null;
        virtual_address: string | null;
        bytes_received: number;
        bytes_sent: number;
      }[]
    >(`/clients/${id}/sessions`),
  clientSubscription: (id: number) => api.get<SubscriptionInfo>(`/clients/${id}/subscription`),
  bulkClients: (action: string, ids: number[]) =>
    api.post<{ action: string; affected: number }>("/clients/bulk", {
      action,
      client_ids: ids,
    }),

  tunnels: (nodeId?: number) => api.get<Tunnel[]>("/tunnels", { node_id: nodeId }),
  tunnel: (id: number) => api.get<TunnelDetail>(`/tunnels/${id}`),
  transports: () => api.get<Transport[]>("/tunnels/transports"),
  createTunnel: (body: Record<string, unknown>, apply: boolean) =>
    api.post<TunnelDetail>("/tunnels", body, { apply }),
  updateTunnel: (id: number, body: Record<string, unknown>) =>
    api.patch<TunnelDetail>(`/tunnels/${id}`, body),
  deleteTunnel: (id: number, teardown: boolean) =>
    api.del<void>(`/tunnels/${id}`, { teardown }),
  tunnelPlan: (id: number) => api.get<TunnelPlan>(`/tunnels/${id}/plan`),
  applyTunnel: (id: number) => api.post<TunnelApplyResult>(`/tunnels/${id}/apply`),
  stopTunnel: (id: number) => api.post<TunnelApplyResult>(`/tunnels/${id}/stop`),
  refreshTunnelStatus: (id: number) => api.post<TunnelDetail>(`/tunnels/${id}/status`),
  tunnelArtifacts: (id: number) =>
    api.get<{
      service: string | null;
      local: { node: string; files: string[]; units: string[] };
      remote: { node: string; files: string[]; units: string[] };
    }>(`/tunnels/${id}/artifacts`),
  backpackStatus: (nodeId?: number) =>
    api.get<{ installed: boolean; version: string | null; tunnels: unknown[] }>(
      "/tunnels/backpack/status",
      { node_id: nodeId },
    ),
  backpackLink: (link: string, name?: string, host?: string) =>
    api.post<{ ok: boolean; message: string; version: string }>("/tunnels/backpack/link", undefined, {
      link,
      name,
      host,
    }),
};

export type { PlanSide };
