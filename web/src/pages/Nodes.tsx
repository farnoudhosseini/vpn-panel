/** Node fleet: register servers, watch health, open the per-node view. */

import { useState } from "react";
import { Link } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import {
  IconCheck,
  IconPlus,
  IconServer,
  IconZap,
} from "../components/Icons";
import { LoadBar, Sparkline } from "../components/charts";
import {
  Button,
  ConfirmDialog,
  CopyButton,
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
import { statusTone } from "../components/AppShell";
import type { NodeInfo } from "../lib/types";

interface NodeForm {
  name: string;
  address: string;
  agent_port: string;
  scheme: "http" | "https";
  api_key: string;
  insecure_tls: boolean;
  note: string;
  tags: string;
}

const emptyForm: NodeForm = {
  name: "",
  address: "",
  agent_port: "8787",
  scheme: "https",
  api_key: "",
  insecure_tls: false,
  note: "",
  tags: "",
};

export default function NodesPage() {
  const { t, lang } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";

  const nodes = useAsync(() => endpoints.nodes(), [], { pollMs: 30_000 });
  const [editing, setEditing] = useState<NodeInfo | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<NodeForm>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<NodeInfo | null>(null);
  const [forceDelete, setForceDelete] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (node: NodeInfo) => {
    setEditing(node);
    setForm({
      name: node.name,
      address: node.address,
      agent_port: String(node.agent_port),
      scheme: node.scheme,
      api_key: "",
      insecure_tls: node.insecure_tls,
      note: node.note ?? "",
      tags: node.tags.join(", "),
    });
    setFormError(null);
    setFormOpen(true);
  };

  const submit = async () => {
    setSaving(true);
    setFormError(null);
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      address: form.address.trim(),
      agent_port: Number(form.agent_port) || 8787,
      scheme: form.scheme,
      insecure_tls: form.insecure_tls,
      note: form.note.trim() || null,
      tags: form.tags
        .split(",")
        .map((tag) => tag.trim())
        .filter(Boolean),
    };
    if (form.api_key.trim()) payload.api_key = form.api_key.trim();
    try {
      if (editing) {
        await endpoints.updateNode(editing.id, payload);
        toast.success(t("action.save"));
      } else {
        await endpoints.createNode(payload);
        toast.success(t("nodes.add"));
      }
      setFormOpen(false);
      await nodes.reload({ silent: true });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  const test = async (node: NodeInfo) => {
    setBusyId(node.id);
    try {
      const result = await endpoints.testNode(node.id);
      if (result.ok) toast.success(t("nodes.testOk"), result.message);
      else toast.failure(t("nodes.testFailed"), result.message);
      await nodes.reload({ silent: true });
    } catch (error) {
      toast.failure(t("nodes.testFailed"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusyId(null);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusyId(deleting.id);
    try {
      await endpoints.deleteNode(deleting.id, forceDelete);
      toast.success(t("action.delete"));
      setDeleting(null);
      setForceDelete(false);
      await nodes.reload({ silent: true });
    } catch (error) {
      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
    } finally {
      setBusyId(null);
    }
  };

  const items = nodes.data ?? [];
  const installCommand = `curl -fsSL https://raw.githubusercontent.com/farnoudhosseini/vpn-panel/main/install.sh | sudo bash -s -- --mode node --panel https://${window.location.host}`;

  return (
    <AppShell
      title={t("nodes.title")}
      subtitle={t("nodes.subtitle")}
      actions={
        <>
          <Button size="sm" variant="ghost" loading={nodes.refreshing} onClick={() => void nodes.reload({ silent: true })}>
            {t("action.refresh")}
          </Button>
          {canWrite && (
            <Button size="sm" variant="primary" icon={<IconPlus size={15} />} onClick={openCreate}>
              {t("nodes.add")}
            </Button>
          )}
        </>
      }
    >
      {nodes.loading && items.length === 0 ? (
        <LoadingRows rows={4} />
      ) : nodes.error && items.length === 0 ? (
        <ErrorState message={nodes.error} onRetry={() => void nodes.reload()} />
      ) : items.length === 0 ? (
        <Panel>
          <EmptyState
            title={t("nodes.empty")}
            hint={t("nodes.emptyHint")}
            icon={<IconServer size={26} />}
            action={
              canWrite ? (
                <Button size="sm" variant="primary" onClick={openCreate} icon={<IconPlus size={15} />}>
                  {t("nodes.add")}
                </Button>
              ) : undefined
            }
          />
          <div className="stack" style={{ marginTop: 18 }}>
            <span className="field__hint">{t("nodes.installHint")}</span>
            <div className="row row--tight">
              <code className="code code--inline" dir="ltr">
                {installCommand}
              </code>
              <CopyButton text={installCommand} />
            </div>
          </div>
        </Panel>
      ) : (
        <div className="grid grid--nodes">
          {items.map((node) => (
            <Panel
              key={node.id}
              className="node-card"
              title={
                <span className="row row--tight">
                  <span
                    aria-hidden="true"
                    style={{
                      width: 8,
                      height: 8,
                      borderRadius: 999,
                      background: statusTone(node.status),
                      boxShadow: node.status === "online" ? `0 0 8px ${statusTone(node.status)}` : "none",
                    }}
                  />
                  <Link to={`/nodes/${node.id}`}>{node.name}</Link>
                  {node.is_local && <span className="badge badge--idle">{t("nodes.localBadge")}</span>}
                </span>
              }
              actions={
                <span className="row row--tight">
                  <StatusBadge status={node.status} />
                </span>
              }
              footer={
                <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
                  <span className="faint" style={{ fontSize: 12 }}>
                    {t("nodes.lastSeen")}: {relativeTime(node.last_seen_at, lang)}
                  </span>
                  <span className="row row--tight">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => void test(node)}
                      loading={busyId === node.id}
                      disabled={!canWrite}
                    >
                      {t("action.test")}
                    </Button>
                    <Link to={`/nodes/${node.id}`}>
                      <Button size="sm" variant="ghost">
                        {t("action.details")}
                      </Button>
                    </Link>
                  </span>
                </div>
              }
            >
              <div className="stack">
                <span className="mono faint" dir="ltr">
                  {node.scheme}://{node.address}:{node.agent_port}
                </span>
                <div className="stack" style={{ gap: 7 }}>
                  <LoadBar label={t("nodes.cpu")} percent={node.cpu_percent} />
                  <LoadBar label={t("nodes.ram")} percent={node.memory_percent} />
                  <LoadBar label={t("nodes.disk")} percent={node.disk_percent} />
                </div>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="row row--tight">
                    {node.openvpn_installed ? (
                      <span className="badge badge--ok">
                        <IconCheck size={13} /> OpenVPN
                      </span>
                    ) : (
                      <span className="badge badge--idle">OpenVPN</span>
                    )}
                    {node.backpack_installed ? (
                      <span className="badge badge--ok">
                        <IconCheck size={13} /> BackPack
                      </span>
                    ) : (
                      <span className="badge badge--idle">BackPack</span>
                    )}
                  </span>
                  <Sparkline
                    values={[
                      node.cpu_percent || 0,
                      node.memory_percent || 0,
                      node.disk_percent || 0,
                      node.uptime_seconds > 0 ? 1 : 0,
                    ]}
                    tone={statusTone(node.status)}
                    height={16}
                    fill={false}
                  />
                </div>
                {node.last_error && (
                  <span className="cell-sub" style={{ color: "var(--crimson)" }}>
                    {node.last_error}
                  </span>
                )}
                {canWrite && (
                  <div className="row row--tight" style={{ marginTop: 2 }}>
                    <Button size="sm" variant="ghost" onClick={() => openEdit(node)}>
                      {t("action.edit")}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleting(node)}>
                      {t("action.delete")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={busyId === node.id}
                      onClick={async () => {
                        setBusyId(node.id);
                        try {
                          await endpoints.refreshNode(node.id);
                          await nodes.reload({ silent: true });
                        } catch (error) {
                          toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                        } finally {
                          setBusyId(null);
                        }
                      }}
                    >
                      <IconZap size={14} /> {t("action.refresh")}
                    </Button>
                  </div>
                )}
              </div>
            </Panel>
          ))}
        </div>
      )}

      <Modal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        title={editing ? t("nodes.edit") : t("nodes.add")}
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
          <Field label={t("nodes.name")} htmlFor="node-name">
            <Input
              id="node-name"
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="de-frankfurt"
            />
          </Field>
          <Field label={t("nodes.address")} hint={t("nodes.addressHint")} htmlFor="node-address">
            <Input
              id="node-address"
              dir="ltr"
              value={form.address}
              onChange={(event) => setForm({ ...form, address: event.target.value })}
              placeholder="203.0.113.10"
            />
          </Field>
          <Field label={t("nodes.agentPort")} htmlFor="node-port">
            <Input
              id="node-port"
              dir="ltr"
              inputMode="numeric"
              value={form.agent_port}
              onChange={(event) => setForm({ ...form, agent_port: event.target.value })}
            />
          </Field>
          <Field label={t("nodes.scheme")} htmlFor="node-scheme">
            <select
              id="node-scheme"
              className="select"
              value={form.scheme}
              onChange={(event) => setForm({ ...form, scheme: event.target.value as "http" | "https" })}
            >
              <option value="https">https</option>
              <option value="http">http</option>
            </select>
          </Field>
          <Field label={t("nodes.apiKey")} hint={t("nodes.apiKeyHint")} htmlFor="node-key">
            <Input
              id="node-key"
              dir="ltr"
              type="password"
              autoComplete="new-password"
              value={form.api_key}
              onChange={(event) => setForm({ ...form, api_key: event.target.value })}
            />
          </Field>
          <Field label={t("nodes.tags")} htmlFor="node-tags">
            <Input
              id="node-tags"
              value={form.tags}
              onChange={(event) => setForm({ ...form, tags: event.target.value })}
              placeholder="ir, eu"
            />
          </Field>
          <Field label={t("nodes.note")} htmlFor="node-note">
            <Input
              id="node-note"
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
            />
          </Field>
          <label className="checkbox" style={{ alignSelf: "end" }}>
            <input
              type="checkbox"
              checked={form.insecure_tls}
              onChange={(event) => setForm({ ...form, insecure_tls: event.target.checked })}
            />
            <span>
              {t("nodes.insecure")}
              <span className="field__hint" style={{ display: "block" }}>
                {t("nodes.insecureHint")}
              </span>
            </span>
          </label>
        </div>

        {formError && <div className="form-error">{formError}</div>}

        {!editing && (
          <div className="stack" style={{ marginTop: 16 }}>
            <span className="field__hint">{t("nodes.installHint")}</span>
            <div className="row row--tight">
              <code className="code code--inline" dir="ltr">
                {installCommand}
              </code>
              <CopyButton text={installCommand} />
            </div>
          </div>
        )}
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
        onCancel={() => {
          setDeleting(null);
          setForceDelete(false);
        }}
        onConfirm={() => void remove()}
        extra={
          <label className="checkbox">
            <input
              type="checkbox"
              checked={forceDelete}
              onChange={(event) => setForceDelete(event.target.checked)}
            />
            <span>
              {t("nodes.deleteForce")}
              <span className="field__hint" style={{ display: "block" }}>
                {t("nodes.deleteBlocked")}
              </span>
            </span>
          </label>
        }
      />
    </AppShell>
  );
}
