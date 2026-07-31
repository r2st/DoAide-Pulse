import { useId } from "react";

/**
 * Chart primitives, hand-rolled in SVG.
 *
 * Herald has no charting dependency and does not want one: the whole surface
 * needed here is a filled line and a row of bars, and every library that draws
 * those arrives with its own type scale, its own colours and its own idea of
 * what a tooltip looks like — none of which match the hairline-and-mono
 * treatment the rest of the app uses. These are ~200 lines, theme with the
 * Tailwind tokens, and render in jsdom, which the container-measuring
 * libraries famously do not.
 *
 * Charts here draw into a fixed viewBox and scale with the container, so
 * strokes and labels scale together rather than smearing.
 */

const W = 640;
const H = 180;
const PAD = { top: 14, right: 8, bottom: 22, left: 8 };

/** Nothing to plot yet — same footprint, so the layout doesn't jump on load. */
function ChartEmpty({ hint }) {
  return (
    <div className="flex h-[180px] items-center justify-center rounded-lg border border-dashed border-line px-6 text-center text-xs text-ink-400">
      {hint}
    </div>
  );
}

/**
 * A filled line over a dense, evenly spaced series — one point per day.
 *
 * `points` is `[{ label, value }]`. Zero days must be present in the data:
 * dropping them compresses a quiet fortnight into a flat line and makes it
 * look like activity (the API returns them for exactly this reason).
 */
export function AreaChart({
  points,
  label,
  formatValue = (v) => String(v),
  emptyHint = "No activity in this window.",
}) {
  const gradientId = useId();
  const values = points.map((p) => Number(p.value) || 0);
  const peak = Math.max(...values, 0);

  if (points.length < 2 || peak === 0) {
    return <ChartEmpty hint={emptyHint} />;
  }

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const baseline = PAD.top + innerH;
  const step = innerW / (points.length - 1);
  const x = (i) => PAD.left + i * step;
  const y = (v) => baseline - (v / peak) * innerH;

  const line = values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`).join(" ");
  const area = `${line} L${x(values.length - 1)},${baseline} L${x(0)},${baseline} Z`;
  const total = values.reduce((sum, v) => sum + v, 0);

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-auto w-full text-brand-500"
        role="img"
        aria-label={`${label}: ${formatValue(total)} over ${points.length} days, peaking at ${formatValue(peak)}`}
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.18" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Peak and midpoint rules. Two lines, not a grid — the shape of the
            series is the message, and a lattice competes with it. */}
        {[0, 0.5].map((fraction) => (
          <line
            key={fraction}
            x1={PAD.left}
            x2={W - PAD.right}
            y1={PAD.top + innerH * fraction}
            y2={PAD.top + innerH * fraction}
            className="stroke-line"
            strokeWidth="1"
            strokeDasharray="2 4"
          />
        ))}
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={baseline}
          y2={baseline}
          className="stroke-line-strong"
          strokeWidth="1"
        />

        <path d={area} fill={`url(#${gradientId})`} />
        <path
          d={line}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* One hit target per day, full height, so a value is readable without
            having to land on a 2px line. */}
        {points.map((point, i) => (
          <rect
            key={point.label}
            x={x(i) - step / 2}
            y={PAD.top}
            width={step}
            height={innerH}
            fill="transparent"
            className="hover:fill-brand-500/5"
          >
            <title>{`${point.label}: ${formatValue(point.value)}`}</title>
          </rect>
        ))}

        <text
          x={PAD.left}
          y={H - 6}
          className="fill-ink-400 font-mono text-[10px]"
        >
          {points[0].label}
        </text>
        <text
          x={W - PAD.right}
          y={H - 6}
          textAnchor="end"
          className="fill-ink-400 font-mono text-[10px]"
        >
          {points[points.length - 1].label}
        </text>
      </svg>
      <figcaption className="sr-only">
        Peak {formatValue(peak)}; {formatValue(total)} in total.
      </figcaption>
    </figure>
  );
}

/**
 * A horizontal bar row. Bars are proportional to `max` across the whole set,
 * never to their own row — a bar scaled to itself is always full, which is a
 * chart that says nothing.
 */
export function BarRow({ label, hint, value, max, display, tone = "brand" }) {
  const fraction = max > 0 ? Math.max(0, Number(value) || 0) / max : 0;
  const fill = tone === "muted" ? "bg-ink-400/40" : "bg-brand-500/80";

  return (
    <div className="px-5 py-3">
      <div className="flex items-baseline justify-between gap-4">
        <span className="text-sm text-ink-900">{label}</span>
        <span className="shrink-0 font-mono text-xs text-ink-700">{display}</span>
      </div>
      <div
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-canvas"
        role="img"
        aria-label={`${label}: ${display}`}
      >
        <div
          className={`h-full rounded-full ${fill}`}
          // Percentage widths are the one thing that has to be inline —
          // Tailwind cannot enumerate a continuous scale.
          style={{ width: `${(fraction * 100).toFixed(2)}%` }}
        />
      </div>
      {hint && <p className="mt-1.5 text-xs text-ink-400">{hint}</p>}
    </div>
  );
}

/**
 * Two shares of the same total, side by side, as one stacked strip per row.
 *
 * Used for "share of output vs share of attention": the comparison is only
 * legible when both are drawn against the same 100%, which is why this is a
 * component and not two `BarRow`s.
 */
export function CompareBars({ rows, aLabel, bLabel }) {
  return (
    <div className="px-5 py-4">
      <div className="mb-3 flex items-center gap-4 font-mono text-[10px] uppercase tracking-[0.16em] text-ink-400">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-sm bg-ink-400/50" />
          {aLabel}
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-sm bg-brand-500/80" />
          {bLabel}
        </span>
      </div>
      <div className="space-y-3">
        {rows.map((row) => (
          <div key={row.label}>
            <div className="flex items-baseline justify-between gap-4">
              <span className="text-sm text-ink-900">{row.label}</span>
              <span className="shrink-0 font-mono text-[11px] text-ink-500">
                {row.hint}
              </span>
            </div>
            <div className="mt-1.5 space-y-1">
              <div className="h-1.5 overflow-hidden rounded-full bg-canvas">
                <div
                  className="h-full rounded-full bg-ink-400/50"
                  style={{ width: `${(row.a * 100).toFixed(2)}%` }}
                  role="img"
                  aria-label={`${row.label}, ${aLabel}: ${(row.a * 100).toFixed(0)}%`}
                />
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-canvas">
                <div
                  className="h-full rounded-full bg-brand-500/80"
                  style={{ width: `${(row.b * 100).toFixed(2)}%` }}
                  role="img"
                  aria-label={`${row.label}, ${bLabel}: ${(row.b * 100).toFixed(0)}%`}
                />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
