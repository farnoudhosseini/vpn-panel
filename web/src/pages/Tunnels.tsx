/** Tunnels: pick a transport, deploy it on both ends, watch the service state. */

import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import {
  IconAlert,
  IconLink,
  IconPlus,
  IconTunnel,
  IconZap,
} from "../components/Icons";
import {
  Badge,
  Button,
  Checkbox,
  CodeBlock,
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
import { relativeTime } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { NodeInfo, Transport, Tunnel, TunnelPlan } from "../lib/types";

interface Forwarded {
  expose: string;
  target: string;
  protocol: "tcp" | "udp";
}

interface TunnelForm {
  name: string;
  transport: string;
  local_node_id: string;
  remote_node_id: string;
  listen_port: string;
  interface_name: string;
  mtu: string;
}

const emptyForm = (local: string, remote: string): TunnelForm => ({
  name: "",
  transport: "stealth",
  local_node_id: local,
  remote_node_id: remote,
  listen_port: "443",
  interface_name: "",
  mtu: "1420",
});

export default function TunnelsPage() {
  const { t, lang } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";
  const [params] = useSearchParams();

  const tunnels = useAsync(() => endpoints.tunnels(), [], { pollMs: 30_000 });
  const nodes = useAsync(() => endpoints.nodes(), []);
  const transports = useAsync(() => endpoints.transports(), []);
  const backpack = useAsync(() => endpoints.backpackStatus(), []);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Tunnel | null>(null);
  const [form, setForm] = useState<TunnelForm>(emptyForm("", ""));
  const [forwarded, setForwarded] = useState<Forwarded[]>([{ expose: "443", target: "8443", protocol: "tcp" }]);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Tunnel | null>(null);
  const [teardown, setTeardown] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [plan, setPlan] = useState<{ tunnel: Tunnel; plan: TunnelPlan } | null>(null);
  const [artifacts, setArtifacts] = useState<{
    service: string | null;
    local: { node: string; files: string[]; units: string[] };
    remote: { node: string; files: string[]; units: string[] };
  } | null>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const [backpackLink, setBackpackLink] = useState("");
  const [linkName, setLinkName] = useState("");
  const [linkHost, setLinkHost] = useState("");

  const nodeList: NodeInfo[] = nodes.data ?? [];
  const rows: Tunnel[] = useMemo(() => tunnels.data ?? [], [tunnels.data]);

  useEffect(() => {
    const focus = params.get("focus");
    if (!focus) return;
    const entry = rows.find((row) => String(row.id) === focus);
    if (entry) void openPlan(entry);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, rows.length]);

  const transportList: Transport[] = transports.data ?? [];

  const openCreate = () => {
    const local = String(nodeList.find((node) => node.is_local)?.id ?? nodeList[0]?.id ?? "");
    const remote = String(nodeList.find((node) => String(node.id) !== local)?.id ?? "");
    setEditing(null);
    setForm(emptyForm(local, remote));
    setForwarded([{ expose: "443", target: "8443", protocol: "tcp" }]);
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (tunnel: Tunnel) => {
    setEditing(tunnel);
    setForm({
      name: tunnel.name,
      transport: tunnel.transport,
      local_node_id: String(tunnel.local_node_id),
      remote_node_id: String(tunnel.remote_node_id),
      listen_port: String(tunnel.listen_port),
      interface_name: tunnel.interface_name ?? "",
      mtu: String(tunnel.mtu),
    });
    setForwarded(
      (tunnel.forwarded_ports ?? []).map((entry) => ({
        expose: String(entry.expose),
        target: String(entry.target),
        protocol: entry.protocol,
      })),
    );
    setFormError(null);
    setFormOpen(true);
  };

  async function openPlan(tunnel: Tunnel) {
    try {
      const result = await endpoints.tunnelPlan(tunnel.id);
      setPlan({ tunnel, plan: result });
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    }
  }

  const submit = async () => {
    setSaving(true);
    setFormError(null);
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      transport: form.transport,
      listen_port: Number(form.listen_port) || 443,
      interface_name: form.interface_name.trim() || null,
      mtu: Number(form.mtu) || 1420,
      forwarded_ports: forwarded
        .filter((entry) => entry.expose.trim() && entry.target.trim())
        .map((entry) => ({
          expose: Number(entry.expose) || 0,
          target: entry.target.includes(":") ? entry.target.trim() : Number(entry.target) || 0,
          protocol: entry.protocol,
        }))
        .filter((entry) => entry.expose > 0 && entry.target !== 0),
    };
    try {
      if (editing) {
        await endpoints.updateTunnel(editing.id, payload);
      } else {
        const spec = transportList.find((entry) => entry.key === form.transport);
        await endpoints.createTunnel(
          {
            ...payload,
            mode: spec?.engine === "native" && spec?.layer === "l3" ? "direct" : "reverse",
            local_node_id: Number(form.local_node_id),
            remote_node_id: Number(form.remote_node_id),
            enabled: true,
          },
          false,
        );
      }
      toast.success(editing ? t("action.save") : t("tunnels.add"));
      setFormOpen(false);
      await tunnels.reload({ silent: true });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const deploy = async (tunnel: Tunnel) => {
    setBusyId(tunnel.id);
    try {
      const result = await endpoints.applyTunnel(tunnel.id);
      if (result.ok) toast.success(t("tunnels.applied"), result.message);
      else toast.failure(t("tunnels.applyFailed"), result.message);
      await tunnels.reload({ silent: true });
    } catch (error) {
      toast.failure(t("tunnels.applyFailed"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusyId(null);
    }
  };

  const stop = async (tunnel: Tunnel) => {
    setBusyId(tunnel.id);
    try {
      const result = await endpoints.stopTunnel(tunnel.id);
      toast.success(t("action.stop"), result.message);
      await tunnels.reload({ silent: true });
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusyId(null);
    }
  };

  const nodeName = (id: number) => nodeList.find((node) => node.id === id)?.name ?? `#${id}`;

  return (
    <AppShell
      title={t("tunnels.title")}
      subtitle={t("tunnels.subtitle")}
      actions={
        <>
          <Button size="sm" variant="ghost" loading={tunnels.refreshing} onClick={() => void tunnels.reload({ silent: true })}>
            {t("action.refresh")}
          </Button>
          {canWrite && (
            <>
              <Button size="sm" variant="ghost" icon={<IconLink size={15} />} onClick={() => setLinkOpen(true)}>
                {t("tunnels.backpackLink")}
              </Button>
              <Button
                size="sm"
                variant="primary"
                icon={<IconPlus size={15} />}
                onClick={openCreate}
                disabled={nodeList.length < 2}
              >
                {t("tunnels.add")}
              </Button>
            </>
          )}
        </>
      }
    >
      <div className="grid grid--halves">
        <Panel
          title={t("tunnels.engine")}
          hint={backpack.data ? (backpack.data.installed ? "BackPack " + (backpack.data.version ?? "") : t("state.none")) : undefined}
        >
          <div className="stack">
            {transportList.map((transport) => (
              <button
                key={transport.key}
                type="button"
                className="transport-chip"
                onClick={() => {
                  setForm((current) => ({ ...current, transport: transport.key }));
                  if (!formOpen) openCreate();
                }}
              >
                <span className="row row--tight">
                  <Badge tone={transport.engine === "backpack" ? "warn" : "idle"}>{transport.engine}</Badge>
                  <span className="cell-primary">{transport.label}</span>
                  {transport.encrypted && <Badge tone="ok">{t("tunnels.encrypted")}</Badge>}
                </span>
                <span className="cell-sub">
                  {transport.layer === "l3" ? t("tunnels.l3") : t("tunnels.l4")}
                  {transport.requirements.length > 0 && ` · ${t("tunnels.requires")}: ${transport.requirements.join(", ")}`}
                </span>
              </button>
            ))}
            {transportList.length === 0 && <LoadingRows rows={3} />}
          </div>
        </Panel>

        <Panel title={t("tunnels.notes")}>
          <div className="stack">
            <p className="faint">{t("bp.hint")}</p>
            <Link to="/topology">
              <Button size="sm">→ {t("nav.blueprint")}</Button>
            </Link>
            {!backpack.data?.installed && (
              <p className="field__hint">
                <IconAlert size={14} /> {t("tunnels.backpackMissing")}
              </p>
            )}
            {backpack.data?.installed && (
              <span className="badge badge--ok">
                BackPack {backpack.data.version ?? ""} · {backpack.data.tunnels.length} {t("nav.tunnels")}
              </span>
            )}
          </div>
        </Panel>
      </div>

      <Panel title={t("nav.tunnels")} flush>
        {tunnels.loading && rows.length === 0 ? (
          <div style={{ padding: 18 }}>
            <LoadingRows rows={4} />
          </div>
        ) : tunnels.error && rows.length === 0 ? (
          <div style={{ padding: 18 }}>
            <ErrorState message={tunnels.error} onRetry={() => void tunnels.reload()} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title={t("tunnels.empty")}
            hint={t("tunnels.emptyHint")}
            icon={<IconTunnel size={26} />}
            action={
              canWrite && nodeList.length >= 2 ? (
                <Button size="sm" variant="primary" onClick={openCreate} icon={<IconPlus size={15} />}>
                  {t("tunnels.add")}
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("tunnels.name")}</th>
                  <th>{t("tunnels.local")} → {t("tunnels.remote")}</th>
                  <th>{t("tunnels.transport")}</th>
                  <th>{t("common.port")}</th>
                  <th>{t("tunnels.forwarded")}</th>
                  <th>{t("tunnels.serviceState")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((tunnel) => (
                  <tr key={tunnel.id}>
                    <td className="cell-primary">
                      {tunnel.name}
                      <span className="cell-sub mono" style={{ display: "block" }} dir="ltr">
                        {tunnel.interface_name ?? ""} {tunnel.last_handshake_at ? `· ${relativeTime(tunnel.last_handshake_at, lang)}` : ""}
                      </span>
                    </td>
                    <td>
                      {nodeName(tunnel.local_node_id)} → {nodeName(tunnel.remote_node_id)}
                    </td>
                    <td>
                      <span className="row row--tight">
                        <span className="mono">{tunnel.transport}</span>
                        <Badge tone={tunnel.engine === "backpack" ? "warn" : "idle"}>{tunnel.engine}</Badge>
                        <Badge tone="idle">{tunnel.mode}</Badge>
                      </span>
                    </td>
                    <td className="num mono" dir="ltr">
                      {tunnel.listen_port}
                    </td>
                    <td className="mono" dir="ltr">
                      {(tunnel.forwarded_ports ?? [])
                        .map((entry) => `${entry.expose}→${entry.target}`)
                        .join(", ") || t("state.none")}
                    </td>
                    <td>
                      <StatusBadge status={tunnel.status} />
                      {tunnel.last_error && (
                        <span className="cell-sub" style={{ display: "block", color: "var(--crimson)" }}>
                          {tunnel.last_error}
                        </span>
                      )}
                    </td>
                    <td className="nowrap">
                      <div className="row row--tight">
                        {canWrite && (
                          <>
                            <Button
                              size="sm"
                              variant="primary"
                              icon={<IconZap size={14} />}
                              loading={busyId === tunnel.id}
                              onClick={() => void deploy(tunnel)}
                            >
                              {t("action.deploy")}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              loading={busyId === tunnel.id}
                              onClick={() => void stop(tunnel)}
                            >
                              {t("action.stop")}
                            </Button>
                          </>
                        )}
                        <Button size="sm" variant="ghost" onClick={() => void openPlan(tunnel)}>
                          {t("action.plan")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={async () => {
                            try {
                              setArtifacts(await endpoints.tunnelArtifacts(tunnel.id));
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                        >
                          {t("tunnels.artifacts")}
                        </Button>
                        {canWrite && (
                          <>
                            <Button size="sm" variant="ghost" onClick={() => openEdit(tunnel)}>
                              {t("action.edit")}
                            </Button>
                            <Button size="sm" variant="ghost" onClick={() => setDeleting(tunnel)}>
                              {t("action.delete")}
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* Create / edit */}
      <Modal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        title={editing ? t("tunnels.edit") : t("tunnels.add")}
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
          <Field label={t("tunnels.name")} htmlFor="tu-name">
            <Input id="tu-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label={t("tunnels.transport")} htmlFor="tu-transport">
            <select
              id="tu-transport"
              className="select"
              value={form.transport}
              onChange={(e) => setForm({ ...form, transport: e.target.value })}
            >
              {transportList.map((transport) => (
                <option key={transport.key} value={transport.key}>
                  {transport.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("tunnels.localNode")} htmlFor="tu-local">
            <select
              id="tu-local"
              className="select"
              value={form.local_node_id}
              onChange={(e) => setForm({ ...form, local_node_id: e.target.value })}
            >
              {nodeList.map((node) => (
                <option key={node.id} value={node.id}>
                  {node.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("tunnels.remoteNode")} htmlFor="tu-remote">
            <select
              id="tu-remote"
              className="select"
              value={form.remote_node_id}
              onChange={(e) => setForm({ ...form, remote_node_id: e.target.value })}
            >
              {nodeList.map((node) => (
                <option key={node.id} value={node.id}>
                  {node.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("tunnels.listenPort")} htmlFor="tu-port">
            <Input id="tu-port" dir="ltr" inputMode="numeric" value={form.listen_port} onChange={(e) => setForm({ ...form, listen_port: e.target.value })} />
          </Field>
          <Field label={t("tunnels.mtu")} htmlFor="tu-mtu">
            <Input id="tu-mtu" dir="ltr" inputMode="numeric" value={form.mtu} onChange={(e) => setForm({ ...form, mtu: e.target.value })} />
          </Field>
          <Field label={t("tunnels.interface")} htmlFor="tu-if">
            <Input id="tu-if" dir="ltr" value={form.interface_name} onChange={(e) => setForm({ ...form, interface_name: e.target.value })} placeholder="bp-tun0" />
          </Field>
        </div>

        {form.local_node_id === form.remote_node_id && (
          <p className="form-error">{t("tunnels.sameNodeBlocked")}</p>
        )}

        <div className="stack" style={{ marginTop: 12 }}>
          <span className="field__label">{t("tunnels.forwarded")}</span>
          <span className="field__hint">{t("tunnels.forwardedHint")}</span>
          {forwarded.map((entry, index) => (
            <div key={index} className="row row--tight">
              <Input
                dir="ltr"
                inputMode="numeric"
                placeholder={t("tunnels.expose")}
                value={entry.expose}
                onChange={(event) =>
                  setForwarded((current) =>
                    current.map((row, rowIndex) => (rowIndex === index ? { ...row, expose: event.target.value } : row)),
                  )
                }
              />
              <Input
                dir="ltr"
                placeholder={t("tunnels.target")}
                value={entry.target}
                onChange={(event) =>
                  setForwarded((current) =>
                    current.map((row, rowIndex) => (rowIndex === index ? { ...row, target: event.target.value } : row)),
                  )
                }
              />
              <select
                className="select"
                value={entry.protocol}
                onChange={(event) =>
                  setForwarded((current) =>
                    current.map((row, rowIndex) =>
                      rowIndex === index ? { ...row, protocol: event.target.value as "tcp" | "udp" } : row,
                    ),
                  )
                }
              >
                <option value="tcp">tcp</option>
                <option value="udp">udp</option>
              </select>
              <Button
                size="sm"
                variant="ghost"
                icon={<IconAlert size={15} />}
                aria-label={t("action.delete")}
                onClick={() => setForwarded((current) => current.filter((_, rowIndex) => rowIndex !== index))}
              />
            </div>
          ))}
          <Button
            size="sm"
            icon={<IconPlus size={15} />}
            onClick={() => setForwarded((current) => [...current, { expose: "", target: "", protocol: "tcp" }])}
          >
            {t("tunnels.addForwarded")}
          </Button>
        </div>

        {formError && <div className="form-error">{formError}</div>}
      </Modal>

      {/* Plan preview */}
      <Modal
        open={plan !== null}
        onClose={() => setPlan(null)}
        wide
        title={`${t("tunnels.plan")} · ${plan?.tunnel.name ?? ""}`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setPlan(null)}>
              {t("action.close")}
            </Button>
            {plan && canWrite && (
              <Button
                variant="primary"
                loading={busyId === plan.tunnel.id}
                onClick={() => void deploy(plan.tunnel).then(() => setPlan(null))}
              >
                {t("action.deploy")}
              </Button>
            )}
          </>
        }
      >
        {plan && (
          <div className="stack">
            <span className="faint">{t("tunnels.confirmApply")}</span>
            {plan.plan.notes.length > 0 && (
              <ul className="notes">
                {plan.plan.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            )}
            {(["local", "remote"] as const).map((side) => {
              const entry = plan.plan[side];
              return (
                <div key={side} className="stack">
                  <span className="field__label">
                    {side === "local" ? t("tunnels.previewLocal") : t("tunnels.previewRemote")} · {entry.node_name}
                  </span>
                  {entry.files.map((file) => (
                    <details key={file.path} className="file-preview">
                      <summary className="mono" dir="ltr">
                        {file.path}
                      </summary>
                      <CodeBlock content={file.content} maxHeight={240} />
                    </details>
                  ))}
                  {entry.units.map((unit) => (
                    <details key={unit.name} className="file-preview">
                      <summary className="mono" dir="ltr">
                        systemd: {unit.name}
                      </summary>
                      <CodeBlock content={unit.content} maxHeight={200} />
                    </details>
                  ))}
                  {entry.files.length === 0 && entry.units.length === 0 && (
                    <span className="faint">{t("state.none")}</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Modal>

      {/* Artifacts */}
      <Modal open={artifacts !== null} onClose={() => setArtifacts(null)} title={t("tunnels.artifacts")}>
        {artifacts && (
          <div className="stack">
            {artifacts.service && <span className="badge badge--idle">{artifacts.service}</span>}
            {(["local", "remote"] as const).map((side) => (
              <div key={side} className="stack">
                <span className="field__label">
                  {side === "local" ? t("tunnels.local") : t("tunnels.remote")} · {artifacts[side].node}
                </span>
                <ul className="notes mono" dir="ltr">
                  {[...artifacts[side].files, ...artifacts[side].units].map((path) => (
                    <li key={path}>{path}</li>
                  ))}
                  {artifacts[side].files.length === 0 && artifacts[side].units.length === 0 && (
                    <li className="faint">{t("state.none")}</li>
                  )}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Modal>

      {/* BackPack link */}
      <Modal
        open={linkOpen}
        onClose={() => setLinkOpen(false)}
        title={t("tunnels.backpackLink")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setLinkOpen(false)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={!backpackLink.trim()}
              onClick={async () => {
                try {
                  const result = await endpoints.backpackLink(
                    backpackLink.trim(),
                    linkName.trim() || undefined,
                    linkHost.trim() || undefined,
                  );
                  toast.success(t("tunnels.backpackLink"), result.message);
                  setLinkOpen(false);
                  setBackpackLink("");
                  await Promise.all([backpack.reload({ silent: true }), tunnels.reload({ silent: true })]);
                } catch (error) {
                  toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                }
              }}
            >
              {t("action.apply")}
            </Button>
          </>
        }
      >
        <div className="stack">
          <span className="field__hint">{t("tunnels.backpackLinkHint")}</span>
          <Field label="backpack://" htmlFor="bp-link">
            <Input
              id="bp-link"
              dir="ltr"
              value={backpackLink}
              onChange={(event) => setBackpackLink(event.target.value)}
              placeholder="backpack://…"
            />
          </Field>
          <div className="form-grid">
            <Field label={t("tunnels.name")} htmlFor="bp-name">
              <Input id="bp-name" value={linkName} onChange={(event) => setLinkName(event.target.value)} />
            </Field>
            <Field label={t("common.host")} htmlFor="bp-host">
              <Input id="bp-host" dir="ltr" value={linkHost} onChange={(event) => setLinkHost(event.target.value)} />
            </Field>
          </div>
        </div>
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
            await endpoints.deleteTunnel(deleting.id, teardown);
            toast.success(t("action.delete"));
            setDeleting(null);
            await tunnels.reload({ silent: true });
          } catch (error) {
            toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
          } finally {
            setBusyId(null);
          }
        }}
        extra={
          <Checkbox label={t("action.deleteTeardown")} checked={teardown} onChange={setTeardown} />
        }
      />
    </AppShell>
  );
}
