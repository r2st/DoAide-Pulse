// Pure helpers behind the content calendar.
//
// Bucketing, filtering and the per-day overflow rule, kept out of the component
// so they can be tested against real entry shapes rather than through a grid of
// 42 divs. The same split as lib/readTime.js and lib/analytics.js.

import { localDayKey } from "./format";

/**
 * How many entries a day cell shows before it collapses the rest.
 *
 * The cell has a fixed minimum height so the weeks line up. Without a cap a
 * single busy Tuesday stretches its whole row and the month stops reading as a
 * grid — which is the one thing a month view is for.
 */
export const DAY_ENTRY_CAP = 3;

/**
 * Which filter bucket an entry belongs to.
 *
 * Three, not one per status: the calendar's question is "has this gone out, is
 * it going to, or did it break", and the difference between `scheduled`,
 * `approved` and `pending` does not change the answer. A filter row with eight
 * chips in it is a filter row nobody uses.
 */
export function statusBucket(entry) {
  if (entry.status === "published") return "published";
  if (entry.status === "failed") return "failed";
  return "upcoming";
}

/** The value the platform filter uses for a piece not routed anywhere yet. */
export const UNROUTED = "unrouted";

/**
 * The distinct platforms present, in a stable order, for the filter row.
 *
 * Built from what is actually on the calendar rather than from every platform
 * Pulse supports: a chip that filters a month down to nothing is a control
 * that only ever disappoints. Content scheduled before it was routed anywhere
 * has no platform at all and is offered under its own value — it is the set
 * most worth finding, since those are the pieces that will not go anywhere
 * until someone picks a destination.
 */
export function platformsIn(entries) {
  const seen = new Set();
  for (const entry of entries ?? []) {
    seen.add(entry.platform ?? UNROUTED);
  }
  // Named platforms alphabetically, with the unrouted bucket pinned last.
  const named = [...seen].filter((value) => value !== UNROUTED).sort();
  return seen.has(UNROUTED) ? [...named, UNROUTED] : named;
}

/**
 * Apply the filter row. A null facet means "not filtering on this".
 */
export function filterEntries(entries, { platform = null, bucket = null } = {}) {
  return (entries ?? []).filter((entry) => {
    if (platform !== null && (entry.platform ?? UNROUTED) !== platform) return false;
    if (bucket !== null && statusBucket(entry) !== bucket) return false;
    return true;
  });
}

/**
 * Bucket entries by the local calendar day they fall on.
 *
 * Local rather than UTC: `toISOString()` would put a 9pm post on the following
 * day for anyone west of Greenwich, which is a post appearing on the wrong
 * square of their own calendar.
 */
export function bucketByDay(entries) {
  const buckets = new Map();
  for (const entry of entries ?? []) {
    const key = localDayKey(entry.when);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(entry);
  }
  return buckets;
}

/**
 * What a day cell draws, and how many it is holding back.
 *
 * `hidden` is zero whenever everything fits, so the caller can render the
 * affordance on truth rather than on a count that says "+0 more".
 */
export function visibleEntries(entries, { expanded = false, cap = DAY_ENTRY_CAP } = {}) {
  const all = entries ?? [];
  if (expanded || all.length <= cap) {
    return { shown: all, hidden: 0 };
  }
  return { shown: all.slice(0, cap), hidden: all.length - cap };
}

/**
 * Where an entry lands if it is dropped on `day`.
 *
 * The date changes, the time of day does not: a post scheduled for 1pm Tuesday
 * that you drag to Thursday should be 1pm Thursday, not midnight.
 */
export function dropTarget(entry, day) {
  const original = new Date(entry.when);
  const target = new Date(day);
  target.setHours(original.getHours(), original.getMinutes(), 0, 0);
  return target;
}

/**
 * Whether `entry` may be dropped on `day` at all.
 *
 * The API refuses a schedule behind "now" — a time in the past is not a plan,
 * it is a publish on the next sweep wearing a date. Asking the same question
 * here means an impossible drop shows the browser's own "no drop" cursor
 * instead of a round-trip that comes back as a red toast, and the two answers
 * cannot drift because both are "would the resulting moment be in the future".
 *
 * Note the check is on the *resulting moment*, not the day: dragging a 9am post
 * onto today at 3pm is still a move into the past, and a rule that only
 * compared dates would wave it through.
 */
export function canDropOn(entry, day, { now = new Date() } = {}) {
  if (!entry?.movable) return false;
  return dropTarget(entry, day).getTime() > now.getTime();
}

/**
 * Counts for the header line — what this window actually holds.
 *
 * Answers "is anything going out this month?" without making the user scan 42
 * squares for a coloured chip.
 */
export function summarize(entries) {
  const counts = { upcoming: 0, published: 0, failed: 0, total: 0 };
  for (const entry of entries ?? []) {
    counts[statusBucket(entry)] += 1;
    counts.total += 1;
  }
  return counts;
}
