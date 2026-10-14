/** Clients: quotas, expiry, multipliers, profiles and subscription links. */

import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { TrafficChart } from "../components/charts";
import {
  IconDownload,
  IconKey,
  IconLink,
  IconPlus,
  IconSearch,
  IconUsers,
} from "../components/Icons";
import {
  Button,
  Checkbox,
  CodeBlock,
  ConfirmDialog,
  CopyButton,
  EmptyState,
  Field,
  Input,
  Meter,
  Modal,
  Panel,
  StatusBadge,
  useToast,
} from "../components/ui";
import { endpoints } from "../lib/api";
import {
  daysBetween,
  formatBytes,
  formatDate,
  meterTone,
  parseBytes,
  relativeTime,
  usageRatio,
} from "../lib/format";
import { useAsync } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { Client, ClientConfig, ClientDetail, Inbound, ResetPolicy } from "../lib/types";

interface ClientForm {
  name: string;
  email: string;
  inbound_id: string;
  common_name: string;
  data_limit: string;
  multiplier: string;
  expire_at: string;
  reset_policy: ResetPolicy;
  ip_limit: string;
  device_limit: string;
  note: string;
  sub_enabled: boolean;
  enabled: boolean;
}

const emptyForm = (inboundId: string): ClientForm => ({
  name: "",
  email: "",
  inbound_id: inboundId,
  common_name: "",
  data_limit: "",
  multiplier: "1",
  expire_at: "",
  reset_policy: "never",
  ip_limit: "0",
  device_limit: "0",
  note: "",
  sub_enabled: true,
  enabled: true,
});

