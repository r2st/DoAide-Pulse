import { statusTone, titleize } from "../../lib/format";

/** A status pill. One component so a status never renders two different ways. */
export function StatusBadge({ status, children }) {
  return (
    <span className={`badge ${statusTone(status)}`}>{children ?? status}</span>
  );
}

/** Section heading with an optional action on the right. */
export function SectionHeader({ title, subtitle, action }) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div className="min-w-0">
        <h2 className="text-base font-semibold text-ink-900">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-ink-500">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

/**
 * The empty state. Always says what to do next — an empty list with no next
 * action is a dead end, and most of this app's lists start empty.
 */
export function Empty({ title, hint, action }) {
  return (
    <div className="panel flex flex-col items-center gap-3 px-6 py-14 text-center">
      <p className="text-sm font-medium text-ink-900">{title}</p>
      {hint && <p className="max-w-sm text-sm text-ink-500">{hint}</p>}
      {action}
    </div>
  );
}

/** A labelled figure for the dashboard's counter row. */
export function Stat({ label, value, hint }) {
  return (
    <div className="panel px-5 py-4">
      <p className="eyebrow">{label}</p>
      <p className="stat-figure mt-2">{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-400">{hint}</p>}
    </div>
  );
}

/** Skeleton rows, sized to the list they stand in for. */
export function Skeleton({ rows = 3, className = "" }) {
  return (
    <div className={`space-y-2 ${className}`} aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="relative h-14 overflow-hidden rounded-lg border border-line bg-paper"
        >
          <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-canvas to-transparent" />
        </div>
      ))}
    </div>
  );
}

/** An inline error with a retry, for a failed load. */
export function ErrorBanner({ message, onRetry }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="flex items-start justify-between gap-4 rounded-lg border border-bad/25 bg-bad-wash px-4 py-3 text-sm text-bad"
    >
      <span className="min-w-0 break-words">{message}</span>
      {onRetry && (
        <button className="btn-quiet shrink-0 text-bad" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** A content-type or platform label, title-cased. */
export function Tag({ children }) {
  return <span className="chip">{titleize(children)}</span>;
}

/** A confidence read-out. Below the auto-publish bar is worth flagging. */
export function Confidence({ value }) {
  if (value === null || value === undefined) return null;
  const percent = Math.round(value * 100);
  const tone =
    value >= 0.8 ? "text-good" : value >= 0.5 ? "text-warn" : "text-ink-400";
  return (
    <span className={`font-mono text-[11px] ${tone}`} title="Model's own confidence">
      {percent}% confident
    </span>
  );
}
