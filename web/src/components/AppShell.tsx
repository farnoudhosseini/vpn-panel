/** Application shell: navigation rail, header and the live node ridge. */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";

import { endpoints } from "../lib/api";
import { useI18n } from "../lib/i18n";
import { useAsync } from "../lib/hooks";
import { useSession } from "../lib/session";
import { Sparkline } from "./charts";
import {
  IconActivity,
  IconDashboard,
  IconInbound,
  IconLogout,
  IconMoon,
  IconServer,
  IconSettings,
  IconSun,
  IconTopology,
  IconTunnel,
  IconUsers,
} from "./Icons";
import { Button } from "./ui";
import type { OverviewNode } from "../lib/types";

const THEME_KEY = "panah.theme";

function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">(
    () => (document.documentElement.dataset.theme === "light" ? "light" : "dark"),
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  return { theme, toggle: () => setTheme((current) => (current === "dark" ? "light" : "dark")) };
}

function statusTone(status: string): string {
  if (status === "online") return "var(--jade)";
  if (status === "degraded") return "var(--amber)";
  if (status === "offline") return "var(--crimson)";
  return "var(--slate)";
}

/**
 * The ridge: one tick per node with its live connections. This is the product's
 * signature reading — the whole fleet visible as a row of instruments.
 */
function NodeRidge({ nodes, onSelect }: { nodes: OverviewNode[]; onSelect?: (id: number) => void }) {
  const t = useI18n().t;
  if (nodes.length === 0) return null;

  return (
    <div className="ridge" role="list">
      {nodes.map((node) => {
        const tone = statusTone(node.status);
        return (
          <button
            key={node.id}
            type="button"
            role="listitem"
            className="ridge__tick"
            onClick={() => onSelect?.(node.id)}
            title={`${node.name} — ${node.address}`}
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
            <span style={{ minWidth: 0 }}>
              <span className="ridge__name" style={{ display: "block" }}>
                {node.name}
                {node.is_local && <span className="faint"> · {t("nodes.localBadge")}</span>}
              </span>
              <Sparkline
                values={node.series.length > 1 ? node.series : [0, 0]}
                tone={tone}
                height={14}
              />
            </span>
            <span className="ridge__value">
              {node.connections}
              <span className="faint"> · {node.status === "online" ? "" : t("state.offline")}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

interface NavEntry {
  to: string;
  label: string;
  icon: ReactNode;
}

export function AppShell({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const t = useI18n().t;
  const { lang, setLang } = useI18n();
  const { account, signOut } = useSession();
  const { theme, toggle } = useTheme();
  const navigate = useNavigate();

  const overview = useAsync(() => endpoints.overview(30), [], { pollMs: 20_000 });
  const nodes: OverviewNode[] = overview.data?.nodes ?? [];

  const handleSignOut = useCallback(async () => {
    await signOut();
    navigate("/login", { replace: true });
  }, [navigate, signOut]);

  const groups: { label: string; items: NavEntry[] }[] = [
    {
      label: t("nav.group.main"),
      items: [
        { to: "/", label: t("nav.dashboard"), icon: <IconDashboard size={17} /> },
        { to: "/nodes", label: t("nav.nodes"), icon: <IconServer size={17} /> },
        { to: "/topology", label: t("nav.blueprint"), icon: <IconTopology size={17} /> },
        { to: "/inbounds", label: t("nav.inbounds"), icon: <IconInbound size={17} /> },
        { to: "/clients", label: t("nav.clients"), icon: <IconUsers size={17} /> },
        { to: "/tunnels", label: t("nav.tunnels"), icon: <IconTunnel size={17} /> },
      ],
    },
    {
      label: t("nav.group.system"),
      items: [
        { to: "/activity", label: t("nav.logs"), icon: <IconActivity size={17} /> },
        { to: "/settings", label: t("nav.settings"), icon: <IconSettings size={17} /> },
      ],
    },
  ];

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            پ
          </span>
          <span>
            <span className="brand__name" style={{ display: "block" }}>
              {t("app.name")}
            </span>
            <span className="brand__meta">{t("app.tagline")}</span>
          </span>
        </div>

        {groups.map((group) => (
          <div key={group.label}>
            <div className="rail__group">{group.label}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className="rail__link"
              >
                {item.icon}
                <span>{item.label}</span>
              </NavLink>
            ))}
          </div>
        ))}

        <div className="rail__foot">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="row row--tight" style={{ minWidth: 0 }}>
              <span
                aria-hidden="true"
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: 8,
                  background: "var(--surface-3)",
                  display: "grid",
                  placeItems: "center",
                  fontSize: 12,
                  fontWeight: 600,
                  flex: "none",
                }}
              >
                {account?.username.slice(0, 1).toUpperCase() ?? "?"}
              </span>
              <span style={{ minWidth: 0 }}>
                <span style={{ fontSize: 13, display: "block" }}>{account?.username ?? "—"}</span>
                <span className="brand__meta">{t(`settings.role.${account?.role ?? "viewer"}` as "settings.role.viewer")}</span>
              </span>
            </span>
            <Button
              variant="ghost"
              size="sm"
              icon={<IconLogout size={16} />}
              onClick={handleSignOut}
              aria-label={t("action.logout")}
              title={t("action.logout")}
            />
          </div>
          <div className="row row--tight">
            <Button
              variant="ghost"
              size="sm"
              icon={theme === "dark" ? <IconMoon size={15} /> : <IconSun size={15} />}
              onClick={toggle}
            >
              {theme === "dark" ? t("settings.theme.dark") : t("settings.theme.light")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setLang(lang === "fa" ? "en" : "fa")}
            >
              {lang === "fa" ? "EN" : "فا"}
            </Button>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar__row">
            <div className="topbar__title">
              <h1>{title}</h1>
              {subtitle && <span className="topbar__sub">{subtitle}</span>}
            </div>
            <div className="topbar__spacer" />
            {actions}
          </div>
          <NodeRidge nodes={nodes} onSelect={(id) => navigate(`/nodes/${id}`)} />
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

/** Shared with the pages so node status reads the same everywhere. */
export { statusTone };
