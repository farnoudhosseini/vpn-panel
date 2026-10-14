/**
 * Blueprint.
 *
 * Every managed node is a card on one canvas. Links between them are tunnels.
 * Dragging a card moves it (positions are remembered per browser), and dragging
 * the red handle from one card onto another opens the tunnel form. This is the
 * fastest path from "two servers" to "connected": no forms to fill before you
 * can see the shape of the network.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { AppShell, statusTone } from "../components/AppShell";
import { IconAlert, IconLink, IconPlus, IconServer, IconTunnel } from "../components/Icons";
import {
  Badge,
  Button,
  Checkbox,
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
import { useAsync } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { Transport } from "../lib/types";

const POSITIONS_KEY = "panah.blueprint.positions";
const CARD_W = 186;
const CARD_H = 88;
const CANVAS_PAD = 48;

interface Point {
  x: number;
  y: number;
}

interface Forwarded {
  expose: string;
  target: string;
  protocol: "tcp" | "udp";
}

function readPositions(): Record<string, Point> {
  try {
    const raw = localStorage.getItem(POSITIONS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, Point>;
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch {
    return {};
  }
}

/** Lay nodes out on a loose spiral so the first paint is never a heap. */
function defaultPosition(index: number): Point {
  const columns = 4;
  const column = index % columns;
  const row = Math.floor(index / columns);
  return {
    x: CANVAS_PAD + column * (CARD_W + 46) + (row % 2 ? 24 : 0),
    y: CANVAS_PAD + row * (CARD_H + 54),
  };
}

