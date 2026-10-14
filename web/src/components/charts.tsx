/**
 * Charts.
 *
 * Hand-rolled SVG instead of a charting library: the panel needs three small
 * shapes, and owning them keeps the bundle small and the styling consistent
 * with the rest of the interface. Strokes use `vector-effect` so they stay
 * crisp while the path scales to the container.
 */

import { useMemo, useState, type PointerEvent as ReactPointerEvent } from "react";

import { formatBytes } from "../lib/format";
import { useT } from "../lib/i18n";

const VIEW_W = 1000;

interface Point {
  x: number;
  y: number;
}

function buildPath(values: number[], width: number, height: number, max: number, pad = 2): string {
  if (values.length === 0) return "";
  const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0;
  return values
    .map((value, index) => {
      const x = pad + index * step;
      const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
      const y = height - pad - ratio * (height - pad * 2);
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function toArea(path: string, width: number, height: number, pad = 2): string {
  if (!path) return "";
  return `${path} L${width - pad},${height - pad} L${pad},${height - pad} Z`;
}

/* ------------------------------------------------------------------ */
/* Sparkline                                                           */
/* ------------------------------------------------------------------ */

export function Sparkline({
  values,
  tone = "var(--jade)",
  height = 18,
  className = "ridge__spark",
  fill = true,
}: {
  values: number[];
  tone?: string;
  height?: number;
  className?: string;
  fill?: boolean;
}) {
  const width = VIEW_W;
  const clean = values.filter((value) => Number.isFinite(value));
  const max = Math.max(1, ...clean);
  const path = buildPath(clean, width, height, max);
  const gradientId = useMemo(() => `spark-${Math.random().toString(36).slice(2, 9)}`, []);

  if (clean.length === 0) {
    return <svg className={className} viewBox={`0 0 ${width} ${height}`} height={height} />;
  }

  return (
    <svg
      className={className}
      viewBox={`0 0 ${width} ${height}`}
      height={height}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={tone} stopOpacity="0.34" />
          <stop offset="100%" stopColor={tone} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={toArea(path, width, height)} fill={`url(#${gradientId})`} />}
      <path
        d={path}
        fill="none"
        stroke={tone}
        strokeWidth={1.6}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/* Traffic area chart                                                  */
/* ------------------------------------------------------------------ */

interface TrafficPoint {
  ts: string;
  up: number;
  down: number;
}

export function TrafficChart({
  series,
  height = 190,
  emptyLabel,
}: {
  series: TrafficPoint[];
  height?: number;
  emptyLabel: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const t = useT();

  if (series.length === 0) {
    return (
      <div className="empty" style={{ padding: "38px 16px" }}>
        <span className="empty__hint">{emptyLabel}</span>
      </div>
    );
  }

  const width = VIEW_W;
  const max = Math.max(1, ...series.map((point) => Math.max(point.up, point.down)));
  const upPath = buildPath(series.map((p) => p.up), width, height, max, 6);
  const downPath = buildPath(series.map((p) => p.down), width, height, max, 6);
  const step = series.length > 1 ? (width - 12) / (series.length - 1) : 0;

  const handleMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width === 0) return;
    const ratio = (event.clientX - rect.left) / rect.width;
    const index = Math.round(ratio * (series.length - 1));
    setHover(Math.min(series.length - 1, Math.max(0, index)));
  };

  const point = hover !== null ? series[hover] : null;
  const hoverX = hover !== null ? 6 + hover * step : 0;

  /** Round gridlines are the only decoration; they anchor the eye to values. */
  const gridLines = [0.25, 0.5, 0.75].map((ratio) => height - 6 - ratio * (height - 12));

  return (
    <div style={{ position: "relative" }}>
      <svg
        className="chart"
        style={{ height }}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onPointerMove={handleMove}
        onPointerLeave={() => setHover(null)}
        role="img"
        aria-label="traffic"
      >
        {gridLines.map((y) => (
          <line
            key={y}
            x1={0}
            x2={width}
            y1={y}
            y2={y}
            stroke="var(--line)"
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <path d={toArea(downPath, width, height, 6)} fill="var(--jade)" fillOpacity={0.12} />
        <path
          d={downPath}
          fill="none"
          stroke="var(--jade)"
          strokeWidth={1.7}
          vectorEffect="non-scaling-stroke"
        />
        <path
          d={upPath}
          fill="none"
          stroke="var(--crimson)"
          strokeWidth={1.7}
          vectorEffect="non-scaling-stroke"
        />
        {point && (
          <line
            x1={hoverX}
            x2={hoverX}
            y1={0}
            y2={height}
            stroke="var(--line-strong)"
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>

      <div className="legend" style={{ marginTop: 8 }}>
        <span className="legend__item">
          <span className="legend__swatch" style={{ background: "var(--crimson)" }} />
          {t("chart.up")}
        </span>
        <span className="legend__item">
          <span className="legend__swatch" style={{ background: "var(--jade)" }} />
          {t("chart.down")}
        </span>
        {point && (
          <span className="legend__item nowrap" style={{ marginInlineStart: "auto" }}>
            {new Date(point.ts).toLocaleString(undefined, {
              month: "short",
              day: "numeric",
              hour: "2-digit",
            })}
            {" · "}
            {formatBytes(point.up)}
            {" / "}
            {formatBytes(point.down)}
          </span>
        )}
        <span className="legend__item nowrap faint">
          {t("chart.peak")} {formatBytes(max)}
        </span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Load bars                                                           */
/* ------------------------------------------------------------------ */

export function LoadBar({
  label,
  percent,
  detail,
}: {
  label: string;
  percent: number;
  detail?: string;
}) {
  const clamped = Math.min(100, Math.max(0, percent));
  const tone = clamped >= 90 ? "bad" : clamped >= 70 ? "warn" : "ok";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 10, alignItems: "center" }}>
      <span className="faint" style={{ fontSize: 12, minWidth: 54 }}>
        {label}
      </span>
      <span className="meter">
        <span
          className={`meter__fill meter__fill--${tone}`}
          style={{ width: `${clamped}%`, display: "block" }}
        />
      </span>
      <span className="num" style={{ fontSize: 12, minWidth: 44, textAlign: "end" }}>
        {detail ?? `${clamped.toFixed(0)}%`}
      </span>
    </div>
  );
}

export function MiniBars({ values, max }: { values: number[]; max?: number }) {
  const peak = max ?? Math.max(1, ...values);
  return (
    <div className="row row--tight" style={{ gap: 2, height: 22, alignItems: "flex-end" }}>
      {values.map((value, index) => (
        <span
          key={index}
          style={{
            width: 3,
            height: `${Math.max(6, Math.min(100, (value / peak) * 100))}%`,
            background: "var(--jade)",
            opacity: 0.35 + 0.65 * (index / Math.max(1, values.length - 1)),
            borderRadius: 999,
            display: "block",
          }}
        />
      ))}
    </div>
  );
}

export type { TrafficPoint, Point };
