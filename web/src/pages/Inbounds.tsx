/** Inbound listeners: create, watch and manage the OpenVPN services. */

import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { IconInbound, IconPlus } from "../components/Icons";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingRows,
  Modal,
  Panel,
  StatusBadge,
  useToast,
} from "../components/ui";
import { endpoints } from "../lib/api";
import { formatBytes } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { Inbound, NodeInfo } from "../lib/types";

interface InboundForm {
  tag: string;
  node_id: string;
  protocol: "openvpn";
  transport: "udp" | "tcp";
  listen_port: string;
  listen_host: string;
  network_cidr: string;
  domain: string;
  enabled: boolean;
}

const emptyForm = (nodeId: string): InboundForm => ({
  tag: "",
  node_id: nodeId,
  protocol: "openvpn",
  transport: "udp",
  listen_port: "1194",
  listen_host: "0.0.0.0",
  network_cidr: "10.8.0.0/24",
  domain: "",
  enabled: true,
});

function serviceState(inbound: Inbound & { service_active?: boolean | null }): string {
  if (!inbound.enabled) return "disabled";
  if (inbound.service_active === true) return "active";
  if (inbound.service_active === false) return "offline";
  return inbound.enabled ? "active" : "disabled";
}

/** Rendered inline so the list type stays loose for the service flag. */
type InboundRow = Inbound & { service_active?: boolean | null; client_count?: number };

