/** Activity: who did what, plus the system events the panel raised. */

import { useMemo, useState } from "react";

import { AppShell } from "../components/AppShell";
import { IconActivity, IconCheck, IconSearch } from "../components/Icons";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Input,
  LoadingRows,
  Panel,
  StatusBadge,
  Tabs,
  useToast,
} from "../components/ui";
import { endpoints } from "../lib/api";
import { formatDate } from "../lib/format";
import { useAsync, usePersistentState } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";

type TabKey = "audit" | "events";

const PAGE_SIZE = 50;

export default function ActivityPage() {
  const { t, lang } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";

  const [tab, setTab] = usePersistentState<TabKey>("panah.activity.tab", "audit");
  const [search, setSearch] = useState("");
  const [level, setLevel] = useState("");
  const [page, setPage] = useState(0);

  const audit = useAsync(
    () =>
      tab === "audit"
        ? endpoints.logs({ search: search.trim() || undefined, level: level || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE })
        : Promise.resolve(null),
    [tab, search, level, page],
  );
  const events = useAsync(
    () =>
      tab === "events"
        ? endpoints.events({ level: level || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE })
        : Promise.resolve(null),
    [tab, level, page],
  );

  const active = tab === "audit" ? audit : events;
  const total = tab === "audit" ? audit.data?.total ?? 0 : events.data?.total ?? 0;
  const rows = useMemo(
    () => (tab === "audit" ? audit.data?.items ?? [] : events.data?.items ?? []),
    [tab, audit.data, events.data],
  );

  return (
    <AppShell
      title={t("logs.title")}
      subtitle={t("logs.subtitle")}
      actions={
        <Button size="sm" variant="ghost" loading={active.refreshing} onClick={() => void active.reload({ silent: true })}>
          {t("action.refresh")}
        </Button>
      }
    >
      <Tabs<TabKey>
        value={tab}
        onChange={(value) => {
          setTab(value);
          setPage(0);
        }}
        tabs={[
          { value: "audit", label: t("logs.audit") },
          { value: "events", label: t("logs.events") },
        ]}
      />

      <Panel
        flush
        title={tab === "audit" ? t("logs.audit") : t("logs.events")}
        hint={`${t("logs.total")} ${total}`}
        actions={
          <div className="row row--tight">
            {tab === "audit" && (
              <span className="row row--tight">
                <IconSearch size={15} />
                <Input
                  placeholder={t("action.search")}
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setPage(0);
                  }}
                  style={{ width: 180 }}
                />
              </span>
            )}
            <select
              className="select"
              value={level}
              onChange={(event) => {
                setLevel(event.target.value);
                setPage(0);
              }}
            >
              <option value="">{t("logs.level")}</option>
              <option value="info">info</option>
              <option value="warning">warning</option>
              <option value="error">error</option>
              <option value="critical">critical</option>
            </select>
          </div>
        }
      >
        {active.loading && rows.length === 0 ? (
          <div style={{ padding: 18 }}>
            <LoadingRows rows={6} />
          </div>
        ) : active.error && rows.length === 0 ? (
          <div style={{ padding: 18 }}>
            <ErrorState message={active.error} onRetry={() => void active.reload()} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title={t("logs.empty")} icon={<IconActivity size={26} />} />
        ) : tab === "audit" ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("logs.time")}</th>
                  <th>{t("logs.user")}</th>
                  <th>{t("logs.action")}</th>
                  <th>{t("logs.target")}</th>
                  <th>{t("logs.ip")}</th>
                  <th>{t("logs.result")}</th>
                </tr>
              </thead>
              <tbody>
                {(audit.data?.items ?? []).map((entry) => (
                  <tr key={entry.id}>
                    <td className="nowrap faint">{formatDate(entry.ts, lang)}</td>
                    <td className="cell-primary">{entry.username ?? "—"}</td>
                    <td className="mono">{entry.action}</td>
                    <td className="mono faint">
                      {entry.target_type ? `${entry.target_type}:${entry.target_id ?? ""}` : "—"}
                    </td>
                    <td className="mono" dir="ltr">
                      {entry.ip ?? "—"}
                    </td>
                    <td>
                      <Badge tone={entry.success ? "ok" : "bad"}>
                        {entry.success ? t("logs.success") : t("logs.failed")}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("logs.time")}</th>
                  <th>{t("common.type")}</th>
                  <th>{t("logs.detail")}</th>
                  <th>{t("logs.level")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(events.data?.items ?? []).map((event) => (
                  <tr key={event.id} style={{ opacity: event.acknowledged ? 0.55 : 1 }}>
                    <td className="nowrap faint">{formatDate(event.ts, lang)}</td>
                    <td className="mono">{event.source}</td>
                    <td>{event.message}</td>
                    <td>
                      <StatusBadge
                        status={
                          event.level === "error" || event.level === "critical"
                            ? "error"
                            : event.level === "warning"
                              ? "degraded"
                              : "active"
                        }
                      />
                    </td>
                    <td>
                      {!event.acknowledged && canWrite && (
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={<IconCheck size={14} />}
                          onClick={async () => {
                            try {
                              await endpoints.acknowledgeEvent(event.id);
                              await events.reload({ silent: true });
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                        >
                          {t("logs.acknowledge")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {total > PAGE_SIZE && (
          <div className="pager">
            <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>
              {t("action.previous")}
            </Button>
            <span className="faint">
              {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} {t("common.of")} {total}
            </span>
            <Button
              size="sm"
              variant="ghost"
              disabled={(page + 1) * PAGE_SIZE >= total}
              onClick={() => setPage((value) => value + 1)}
            >
              {t("action.next")}
            </Button>
          </div>
        )}
      </Panel>
    </AppShell>
  );
}
