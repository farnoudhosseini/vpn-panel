/** Single node: host facts, load history, live sessions and the agent command. */

import { Link, useNavigate, useParams } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { LoadBar, Sparkline } from "../components/charts";
import {
  Button,
  CodeBlock,
  CopyButton,
  EmptyState,
  ErrorState,
  KeyValue,
  LoadingRows,
  Panel,
  StatusBadge,
  useToast,
} from "../components/ui";
import { endpoints } from "../lib/api";
import { formatBytes, formatDuration, relativeTime } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { Capability } from "../lib/types";

export default function NodeDetailPage() {
  const { id } = useParams();
  const nodeId = Number(id);
  const navigate = useNavigate();
  const { t, lang } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";

  const detail = useAsync(() => endpoints.node(nodeId), [nodeId], { pollMs: 30_000 });
  const samples = useAsync(() => endpoints.nodeSamples(nodeId, 24), [nodeId], { pollMs: 60_000 });
  const sessions = useAsync(() => endpoints.nodeSessions(nodeId), [nodeId], { pollMs: 30_000 });

  if (!Number.isFinite(nodeId)) {
    return (
      <AppShell title={t("nodes.title")}>
        <ErrorState message={t("state.error")} onRetry={() => navigate("/nodes")} />
      </AppShell>
    );
  }

  if (detail.loading && !detail.data) {
    return (
      <AppShell title={t("nodes.title")} subtitle={`#${nodeId}`}>
        <LoadingRows rows={5} />
      </AppShell>
    );
  }

  if (detail.error && !detail.data) {
    return (
      <AppShell title={t("nodes.title")}>
        <ErrorState message={detail.error} onRetry={() => void detail.reload()} />
      </AppShell>
    );
  }

  const node = detail.data;
  if (!node) return null;

  const capabilityItems: Capability[] = node.capabilities?.items ?? [];

  const history = samples.data ?? [];
  const cpuSeries = history.map((row) => row.cpu_percent);
  const memorySeries = history.map((row) => row.memory_percent);
  const trafficPeak = history.reduce(
    (peak, row) => Math.max(peak, row.net_rx_bytes + row.net_tx_bytes),
    0,
  );
  const openSessions = (sessions.data?.inbounds ?? []).flatMap((entry) =>
    entry.sessions.map((session) => ({ ...session, tag: entry.tag, error: entry.error })),
  );

  const installCommand = `curl -fsSL https://raw.githubusercontent.com/farnoudhosseini/vpn-panel/main/install.sh | sudo bash -s -- --mode node --panel ${window.location.origin}`;

  return (
    <AppShell
      title={node.name}
      subtitle={`${node.scheme}://${node.address}:${node.agent_port}`}
      actions={
        <>
          <Button size="sm" variant="ghost" onClick={() => navigate("/nodes")}>
            {t("action.back")}
          </Button>
          {canWrite && (
            <Button
              size="sm"
              variant="primary"
              onClick={async () => {
                try {
                  const result = await endpoints.testNode(node.id);
                  if (result.ok) toast.success(t("nodes.testOk"), result.message);
                  else toast.failure(t("nodes.testFailed"), result.message);
                  await detail.reload({ silent: true });
                } catch (error) {
                  toast.failure(t("nodes.testFailed"), error instanceof Error ? error.message : undefined);
                }
              }}
            >
              {t("action.test")}
            </Button>
          )}
        </>
      }
    >
      <div className="grid grid--split">
        <Panel
          title={t("nodes.capabilities")}
          actions={<StatusBadge status={node.status} />}
        >
          <div className="stack">
            <div className="stack" style={{ gap: 7 }}>
              <LoadBar label={t("nodes.cpu")} percent={node.cpu_percent} />
              <LoadBar label={t("nodes.ram")} percent={node.memory_percent} />
              <LoadBar label={t("nodes.disk")} percent={node.disk_percent} />
            </div>
            <KeyValue
              items={[
                { label: t("nodes.hostname"), value: node.capabilities?.hostname ?? "—" },
                { label: t("nodes.kernel"), value: node.capabilities?.kernel ?? node.os_info ?? "—" },
                {
                  label: t("nodes.root"),
                  value: node.capabilities?.is_root ? t("state.yes") : t("state.no"),
                },
                { label: t("common.uptime"), value: formatDuration(node.uptime_seconds, lang) },
                { label: t("common.version"), value: node.version ?? "—" },
                { label: t("nodes.lastSeen"), value: relativeTime(node.last_seen_at, lang) },
                {
                  label: t("common.status"),
                  value: (
                    <span className="row row--tight">
                      <StatusBadge status={node.status} />
                      {node.enabled ? null : <span className="badge badge--idle">{t("state.disabled")}</span>}
                    </span>
                  ),
                },
                { label: t("common.note"), value: node.note || "—" },
                {
                  label: t("nodes.tags"),
                  value: node.tags.length ? node.tags.join("، ") : "—",
                },
              ]}
            />
          </div>
        </Panel>

        <Panel title={t("nodes.capabilities")} hint={`${capabilityItems.filter((item) => item.installed).length}/${capabilityItems.length || 0}`}>
          {capabilityItems.length === 0 ? (
            <EmptyState title={t("state.unknown")} hint={t("action.test")} />
          ) : (
            <div className="stack">
              {capabilityItems.map((capability) => (
                <div
                  key={capability.key}
                  className="row"
                  style={{ justifyContent: "space-between", gap: 10 }}
                >
                  <span>
                    <span className="cell-primary" style={{ display: "block" }}>
                      {capability.key}
                    </span>
                    <span className="cell-sub">{capability.detail ?? ""}</span>
                  </span>
                  {capability.installed ? (
                    <span className="badge badge--ok">
                      {capability.version ?? t("state.active")}
                    </span>
                  ) : (
                    <span className="badge badge--idle">{t("state.none")}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="stack" style={{ marginTop: 16 }}>
            <span className="field__hint">{t("nodes.ports")}</span>
            <div className="row row--tight">
              {(node.capabilities?.ports ?? []).slice(0, 24).map((port) => (
                <span key={port} className="badge badge--idle mono">
                  {port}
                </span>
              ))}
              {(node.capabilities?.ports ?? []).length === 0 && (
                <span className="faint">{t("state.none")}</span>
              )}
            </div>
          </div>
        </Panel>
      </div>

      <div className="grid grid--halves">
        <Panel title={t("nodes.counts")}>
          <div className="row" style={{ justifyContent: "space-around" }}>
            <Link to={`/inbounds?node_id=${node.id}`} className="stack" style={{ textAlign: "center" }}>
              <span className="stat__value">{node.inbounds}</span>
              <span className="stat__label">{t("nav.inbounds")}</span>
            </Link>
            <Link to="/tunnels" className="stack" style={{ textAlign: "center" }}>
              <span className="stat__value">{node.tunnels}</span>
              <span className="stat__label">{t("nav.tunnels")}</span>
            </Link>
            <Link to="/clients" className="stack" style={{ textAlign: "center" }}>
              <span className="stat__value">{node.clients}</span>
              <span className="stat__label">{t("nav.clients")}</span>
            </Link>
          </div>
          {!node.has_tls_cert && (
            <p className="field__hint" style={{ marginTop: 14 }}>
              {t("nodes.tlsCertHint")}
            </p>
          )}
        </Panel>

        <Panel title={t("nodes.installHint")}>
          <CodeBlock content={installCommand} maxHeight={120} />
          <div className="row row--tight" style={{ marginTop: 10 }}>
            <CopyButton text={installCommand} />
          </div>
        </Panel>
      </div>

      <Panel title={t("dash.nodeLoad")} hint={`${history.length} × ${t("chart.hour")}`}>
        {history.length === 0 ? (
          <EmptyState title={t("state.empty")} hint={t("dash.noTraffic")} />
        ) : (
          <div className="stack">
            <div className="row" style={{ gap: 18 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <span className="field__hint">{t("nodes.cpu")}</span>
                <Sparkline values={cpuSeries} tone="var(--crimson)" height={34} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <span className="field__hint">{t("nodes.ram")}</span>
                <Sparkline values={memorySeries} tone="var(--jade)" height={34} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <span className="field__hint">{t("inbounds.traffic")}</span>
                <Sparkline
                  values={history.map((row) => row.net_rx_bytes + row.net_tx_bytes)}
                  tone="var(--amber)"
                  height={34}
                />
                <span className="faint" style={{ fontSize: 11.5 }}>
                  {t("chart.peak")} {formatBytes(trafficPeak)}
                </span>
              </div>
            </div>
          </div>
        )}
      </Panel>

      <Panel title={t("nodes.sessions")} flush>
        {openSessions.length === 0 ? (
          <EmptyState title={t("inbounds.noSessions")} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("inbounds.tag")}</th>
                  <th>{t("clients.commonName")}</th>
                  <th>{t("logs.ip")}</th>
                  <th>{t("inbounds.network")}</th>
                  <th>{t("chart.up")}</th>
                  <th>{t("chart.down")}</th>
                </tr>
              </thead>
              <tbody>
                {openSessions.map((session, index) => (
                  <tr key={`${session.tag}-${session.common_name}-${index}`}>
                    <td className="mono">{session.tag}</td>
                    <td>{session.common_name || "—"}</td>
                    <td className="mono" dir="ltr">
                      {session.real_address}
                    </td>
                    <td className="mono" dir="ltr">
                      {session.virtual_address}
                    </td>
                    <td className="num">{formatBytes(session.bytes_sent)}</td>
                    <td className="num">{formatBytes(session.bytes_received)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {sessions.data?.inbounds?.some((entry) => entry.error) && (
        <Panel title={t("state.error")}>
          <div className="stack">
            {(sessions.data?.inbounds ?? [])
              .filter((entry) => entry.error)
              .map((entry) => (
                <span key={entry.tag} className="cell-sub">
                  <strong className="mono">{entry.tag}</strong>: {entry.error}
                </span>
              ))}
          </div>
        </Panel>
      )}
    </AppShell>
  );
}