export default function InboundsPage() {
  const { t } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const canWrite = account?.role === "admin" || account?.role === "operator";

  const nodeFilter = params.get("node_id");
  const inbounds = useAsync(
    () => endpoints.inbounds(nodeFilter ? { node_id: nodeFilter } : undefined),
    [nodeFilter],
    { pollMs: 30_000 },
  );
  const nodes = useAsync(() => endpoints.nodes(), []);
  const protocols = useAsync(() => endpoints.protocols(), []);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<InboundRow | null>(null);
  const [form, setForm] = useState<InboundForm>(emptyForm(""));
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<InboundRow | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const nodeList: NodeInfo[] = nodes.data ?? [];
  const rows: InboundRow[] = useMemo(() => inbounds.data ?? [], [inbounds.data]);

  const openCreate = () => {
    const preferred =
      nodeFilter && nodeList.some((node) => String(node.id) === nodeFilter)
        ? nodeFilter
        : String(nodeList[0]?.id ?? "");
    setEditing(null);
    setForm(emptyForm(preferred));
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (inbound: InboundRow) => {
    setEditing(inbound);
    setForm({
      tag: inbound.tag,
      node_id: String(inbound.node_id),
      protocol: "openvpn",
      transport: inbound.transport,
      listen_port: String(inbound.listen_port),
      listen_host: inbound.listen_host,
      network_cidr: inbound.network_cidr,
      domain: inbound.domain ?? "",
      enabled: inbound.enabled,
    });
    setFormError(null);
    setFormOpen(true);
  };

  const submit = async () => {
    setSaving(true);
    setFormError(null);
    try {
      const payload: Record<string, unknown> = {
        transport: form.transport,
        listen_port: Number(form.listen_port),
        listen_host: form.listen_host.trim(),
        network_cidr: form.network_cidr.trim(),
        domain: form.domain.trim() || null,
        enabled: form.enabled,
      };
      if (editing) {
        await endpoints.updateInbound(editing.id, payload);
      } else {
        await endpoints.createInbound({
          ...payload,
          tag: form.tag.trim(),
          node_id: Number(form.node_id),
          protocol: "openvpn",
        });
      }
      toast.success(editing ? t("action.save") : t("inbounds.add"));
      setFormOpen(false);
      await inbounds.reload({ silent: true });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusyId(deleting.id);
    try {
      await endpoints.deleteInbound(deleting.id);
      toast.success(t("action.delete"));
      setDeleting(null);
      await inbounds.reload({ silent: true });
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusyId(null);
    }
  };

  const primary = protocols.data?.protocols?.[0];

  return (
    <AppShell
      title={t("inbounds.title")}
      subtitle={t("inbounds.subtitle")}
      actions={
        <>
          <Button size="sm" variant="ghost" loading={inbounds.refreshing} onClick={() => void inbounds.reload({ silent: true })}>
            {t("action.refresh")}
          </Button>
          {canWrite && (
            <Button
              size="sm"
              variant="primary"
              icon={<IconPlus size={15} />}
              onClick={openCreate}
              disabled={nodeList.length === 0}
            >
              {t("inbounds.add")}
            </Button>
          )}
        </>
      }
    >
      {primary && (
        <Panel title={t("inbounds.protocols")} hint={primary.status}>
          <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
            <span className="badge badge--ok">{primary.label}</span>
            <span className="faint">{primary.notes}</span>
            <span className="row row--tight">
              {primary.transports.map((transport: string) => (
                <span key={transport} className="badge badge--idle mono">
                  {transport.toUpperCase()}
                </span>
              ))}
            </span>
          </div>
        </Panel>
      )}

      <Panel
        title={nodeFilter ? `${t("inbounds.title")} · ${nodeList.find((n) => String(n.id) === nodeFilter)?.name ?? ""}` : t("inbounds.title")}
        flush
        actions={
          nodeFilter ? (
            <Button size="sm" variant="ghost" onClick={() => setParams({})}>
              {t("action.clear")}
            </Button>
          ) : undefined
        }
      >
        {inbounds.loading && rows.length === 0 ? (
          <div style={{ padding: 18 }}>
            <LoadingRows rows={4} />
          </div>
        ) : inbounds.error && rows.length === 0 ? (
          <div style={{ padding: 18 }}>
            <ErrorState message={inbounds.error} onRetry={() => void inbounds.reload()} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title={t("inbounds.empty")}
            hint={t("inbounds.emptyHint")}
            icon={<IconInbound size={26} />}
            action={
              canWrite && nodeList.length > 0 ? (
                <Button size="sm" variant="primary" onClick={openCreate} icon={<IconPlus size={15} />}>
                  {t("inbounds.add")}
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("inbounds.tag")}</th>
                  <th>{t("inbounds.node")}</th>
                  <th>{t("inbounds.protocol")}</th>
                  <th>{t("common.port")}</th>
                  <th>{t("inbounds.network")}</th>
                  <th>{t("inbounds.clients")}</th>
                  <th>{t("inbounds.traffic")}</th>
                  <th>{t("common.status")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((inbound) => (
                  <tr key={inbound.id} className="row-link" onClick={() => navigate(`/inbounds/${inbound.id}`)}>
                    <td className="cell-primary">
                      <Link to={`/inbounds/${inbound.id}`}>{inbound.tag}</Link>
                      {inbound.domain && (
                        <span className="cell-sub mono" style={{ display: "block" }} dir="ltr">
                          {inbound.domain}
                        </span>
                      )}
                    </td>
                    <td>{nodeList.find((node) => node.id === inbound.node_id)?.name ?? `#${inbound.node_id}`}</td>
                    <td>
                      <span className="row row--tight">
                        <span className="badge badge--idle">OpenVPN</span>
                        <span className="mono">{inbound.transport.toUpperCase()}</span>
                      </span>
                    </td>
                    <td className="num mono" dir="ltr">
                      {inbound.listen_port}
                      {inbound.nginx_enabled && (
                        <span className="badge badge--warn" style={{ marginInlineStart: 6 }}>
                          +{inbound.nginx_port ?? "—"}
                        </span>
                      )}
                    </td>
                    <td className="mono" dir="ltr">
                      {inbound.network_cidr}
                    </td>
                    <td className="num">{inbound.client_count ?? "—"}</td>
                    <td className="num">
                      {formatBytes(inbound.up_bytes)}
                      {" / "}
                      {formatBytes(inbound.down_bytes)}
                    </td>
                    <td>
                      <StatusBadge status={serviceState(inbound)} />
                    </td>
                    <td className="nowrap" onClick={(event) => event.stopPropagation()}>
                      {canWrite && (
                        <>
                          <Button size="sm" variant="ghost" onClick={() => openEdit(inbound)}>
                            {t("action.edit")}
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            loading={busyId === inbound.id}
                            onClick={async () => {
                              setBusyId(inbound.id);
                              try {
                                await endpoints.applyInbound(inbound.id);
                                toast.success(t("action.apply"));
                                await inbounds.reload({ silent: true });
                              } catch (error) {
                                toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                              } finally {
                                setBusyId(null);
                              }
                            }}
                          >
                            {t("action.apply")}
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => setDeleting(inbound)}>
                            {t("action.delete")}
                          </Button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Modal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        title={editing ? t("inbounds.edit") : t("inbounds.add")}
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
          <Field label={t("inbounds.tag")} hint={t("inbounds.tagHint")} htmlFor="ib-tag">
            <Input
              id="ib-tag"
              dir="ltr"
              value={form.tag}
              disabled={Boolean(editing)}
              onChange={(event) => setForm({ ...form, tag: event.target.value })}
              placeholder="de-frankfurt"
            />
          </Field>
          <Field label={t("inbounds.node")} htmlFor="ib-node">
            <select
              id="ib-node"
              className="select"
              value={form.node_id}
              disabled={Boolean(editing)}
              onChange={(event) => setForm({ ...form, node_id: event.target.value })}
            >
              {nodeList.map((node) => (
                <option key={node.id} value={node.id}>
                  {node.name}
                  {node.is_local ? ` (${t("nodes.localBadge")})` : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("inbounds.transport")} htmlFor="ib-transport">
            <select
              id="ib-transport"
              className="select"
              value={form.transport}
              onChange={(event) => setForm({ ...form, transport: event.target.value as "udp" | "tcp" })}
            >
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
            </select>
          </Field>
          <Field label={t("inbounds.port")} htmlFor="ib-port">
            <Input
              id="ib-port"
              dir="ltr"
              inputMode="numeric"
              value={form.listen_port}
              onChange={(event) => setForm({ ...form, listen_port: event.target.value })}
            />
          </Field>
          <Field label={t("inbounds.listenHost")} htmlFor="ib-host">
            <Input
              id="ib-host"
              dir="ltr"
              value={form.listen_host}
              onChange={(event) => setForm({ ...form, listen_host: event.target.value })}
            />
          </Field>
          <Field label={t("inbounds.network")} hint={t("inbounds.networkHint")} htmlFor="ib-net">
            <Input
              id="ib-net"
              dir="ltr"
              value={form.network_cidr}
              onChange={(event) => setForm({ ...form, network_cidr: event.target.value })}
            />
          </Field>
          <Field label={t("inbounds.domain")} hint={t("inbounds.domainHint")} htmlFor="ib-domain">
            <Input
              id="ib-domain"
              dir="ltr"
              value={form.domain}
              onChange={(event) => setForm({ ...form, domain: event.target.value })}
              placeholder="vpn.example.com"
            />
          </Field>
          <label className="checkbox" style={{ alignSelf: "end" }}>
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
            />
            <span>{t("inbounds.enabled")}</span>
          </label>
        </div>
        {formError && <div className="form-error">{formError}</div>}
      </Modal>

      <ConfirmDialog
        open={deleting !== null}
        title={t("action.deleteConfirmTitle")}
        body={
          <span>
            {t("action.deleteConfirmBody")}
            <br />
            <strong className="mono">{deleting?.tag}</strong>
          </span>
        }
        busy={busyId === deleting?.id}
        onCancel={() => setDeleting(null)}
        onConfirm={() => void remove()}
      />
    </AppShell>
  );
}
