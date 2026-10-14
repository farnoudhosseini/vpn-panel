/** Presentation helpers. All numbers are formatted for the active locale. */

const KIB = 1024;
const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

export function formatBytes(value: number | null | undefined, digits = 1): string {
  const bytes = Number(value ?? 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(KIB)), UNITS.length - 1);
  const scaled = bytes / KIB ** exponent;
  const precision = exponent === 0 ? 0 : scaled >= 100 ? 0 : digits;
  return `${scaled.toFixed(precision)} ${UNITS[exponent]}`;
}

export function formatBits(value: number | null | undefined): string {
  const bytes = Number(value ?? 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 bps";
  return `${(bytes * 8).toFixed(bytes * 8 >= 100 ? 0 : 1)} bps`;
}

export function formatNumber(value: number | null | undefined): string {
  return new Intl.NumberFormat("en-US").format(Number(value ?? 0));
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  const number = Number(value ?? 0);
  return `${number.toFixed(digits)}%`;
}

/** Human duration such as "3 روز و 4 ساعت" — localised by the caller's strings. */
export function formatDuration(seconds: number | null | undefined, locale = "fa"): string {
  const total = Math.max(0, Math.floor(Number(seconds ?? 0)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);

  const labels =
    locale === "fa"
      ? { d: "روز", h: "ساعت", m: "دقیقه", s: "ثانیه" }
      : { d: "d", h: "h", m: "m", s: "s" };

  if (days > 0) return `${days} ${labels.d} ${hours} ${labels.h}`;
  if (hours > 0) return `${hours} ${labels.h} ${minutes} ${labels.m}`;
  if (minutes > 0) return `${minutes} ${labels.m}`;
  return `${total} ${labels.s}`;
}

/**
 * Compact remaining days, used in quota columns.
 */
export function daysBetween(from: string | null, to: Date = new Date()): number | null {
  if (!from) return null;
  const target = new Date(from);
  if (Number.isNaN(target.getTime())) return null;
  return Math.ceil((target.getTime() - to.getTime()) / 86_400_000);
}

export function formatDate(
  value: string | null | undefined,
  locale = "fa",
  withTime = true,
): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const calendar = locale === "fa" ? "persian" : "gregory";
  try {
    return new Intl.DateTimeFormat(locale === "fa" ? "fa-IR" : "en-GB", {
      calendar,
      numberingSystem: locale === "fa" ? "arabext" : "latn",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
    }).format(date);
  } catch {
    return date.toISOString().slice(0, withTime ? 16 : 10);
  }
}

export function relativeTime(value: string | null | undefined, locale = "fa"): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  const rtf = new Intl.RelativeTimeFormat(locale === "fa" ? "fa-IR" : "en", {
    numeric: "auto",
  });
  if (abs < 60) return rtf.format(Math.round(seconds), "second");
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), "hour");
  return rtf.format(Math.round(seconds / 86400), "day");
}

/** ISO string for a `<input type="datetime-local">` value. */
export function toDateTimeLocal(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** Convert a local datetime input back to an ISO string, or null when empty. */
export function fromDateTimeLocal(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** Parse a human byte size such as "10 GB" or "512MB". */
export function parseBytes(input: string): number | null {
  const text = input.trim().replace(/,/g, "");
  if (!text) return null;
  const match = /^([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)$/.exec(text);
  if (!match) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount)) return null;
  const suffix = (match[2] ?? "").toUpperCase();
  const index = ["", "B", "K", "KB", "KIB", "M", "MB", "MIB", "G", "GB", "GIB", "T", "TB", "TIB"].indexOf(
    suffix,
  );
  if (index <= 0) return suffix === "B" ? amount : null;
  const step = suffix.startsWith("K") ? 1 : suffix.startsWith("M") ? 2 : suffix.startsWith("G") ? 3 : 4;
  return Math.round(amount * KIB ** step);
}

export function usageRatio(used: number, limit: number): number {
  if (!limit || limit <= 0) return 0;
  return Math.min(1, Math.max(0, used / limit));
}

export function meterTone(ratio: number): "ok" | "warn" | "bad" {
  if (ratio >= 0.9) return "bad";
  if (ratio >= 0.7) return "warn";
  return "ok";
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

/** Extract a readable reason from an API error payload. */
export function errorMessage(payload: unknown, fallback = "request failed"): string {
  if (!payload) return fallback;
  if (typeof payload === "string") return payload;
  if (typeof payload !== "object") return fallback;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const nested = detail as { message?: string; report?: { commands?: { message?: string }[] } };
    if (nested.message) {
      const command = nested.report?.commands?.find((entry) => entry?.message);
      return command?.message ? `${nested.message}: ${command.message}` : nested.message;
    }
  }
  const errors = (payload as { errors?: { msg?: string; loc?: unknown[] }[] }).errors;
  if (Array.isArray(errors) && errors.length) {
    const first = errors[0];
    const where = Array.isArray(first?.loc) ? first.loc.slice(1).join(".") : "";
    return `${where ? `${where}: ` : ""}${first?.msg ?? fallback}`;
  }
  return fallback;
}
