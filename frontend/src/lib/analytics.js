// Pure helpers behind the analytics page.
//
// Everything here is a function of what `/analytics/overview` and
// `/analytics/velocity` send. Kept out of the component so the judgements the
// page makes — is this going up, which platform is worth the effort — can be
// tested against real payload shapes rather than through a rendered DOM. The
// same split as lib/readTime.js and lib/alerts.js.

/**
 * The key `/analytics/velocity` uses for a curve's early view count.
 *
 * The window is a setting, so the field name is built from it at runtime
 * (`views_first_24h` by default). Reading it off the summary rather than
 * hardcoding 24 is what keeps this working on an install that widened it.
 */
export function earlyViewsKey(hours) {
  return `views_first_${hours}h`;
}

function sum(values) {
  return values.reduce((total, value) => total + (Number(value) || 0), 0);
}

/**
 * Split a daily series down the middle and compare the halves.
 *
 * Answers the question a trend line invites but never states: is this better
 * than it was? Halves rather than "last 7 days vs the 7 before" so the
 * comparison always uses the whole window the user selected — a fixed
 * sub-window would silently ignore most of a 90-day view.
 *
 * Returns `{ direction, change, older, newer, sentence }`. `direction` is
 * "up", "down", "flat" or "unknown", and the last is a real answer: two days of
 * data cannot support a claim about a direction.
 */
export function comparePeriods(points, { noun = "Views" } = {}) {
  const values = (points ?? []).map((point) => Number(point.value) || 0);

  // Four is the smallest window where each half has more than one day in it.
  // Below that a single quiet Sunday is the entire trend.
  if (values.length < 4) {
    return {
      direction: "unknown",
      change: null,
      older: 0,
      newer: 0,
      sentence: "Not enough days yet to compare.",
    };
  }

  const middle = Math.floor(values.length / 2);
  const older = sum(values.slice(0, middle));
  const newer = sum(values.slice(middle));
  const days = values.length - middle;

  if (older === 0 && newer === 0) {
    return {
      direction: "flat",
      change: null,
      older,
      newer,
      sentence: `No ${noun.toLowerCase()} recorded in this window.`,
    };
  }

  // Nothing to divide by. "Infinitely up" is not a number worth printing, so
  // the sentence carries the meaning instead.
  if (older === 0) {
    return {
      direction: "up",
      change: null,
      older,
      newer,
      sentence: `${noun} in the last ${days} days, none in the ${middle} before.`,
    };
  }

  const change = (newer - older) / older;
  // Under five percent either way is noise, not a direction. Calling it would
  // have the page announce a new trend every time it was opened.
  if (Math.abs(change) < 0.05) {
    return {
      direction: "flat",
      change,
      older,
      newer,
      sentence: `${noun} are holding steady against the previous ${middle} days.`,
    };
  }

  const percent = Math.round(Math.abs(change) * 100);
  return {
    direction: change > 0 ? "up" : "down",
    change,
    older,
    newer,
    sentence: `${noun} are ${percent}% ${
      change > 0 ? "up on" : "down on"
    } the previous ${middle} days.`,
  };
}

/**
 * Each platform's share of what was published against its share of the views.
 *
 * The comparison is the useful one: a platform taking a third of the output and
 * returning a twentieth of the attention is the fact that should change what
 * gets posted where, and neither number says it alone.
 *
 * Platforms that have published nothing and earned nothing are dropped — an
 * adapter that was configured and never used is not a finding. Sorted by view
 * share, so the answer to "where does the attention actually come from" is the
 * first row.
 */
export function platformShares(rows) {
  const usable = (rows ?? []).filter(
    (row) => (row.published || 0) > 0 || (row.views || 0) > 0,
  );
  const totalPublished = sum(usable.map((row) => row.published));
  const totalViews = sum(usable.map((row) => row.views));

  return usable
    .map((row) => ({
      platform: row.platform,
      published: row.published || 0,
      views: row.views || 0,
      // A zero denominator means nothing has been published at all, in which
      // case every share is zero rather than NaN.
      publicationShare: totalPublished ? (row.published || 0) / totalPublished : 0,
      viewShare: totalViews ? (row.views || 0) / totalViews : 0,
    }))
    .sort((a, b) => b.viewShare - a.viewShare);
}

/**
 * The platform that turns a view into an interaction most often.
 *
 * Engagement rate rather than raw views, because raw views only ever names the
 * biggest audience — Dev.to will win that on every account that has one, which
 * makes it a fact about Dev.to rather than about this user's writing.
 *
 * Returns null until some platform has enough views to be worth believing;
 * `minViews` is the bar, and one visitor clapping on a post nobody saw is
 * exactly the artefact it exists to keep off the page.
 */
export function bestPlatform(rows, { minViews = 50 } = {}) {
  const usable = (rows ?? []).filter(
    (row) =>
      (row.views || 0) >= minViews &&
      row.engagement_rate !== null &&
      row.engagement_rate !== undefined,
  );
  if (usable.length === 0) return null;
  return usable.reduce((best, row) =>
    row.engagement_rate > best.engagement_rate ? row : best,
  );
}
