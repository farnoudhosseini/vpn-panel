/** Sign-in. Also handles the mandatory password change on first login. */

import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, endpoints } from "../lib/api";
import { errorMessage } from "../lib/format";
import { useI18n } from "../lib/i18n";
import { useSession } from "../lib/session";
import { IconAlert, IconCheck, IconEye, IconEyeOff, IconShield } from "../components/Icons";
import { Button, Field, Input } from "../components/ui";

function strength(password: string): { score: number; label: string } {
  let score = 0;
  if (password.length >= 10) score += 1;
  if (password.length >= 14) score += 1;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1;
  if (/\d/.test(password)) score += 1;
  if (/[^\w\s]/.test(password)) score += 1;
  const labels = ["", "weak", "fair", "good", "strong", "excellent"];
  return { score, label: labels[Math.min(score, 5)] ?? "" };
}

export default function LoginPage() {
  const { t } = useI18n();
  const { signIn, status } = useSession();
  const navigate = useNavigate();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lockMessage, setLockMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Password change step
  const [changing, setChanging] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changeError, setChangeError] = useState<string | null>(null);
  const [changeDone, setChangeDone] = useState(false);

  useEffect(() => {
    if (status === "signed-in" && !changing) navigate("/", { replace: true });
  }, [status, navigate, changing]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setLockMessage(null);
    setBusy(true);
    try {
      const account = await signIn(username, password, needsTotp ? totp : undefined);
      if (account.must_change_password) {
        setChanging(true);
      } else {
        navigate("/", { replace: true });
      }
    } catch (err) {
      const payload = err instanceof ApiError ? err.payload : null;
      const detail = errorMessage(payload, t("login.failed"));
      if (err instanceof ApiError && err.status === 401 && /two-factor/i.test(detail)) {
        setNeedsTotp(true);
        setError(t("login.totpHint"));
      } else if (err instanceof ApiError && err.status === 423) {
        setLockMessage(detail);
      } else {
        setError(detail);
      }
    } finally {
      setBusy(false);
    }
  };

  const changePassword = async (event: FormEvent) => {
    event.preventDefault();
    setChangeError(null);
    if (newPassword.length < 10) {
      setChangeError(t("password.tooShort"));
      return;
    }
    if (newPassword !== confirmPassword) {
      setChangeError(t("password.mismatch"));
      return;
    }
    setBusy(true);
    try {
      await endpoints.changePassword(password, newPassword);
      setChangeDone(true);
      setChanging(false);
      // The server revoked every session, so the operator signs in again.
      window.setTimeout(() => {
        window.location.href = "/login";
      }, 1600);
    } catch (err) {
      setChangeError(errorMessage(err instanceof ApiError ? err.payload : null, t("state.error")));
    } finally {
      setBusy(false);
    }
  };

  const meter = strength(newPassword);

  return (
    <div className="login">
      <div className="login__art">
        <p className="login__claim">{t("login.claim")}</p>
        <p className="login__sub">{t("login.claimBody")}</p>
        <div className="login__facts">
          <span>{t("login.fact.encrypted")}</span>
          <span>{t("login.fact.locked")}</span>
          <span>{t("login.fact.local")}</span>
        </div>
      </div>

      <div className="login__panel">
        <div className="login__form">
          <div className="brand" style={{ padding: 0 }}>
            <span className="brand__mark" aria-hidden="true">
              پ
            </span>
            <span>
              <span className="brand__name" style={{ display: "block", fontSize: 17 }}>
                {t("app.name")}
              </span>
              <span className="brand__meta">{t("app.tagline")}</span>
            </span>
          </div>

          {changeDone ? (
            <div className="stack" style={{ textAlign: "center", padding: "24px 0" }}>
              <IconCheck size={30} style={{ color: "var(--jade)", margin: "0 auto" }} />
              <p style={{ fontWeight: 600 }}>{t("password.changed")}</p>
              <p className="muted" style={{ fontSize: 13 }}>
                {t("login.title")}…
              </p>
            </div>
          ) : changing ? (
            <form className="form" onSubmit={changePassword}>
              <div className="row row--tight" style={{ alignItems: "flex-start" }}>
                <IconShield size={18} style={{ color: "var(--crimson)", flex: "none", marginTop: 3 }} />
                <p className="muted" style={{ fontSize: 13 }}>
                  {t("password.mustChange")}
                </p>
              </div>

              <Field label={t("password.new")} htmlFor="new-password" hint={t("password.rules")}>
                <Input
                  id="new-password"
                  type="password"
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  required
                  minLength={10}
                />
              </Field>

              {newPassword && (
                <div className="row row--tight" aria-hidden="true">
                  <span className="meter" style={{ flex: 1 }}>
                    <span
                      className={`meter__fill ${
                        meter.score >= 4 ? "meter__fill--ok" : meter.score >= 2 ? "meter__fill--warn" : ""
                      }`}
                      style={{ width: `${(meter.score / 5) * 100}%`, display: "block" }}
                    />
                  </span>
                  <span className="faint" style={{ fontSize: 11.5, minWidth: 62, textAlign: "end" }}>
                    {meter.label}
                  </span>
                </div>
              )}

              <Field label={t("password.confirm")} htmlFor="confirm-password">
                <Input
                  id="confirm-password"
                  type="password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  required
                />
              </Field>

              {changeError && (
                <p className="field__error row row--tight">
                  <IconAlert size={15} /> {changeError}
                </p>
              )}

              <Button type="submit" variant="primary" block loading={busy}>
                {t("action.save")}
              </Button>
            </form>
          ) : (
            <form className="form" onSubmit={submit}>
              <h1 style={{ fontSize: "1.25rem" }}>{t("login.title")}</h1>

              <Field label={t("login.username")} htmlFor="username">
                <Input
                  id="username"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                  autoFocus
                  dir="ltr"
                />
              </Field>

              <Field label={t("login.password")} htmlFor="password">
                <div style={{ position: "relative" }}>
                  <Input
                    id="password"
                    type={reveal ? "text" : "password"}
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    dir="ltr"
                    style={{ paddingInlineEnd: 38 }}
                  />
                  <button
                    type="button"
                    onClick={() => setReveal((current) => !current)}
                    aria-label={reveal ? t("action.hide") : t("action.show")}
                    style={{
                      position: "absolute",
                      insetInlineEnd: 6,
                      insetBlockStart: "50%",
                      translate: "0 -50%",
                      background: "none",
                      border: "none",
                      color: "var(--faint)",
                      cursor: "pointer",
                      padding: 4,
                    }}
                  >
                    {reveal ? <IconEyeOff size={16} /> : <IconEye size={16} />}
                  </button>
                </div>
              </Field>

              {needsTotp && (
                <Field label={t("login.totp")} htmlFor="totp" hint={t("login.totpHint")}>
                  <Input
                    id="totp"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    value={totp}
                    onChange={(event) => setTotp(event.target.value.replace(/\D/g, ""))}
                    dir="ltr"
                    className="input--mono"
                    autoFocus
                  />
                </Field>
              )}

              {lockMessage && (
                <p className="field__error row row--tight">
                  <IconAlert size={15} /> {lockMessage}
                </p>
              )}
              {error && !lockMessage && (
                <p className="field__error row row--tight">
                  <IconAlert size={15} /> {error}
                </p>
              )}

              <Button type="submit" variant="primary" block loading={busy}>
                {t("login.submit")}
              </Button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
