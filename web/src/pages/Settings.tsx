/** Settings: the operator's own account, panel users, keys and system health. */

import { useState } from "react";

import { AppShell } from "../components/AppShell";
import { IconCopy, IconKey, IconLock, IconPlus, IconShield } from "../components/Icons";
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
  KeyValue,
  LoadingRows,
  Modal,
  Panel,
  Tabs,
  useToast,
} from "../components/ui";
import { endpoints } from "../lib/api";
import { formatBytes, formatDate, formatDuration, relativeTime } from "../lib/format";
import { useAsync, usePersistentState } from "../lib/hooks";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import type { Account, ApiToken, ApiTokenCreated, Role } from "../lib/types";

type TabKey = "account" | "security" | "tls" | "users" | "tokens" | "system" | "database" | "appearance";

export default function SettingsPage() {
  const { t, lang, setLang } = useI18n();
  const { account, reload } = useSession();
  const toast = useToast();
  const isAdmin = account?.role === "admin";

  const [tab, setTab] = usePersistentState<TabKey>("panah.settings.tab", "account");

  const info = useAsync(() => (tab === "system" || tab === "database" ? endpoints.systemInfo() : Promise.resolve(null)), [tab]);
  const jobs = useAsync(() => (tab === "system" ? endpoints.jobs() : Promise.resolve(null)), [tab], { pollMs: 30_000 });
  const database = useAsync(() => (tab === "database" && isAdmin ? endpoints.database() : Promise.resolve(null)), [tab, isAdmin]);
  const users = useAsync(() => (tab === "users" && isAdmin ? endpoints.users() : Promise.resolve(null)), [tab, isAdmin]);
  const tokens = useAsync(() => (tab === "tokens" ? endpoints.tokens() : Promise.resolve(null)), [tab]);
  const sessions = useAsync(() => (tab === "security" ? endpoints.sessions() : Promise.resolve(null)), [tab]);

  const [passwordForm, setPasswordForm] = useState({ current: "", next: "", repeat: "" });
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [totpSetup, setTotpSetup] = useState<{ secret: string; provisioning_uri: string; qr_svg: string } | null>(null);
  const [totpCode, setTotpCode] = useState("");
  const [disableForm, setDisableForm] = useState({ password: "", code: "" });
  const [certForm, setCertForm] = useState({ domain: window.location.hostname, email: "", staging: true });
  const [userModal, setUserModal] = useState(false);
  const [userForm, setUserForm] = useState<{ username: string; password: string; email: string; role: Role }>({
    username: "",
    password: "",
    email: "",
    role: "viewer",
  });
  const [tokenModal, setTokenModal] = useState(false);
  const [tokenForm, setTokenForm] = useState({ name: "", role: "viewer" as Role, expires_in_days: "" });
  const [createdToken, setCreatedToken] = useState<ApiTokenCreated | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [busy, setBusy] = useState(false);

  const changePassword = async () => {
    setPasswordError(null);
    if (passwordForm.next !== passwordForm.repeat) {
      setPasswordError(t("password.mismatch"));
      return;
    }
    setBusy(true);
    try {
      await endpoints.changePassword(passwordForm.current, passwordForm.next);
      toast.success(t("password.changed"));
      setPasswordForm({ current: "", next: "", repeat: "" });
      await reload();
    } catch (error) {
      setPasswordError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppShell title={t("settings.title")} subtitle={t("settings.subtitle")}>
      <Tabs<TabKey>
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "account", label: t("settings.tab.account") },
          { value: "security", label: t("settings.tab.security") },
          ...(isAdmin ? [{ value: "tls" as TabKey, label: t("settings.tab.tls") }] : []),
          ...(isAdmin ? [{ value: "users" as TabKey, label: t("settings.tab.users") }] : []),
          { value: "tokens", label: t("settings.tab.tokens") },
          { value: "system", label: t("settings.tab.system") },
          ...(isAdmin ? [{ value: "database" as TabKey, label: t("settings.tab.database") }] : []),
          { value: "appearance", label: t("settings.tab.appearance") },
        ]}
      />

      {tab === "account" && (
        <div className="grid grid--halves">
          <Panel title={t("settings.tab.account")}>
            <KeyValue
              items={[
                { label: t("logs.user"), value: account?.username ?? "—" },
                { label: t("settings.role"), value: t(`settings.role.${account?.role ?? "viewer"}` as "settings.role.viewer") },
                { label: t("clients.email"), value: account?.email ?? "—" },
                { label: t("clients.lastOnline"), value: relativeTime(account?.last_login_at, lang) },
                { label: t("logs.ip"), value: account?.last_login_ip ?? "—" },
                { label: t("common.date"), value: formatDate(account?.created_at, lang) },
                {
                  label: t("settings.totp"),
                  value: account?.totp_enabled ? (
                    <Badge tone="ok">{t("settings.totpEnabled")}</Badge>
                  ) : (
                    <Badge tone="warn">{t("action.disable")}</Badge>
                  ),
                },
              ]}
            />
          </Panel>

          <Panel title={t("password.change.title")} hint={t("password.rules")}>
            <div className="stack">
              <Field label={t("password.current")} htmlFor="pw-current">
                <Input
                  id="pw-current"
                  type="password"
                  autoComplete="current-password"
                  value={passwordForm.current}
                  onChange={(event) => setPasswordForm({ ...passwordForm, current: event.target.value })}
                />
              </Field>
              <Field label={t("password.new")} htmlFor="pw-next">
                <Input
                  id="pw-next"
                  type="password"
                  autoComplete="new-password"
                  value={passwordForm.next}
                  onChange={(event) => setPasswordForm({ ...passwordForm, next: event.target.value })}
                />
              </Field>
              <Field label={t("password.confirm")} htmlFor="pw-repeat">
                <Input
                  id="pw-repeat"
                  type="password"
                  autoComplete="new-password"
                  value={passwordForm.repeat}
                  onChange={(event) => setPasswordForm({ ...passwordForm, repeat: event.target.value })}
                />
              </Field>
              {passwordError && <div className="form-error">{passwordError}</div>}
              <Button
                variant="primary"
                loading={busy}
                disabled={!passwordForm.current || passwordForm.next.length < 10}
                onClick={() => void changePassword()}
              >
                {t("action.save")}
              </Button>
            </div>
          </Panel>
        </div>
      )}

      {tab === "security" && (
        <div className="grid grid--halves">
          <Panel
            title={t("settings.totp")}
            actions={account?.totp_enabled ? <Badge tone="ok">{t("state.active")}</Badge> : <Badge tone="warn">{t("state.disabled")}</Badge>}
          >
            {account?.totp_enabled ? (
              <div className="stack">
                <p className="faint">{t("settings.totpEnabled")}</p>
                <Field label={t("login.password")} htmlFor="td-pass">
                  <Input
                    id="td-pass"
                    type="password"
                    autoComplete="current-password"
                    value={disableForm.password}
                    onChange={(event) => setDisableForm({ ...disableForm, password: event.target.value })}
                  />
                </Field>
                <Field label={t("settings.totpCode")} htmlFor="td-code">
                  <Input
                    id="td-code"
                    dir="ltr"
                    inputMode="numeric"
                    value={disableForm.code}
                    onChange={(event) => setDisableForm({ ...disableForm, code: event.target.value })}
                  />
                </Field>
                <Button
                  variant="danger"
                  loading={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await endpoints.totpDisable(disableForm.password, disableForm.code || undefined);
                      toast.success(t("settings.totpDisable"));
                      await reload();
                    } catch (error) {
                      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {t("settings.totpDisable")}
                </Button>
              </div>
            ) : totpSetup ? (
              <div className="stack">
                <span className="field__hint">{t("settings.totpScan")}</span>
                <div className="qr" dangerouslySetInnerHTML={{ __html: totpSetup.qr_svg }} />
                <KeyValue
                  items={[
                    { label: t("settings.totpSecret"), value: <span className="mono" dir="ltr">{totpSetup.secret}</span> },
                    { label: t("settings.issuer"), value: <span className="mono" dir="ltr">{totpSetup.provisioning_uri.split("?")[0]}</span> },
                  ]}
                />
                <Field label={t("settings.totpCode")} htmlFor="te-code">
                  <Input
                    id="te-code"
                    dir="ltr"
                    inputMode="numeric"
                    value={totpCode}
                    onChange={(event) => setTotpCode(event.target.value)}
                  />
                </Field>
                <Button
                  variant="primary"
                  loading={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await endpoints.totpEnable(totpSetup.secret, totpCode);
                      toast.success(t("settings.totpEnabled"));
                      setTotpSetup(null);
                      setTotpCode("");
                      await reload();
                    } catch (error) {
                      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {t("settings.totpEnable")}
                </Button>
              </div>
            ) : (
              <div className="stack">
                <p className="faint">{t("login.totpHint")}</p>
                <Button
                  variant="primary"
                  icon={<IconShield size={15} />}
                  loading={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      setTotpSetup(await endpoints.totpSetup());
                    } catch (error) {
                      toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {t("settings.totpEnable")}
                </Button>
              </div>
            )}
          </Panel>

          <Panel
            title={t("settings.sessions")}
            flush
            actions={
              <Button size="sm" variant="danger" onClick={() => setConfirmRevoke(true)}>
                {t("settings.revokeAll")}
              </Button>
            }
          >
            {sessions.loading ? (
              <div style={{ padding: 18 }}>
                <LoadingRows rows={3} />
              </div>
            ) : (sessions.data ?? []).length === 0 ? (
              <EmptyState title={t("state.empty")} />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("logs.ip")}</th>
                      <th>{t("settings.tab.account")}</th>
                      <th>{t("logs.time")}</th>
                      <th>{t("settings.expiresIn")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(sessions.data ?? []).map((session) => (
                      <tr key={session.id}>
                        <td className="mono" dir="ltr">{session.ip ?? "—"}</td>
                        <td className="faint" style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>
                          {session.user_agent ?? "—"}
                        </td>
                        <td className="nowrap faint">{relativeTime(session.created_at, lang)}</td>
                        <td className="nowrap faint">{relativeTime(session.expires_at, lang)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}

      {tab === "tls" && isAdmin && (
        <Panel title={t("settings.certIssue")} hint={t("settings.certNote")}>
          <div className="form-grid">
            <Field label={t("settings.certDomain")} htmlFor="cert-domain">
              <Input
                id="cert-domain"
                dir="ltr"
                value={certForm.domain}
                onChange={(event) => setCertForm({ ...certForm, domain: event.target.value })}
              />
            </Field>
            <Field label={t("settings.certEmail")} htmlFor="cert-email">
              <Input
                id="cert-email"
                dir="ltr"
                value={certForm.email}
                onChange={(event) => setCertForm({ ...certForm, email: event.target.value })}
              />
            </Field>
          </div>
          <Checkbox
            label={t("settings.certStaging")}
            checked={certForm.staging}
            onChange={(value) => setCertForm({ ...certForm, staging: value })}
          />
          <div className="row row--tight" style={{ marginTop: 14 }}>
            <Button
              variant="primary"
              loading={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const result = await endpoints.issueCertificate(certForm.domain, certForm.email, certForm.staging);
                  toast.success(result.message, result.domain);
                } catch (error) {
                  toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {t("settings.certIssue")}
            </Button>
          </div>
        </Panel>
      )}

      {tab === "users" && isAdmin && (
        <Panel
          title={t("settings.tab.users")}
          flush
          actions={
            <Button size="sm" variant="primary" icon={<IconPlus size={15} />} onClick={() => setUserModal(true)}>
              {t("settings.createUser")}
            </Button>
          }
        >
          {users.loading ? (
            <div style={{ padding: 18 }}>
              <LoadingRows rows={3} />
            </div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("logs.user")}</th>
                    <th>{t("settings.role")}</th>
                    <th>{t("settings.totp")}</th>
                    <th>{t("clients.lastOnline")}</th>
                    <th>{t("common.status")}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(users.data ?? []).map((user: Account) => (
                    <tr key={user.id}>
                      <td className="cell-primary">
                        {user.username}
                        <span className="cell-sub" style={{ display: "block" }}>
                          {user.email ?? ""}
                        </span>
                      </td>
                      <td>
                        <select
                          className="select"
                          value={user.role}
                          disabled={user.id === account?.id}
                          onChange={async (event) => {
                            try {
                              await endpoints.updateUser(user.id, { role: event.target.value });
                              toast.success(t("action.save"));
                              await users.reload({ silent: true });
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                        >
                          <option value="admin">{t("settings.role.admin")}</option>
                          <option value="operator">{t("settings.role.operator")}</option>
                          <option value="viewer">{t("settings.role.viewer")}</option>
                        </select>
                      </td>
                      <td>{user.totp_enabled ? <Badge tone="ok">{t("state.yes")}</Badge> : <Badge tone="idle">{t("state.no")}</Badge>}</td>
                      <td className="nowrap faint">{relativeTime(user.last_login_at, lang)}</td>
                      <td>
                        <Badge tone={user.is_active ? "ok" : "idle"}>
                          {user.is_active ? t("state.active") : t("state.disabled")}
                        </Badge>
                      </td>
                      <td className="nowrap">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={async () => {
                            try {
                              await endpoints.updateUser(user.id, { is_active: !user.is_active });
                              await users.reload({ silent: true });
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                        >
                          {user.is_active ? t("action.disable") : t("action.enable")}
                        </Button>
                        {user.id !== account?.id && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={async () => {
                              try {
                                await endpoints.deleteUser(user.id);
                                toast.success(t("action.delete"));
                                await users.reload({ silent: true });
                              } catch (error) {
                                toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                              }
                            }}
                          >
                            {t("action.delete")}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}

      {tab === "tokens" && (
        <Panel
          title={t("settings.tab.tokens")}
          flush
          actions={
            <Button size="sm" variant="primary" icon={<IconKey size={15} />} onClick={() => setTokenModal(true)}>
              {t("settings.createToken")}
            </Button>
          }
        >
          {(tokens.data ?? []).length === 0 ? (
            <EmptyState title={t("state.empty")} />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("settings.tokenName")}</th>
                    <th>{t("settings.role")}</th>
                    <th>{t("common.date")}</th>
                    <th>{t("settings.expiresIn")}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(tokens.data ?? []).map((token: ApiToken) => (
                    <tr key={token.id}>
                      <td className="cell-primary">{token.name}</td>
                      <td>
                        <Badge tone="idle">{t(`settings.role.${token.role}` as "settings.role.viewer")}</Badge>
                      </td>
                      <td className="nowrap faint">{relativeTime(token.created_at, lang)}</td>
                      <td className="nowrap faint">
                        {token.expires_at ? formatDate(token.expires_at, lang, false) : t("state.never")}
                      </td>
                      <td>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={async () => {
                            try {
                              await endpoints.deleteToken(token.id);
                              toast.success(t("action.delete"));
                              await tokens.reload({ silent: true });
                            } catch (error) {
                              toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                            }
                          }}
                        >
                          {t("action.delete")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}

      {tab === "system" && (
        <div className="grid grid--halves">
          <Panel title={t("settings.tab.system")}>
            {info.loading && !info.data ? (
              <LoadingRows rows={4} />
            ) : info.error ? (
              <ErrorState message={info.error} onRetry={() => void info.reload()} />
            ) : info.data ? (
              <KeyValue
                items={[
                  { label: t("common.version"), value: `${info.data.product} ${info.data.version}` },
                  { label: t("common.role"), value: info.data.mode },
                  { label: t("common.domain"), value: info.data.domain },
                  { label: t("common.uptime"), value: formatDuration(info.data.uptime_seconds, lang) },
                  { label: "Python", value: info.data.python },
                  { label: t("chart.down"), value: formatBytes(info.data.bandwidth_in_bytes) },
                  { label: t("chart.up"), value: formatBytes(info.data.bandwidth_out_bytes) },
                  { label: t("dash.billedTraffic"), value: formatBytes(info.data.billed_bytes) },
                  { label: t("settings.nextRun"), value: info.data.scheduler?.running ? t("settings.running") : t("state.disabled") },
                ]}
              />
            ) : null}
          </Panel>

          <Panel
            title={t("settings.jobs")}
            flush
            actions={
              <Button size="sm" variant="ghost" loading={jobs.refreshing} onClick={() => void jobs.reload({ silent: true })}>
                {t("action.refresh")}
              </Button>
            }
          >
            {(jobs.data?.jobs ?? []).length === 0 ? (
              <EmptyState title={t("state.empty")} />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("settings.jobs")}</th>
                      <th>{t("settings.nextRun")}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {(jobs.data?.jobs ?? []).map((job) => (
                      <tr key={job.id}>
                        <td className="mono">{job.id}</td>
                        <td className="nowrap faint">{relativeTime(job.next_run, lang)}</td>
                        <td>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={async () => {
                              try {
                                const result = await endpoints.runJob(job.id);
                                toast.success(t("settings.runNow"), JSON.stringify(result.result).slice(0, 200));
                                await jobs.reload({ silent: true });
                              } catch (error) {
                                toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                              }
                            }}
                          >
                            {t("settings.runNow")}
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}

      {tab === "database" && isAdmin && (
        <div className="grid grid--halves">
          <Panel
            title={t("settings.storage")}
            flush
            actions={
              <Button
                size="sm"
                variant="ghost"
                loading={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const result = await endpoints.vacuum();
                    toast.success(result.message);
                    await database.reload({ silent: true });
                  } catch (error) {
                    toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {t("settings.vacuum")}
              </Button>
            }
          >
            {database.loading ? (
              <div style={{ padding: 18 }}>
                <LoadingRows rows={3} />
              </div>
            ) : database.error ? (
              <div style={{ padding: 18 }}>
                <ErrorState message={database.error} onRetry={() => void database.reload()} />
              </div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("settings.tables")}</th>
                      <th>{t("common.count")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(database.data?.tables ?? {}).map(([table, count]) => (
                      <tr key={table}>
                        <td className="mono">{table}</td>
                        <td className="num">{count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel title={t("settings.vacuum")}>
            <KeyValue
              items={[
                { label: t("settings.storage"), value: formatBytes(database.data?.size_bytes ?? 0) },
                { label: "journal_mode", value: <span className="mono" dir="ltr">{database.data?.journal_mode ?? "—"}</span> },
                { label: t("common.version"), value: info.data?.version ?? "—" },
              ]}
            />
            <p className="field__hint">{t("settings.schedulerRunning")}</p>
          </Panel>
        </div>
      )}

      {tab === "appearance" && (
        <Panel title={t("settings.tab.appearance")}>
          <div className="stack">
            <Field label={t("settings.language")}>
              <div className="row row--tight">
                <Button size="sm" variant={lang === "fa" ? "primary" : "default"} onClick={() => setLang("fa")}>
                  فارسی
                </Button>
                <Button size="sm" variant={lang === "en" ? "primary" : "default"} onClick={() => setLang("en")}>
                  English
                </Button>
              </div>
            </Field>
            <Field label={t("settings.theme")}>
              <span className="field__hint">
                {t("settings.theme.dark")} / {t("settings.theme.light")} — {t("settings.theme.dark")}
              </span>
            </Field>
          </div>
        </Panel>
      )}

      <Modal
        open={userModal}
        onClose={() => setUserModal(false)}
        title={t("settings.createUser")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setUserModal(false)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              loading={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await endpoints.createUser({
                    username: userForm.username,
                    password: userForm.password || undefined,
                    email: userForm.email || undefined,
                    role: userForm.role,
                  });
                  toast.success(t("settings.createUser"));
                  setUserModal(false);
                  setUserForm({ username: "", password: "", email: "", role: "viewer" });
                  await users.reload({ silent: true });
                } catch (error) {
                  toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {t("action.create")}
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <Field label={t("logs.user")} htmlFor="u-name">
            <Input id="u-name" dir="ltr" value={userForm.username} onChange={(e) => setUserForm({ ...userForm, username: e.target.value })} />
          </Field>
          <Field label={t("settings.role")} htmlFor="u-role">
            <select
              id="u-role"
              className="select"
              value={userForm.role}
              onChange={(e) => setUserForm({ ...userForm, role: e.target.value as Role })}
            >
              <option value="admin">{t("settings.role.admin")}</option>
              <option value="operator">{t("settings.role.operator")}</option>
              <option value="viewer">{t("settings.role.viewer")}</option>
            </select>
          </Field>
          <Field label={t("clients.email")} htmlFor="u-email">
            <Input id="u-email" dir="ltr" value={userForm.email} onChange={(e) => setUserForm({ ...userForm, email: e.target.value })} />
          </Field>
          <Field label={t("password.new")} hint={t("password.rules")} htmlFor="u-pass">
            <Input
              id="u-pass"
              type="password"
              autoComplete="new-password"
              value={userForm.password}
              onChange={(e) => setUserForm({ ...userForm, password: e.target.value })}
            />
          </Field>
        </div>
      </Modal>

      <Modal
        open={tokenModal}
        onClose={() => setTokenModal(false)}
        title={t("settings.createToken")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setTokenModal(false)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              loading={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const created = await endpoints.createToken({
                    name: tokenForm.name,
                    role: tokenForm.role,
                    expires_in_days: tokenForm.expires_in_days ? Number(tokenForm.expires_in_days) : undefined,
                  });
                  setCreatedToken(created);
                  setTokenModal(false);
                  await tokens.reload({ silent: true });
                } catch (error) {
                  toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {t("action.create")}
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <Field label={t("settings.tokenName")} htmlFor="t-name">
            <Input id="t-name" value={tokenForm.name} onChange={(e) => setTokenForm({ ...tokenForm, name: e.target.value })} />
          </Field>
          <Field label={t("settings.role")} htmlFor="t-role">
            <select
              id="t-role"
              className="select"
              value={tokenForm.role}
              onChange={(e) => setTokenForm({ ...tokenForm, role: e.target.value as Role })}
            >
              <option value="admin">{t("settings.role.admin")}</option>
              <option value="operator">{t("settings.role.operator")}</option>
              <option value="viewer">{t("settings.role.viewer")}</option>
            </select>
          </Field>
          <Field label={t("settings.expiresIn")} htmlFor="t-days">
            <Input id="t-days" dir="ltr" inputMode="numeric" value={tokenForm.expires_in_days} onChange={(e) => setTokenForm({ ...tokenForm, expires_in_days: e.target.value })} />
          </Field>
        </div>
      </Modal>

      <Modal open={createdToken !== null} onClose={() => setCreatedToken(null)} title={t("settings.tokenValue")}>
        {createdToken && (
          <div className="stack">
            <span className="field__hint">{t("settings.tokenOnce")}</span>
            <CodeBlock content={createdToken.token} maxHeight={120} />
            <Button
              icon={<IconCopy size={15} />}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(createdToken.token);
                  toast.success(t("action.copied"));
                } catch {
                  toast.failure(t("state.error"));
                }
              }}
            >
              {t("action.copy")}
            </Button>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmRevoke}
        title={t("settings.revokeAll")}
        body={<span>{t("action.deleteConfirmBody")}</span>}
        confirmLabel={t("settings.revokeAll")}
        onCancel={() => setConfirmRevoke(false)}
        onConfirm={async () => {
          try {
            await endpoints.revokeSessions();
            setConfirmRevoke(false);
            toast.success(t("settings.revokeAll"));
            await reload();
          } catch (error) {
            toast.failure(t("state.error"), error instanceof Error ? error.message : undefined);
          }
        }}
        extra={<IconLock size={18} />}
      />
    </AppShell>
  );
}
