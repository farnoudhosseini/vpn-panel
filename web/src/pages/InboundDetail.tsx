/** Inbound detail: tuning, decoy site / port sharing, rendered server file, sessions. */

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import {
  Button,
  CodeBlock,
  CopyButton,
  EmptyState,
  ErrorState,
  Field,
  Input,
  KeyValue,
  LoadingRows,
  Panel,
  StatusBadge,
  Tabs,
  useToast,
} from "../components/ui";
import { endpoints } from "../lib/api";
import { formatBytes, relativeTime } from "../lib/format";
import { useAsync, usePersistentState } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { InboundSettings } from "../lib/types";

type TabKey = "general" | "tuning" | "nginx" | "server" | "sessions";

interface TuningForm {
  mtu: string;
  max_clients: string;
  data_ciphers: string;
  auth_digest: string;
  tls_version_min: string;
  dns_servers: string;
  push_routes: string;
  redirect_gateway: boolean;
  static_addresses: boolean;
  duplicate_cn: boolean;
  port_share: boolean;
  port_share_target: string;
  keepalive_interval: string;
  keepalive_timeout: string;
  verbosity: string;
  compress: string;
}

function tuningFrom(settings: InboundSettings): TuningForm {
  const join = (value: unknown) =>
    Array.isArray(value) ? value.join(", ") : typeof value === "string" ? value : "";
  return {
    mtu: settings.mtu ? String(settings.mtu) : "",
    max_clients: settings.max_clients ? String(settings.max_clients) : "",
    data_ciphers: settings.data_ciphers ?? "",
    auth_digest: settings.auth_digest ?? "",
    tls_version_min: settings.tls_version_min ?? "",
    dns_servers: join(settings.dns_servers),
    push_routes: join(settings.push_routes),
    redirect_gateway: settings.redirect_gateway ?? true,
    static_addresses: settings.static_addresses ?? false,
    duplicate_cn: settings.duplicate_cn ?? false,
    port_share: settings.port_share ?? false,
    port_share_target: settings.port_share_target ?? "",
    keepalive_interval: settings.keepalive_interval ? String(settings.keepalive_interval) : "",
    keepalive_timeout: settings.keepalive_timeout ? String(settings.keepalive_timeout) : "",
    verbosity: settings.verbosity !== null && settings.verbosity !== undefined ? String(settings.verbosity) : "",
    compress: settings.compress ?? "",
  };
}

function splitList(value: string): string[] | null {
  const items = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return items.length ? items : null;
}