export default function TopologyPage() {
  const { t } = useI18n();
  const { account } = useSession();
  const toast = useToast();
  const canWrite = account?.role === "admin" || account?.role === "operator";

  const nodes = useAsync(() => endpoints.nodes(), [], { pollMs: 30_000 });
  const tunnels = useAsync(() => endpoints.tunnels(), [], { pollMs: 30_000 });
  const transports = useAsync(() => endpoints.transports(), []);

  const canvasRef = useRef<HTMLDivElement>(null);
  const [positions, setPositions] = useState<Record<string, Point>>(readPositions);
  const [dragging, setDragging] = useState<{ id: number; dx: number; dy: number } | null>(null);
  const [linking, setLinking] = useState<{ from: number; x: number; y: number } | null>(null);
  const [hoverNode, setHoverNode] = useState<number | null>(null);
  const [pending, setPending] = useState<{ from: number; to: number } | null>(null);
  const [transportKey, setTransportKey] = useState("stealth");
  const [listenPort, setListenPort] = useState("443");
  const [mtu, setMtu] = useState("1420");
  const [forwarded, setForwarded] = useState<Forwarded[]>([{ expose: "443", target: "8443", protocol: "tcp" }]);
  const [deployNow, setDeployNow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(POSITIONS_KEY, JSON.stringify(positions));
    } catch {
      /* the layout is cosmetic; losing it is harmless */
    }
  }, [positions]);

  const items = useMemo(
    () => [...(nodes.data ?? [])].sort((a, b) => Number(b.is_local) - Number(a.is_local) || a.id - b.id),
    [nodes.data],
  );

  const positionOf = useCallback(
    (id: number): Point => positions[String(id)] ?? defaultPosition(items.findIndex((node) => node.id === id)),
    [positions, items],
  );

  useEffect(() => {
    if (!dragging && !linking) return;
    const onMove = (event: PointerEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect();
      if (!rect) return;
      if (dragging) {
        setPositions((current) => ({
          ...current,
          [String(dragging.id)]: {
            x: Math.max(0, event.clientX - rect.left - dragging.dx + (canvasRef.current?.scrollLeft ?? 0)),
            y: Math.max(0, event.clientY - rect.top - dragging.dy + (canvasRef.current?.scrollTop ?? 0)),
          },
        }));
      }
      if (linking) {
        setLinking({ ...linking, x: event.clientX - rect.left, y: event.clientY - rect.top });
      }
    };
    const onUp = (event: PointerEvent) => {
      if (linking) {
        const target = document
          .elementsFromPoint(event.clientX, event.clientY)
          .find((element) => element instanceof HTMLElement && element.dataset.nodeId);
        const targetId = target instanceof HTMLElement ? Number(target.dataset.nodeId) : null;
        if (targetId && targetId !== linking.from) {
          setTransportKey("stealth");
          setFormError(null);
          setPending({ from: linking.from, to: targetId });
        } else if (targetId && targetId === linking.from) {
          toast.failure(t("bp.selfLink"));
        }
        setLinking(null);
        setHoverNode(null);
      }
      setDragging(null);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, linking, t, toast]);

  const canvasSize = useMemo(() => {
    let width = 900;
    let height = 460;
    for (const node of items) {
      const point = positionOf(node.id);
      width = Math.max(width, point.x + CARD_W + CANVAS_PAD);
      height = Math.max(height, point.y + CARD_H + CANVAS_PAD);
    }
    return { width, height };
  }, [items, positionOf]);

  const nodeName = useCallback(
    (id: number) => items.find((node) => node.id === id)?.name ?? `#${id}`,
    [items],
  );

  const createTunnel = async () => {
    if (!pending) return;
    setBusy(true);
    setFormError(null);
    const spec = (transports.data ?? []).find((entry: Transport) => entry.key === transportKey);
    try {
      await endpoints.createTunnel(
        {
          name: `${nodeName(pending.from)}-${nodeName(pending.to)}`.slice(0, 70),
          transport: transportKey,
          mode: spec?.layer === "l3" && spec?.engine === "native" ? "direct" : "reverse",
          local_node_id: pending.from,
          remote_node_id: pending.to,
          listen_port: Number(listenPort) || 443,
          mtu: Number(mtu) || 1420,
          forwarded_ports: forwarded
            .filter((entry) => entry.expose.trim())
            .map((entry) => ({
              expose: Number(entry.expose) || 0,
              target: entry.target.includes(":") ? entry.target.trim() : Number(entry.target) || 0,
              protocol: entry.protocol,
            }))
            .filter((entry) => entry.expose > 0 && entry.target !== 0),
          enabled: true,
        },
        deployNow,
      );
      toast.success(t("bp.linked"), deployNow ? t("tunnels.applied") : t("bp.createdHint"));
      setPending(null);
      await tunnels.reload({ silent: true });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  if (nodes.loading && items.length === 0) {
    return (
      <AppShell title={t("bp.title")} subtitle={t("bp.subtitle")}>
        <LoadingRows rows={3} />
      </AppShell>
    );
  }
  if (nodes.error && items.length === 0) {
    return (
      <AppShell title={t("bp.title")}>
        <ErrorState message={nodes.error} onRetry={() => void nodes.reload()} />
      </AppShell>
    );
  }

  return (
    <AppShell
      title={t("bp.title")}
      subtitle={t("bp.subtitle")}
      actions={
        <Button size="sm" variant="ghost" loading={nodes.refreshing} onClick={() => void nodes.reload({ silent: true })}>
          {t("action.refresh")}
        </Button>
      }
    >
      {items.length < 2 ? (
        <Panel>
          <EmptyState
            title={t("bp.noNodes")}
            hint={t("nodes.emptyHint")}
            icon={<IconServer size={26} />}
            action={
              <Link to="/nodes">
                <Button size="sm" variant="primary">
                  {t("bp.noNodesAction")}
                </Button>
              </Link>
            }
          />
        </Panel>
      ) : (
        <Panel
          title={t("bp.links")}
          hint={`${items.length} ${t("nav.nodes")} · ${(tunnels.data ?? []).length} ${t("nav.tunnels")}`}
          flush
        >
          <div className="blueprint-head">
            <span className="legend__item">
              <span className="legend__swatch" style={{ background: "var(--crimson)" }} />
              {t("bp.legend.link")}
            </span>
            <span className="legend__item">
              <span className="legend__swatch" style={{ background: "var(--slate)" }} />
              {t("bp.legend.drag")}
            </span>
            <span className="legend__item faint">{t("bp.legend.delete")}</span>
          </div>

          <div className="blueprint" ref={canvasRef} dir="ltr">
            <svg
              className="blueprint__wires"
              width={canvasSize.width}
              height={canvasSize.height}
              aria-hidden="true"
            >
              {(tunnels.data ?? []).map((tunnel) => {
                const from = positionOf(tunnel.local_node_id);
                const to = positionOf(tunnel.remote_node_id);
                const start: Point = { x: from.x + CARD_W / 2, y: from.y + CARD_H / 2 };
                const end: Point = { x: to.x + CARD_W / 2, y: to.y + CARD_H / 2 };
                const tone =
                  tunnel.status === "up"
                    ? "var(--jade)"
                    : tunnel.status === "error"
                      ? "var(--crimson)"
                      : "var(--slate)";
                return (
                  <g key={tunnel.id} className="wire">
                    <line
                      x1={start.x}
                      y1={start.y}
                      x2={end.x}
                      y2={end.y}
                      stroke={tone}
                      strokeWidth={1.8}
                      strokeDasharray={tunnel.status === "up" ? undefined : "5 5"}
                    />
                    <circle cx={(start.x + end.x) / 2} cy={(start.y + end.y) / 2} r={3.4} fill={tone} />
                    <text
                      x={(start.x + end.x) / 2}
                      y={(start.y + end.y) / 2 - 9}
                      textAnchor="middle"
                      className="wire__label"
                    >
                      {tunnel.transport}
                      {tunnel.status === "up" ? "" : ` · ${t("state.down")}`}
                    </text>
                  </g>
                );
              })}
              {linking && (
                <line
                  x1={positionOf(linking.from).x + CARD_W / 2}
                  y1={positionOf(linking.from).y + CARD_H / 2}
                  x2={linking.x + (canvasRef.current?.scrollLeft ?? 0)}
                  y2={linking.y + (canvasRef.current?.scrollTop ?? 0)}
                  stroke="var(--crimson)"
                  strokeWidth={1.6}
                  strokeDasharray="4 4"
                />
              )}
            </svg>

            {items.map((node) => {
              const point = positionOf(node.id);
              const tone = statusTone(node.status);
              const isTarget = hoverNode === node.id && linking !== null && linking.from !== node.id;
              return (
                <div
                  key={node.id}
                  data-node-id={node.id}
                  className="bp-node"
                  style={{
                    left: point.x,
                    top: point.y,
                    width: CARD_W,
                    height: CARD_H,
                    borderColor: isTarget ? "var(--crimson)" : undefined,
                  }}
                >
                  <div
                    className="bp-node__grip"
                    onPointerDown={(event) => {
                      const rect = (event.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
                      setDragging({ id: node.id, dx: event.clientX - rect.left, dy: event.clientY - rect.top });
                      setHoverNode(null);
                    }}
                  >
                    <span
                      aria-hidden="true"
                      style={{
                        width: 7,
                        height: 7,
                        borderRadius: 999,
                        background: tone,
                        boxShadow: node.status === "online" ? `0 0 8px ${tone}` : "none",
                        flex: "none",
                      }}
                    />
                    <span className="bp-node__name">{node.name}</span>
                    <span className="bp-node__badge">
                      {node.is_local ? t("nodes.localBadge") : node.address}
                    </span>
                  </div>
                  <div className="row" style={{ justifyContent: "space-between", marginTop: 4 }}>
                    <StatusBadge status={node.status} />
                    <span className="faint mono" style={{ fontSize: 10.5 }}>
                      {node.cpu_percent.toFixed(0)}% · {node.memory_percent.toFixed(0)}%
                    </span>
                  </div>
                  {canWrite && (
                    <button
                      type="button"
                      className="bp-node__port"
                      title={t("bp.hint")}
                      aria-label={t("bp.newTunnel")}
                      onPointerDown={(event) => {
                        event.stopPropagation();
                        const rect = canvasRef.current?.getBoundingClientRect();
                        if (!rect) return;
                        setLinking({
                          from: node.id,
                          x: event.clientX - rect.left,
                          y: event.clientY - rect.top,
                        });
                      }}
                      onPointerEnter={() => linking && setHoverNode(node.id)}
                      onPointerLeave={() => setHoverNode((current) => (current === node.id ? null : current))}
                    >
                      <IconLink size={13} />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </Panel>
      )}

      <Panel title={t("bp.existing")} flush>
        {(tunnels.data ?? []).length === 0 ? (
          <EmptyState title={t("tunnels.empty")} hint={t("tunnels.emptyHint")} icon={<IconTunnel size={26} />} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("tunnels.name")}</th>
                  <th>{t("tunnels.local")}</th>
                  <th>{t("tunnels.remote")}</th>
                  <th>{t("tunnels.transport")}</th>
                  <th>{t("common.status")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(tunnels.data ?? []).map((tunnel) => (
                  <tr key={tunnel.id}>
                    <td className="cell-primary">{tunnel.name}</td>
                    <td>{nodeName(tunnel.local_node_id)}</td>
                    <td>{nodeName(tunnel.remote_node_id)}</td>
                    <td>
                      <span className="row row--tight">
                        <span className="mono">{tunnel.transport}</span>
                        <Badge tone={tunnel.engine === "backpack" ? "warn" : "idle"}>
                          {tunnel.engine}
                        </Badge>
                      </span>
                    </td>
                    <td>
                      <StatusBadge status={tunnel.status} />
                    </td>
                    <td className="nowrap">
                      <Link to="/tunnels">
                        <Button size="sm" variant="ghost">
                          {t("action.details")}
                        </Button>
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        title={t("bp.newTunnel")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={busy}>
              {t("action.cancel")}
            </Button>
            <Button variant="primary" onClick={() => void createTunnel()} loading={busy}>
              {deployNow ? t("action.deploy") : t("action.create")}
            </Button>
          </>
        }
      >
        {pending && (
          <div className="stack">
            <div className="stack" style={{ gap: 4 }}>
              <span className="field__label">{t("tunnels.localNode")} → {t("tunnels.remoteNode")}</span>
              <span className="cell-primary" dir="ltr">
                {nodeName(pending.from)} → {nodeName(pending.to)}
              </span>
            </div>

            <Field label={t("bp.selectTransport")} htmlFor="bp-transport">
              <select
                id="bp-transport"
                className="select"
                value={transportKey}
                onChange={(event) => setTransportKey(event.target.value)}
              >
                {(transports.data ?? []).map((transport: Transport) => (
                  <option key={transport.key} value={transport.key}>
                    {transport.label} · {transport.engine} · {transport.layer === "l3" ? t("tunnels.l3") : t("tunnels.l4")}
                  </option>
                ))}
              </select>
            </Field>

            <div className="form-grid">
              <Field label={t("tunnels.listenPort")} htmlFor="bp-port">
                <Input
                  id="bp-port"
                  dir="ltr"
                  inputMode="numeric"
                  value={listenPort}
                  onChange={(event) => setListenPort(event.target.value)}
                />
              </Field>
              <Field label={t("tunnels.mtu")} htmlFor="bp-mtu">
                <Input
                  id="bp-mtu"
                  dir="ltr"
                  inputMode="numeric"
                  value={mtu}
                  onChange={(event) => setMtu(event.target.value)}
                />
              </Field>
            </div>

            <div className="stack">
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
                        current.map((row, rowIndex) =>
                          rowIndex === index ? { ...row, expose: event.target.value } : row,
                        ),
                      )
                    }
                  />
                  <Input
                    dir="ltr"
                    placeholder={t("tunnels.target")}
                    value={entry.target}
                    onChange={(event) =>
                      setForwarded((current) =>
                        current.map((row, rowIndex) =>
                          rowIndex === index ? { ...row, target: event.target.value } : row,
                        ),
                      )
                    }
                  />
                  <select
                    className="select"
                    value={entry.protocol}
                    onChange={(event) =>
                      setForwarded((current) =>
                        current.map((row, rowIndex) =>
                          rowIndex === index
                            ? { ...row, protocol: event.target.value as "tcp" | "udp" }
                            : row,
                        ),
                      )
                    }
                  >
                    <option value="tcp">tcp</option>
                    <option value="udp">udp</option>
                  </select>
                  <Button
                    variant="ghost"
                    size="sm"
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

            <Checkbox
              checked={deployNow}
              onChange={setDeployNow}
              label={
                <span>
                  {t("tunnels.apply")}
                  <span className="field__hint" style={{ display: "block" }}>
                    {t("tunnels.confirmApply")}
                  </span>
                </span>
              }
            />

            {formError && <div className="form-error">{formError}</div>}
          </div>
        )}
      </Modal>
    </AppShell>
  );
}
