/** Shared UI primitives. */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { IconAlert, IconCheck, IconClose, IconInfo } from "./Icons";
import { useT } from "../lib/i18n";

/* ------------------------------------------------------------------ */
/* Panel                                                               */
/* ------------------------------------------------------------------ */

export function Panel({
  title,
  hint,
  actions,
  children,
  flush = false,
  footer,
  className = "",
}: {
  title?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  footer?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="panel__head">
          {title && <h2>{title}</h2>}
          {hint && <span className="panel__hint">{hint}</span>}
          {actions && <div className="panel__actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? "panel__body panel__body--flush" : "panel__body"}>{children}</div>
      {footer && <footer className="panel__foot">{footer}</footer>}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Button                                                              */
/* ------------------------------------------------------------------ */

type ButtonVariant = "default" | "primary" | "danger" | "ghost";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  icon?: ReactNode;
  loading?: boolean;
  block?: boolean;
}

export function Button({
  variant = "default",
  size = "md",
  icon,
  loading = false,
  block = false,
  children,
  className = "",
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  const classes = [
    "btn",
    variant !== "default" ? `btn--${variant}` : "",
    size === "sm" ? "btn--sm" : "",
    block ? "btn--block" : "",
    !children && icon ? "btn--icon" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button type={type} className={classes} disabled={disabled || loading} {...rest}>
      {loading ? <span className="spinner" aria-hidden="true" /> : icon}
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Badge                                                               */
/* ------------------------------------------------------------------ */

export type BadgeTone = "ok" | "warn" | "bad" | "idle";

export function Badge({
  tone = "idle",
  children,
  dot = false,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  dot?: boolean;
}) {
  return (
    <span className={`badge badge--${tone}`}>
      {dot && <span className="badge__dot" />}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const t = useT();
  const mapping: Record<string, { tone: BadgeTone; key: string; dot?: boolean }> = {
    online: { tone: "ok", key: "state.online", dot: true },
    up: { tone: "ok", key: "state.up", dot: true },
    active: { tone: "ok", key: "state.active", dot: true },
    offline: { tone: "bad", key: "state.offline" },
    down: { tone: "bad", key: "state.down" },
    error: { tone: "bad", key: "state.error.tunnel" },
    expired: { tone: "bad", key: "state.expired" },
    limited: { tone: "bad", key: "state.limited" },
    degraded: { tone: "warn", key: "state.degraded" },
    pending: { tone: "warn", key: "state.pending" },
    on_hold: { tone: "warn", key: "state.onHold" },
    disabled: { tone: "idle", key: "state.disabled" },
    unknown: { tone: "idle", key: "state.unknown" },
  };
  const entry = mapping[status] ?? { tone: "idle" as BadgeTone, key: "state.unknown" };
  return (
    <Badge tone={entry.tone} dot={entry.dot}>
      {t(entry.key as Parameters<typeof t>[0])}
    </Badge>
  );
}

/* ------------------------------------------------------------------ */
/* Fields                                                              */
/* ------------------------------------------------------------------ */

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="field">
      {label && (
        <label className="field__label" htmlFor={htmlFor}>
          {label}
        </label>
      )}
      {children}
      {hint && !error && <span className="field__hint">{hint}</span>}
      {error && <span className="field__error">{error}</span>}
    </div>
  );
}

export function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`input ${className}`} {...rest} />;
}

export function Select({
  className = "",
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={`select ${className}`} {...rest}>
      {children}
    </select>
  );
}

export function Textarea({ className = "", ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`textarea ${className}`} {...rest} />;
}

export function Checkbox({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="checkbox">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { value: T; label: string; badge?: ReactNode }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          className="tab"
          aria-selected={tab.value === value}
          onClick={() => onChange(tab.value)}
        >
          {tab.label}
          {tab.badge !== undefined && <span className="rail__count"> {tab.badge}</span>}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Stat                                                                */
/* ------------------------------------------------------------------ */

export function Stat({
  label,
  value,
  unit,
  foot,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  foot?: ReactNode;
}) {
  return (
    <div className="stat">
      <span className="stat__label">{label}</span>
      <span className="stat__value">
        {value}
        {unit && <span className="stat__unit">{unit}</span>}
      </span>
      {foot && <span className="stat__foot">{foot}</span>}
    </div>
  );
}

export function Meter({ ratio, tone }: { ratio: number; tone: "ok" | "warn" | "bad" }) {
  const width = `${Math.round(Math.min(1, Math.max(0, ratio)) * 100)}%`;
  const className = tone === "ok" ? "meter__fill meter__fill--ok" : tone === "warn" ? "meter__fill meter__fill--warn" : "meter__fill";
  return (
    <div className="meter" role="presentation">
      <div className={className} style={{ width }} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Empty / Error / Skeleton                                            */
/* ------------------------------------------------------------------ */

export function EmptyState({
  title,
  hint,
  action,
  icon,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon}
      <div className="empty__title">{title}</div>
      {hint && <p className="empty__hint">{hint}</p>}
      {action}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const t = useT();
  return (
    <EmptyState
      title={t("state.error")}
      hint={message}
      icon={<IconAlert size={26} />}
      action={
        onRetry ? (
          <Button size="sm" onClick={onRetry}>
            {t("action.retry")}
          </Button>
        ) : undefined
      }
    />
  );
}

export function LoadingRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="stack" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton" style={{ width: `${96 - index * 7}%` }} />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Modal                                                               */
/* ------------------------------------------------------------------ */

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  wide = false,
  drawer = false,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  drawer?: boolean;
}) {
  const t = useT();
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // Move focus into the dialog so keyboard users are not stranded behind it.
    ref.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className={drawer ? "overlay drawer-overlay" : "overlay"}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={`modal ${wide ? "modal--wide" : ""} ${drawer ? "drawer" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={ref}
      >
        <header className="panel__head">
          <h2 id={titleId}>{title}</h2>
          <div className="panel__actions">
            <Button variant="ghost" size="sm" icon={<IconClose size={16} />} onClick={onClose} aria-label={t("action.close")} />
          </div>
        </header>
        <div className="panel__body" style={{ overflowY: "auto", flex: 1 }}>
          {children}
        </div>
        {footer && <footer className="panel__foot">{footer}</footer>}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  tone = "danger",
  onConfirm,
  onCancel,
  busy = false,
  extra,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  tone?: ButtonVariant;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
  extra?: ReactNode;
}) {
  const t = useT();
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {t("action.cancel")}
          </Button>
          <Button variant={tone} onClick={onConfirm} loading={busy}>
            {confirmLabel ?? t("action.confirm")}
          </Button>
        </>
      }
    >
      <div className="stack">
        <p>{body}</p>
        {extra}
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ */
/* Toasts                                                              */
/* ------------------------------------------------------------------ */

type ToastTone = "info" | "ok" | "warn" | "error";

interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  body?: string;
}

interface ToastValue {
  push: (tone: ToastTone, title: string, body?: string) => void;
  success: (title: string, body?: string) => void;
  failure: (title: string, body?: string) => void;
}

const ToastContext = createContext<ToastValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback<ToastValue["push"]>((tone, title, body) => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current.slice(-3), { id, tone, title, body }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== id));
    }, tone === "error" ? 9000 : 5000);
  }, []);

  const value = useMemo<ToastValue>(
    () => ({
      push,
      success: (title, body) => push("ok", title, body),
      failure: (title, body) => push("error", title, body),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`toast ${toast.tone === "ok" ? "toast--ok" : toast.tone === "warn" ? "toast--warn" : ""}`}
          >
            <div className="row row--tight toast__title">
              {toast.tone === "ok" ? (
                <IconCheck size={15} />
              ) : toast.tone === "error" || toast.tone === "warn" ? (
                <IconAlert size={15} />
              ) : (
                <IconInfo size={15} />
              )}
              {toast.title}
            </div>
            {toast.body && <div className="toast__body">{toast.body}</div>}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastValue {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast must be used inside ToastProvider");
  return value;
}

/* ------------------------------------------------------------------ */
/* Misc                                                                */
/* ------------------------------------------------------------------ */

export function KeyValue({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="kv">
      {items.map((item) => (
        <div key={item.label} style={{ display: "contents" }}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function CopyButton({ text, label }: { text: string; label?: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="sm"
      variant="ghost"
      icon={<IconCheck size={15} style={{ opacity: copied ? 1 : 0, display: copied ? "block" : "none" }} />}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        } catch {
          /* clipboard may be blocked */
        }
      }}
    >
      {copied ? t("action.copied") : (label ?? t("action.copy"))}
    </Button>
  );
}

export function CodeBlock({ content, maxHeight }: { content: string; maxHeight?: number }) {
  return (
    <pre className="code" style={maxHeight ? { maxHeight } : undefined}>
      {content}
    </pre>
  );
}
