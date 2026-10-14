/** Dashboard: the state of the fleet at a glance. */

import { Link } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { TrafficChart } from "../components/charts";
import {
  IconAlert,
  IconArrowDown,
  IconArrowUp,
  IconInbound,
  IconPlus,
  IconServer,
  IconTunnel,
  IconUsers,
  IconZap,
} from "../components/Icons";
import {
  Button,
  EmptyState,
  ErrorState,
  LoadingRows,
  Panel,
  Stat,
  StatusBadge,
} from "../components/ui";
import { endpoints } from "../lib/api";
import { formatBytes, formatDuration, relativeTime, usageRatio, meterTone } from "../lib/format";
import { useAsync, useTicker } from "../lib/hooks";
import { useI18n } from "../lib/i18n";

function eventTone(level: string): "ok" | "warn" | "bad" | "idle" {
  if (level === "error" || level === "critical") return "bad";
  if (level === "warning") return "warn";
  return "idle";
}

export default function DashboardPage() {
  const { t, lang } = useI18n();
  useTicker(30_000);
  const { data, error, loading, reload } = useAsync(() => endpoints.systemInfo(), [], {
    pollMs: 20_000,
  });
  const dashboard = useAsync(() => endpoints.dashboard(24), [], { pollMs: 60_000 });

  if (loading && !data) {
    return (
      <AppShell title={t("dash.title")} subtitle={t("dash.subtitle")}>
        <LoadingRows rows={5} />
      </AppShell>
    );
  }
  if (error && !data) {
    return (
      <AppShell title={t("dash.title")}>
        <ErrorState message={error} onRetry={() => void reload()} />
      </AppShell>
    );
  }
  if (!data) return null;

  const nodes = dashboard.data?.nodes ?? [];
  const events = dashboard.data?.events ?? [];
  const topClients = dashboard.data?.top_clients ?? [];
  const traffic = dashboard.data?.traffic ?? [];

  const lastRun = data.last_accounting ?? {};
  const runTime = typeof lastRun === "object" && lastRun && "inbounds" in lastRun ? lastRun : null;

  return (
    <AppShell
      title={t("dash.title")}
      subtitle={t("dash.subtitle")}
      actions={
        <>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              void reload({ silent: true });
              void dashboard.reload({ silent: true });
            }}
            loading={dashboard.refreshing}
          >
            {t("action.refresh")}
          </Button>
          <Link to="/inbounds">
            <Button size="sm" variant="primary" icon={<IconPlus size={15} />}>
              {t("inbounds.add")}
            </Button>
          </Link>
        </>
      }
    >
      <div className="grid grid--stats">
        <Stat
          label={t("dash.nodesOnline")}
          value={data.nodes_online}
          unit={`/ ${data.nodes_total}`}
          foot={
            <span className="row row--tight">
              <IconServer size={14} /> {t("nav.nodes")}
            </span>
          }
        />
        <Stat
          label={t("dash.bandwidthIn")}
          value={formatBytes(data.bandwidth_in_bytes)}
          foot={
            <span className="row row--tight">
              <IconArrowDown size={14} style={{ color: "var(--jade)" }} /> {t("chart.down")}
            </span>
          }
        />
        <Stat
          label={t("dash.bandwidthOut")}
          value={formatBytes(data.bandwidth_out_bytes)}
          foot={
            <span className="row row--tight">
              <IconArrowUp size={14} style={{ color: "var(--crimson)" }} /> {t("chart.up")}
            </span>
          }
        />
        <Stat
          label={t("dash.billedTraffic")}
          value={formatBytes(data.billed_bytes)}
          foot={
            <span className="row row--tight">
              <IconZap size={14} /> {t("clients.multiplier")}
            </span>
          }
        />
        <Stat
          label={t("dash.clients")}
          value={data.clients_total}
          unit={`${data.clients_active} ${t("dash.clientsActive")}`}
          foot={
            <span className="row row--tight">
              <IconUsers size={14} /> {t("clients.subscription")}
            </span>
          }
        />
        <Stat
          label={t("dash.inbounds")}
          value={data.inbounds_total}
          foot={
            <span className="row row--tight">
              <IconInbound size={14} /> OpenVPN
            </span>
          }
        />
        <Stat
          label={t("dash.tunnels")}
          value={`${data.tunnels_up} / ${data.tunnels_total}`}
          foot={
            <span className="row row--tight">
              <IconTunnel size={14} /> {t("dash.tunnelsUp")}
            </span>
          }
        />
        <Stat
          label={t("dash.uptime")}
          value={formatDuration(data.uptime_seconds, lang)}
          foot={
            <span className="row row--tight">
              <IconAlert size={14} /> {runTime ? t("dash.lastAccounting") : t("state.ready")}
            </span>
          }
        />
      </div>

      <div className="grid grid--split">
        <Panel title={t("dash.trafficChart")} hint={`${formatBytes(data.billed_bytes)} ${t("common.total")}`}>
          <TrafficChart series={traffic} emptyLabel={t("dash.noTraffic")} />
        </Panel>

        <Panel title={t("dash.nodeLoad")}>
          {nodes.length === 0 ? (
            <EmptyState
              title={t("dash.noNodes")}
              hint={t("nodes.emptyHint")}
              action={
                <Link to="/nodes">
                  <Button size="sm">{t("dash.noNodesAction")}</Button>
                </Link>
              }
            />
          ) : (
            <div className="stack">
              {nodes.map((node) => (
                <Link
                  key={node.id}
                  to={`/nodes/${node.id}`}
                  className="row"
                  style={{ justifyContent: "space-between", gap: 12 }}
                >
                  <span style={{ minWidth: 0 }}>
                    <span className="cell-primary" style={{ display: "block" }}>
                      {node.name}
                    </span>
                    <span className="cell-sub mono" dir="ltr">
                      {node.address}
                    </span>
                  </span>
                  <span className="row row--tight">
                    <span className="faint" style={{ fontSize: 12 }}>
                      {t("dash.cpu")} {node.cpu.toFixed(0)}% · {t("dash.memory")}{" "}
                      {node.memory.toFixed(0)}%
                    </span>
                    <StatusBadge status={node.status} />
                  </span>
                </Link>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <div className="grid grid--halves">
        <Panel title={t("dash.topClients")} flush>
          {topClients.length === 0 ? (
            <EmptyState title={t("clients.empty")} hint={t("clients.emptyHint")} />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("clients.name")}</th>
                    <th>{t("clients.used")}</th>
                    <th>{t("clients.multiplier")}</th>
                    <th>{t("common.status")}</th>
                  </tr>
                </thead>
                <tbody>
                  {topClients.map((client) => {
                    const ratio = usageRatio(client.billed_bytes, client.data_limit_bytes);
                    return (
                      <tr key={client.id}>
                        <td>
                          <span className="cell-primary">{client.name}</span>
                          <span className="cell-sub mono" style={{ display: "block" }} dir="ltr">
                            {client.common_name}
                          </span>
                        </td>
                        <td>
                          <span className="row row--tight">
                            <span className="num">{formatBytes(client.billed_bytes)}</span>
                            {client.data_limit_bytes > 0 && (
                              <span className="faint" style={{ fontSize: 11.5 }}>
                                / {formatBytes(client.data_limit_bytes)}
                              </span>
                            )}
                          </span>
                          {client.data_limit_bytes > 0 && (
                            <span className="meter" style={{ marginTop: 5, display: "block" }}>
                              <span
                                className={`meter__fill ${
                                  meterTone(ratio) === "ok"
                                    ? "meter__fill--ok"
                                    : meterTone(ratio) === "warn"
                                      ? "meter__fill--warn"
                                      : ""
                                }`}
                                style={{ width: `${ratio * 100}%`, display: "block" }}
                              />
                            </span>
                          )}
                        </td>
                        <td className="num">{client.multiplier}×</td>
                        <td>
                          <StatusBadge status={client.status} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title={t("dash.recentEvents")} flush>
          {events.length === 0 ? (
            <EmptyState title={t("dash.noEvents")} />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("logs.time")}</th>
                    <th>{t("logs.detail")}</th>
                    <th>{t("logs.level")}</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => (
                    <tr key={event.id}>
                      <td className="nowrap faint" title={event.ts}>
                        {relativeTime(event.ts, lang)}
                      </td>
                      <td>{event.message}</td>
                      <td>
                        <StatusBadge status={eventTone(event.level) === "bad" ? "error" : eventTone(event.level) === "warn" ? "degraded" : "active"} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </AppShell>
  );
}
