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
 * How successive series are told apart: weight and dash, not hue.
 *
 * Herald's palette has exactly one accent — "used only for the thing you should
 * look at or click next" — so a chart that spent three colours on three lines
 * would be the loudest thing on the page and would mean nothing by being loud.
 * The first series gets the accent because it is the one the panel is about;
 * the rest are context, drawn in ink and distinguished by stroke.
 */
const SERIES_TONES = [
  { stroke: "stroke-brand-500", width: 1.75, dash: undefined, swatch: "bg-brand-500" },
  { stroke: "stroke-ink-400", width: 1.25, dash: undefined, swatch: "bg-ink-400" },
  { stroke: "stroke-ink-400", width: 1.25, dash: "3 3", swatch: "bg-ink-400/50" },
];

/**
 * Several series over the same days, on one shared scale.
 *
 * The shared scale is the point, and it is only honest when the series are the
 * same unit — views, reads and clicks are all counts of people, and each is a
 * subset of the one before, so drawing them together says how much of the reach
 * turned into something. Giving each series its own axis would let a line with
 * a hundredth of the magnitude fill the same height, which is how a chart tells
 * a lie without a single wrong number in it.
 *
 * `series` is `[{ key, label, points: [{ label, value }] }]`. The first series
 * supplies the x axis, so they must cover the same days — the API returns every
 * day in the window, including the empty ones, so they do.
 */
export function MultiLineChart({
  series,
  label,
  formatValue = (v) => String(v),
  emptyHint = "No activity in this window.",
}) {
  const axis = series[0]?.points ?? [];
  const peak = Math.max(
    ...series.flatMap((s) => s.points.map((p) => Number(p.value) || 0)),
    0,
  );

  if (axis.length < 2 || peak === 0) {
    return <ChartEmpty hint={emptyHint} />;
  }

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const baseline = PAD.top + innerH;
  const step = innerW / (axis.length - 1);
  const x = (i) => PAD.left + i * step;
  const y = (v) => baseline - ((Number(v) || 0) / peak) * innerH;

  const path = (points) =>
    points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(p.value)}`).join(" ");

  return (
    <figure className="m-0">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10px] uppercase tracking-[0.16em] text-ink-400">
        {series.map((s, index) => (
          <span key={s.key} className="flex items-center gap-1.5">
            <span
              className={`h-2 w-2 rounded-sm ${
                SERIES_TONES[index % SERIES_TONES.length].swatch
              }`}
            />
            {s.label}
          </span>
        ))}
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-auto w-full"
        role="img"
        aria-label={`${label}: ${series
          .map((s) => `${s.label} peaking at ${formatValue(
            Math.max(...s.points.map((p) => Number(p.value) || 0), 0),
          )}`)
          .join(", ")} over ${axis.length} days`}
        preserveAspectRatio="xMidYMid meet"
      >
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

        {/* Drawn back to front, so the accent series sits on top of the context
            it is being compared against. */}
        {series
          .map((s, index) => ({ s, tone: SERIES_TONES[index % SERIES_TONES.length] }))
          .reverse()
          .map(({ s, tone }) => (
            <path
              key={s.key}
              d={path(s.points)}
              fill="none"
              className={tone.stroke}
              strokeWidth={tone.width}
              strokeDasharray={tone.dash}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ))}

        {/* One full-height hit target per day, carrying every series' value for
            that day — the comparison is the reason to hover at all. */}
        {axis.map((point, i) => (
          <rect
            key={point.label}
            x={x(i) - step / 2}
            y={PAD.top}
            width={step}
            height={innerH}
            fill="transparent"
            className="hover:fill-brand-500/5"
          >
            <title>
              {`${point.label}\n${series
                .map((s) => `${s.label}: ${formatValue(s.points[i]?.value ?? 0)}`)
                .join("\n")}`}
            </title>
          </rect>
        ))}

        <text x={PAD.left} y={H - 6} className="fill-ink-400 font-mono text-[10px]">
          {axis[0].label}
        </text>
        <text
          x={W - PAD.right}
          y={H - 6}
          textAnchor="end"
          className="fill-ink-400 font-mono text-[10px]"
        >
          {axis[axis.length - 1].label}
        </text>
      </svg>
      <figcaption className="sr-only">
        Peak {formatValue(peak)} across {series.length} series.
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
