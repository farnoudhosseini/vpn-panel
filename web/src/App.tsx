/**
 * Routing.
 *
 * Two gates stand between the URL and a page: the session must have finished
 * resolving (otherwise a reload would bounce a signed-in operator to the login
 * screen for a frame), and it must be signed in. Pages render their own
 * `AppShell`, so the gate only decides *which* page mounts.
 */

import { Suspense, lazy, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { Button } from "./components/ui";
import { useI18n } from "./lib/i18n";
import { SessionProvider, useSession } from "./lib/session";

import DashboardPage from "./pages/Dashboard";
import LoginPage from "./pages/Login";

// The sign-in screen is the only page on the critical path, so the rest are
// split out and fetched when they are first visited.
const NodesPage = lazy(() => import("./pages/Nodes"));
const NodeDetailPage = lazy(() => import("./pages/NodeDetail"));
const TopologyPage = lazy(() => import("./pages/Topology"));
const InboundsPage = lazy(() => import("./pages/Inbounds"));
const InboundDetailPage = lazy(() => import("./pages/InboundDetail"));
const ClientsPage = lazy(() => import("./pages/Clients"));
const TunnelsPage = lazy(() => import("./pages/Tunnels"));
const ActivityPage = lazy(() => import("./pages/Activity"));
const SettingsPage = lazy(() => import("./pages/Settings"));

function BootScreen() {
  const { t } = useI18n();
  return (
    <div className="boot" role="status" aria-live="polite">
      <span className="boot__mark" aria-hidden="true">
        پ
      </span>
      <span className="boot__text">{t("app.booting")}</span>
      <span className="boot__bar">
        <span className="boot__barFill" />
      </span>
    </div>
  );
}

function PageFallback() {
  const { t } = useI18n();
  return (
    <div className="boot boot--inline" role="status" aria-live="polite">
      <span className="boot__text">{t("state.loading")}</span>
    </div>
  );
}

function NotFound() {
  const { t } = useI18n();
  return (
    <div className="boot" role="alert">
      <span className="boot__text">{t("app.notFound")}</span>
      <span className="boot__text faint">{t("app.notFoundHint")}</span>
      <Button variant="primary" onClick={() => window.location.assign("/")}>
        {t("app.backHome")}
      </Button>
    </div>
  );
}

/** Session resolution guard: nothing else may render until we know who is here. */
function RequireSession({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const location = useLocation();

  if (status === "loading") return <BootScreen />;
  if (status === "signed-out") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

/** The login page is public, but a signed-in operator should not see it. */
function PublicOnly({ children }: { children: ReactNode }) {
  const { status } = useSession();
  if (status === "loading") return <BootScreen />;
  if (status === "signed-in") return <Navigate to="/" replace />;
  return <>{children}</>;
}

function AppRoutes() {
  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        <Route
          path="/login"
          element={
            <PublicOnly>
              <LoginPage />
            </PublicOnly>
          }
        />
        <Route
          path="/"
          element={
            <RequireSession>
              <DashboardPage />
            </RequireSession>
          }
        />
        <Route
          path="/nodes"
          element={
            <RequireSession>
              <NodesPage />
            </RequireSession>
          }
        />
        <Route
          path="/nodes/:id"
          element={
            <RequireSession>
              <NodeDetailPage />
            </RequireSession>
          }
        />
        <Route
          path="/topology"
          element={
            <RequireSession>
              <TopologyPage />
            </RequireSession>
          }
        />
        <Route
          path="/inbounds"
          element={
            <RequireSession>
              <InboundsPage />
            </RequireSession>
          }
        />
        <Route
          path="/inbounds/:id"
          element={
            <RequireSession>
              <InboundDetailPage />
            </RequireSession>
          }
        />
        <Route
          path="/clients"
          element={
            <RequireSession>
              <ClientsPage />
            </RequireSession>
          }
        />
        <Route
          path="/tunnels"
          element={
            <RequireSession>
              <TunnelsPage />
            </RequireSession>
          }
        />
        <Route
          path="/activity"
          element={
            <RequireSession>
              <ActivityPage />
            </RequireSession>
          }
        />
        <Route
          path="/settings"
          element={
            <RequireSession>
              <SettingsPage />
            </RequireSession>
          }
        />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <SessionProvider>
        <AppRoutes />
      </SessionProvider>
    </BrowserRouter>
  );
}