function numberOrNull(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export default function InboundDetailPage() {
  const { id } = useParams();
  const inboundId = Number(id);
  const navigate = useNavigate();
  const { t, lang } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";

  const [tab, setTab] = usePersistentState<TabKey>("panah.inbound.tab", "general");
  const detail = useAsync(() => endpoints.inbound(inboundId), [inboundId], { pollMs: 30_000 });
  const status = useAsync(() => endpoints.inboundStatus(inboundId), [inboundId], { pollMs: 20_000 });
  const serverFile = useAsync(
    () => (tab === "server" ? endpoints.inboundServerConfig(inboundId) : Promise.resolve(null)),
    [inboundId, tab],
  );

  const [form, setForm] = useState<TuningForm | null>(null);
  const [nginx, setNginx] = useState({
    enabled: false,
    port: "",
    domain: "",
    title: "",
  });
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!detail.data) return;
    const settings = (detail.data.effective_settings ?? detail.data.settings ?? {}) as InboundSettings;
    setForm(tuningFrom(settings));
    setNginx({
      enabled: detail.data.nginx_enabled,
      port: detail.data.nginx_port ? String(detail.data.nginx_port) : "",
      domain: detail.data.fake_site_domain ?? detail.data.domain ?? "",
      title: detail.data.fake_site_title ?? "",
    });
  }, [detail.data]);

  if (!Number.isFinite(inboundId)) {
    return (
      <AppShell title={t("inbounds.title")}>
        <ErrorState message={t("state.error")} onRetry={() => navigate("/inbounds")} />
      </AppShell>
    );
  }
  if (detail.loading && !detail.data) {
    return (
      <AppShell title={t("inbounds.title")} subtitle={`#${inboundId}`}>
        <LoadingRows rows={5} />
      </AppShell>
    );
  }
  if (detail.error && !detail.data) {
    return (
      <AppShell title={t("inbounds.title")}>
        <ErrorState message={detail.error} onRetry={() => void detail.reload()} />
      </AppShell>
    );
  }

  const inbound = detail.data;
  if (!inbound) return null;

  const save = async (payload: Record<string, unknown>) => {
    setSaving(true);
    setFormError(null);
    try {
      await endpoints.updateInbound(inbound.id, payload);
      toast.success(t("action.save"));
      await detail.reload({ silent: true });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      toast.success(label);
      await detail.reload({ silent: true });
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusy(false);
    }
  };

  const sessions = status.data?.sessions ?? [];

  return (
    <AppShell
      title={inbound.tag}
      subtitle={`${inbound.transport.toUpperCase()} :${inbound.listen_port} · ${inbound.network_cidr}`}
      actions={
        <>
          <Button size="sm" variant="ghost" onClick={() => navigate("/inbounds")}>
            {t("action.back")}
          </Button>
          {canWrite && (
            <>
              <Button
                size="sm"
                variant="primary"
                loading={busy}
                onClick={() => void run(t("action.apply"), () => endpoints.applyInbound(inbound.id))}
              >
                {t("action.apply")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                loading={busy}
                onClick={() => void run(t("action.restart"), () => endpoints.restartInbound(inbound.id))}
              >
                {t("action.restart")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                loading={busy}
                onClick={() => void run(t("inbounds.stopped"), () => endpoints.stopInbound(inbound.id))}
              >
                {t("action.stop")}
              </Button>
            </>
          )}
        </>
      }
    >
      <Panel
        title={t("inbounds.tab.general")}
        actions={
          <span className="row row--tight">
            <StatusBadge status={detail.data?.service_active ? "active" : inbound.enabled ? "active" : "disabled"} />
            {inbound.domain && (
              <span className="badge badge--idle mono" dir="ltr">
                {inbound.domain}
              </span>
            )}
          </span>
        }
      >
        <KeyValue
          items={[
            { label: t("inbounds.node"), value: inbound.node_name ?? `#${inbound.node_id}` },
            { label: t("inbounds.transport"), value: inbound.transport.toUpperCase() },
            { label: t("common.port"), value: <span className="mono" dir="ltr">{inbound.listen_port}</span> },
            { label: t("inbounds.listenHost"), value: <span className="mono" dir="ltr">{inbound.listen_host}</span> },
            { label: t("inbounds.network"), value: <span className="mono" dir="ltr">{inbound.network_cidr}</span> },
            { label: t("inbounds.clients"), value: <Link to={`/clients?inbound_id=${inbound.id}`}>{inbound.client_count}</Link> },
            { label: t("inbounds.connections"), value: inbound.active_connections },
            { label: t("inbounds.traffic"), value: `${formatBytes(inbound.up_bytes)} / ${formatBytes(inbound.down_bytes)}` },
            { label: t("inbounds.service"), value: detail.data?.service_state ?? "—" },
            { label: t("clients.certificate"), value: inbound.has_pki ? t("state.yes") : t("state.no") },
            { label: t("common.date"), value: relativeTime(inbound.created_at, lang) },
          ]}
        />
        <div className="row row--tight" style={{ marginTop: 14 }}>
          <Link to={`/clients?inbound_id=${inbound.id}`}>
            <Button size="sm">{t("clients.add")}</Button>
          </Link>
          {canWrite && (
            <Button
              size="sm"
              variant="ghost"
              loading={busy}
              onClick={() => void run(t("inbounds.rotatePki"), () => endpoints.rotatePki(inbound.id))}
            >
              {t("inbounds.rotatePki")}
            </Button>
          )}
        </div>
        {canWrite && <p className="field__hint">{t("inbounds.rotatePkiWarning")}</p>}
      </Panel>

      <Tabs<TabKey>
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "tuning", label: t("inbounds.tab.tuning") },
          { value: "nginx", label: t("inbounds.tab.nginx") },
          { value: "server", label: t("inbounds.tab.server") },
          { value: "sessions", label: t("inbounds.tab.sessions"), badge: sessions.length },
        ]}
      />

      {tab === "tuning" && form && (
        <Panel title={t("inbounds.tab.tuning")} hint={t("state.saving")}>
          <div className="form-grid">
            <Field label={t("tuning.mtu")} htmlFor="t-mtu">
              <Input id="t-mtu" dir="ltr" inputMode="numeric" value={form.mtu} onChange={(e) => setForm({ ...form, mtu: e.target.value })} />
            </Field>
            <Field label={t("tuning.maxClients")} htmlFor="t-max">
              <Input id="t-max" dir="ltr" inputMode="numeric" value={form.max_clients} onChange={(e) => setForm({ ...form, max_clients: e.target.value })} />
            </Field>
            <Field label={t("tuning.ciphers")} htmlFor="t-ciphers">
              <Input id="t-ciphers" dir="ltr" value={form.data_ciphers} onChange={(e) => setForm({ ...form, data_ciphers: e.target.value })} placeholder="AES-256-GCM:AES-128-GCM" />
            </Field>
            <Field label={t("tuning.authDigest")} htmlFor="t-auth">
              <Input id="t-auth" dir="ltr" value={form.auth_digest} onChange={(e) => setForm({ ...form, auth_digest: e.target.value })} placeholder="SHA256" />
            </Field>
            <Field label={t("tuning.tlsMin")} htmlFor="t-tls">
              <select id="t-tls" className="select" value={form.tls_version_min} onChange={(e) => setForm({ ...form, tls_version_min: e.target.value })}>
                <option value="">—</option>
                <option value="1.2">1.2</option>
                <option value="1.3">1.3</option>
              </select>
            </Field>
            <Field label={t("tuning.verbosity")} htmlFor="t-verb">
              <Input id="t-verb" dir="ltr" inputMode="numeric" value={form.verbosity} onChange={(e) => setForm({ ...form, verbosity: e.target.value })} />
            </Field>
            <Field label={t("tuning.dns")} hint={t("tuning.dnsHint")} htmlFor="t-dns">
              <Input id="t-dns" dir="ltr" value={form.dns_servers} onChange={(e) => setForm({ ...form, dns_servers: e.target.value })} placeholder="1.1.1.1, 8.8.8.8" />
            </Field>
            <Field label={t("tuning.routes")} hint={t("tuning.routesHint")} htmlFor="t-routes">
              <Input id="t-routes" dir="ltr" value={form.push_routes} onChange={(e) => setForm({ ...form, push_routes: e.target.value })} placeholder="192.168.0.0/16" />
            </Field>
            <Field label={t("tuning.keepalive")} htmlFor="t-keep">
              <Input id="t-keep" dir="ltr" inputMode="numeric" value={form.keepalive_interval} onChange={(e) => setForm({ ...form, keepalive_interval: e.target.value })} />
            </Field>
            <Field label={t("tuning.portShareTarget")} htmlFor="t-share">
              <Input id="t-share" dir="ltr" value={form.port_share_target} onChange={(e) => setForm({ ...form, port_share_target: e.target.value })} placeholder="127.0.0.1:8080" />
            </Field>
          </div>

          <div className="row" style={{ gap: 16, flexWrap: "wrap", marginTop: 12 }}>
            <label className="checkbox">
              <input type="checkbox" checked={form.redirect_gateway} onChange={(e) => setForm({ ...form, redirect_gateway: e.target.checked })} />
              <span>{t("tuning.redirect")}</span>
            </label>
            <label className="checkbox">
              <input type="checkbox" checked={form.static_addresses} onChange={(e) => setForm({ ...form, static_addresses: e.target.checked })} />
              <span>{t("tuning.staticAddresses")}</span>
            </label>
            <label className="checkbox">
              <input type="checkbox" checked={form.duplicate_cn} onChange={(e) => setForm({ ...form, duplicate_cn: e.target.checked })} />
              <span>{t("tuning.duplicateCn")}</span>
            </label>
            <label className="checkbox">
              <input type="checkbox" checked={form.port_share} onChange={(e) => setForm({ ...form, port_share: e.target.checked })} />
              <span>{t("tuning.portShare")}</span>
            </label>
          </div>

          {formError && <div className="form-error">{formError}</div>}

          <div className="row row--tight" style={{ marginTop: 16 }}>
            <Button
              variant="primary"
              loading={saving}
              disabled={!canWrite}
              onClick={() =>
                void save({
                  settings: {
                    mtu: numberOrNull(form.mtu),
                    max_clients: numberOrNull(form.max_clients),
                    data_ciphers: form.data_ciphers.trim() || null,
                    auth_digest: form.auth_digest.trim() || null,
                    tls_version_min: form.tls_version_min || null,
                    dns_servers: splitList(form.dns_servers),
                    push_routes: splitList(form.push_routes),
                    redirect_gateway: form.redirect_gateway,
                    static_addresses: form.static_addresses,
                    duplicate_cn: form.duplicate_cn,
                    port_share: form.port_share,
                    port_share_target: form.port_share_target.trim() || null,
                    keepalive_interval: numberOrNull(form.keepalive_interval),
                    keepalive_timeout: numberOrNull(form.keepalive_timeout),
                    verbosity: numberOrNull(form.verbosity),
                    compress: form.compress.trim() || null,
                  },
                })
              }
            >
              {t("action.save")}
            </Button>
            {canWrite && (
              <Button
                loading={busy}
                onClick={() =>
                  void run(t("action.apply"), () =>
                    endpoints.updateInbound(inbound.id, {}).then(() => endpoints.applyInbound(inbound.id)),
                  )
                }
              >
                {t("action.apply")}
              </Button>
            )}
          </div>
        </Panel>
      )}

      {tab === "nginx" && (
        <Panel title={t("inbounds.tab.nginx")} hint={t("inbounds.trafficNote")}>
          <div className="form-grid">
            <Field label={t("nginx.port")} hint={t("nginx.portHint")} htmlFor="n-port">
              <Input id="n-port" dir="ltr" inputMode="numeric" value={nginx.port} onChange={(e) => setNginx({ ...nginx, port: e.target.value })} />
            </Field>
            <Field label={t("nginx.domain")} htmlFor="n-domain">
              <Input id="n-domain" dir="ltr" value={nginx.domain} onChange={(e) => setNginx({ ...nginx, domain: e.target.value })} />
            </Field>
            <Field label={t("nginx.title")} htmlFor="n-title">
              <Input id="n-title" value={nginx.title} onChange={(e) => setNginx({ ...nginx, title: e.target.value })} />
            </Field>
            <Field label={t("inbounds.sni")} htmlFor="n-sni">
              <Input id="n-sni" dir="ltr" value={inbound.sni ?? ""} readOnly />
            </Field>
          </div>
          <label className="checkbox" style={{ marginTop: 8 }}>
            <input type="checkbox" checked={nginx.enabled} onChange={(e) => setNginx({ ...nginx, enabled: e.target.checked })} />
            <span>{t("nginx.enabled")}</span>
          </label>
          <p className="field__hint">{t("nginx.tip")}</p>
          <p className="field__hint">{t("nginx.requires")}</p>
          <div className="row row--tight" style={{ marginTop: 14 }}>
            <Button
              variant="primary"
              loading={saving}
              disabled={!canWrite}
              onClick={() =>
                void save({
                  nginx_enabled: nginx.enabled,
                  nginx_port: numberOrNull(nginx.port),
                  fake_site_domain: nginx.domain.trim() || null,
                  fake_site_title: nginx.title.trim() || null,
                })
              }
            >
              {t("action.save")}
            </Button>
            {canWrite && (
              <Button
                loading={busy}
                onClick={() => void run(t("nginx.apply"), () => endpoints.applyNginxFront(inbound.id))}
              >
                {t("nginx.apply")}
              </Button>
            )}
          </div>
        </Panel>
      )}

      {tab === "server" && (
        <Panel
          title={t("inbounds.serverConfig")}
          actions={
            serverFile.data ? <CopyButton text={serverFile.data.config} /> : undefined
          }
        >
          {serverFile.loading ? (
            <LoadingRows rows={3} />
          ) : serverFile.error ? (
            <ErrorState message={serverFile.error} onRetry={() => void serverFile.reload()} />
          ) : serverFile.data ? (
            <CodeBlock content={serverFile.data.config} maxHeight={520} />
          ) : (
            <EmptyState title={t("state.empty")} />
          )}
        </Panel>
      )}

      {tab === "sessions" && (
        <Panel title={t("inbounds.tab.sessions")} flush hint={`${sessions.length} ${t("dash.connected")}`}>
          {status.loading && sessions.length === 0 ? (
            <div style={{ padding: 18 }}>
              <LoadingRows rows={3} />
            </div>
          ) : sessions.length === 0 ? (
            <EmptyState title={t("inbounds.noSessions")} />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("clients.commonName")}</th>
                    <th>{t("logs.ip")}</th>
                    <th>{t("inbounds.network")}</th>
                    <th>{t("chart.up")}</th>
                    <th>{t("chart.down")}</th>
                    <th>{t("logs.time")}</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session, index) => (
                    <tr key={`${session.common_name}-${index}`}>
                      <td className="cell-primary">{session.common_name || "—"}</td>
                      <td className="mono" dir="ltr">{session.real_address}</td>
                      <td className="mono" dir="ltr">{session.virtual_address}</td>
                      <td className="num">{formatBytes(session.bytes_sent)}</td>
                      <td className="num">{formatBytes(session.bytes_received)}</td>
                      <td className="nowrap faint">{session.connected_since ? relativeTime(session.connected_since, lang) : t("state.none")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
    </AppShell>
  );
}