export default function ClientsPage() {
  const { t, lang } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";
  const [params, setParams] = useSearchParams();

  const inboundFilter = params.get("inbound_id") ?? "";
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  const clients = useAsync(
    () =>
      endpoints.clients({
        inbound_id: inboundFilter || undefined,
        status: statusFilter || undefined,
        search: search.trim() || undefined,
        limit: 500,
      }),
    [inboundFilter, statusFilter, search],
    { pollMs: 30_000 },
  );
  const inbounds = useAsync(() => endpoints.inbounds(), []);

  const [selected, setSelected] = useState<number[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Client | null>(null);
  const [form, setForm] = useState<ClientForm>(emptyForm(""));
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Client | null>(null);
  const [detail, setDetail] = useState<ClientDetail | null>(null);
  const [config, setConfig] = useState<ClientConfig | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  const inboundList: Inbound[] = inbounds.data ?? [];
  const rows: Client[] = useMemo(() => clients.data ?? [], [clients.data]);

  const detailUsage = useAsync(
    () => (detail ? endpoints.clientUsage(detail.id, 168) : Promise.resolve(null)),
    [detail?.id],
  );
  const detailSessions = useAsync(
    () => (detail ? endpoints.clientSessions(detail.id) : Promise.resolve(null)),
    [detail?.id],
  );

  const openCreate = () => {
    const preferred =
      inboundFilter && inboundList.some((inbound) => String(inbound.id) === inboundFilter)
        ? inboundFilter
        : String(inboundList[0]?.id ?? "");
    setEditing(null);
    setForm(emptyForm(preferred));
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (client: Client) => {
    setEditing(client);
    setForm({
      name: client.name,
      email: client.email ?? "",
      inbound_id: String(client.inbound_id),
      common_name: client.common_name,
      data_limit: client.data_limit_bytes ? formatBytes(client.data_limit_bytes, 0) : "",
      multiplier: String(client.multiplier),
      expire_at: client.expire_at ? client.expire_at.slice(0, 10) : "",
      reset_policy: client.reset_policy,
      ip_limit: String(client.ip_limit),
      device_limit: String(client.device_limit),
      note: client.note ?? "",
      sub_enabled: client.sub_enabled,
      enabled: client.enabled,
    });
    setFormError(null);
    setFormOpen(true);
  };

  const submit = async () => {
    setSaving(true);
    setFormError(null);
    const limit = form.data_limit.trim() ? parseBytes(form.data_limit) : 0;
    if (form.data_limit.trim() && limit === null) {
      setFormError(t("clients.dataLimitHint"));
      setSaving(false);
      return;
    }
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      email: form.email.trim() || null,
      data_limit_bytes: limit ?? 0,
      multiplier: Number(form.multiplier) || 1,
      expire_at: form.expire_at ? new Date(`${form.expire_at}T23:59:59`).toISOString() : null,
      reset_policy: form.reset_policy,
      ip_limit: Number(form.ip_limit) || 0,
      device_limit: Number(form.device_limit) || 0,
      note: form.note.trim() || null,
      sub_enabled: form.sub_enabled,
      enabled: form.enabled,
    };
    try {
      if (editing) {
        await endpoints.updateClient(editing.id, payload);
      } else {
        await endpoints.createClient({
          ...payload,
          inbound_id: Number(form.inbound_id),
          common_name: form.common_name.trim() || null,
        });
      }
      toast.success(editing ? t("action.save") : t("clients.add"));
      setFormOpen(false);
      await clients.reload({ silent: true });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const showConfig = async (client: Client, download = false) => {
    setBusyId(client.id);
    try {
      const result = await endpoints.clientConfig(client.id);
      if (download) {
        const blob = new Blob([result.content], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = result.filename;
        anchor.click();
        URL.revokeObjectURL(url);
      } else {
        setConfig(result);
        setDetail((current) => (current && current.id === client.id ? current : null));
      }
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusyId(null);
    }
  };

  const bulk = async (action: string) => {
    if (selected.length === 0) return;
    setBulkBusy(true);
    try {
      const result = await endpoints.bulkClients(action, selected);
      toast.success(`${result.affected} × ${action}`);
      setSelected([]);
      await clients.reload({ silent: true });
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <AppShell
      title={t("clients.title")}
      subtitle={t("clients.subtitle")}
      actions={
        <>
          <Button size="sm" variant="ghost" loading={clients.refreshing} onClick={() => void clients.reload({ silent: true })}>
            {t("action.refresh")}
          </Button>
          {canWrite && (
            <Button
              size="sm"
              variant="primary"
              icon={<IconPlus size={15} />}
              onClick={openCreate}
              disabled={inboundList.length === 0}
            >
              {t("clients.add")}
            </Button>
          )}
        </>
      }
    >
      <Panel
        flush
        className="toolbar-panel"
        actions={
          <div className="row row--tight" style={{ flexWrap: "wrap" }}>
            <span className="row row--tight">
              <IconSearch size={15} />
              <Input
                placeholder={t("action.search")}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                style={{ width: 190 }}
              />
            </span>
            <select
              className="select"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              <option value="">{t("common.status")}</option>
              <option value="active">{t("state.active")}</option>
              <option value="disabled">{t("state.disabled")}</option>
              <option value="expired">{t("state.expired")}</option>
              <option value="limited">{t("state.limited")}</option>
              <option value="on_hold">{t("state.onHold")}</option>
            </select>
            <select
              className="select"
              value={inboundFilter}
              onChange={(event) => {
                const value = event.target.value;
                if (value) setParams({ inbound_id: value });
                else setParams({});
              }}
            >
              <option value="">{t("clients.inbound")}</option>
              {inboundList.map((inbound) => (
                <option key={inbound.id} value={inbound.id}>
                  {inbound.tag}
                </option>
              ))}
            </select>
          </div>
        }
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {canWrite && (
                  <th style={{ width: 34 }}>
                    <input
                      type="checkbox"
                      aria-label={t("action.select")}
                      checked={rows.length > 0 && selected.length === rows.length}
                      onChange={(event) => setSelected(event.target.checked ? rows.map((row) => row.id) : [])}
                    />
                  </th>
                )}
                <th>{t("clients.name")}</th>
                <th>{t("clients.inbound")}</th>
                <th>{t("clients.used")}</th>
                <th>{t("clients.multiplier")}</th>
                <th>{t("clients.expires")}</th>
                <th>{t("clients.lastOnline")}</th>
                <th>{t("common.status")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((client) => {
                const ratio = usageRatio(client.billed_bytes, client.data_limit_bytes);
                const days = daysBetween(client.expire_at);
                return (
                  <tr key={client.id}>
                    {canWrite && (
                      <td>
                        <input
                          type="checkbox"
                          aria-label={t("action.select")}
                          checked={selected.includes(client.id)}
                          onChange={(event) =>
                            setSelected((current) =>
                              event.target.checked
                                ? [...current, client.id]
                                : current.filter((id) => id !== client.id),
                            )
                          }
                        />
                      </td>
                    )}
                    <td>
                      <span className="cell-primary">{client.name}</span>
                      <span className="cell-sub mono" style={{ display: "block" }} dir="ltr">
                        {client.common_name}
                      </span>
                    </td>
                    <td>{inboundList.find((inbound) => inbound.id === client.inbound_id)?.tag ?? client.inbound_id}</td>
                    <td style={{ minWidth: 150 }}>
                      <span className="row row--tight">
                        <span className="num">{formatBytes(client.billed_bytes)}</span>
                        {client.data_limit_bytes > 0 && (
                          <span className="faint" style={{ fontSize: 11.5 }}>
                            / {formatBytes(client.data_limit_bytes)}
                          </span>
                        )}
                      </span>
                      {client.data_limit_bytes > 0 && <Meter ratio={ratio} tone={meterTone(ratio)} />}
                    </td>
                    <td className="num">{client.multiplier}×</td>
                    <td className="nowrap">
                      {client.expire_at ? (
                        <span>
                          {formatDate(client.expire_at, lang, false)}
                          <span className="cell-sub" style={{ display: "block" }}>
                            {days !== null && days >= 0 ? `${days} ${t("state.days")}` : t("state.expired")}
                          </span>
                        </span>
                      ) : (
                        <span className="faint">{t("state.unlimited")}</span>
                      )}
                    </td>
                    <td className="nowrap faint">{relativeTime(client.last_online_at, lang)}</td>
                    <td>
                      <StatusBadge status={client.status} />
                    </td>
                    <td className="nowrap">
                      <div className="row row--tight">
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={<IconLink size={14} />}
                          disabled={!client.sub_enabled}
                          onClick={async () => {
                            try {
                              const info = await endpoints.clientSubscription(client.id);
                              if (info.subscription_url) {
                                await navigator.clipboard.writeText(info.subscription_url);
                                toast.success(t("clients.subUrlCopied"));
                              }
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                          aria-label={t("clients.subscription")}
                        />
                        <Button
                          size="sm"
                          variant="ghost"
                          icon={<IconDownload size={14} />}
                          loading={busyId === client.id}
                          onClick={() => void showConfig(client, true)}
                          aria-label={t("clients.downloadConfig")}
                        />
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={async () => {
                            try {
                              const full = await endpoints.client(client.id);
                              setDetail(full);
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                        >
                          {t("action.details")}
                        </Button>
                        {canWrite && (
                          <>
                            <Button size="sm" variant="ghost" onClick={() => openEdit(client)}>
                              {t("action.edit")}
                            </Button>
                            <Button size="sm" variant="ghost" onClick={() => setDeleting(client)}>
                              {t("action.delete")}
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {rows.length === 0 && (
            <EmptyState
              title={t("clients.empty")}
              hint={t("clients.emptyHint")}
              icon={<IconUsers size={26} />}
              action={
                canWrite && inboundList.length > 0 ? (
                  <Button size="sm" variant="primary" onClick={openCreate} icon={<IconPlus size={15} />}>
                    {t("clients.add")}
                  </Button>
                ) : undefined
              }
            />
          )}
        </div>

        {selected.length > 0 && canWrite && (
          <div className="bulk-bar">
            <span>
              {selected.length} {t("clients.bulkSelected")}
            </span>
            <div className="row row--tight">
              <Button size="sm" loading={bulkBusy} onClick={() => void bulk("enable")}>
                {t("action.enable")}
              </Button>
              <Button size="sm" loading={bulkBusy} onClick={() => void bulk("disable")}>
                {t("action.disable")}
              </Button>
              <Button size="sm" loading={bulkBusy} onClick={() => void bulk("disconnect")}>
                {t("action.disconnect")}
              </Button>
              <Button size="sm" loading={bulkBusy} onClick={() => void bulk("reset")}>
                {t("action.reset")}
              </Button>
              <Button size="sm" variant="danger" loading={bulkBusy} onClick={() => void bulk("delete")}>
                {t("action.delete")}
              </Button>
            </div>
          </div>
        )}
      </Panel>

      {/* Create / edit */}
      <Modal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        title={editing ? t("clients.edit") : t("clients.add")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setFormOpen(false)} disabled={saving}>
              {t("action.cancel")}
            </Button>
            <Button variant="primary" onClick={() => void submit()} loading={saving}>
              {editing ? t("action.save") : t("action.create")}
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <Field label={t("clients.name")} htmlFor="c-name">
            <Input id="c-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label={t("clients.email")} htmlFor="c-email">
            <Input id="c-email" dir="ltr" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          </Field>
          <Field label={t("clients.inbound")} htmlFor="c-inbound">
            <select
              id="c-inbound"
              className="select"
              value={form.inbound_id}
              disabled={Boolean(editing)}
              onChange={(e) => setForm({ ...form, inbound_id: e.target.value })}
            >
              {inboundList.map((inbound) => (
                <option key={inbound.id} value={inbound.id}>
                  {inbound.tag}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("clients.commonName")} hint={t("clients.commonNameHint")} htmlFor="c-cn">
            <Input
              id="c-cn"
              dir="ltr"
              value={form.common_name}
              disabled={Boolean(editing)}
              onChange={(e) => setForm({ ...form, common_name: e.target.value })}
            />
          </Field>
          <Field label={t("clients.dataLimit")} hint={t("clients.dataLimitHint")} htmlFor="c-limit">
            <Input id="c-limit" dir="ltr" value={form.data_limit} onChange={(e) => setForm({ ...form, data_limit: e.target.value })} placeholder="50 GB" />
          </Field>
          <Field label={t("clients.multiplier")} hint={t("clients.multiplierHint")} htmlFor="c-mult">
            <Input id="c-mult" dir="ltr" inputMode="decimal" value={form.multiplier} onChange={(e) => setForm({ ...form, multiplier: e.target.value })} />
          </Field>
          <Field label={t("clients.expireAt")} htmlFor="c-expire">
            <Input id="c-expire" dir="ltr" type="date" value={form.expire_at} onChange={(e) => setForm({ ...form, expire_at: e.target.value })} />
          </Field>
          <Field label={t("clients.resetPolicy")} htmlFor="c-reset">
            <select
              id="c-reset"
              className="select"
              value={form.reset_policy}
              onChange={(e) => setForm({ ...form, reset_policy: e.target.value as ResetPolicy })}
            >
              <option value="never">{t("state.never")}</option>
              <option value="daily">daily</option>
              <option value="weekly">weekly</option>
              <option value="monthly">monthly</option>
            </select>
          </Field>
          <Field label={t("clients.ipLimit")} htmlFor="c-ip">
            <Input id="c-ip" dir="ltr" inputMode="numeric" value={form.ip_limit} onChange={(e) => setForm({ ...form, ip_limit: e.target.value })} />
          </Field>
          <Field label={t("clients.deviceLimit")} htmlFor="c-dev">
            <Input id="c-dev" dir="ltr" inputMode="numeric" value={form.device_limit} onChange={(e) => setForm({ ...form, device_limit: e.target.value })} />
          </Field>
        </div>
        <Field label={t("clients.note")} htmlFor="c-note">
          <Input id="c-note" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
        </Field>
        <div className="row" style={{ gap: 16, marginTop: 12 }}>
          <Checkbox label={t("clients.subscriptionEnabled")} checked={form.sub_enabled} onChange={(value) => setForm({ ...form, sub_enabled: value })} />
          <Checkbox label={t("inbounds.enabled")} checked={form.enabled} onChange={(value) => setForm({ ...form, enabled: value })} />
        </div>
        {formError && <div className="form-error">{formError}</div>}
      </Modal>

      {/* Detail drawer */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        drawer
        wide
        title={detail ? `${detail.name}` : ""}
        footer={
          detail && (
            <>
              <Button variant="ghost" onClick={() => void showConfig(detail, true)}>
                {t("clients.downloadConfig")}
              </Button>
              {canWrite && (
                <>
                  <Button
                    loading={busyId === detail.id}
                    onClick={async () => {
                      setBusyId(detail.id);
                      try {
                        await endpoints.resetClient(detail.id, true);
                        toast.success(t("action.reset"));
                        await clients.reload({ silent: true });
                      } catch (error) {
                        toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                      } finally {
                        setBusyId(null);
                      }
                    }}
                  >
                    {t("action.reset")}
                  </Button>
                  <Button
                    icon={<IconKey size={15} />}
                    loading={busyId === detail.id}
                    onClick={async () => {
                      setBusyId(detail.id);
                      try {
                        const rotated = await endpoints.rotateClientKeys(detail.id);
                        setConfig(rotated);
                        toast.success(t("clients.rotateKeys"));
                        await clients.reload({ silent: true });
                      } catch (error) {
                        toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                      } finally {
                        setBusyId(null);
                      }
                    }}
                  >
                    {t("clients.rotateKeys")}
                  </Button>
                </>
              )}
            </>
          )
        }
      >
        {detail && (
          <div className="stack">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <StatusBadge status={detail.status} />
              <span className="row row--tight">
                <span className="badge badge--idle">{detail.multiplier}×</span>
                {detail.online && <span className="badge badge--ok">{t("clients.online")}</span>}
              </span>
            </div>

            <div className="grid grid--halves">
              <div className="stack">
                <span className="field__label">{t("clients.used")}</span>
                <span className="stat__value">
                  {formatBytes(detail.billed_bytes)}
                  {detail.data_limit_bytes > 0 && (
                    <span className="stat__unit">/ {formatBytes(detail.data_limit_bytes)}</span>
                  )}
                </span>
                {detail.data_limit_bytes > 0 && (
                  <Meter
                    ratio={usageRatio(detail.billed_bytes, detail.data_limit_bytes)}
                    tone={meterTone(usageRatio(detail.billed_bytes, detail.data_limit_bytes))}
                  />
                )}
              </div>
              <div className="stack">
                <span className="field__label">{t("clients.expires")}</span>
                <span className="stat__value" style={{ fontSize: 17 }}>
                  {detail.expire_at ? formatDate(detail.expire_at, lang, false) : t("state.unlimited")}
                </span>
                <span className="faint">
                  {detail.expire_at ? `${daysBetween(detail.expire_at) ?? 0} ${t("state.days")}` : t("state.never")}
                </span>
              </div>
            </div>

            <div className="stack">
              <span className="field__label">{t("clients.usage")}</span>
              <TrafficChart series={detailUsage.data?.series ?? []} height={150} emptyLabel={t("dash.noTraffic")} />
            </div>

            <div className="stack">
              <span className="field__label">{t("clients.sessions")}</span>
              {(detailSessions.data ?? []).length === 0 ? (
                <span className="faint">{t("clients.noSessions")}</span>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{t("logs.time")}</th>
                        <th>{t("logs.ip")}</th>
                        <th>{t("inbounds.network")}</th>
                        <th>{t("chart.up")}</th>
                        <th>{t("chart.down")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(detailSessions.data ?? []).slice(0, 12).map((session) => (
                        <tr key={session.id}>
                          <td className="nowrap faint">{relativeTime(session.started_at, lang)}</td>
                          <td className="mono" dir="ltr">{session.real_address ?? "—"}</td>
                          <td className="mono" dir="ltr">{session.virtual_address ?? "—"}</td>
                          <td className="num">{formatBytes(session.bytes_sent)}</td>
                          <td className="num">{formatBytes(session.bytes_received)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div className="stack">
              <span className="field__label">{t("clients.subscription")}</span>
              {detail.subscription_url ? (
                <div className="row row--tight">
                  <code className="code code--inline" dir="ltr">
                    {detail.subscription_url}
                  </code>
                  <CopyButton text={detail.subscription_url} />
                </div>
              ) : (
                <span className="faint">{t("state.none")}</span>
              )}
            </div>

            <div className="row row--tight">
              <Button size="sm" onClick={() => void showConfig(detail)}>
                {t("clients.viewConfig")}
              </Button>
              {canWrite && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={async () => {
                    try {
                      const result = await endpoints.disconnectClient(detail.id);
                      toast.success(result.message);
                    } catch (error) {
                      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                    }
                  }}
                >
                  {t("action.disconnect")}
                </Button>
              )}
            </div>
          </div>
        )}
      </Modal>

      {/* Profile */}
      <Modal
        open={config !== null}
        onClose={() => setConfig(null)}
        wide
        title={config ? config.filename : t("clients.config")}
        footer={
          config && (
            <>
              <CopyButton text={config.content} />
              <Button
                variant="primary"
                icon={<IconDownload size={15} />}
                onClick={() => {
                  const blob = new Blob([config.content], { type: "text/plain;charset=utf-8" });
                  const url = URL.createObjectURL(blob);
                  const anchor = document.createElement("a");
                  anchor.href = url;
                  anchor.download = config.filename;
                  anchor.click();
                  URL.revokeObjectURL(url);
                }}
              >
                {t("clients.downloadConfig")}
              </Button>
            </>
          )
        }
      >
        {config && <CodeBlock content={config.content} maxHeight={520} />}
      </Modal>

      <ConfirmDialog
        open={deleting !== null}
        title={t("action.deleteConfirmTitle")}
        body={
          <span>
            {t("action.deleteConfirmBody")}
            <br />
            <strong>{deleting?.name}</strong>
          </span>
        }
        busy={busyId === deleting?.id}
        onCancel={() => setDeleting(null)}
        onConfirm={async () => {
          if (!deleting) return;
          setBusyId(deleting.id);
          try {
            await endpoints.deleteClient(deleting.id);
            toast.success(t("action.delete"));
            setDeleting(null);
            await clients.reload({ silent: true });
          } catch (error) {
            toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
          } finally {
            setBusyId(null);
          }
        }}
      />
    </AppShell>
  );
}
