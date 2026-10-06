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
  else if (abs < 1440) magnitude = `${Math.floor(abs / 60)}h`;
  else if (abs < 10080) magnitude = `${Math.floor(abs / 1440)}d`;
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

/**
 * A 0..1 rate as a percentage, or "—" when there is nothing to divide by.
 *
 * The API returns null rather than 0 for a rate whose denominator is missing —
 * Mastodon does not count reads, so its read rate is unknown, not zero. Keeping
 * that distinction visible is the whole point of the "—".
 */
export function formatRate(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return "—";
  return `${(number * 100).toFixed(digits)}%`;
}

/**
 * A count of minutes as a duration: "45m", "3h 20m", "12d 4h".
 *
 * Reader-minutes reach five figures quickly, and "18,420" tells you nothing
 * about whether that is a lot. "12d 19h" does. Zero stays "0m" rather than
 * becoming "—": nobody having read anything yet is a real answer, and the
 * unknown case is handled by the caller, which knows whether any platform
 * reports reads at all.
 */
export function formatDuration(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  const total = Math.round(Number(minutes));
  if (Number.isNaN(total)) return "—";
  if (total < 60) return `${total}m`;

  const hours = Math.floor(total / 60);
  if (hours < 24) {
    const rest = total % 60;
    return rest ? `${hours}h ${rest}m` : `${hours}h`;
  }
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  return rest ? `${days}d ${rest}h` : `${days}d`;
}

/** "6 min read", the way the piece itself would put it. */
export function formatReadLength(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  return `${Number(minutes).toLocaleString()} min read`;
}

/** "feature_spotlight" -> "Feature Spotlight". */
export function titleize(value) {
  if (!value) return "";
  return String(value)
    .split(/[_\s-]+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** Tailwind classes for a content, publication or trigger-event status pill. */
export function statusTone(status) {
  switch (status) {
    case "published":
    // A trigger event that reached a draft. The same green as a publication
    // that landed: in both cases the machinery did its whole job.
    case "generated":
      return "bg-good-wash text-good";
    case "approved":
    case "scheduled":
    case "received":
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
