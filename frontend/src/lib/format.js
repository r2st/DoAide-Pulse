// Small formatting helpers shared across pages.

/**
 * Relative time, both directions: "in 3d" for the future, "2h ago" for the
 * past, and a calendar date once relative time stops being useful. Returns
 * "—" for empty values so table cells stay aligned.
 *
 * Dates outside the current year carry it: a bare "Jun 20" on something from
 * two years ago reads as recent.
 */
export function formatWhen(value) {
  if (!value) return "—";
  const then = new Date(value);
  const minutes = Math.round((then.getTime() - Date.now()) / 60000);
  const future = minutes > 0;
  const abs = Math.abs(minutes);

  if (abs < 1) return "just now";

  let magnitude;
  if (abs < 60) magnitude = `${abs}m`;
  else if (abs < 1440) magnitude = `${Math.round(abs / 60)}h`;
  else if (abs < 10080) magnitude = `${Math.round(abs / 1440)}d`;
  else
    return then.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      ...(then.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }),
    });

  return future ? `in ${magnitude}` : `${magnitude} ago`;
}

/** "Tue 22 Jul, 13:00" — the calendar's absolute label. */
export function formatDateTime(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** A number with thousands separators, or "—" for nothing. */
export function formatCount(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString();
}

/** "feature_spotlight" -> "Feature Spotlight". */
export function titleize(value) {
  if (!value) return "";
  return String(value)
    .split(/[_\s-]+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** Tailwind classes for a content or publication status pill. */
export function statusTone(status) {
  switch (status) {
    case "published":
      return "bg-good-wash text-good";
    case "approved":
    case "scheduled":
      return "bg-brand-50 text-brand-600";
    case "review":
    case "publishing":
    case "pending":
      return "bg-warn-wash text-warn";
    case "failed":
      return "bg-bad-wash text-bad";
    default:
      return "bg-canvas text-ink-500";
  }
}

/**
 * Split an ISO instant into the local calendar day it falls on.
 * Used to bucket calendar entries — `toISOString()` would bucket by UTC and
 * put a 9pm post on the wrong day for anyone west of Greenwich.
 */
export function localDayKey(value) {
  const date = new Date(value);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}
