// Presenting performance alerts. Pure functions over what `/analytics/alerts`
// and `/analytics/dashboard` send, so the rendering decisions are testable
// without a DOM.
//
// The backend already decided *what* is worth saying and wrote the sentence
// (see app/services/alerts.py) — it holds the medians and the windows, and a
// second opinion computed in the browser from rounded numbers would drift from
// the one in the weekly email. What is decided here is only how it looks and
// where it points.

/** Visual weight per severity. Unknown severities render as a plain notice. */
export function alertTone(severity) {
  return severity === "warning" ? "bad" : "muted";
}

/** Short label for the chip on an alert row. */
export function alertLabel(kind) {
  if (kind === "underperforming") return "Underperforming";
  if (kind === "stalled") return "Stalled";
  return "Attention";
}

/**
 * Where clicking an alert should go.
 *
 * Both kinds are answered by editing the piece — a headline swap for one, a
 * refresh or a re-share for the other — so both point at the editor rather
 * than at an analytics view the user would have to navigate onward from.
 */
export function alertHref(alert) {
  return `/content/${alert.content_id}`;
}

/**
 * "27% of usual" for a comparison alert, or null when there is nothing to show.
 *
 * A ratio of exactly zero is a real answer — the post took no views at all —
 * so this checks for null explicitly rather than leaning on falsiness.
 */
export function formatAlertRatio(ratio) {
  if (ratio === null || ratio === undefined) return null;
  return `${Math.round(ratio * 100)}% of usual`;
}

/**
 * Whether a cadence entry's timing was learned from the user's own results.
 *
 * `source` is what the API sends; the fallback to "table" here matters because
 * an older cached response has no `source` field at all, and an entry that
 * silently claimed to be learned would be the one misleading answer in a panel
 * whose whole job is to be interrogable.
 */
export function isLearned(cadenceEntry) {
  return (cadenceEntry?.source ?? "table") === "learned";
}

/**
 * Whether a cadence entry's *weekdays* were learned as well as its hour.
 *
 * The two clear the evidence bar separately: an hour can have enough posts
 * behind it while every individual weekday is still too thin to name, and the
 * rota is then the published table's. Defaults to "table" when the field is
 * missing, for the same cached-response reason as `isLearned` — the panel may
 * understate what it knows, never overstate it.
 */
export function weekdaysLearned(cadenceEntry) {
  return (cadenceEntry?.weekdays_source ?? "table") === "learned";
}

/** "from 12 posts" / "generic guidance" — the provenance line under a cadence. */
export function cadenceProvenance(cadenceEntry) {
  if (!isLearned(cadenceEntry)) return "Generic guidance";
  const sample = cadenceEntry.sample ?? 0;
  const posts = `${sample} post${sample === 1 ? "" : "s"}`;
  // The days shown alongside are the table's in this case, and "Learned from 6
  // posts" sitting next to them reads as a claim about both.
  if (!weekdaysLearned(cadenceEntry)) return `Hour learned from ${posts}`;
  return `Learned from ${posts}`;
}
