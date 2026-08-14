// Pure helpers behind the read-time panel.
//
// Everything here is a function of the `/analytics/read-time` payload alone.
// Kept out of the component so the one judgement the panel makes — "is it
// worth writing longer?" — can be tested against real band shapes rather than
// through a rendered DOM.

/**
 * "short" -> "Short", plus the minute range it covers.
 *
 * The range has to be derived from the neighbouring band's upper bound: the
 * API sends `max_read_minutes` per band and nothing else, because the bounds
 * are contiguous by construction.
 */
export function bandLabel(bands, index) {
  const band = bands[index];
  const lower = index === 0 ? 1 : (bands[index - 1].max_read_minutes ?? 0) + 1;
  const upper = band.max_read_minutes;
  const name = band.band.charAt(0).toUpperCase() + band.band.slice(1);
  const range =
    upper === null || upper === undefined
      ? `${lower}+ min`
      : lower === upper
        ? `${upper} min`
        : `${lower}–${upper} min`;
  return { name, range, full: `${name} · ${range}` };
}

/** A band nobody has published into yet has nothing to say about payoff. */
function usableBands(bands) {
  return (bands ?? []).filter(
    (band) =>
      band.publications > 0 &&
      band.views > 0 &&
      band.engagement_rate !== null &&
      band.engagement_rate !== undefined,
  );
}

/**
 * Does length pay off — per view, not per post?
 *
 * Per view is the only fair comparison. Long pieces tend to be the ones worth
 * promoting, so they collect more views, and a raw engagement total rewards
 * them for attention the length had nothing to do with. Dividing by views asks
 * the question that actually informs the next commission: given a reader who
 * showed up, does more of them staying depend on how long the piece is?
 *
 * Returns `{ verdict, ratio, longer, shorter, sentence }`. `verdict` is one of
 * "longer", "shorter", "even" or "unknown" — the last is a real answer, not a
 * failure, and is what you get until both ends of the range have been tried.
 */
export function lengthPayoff(bands) {
  const usable = usableBands(bands);
  if (usable.length < 2) {
    return {
      verdict: "unknown",
      ratio: null,
      longer: null,
      shorter: null,
      sentence:
        "Not enough range yet — publish across both short and long pieces before this can answer.",
    };
  }

  const shorter = usable[0];
  const longer = usable[usable.length - 1];
  const short = shorter.engagement_rate;
  const long = longer.engagement_rate;

  if (short === 0) {
    return {
      verdict: long > 0 ? "longer" : "even",
      ratio: null,
      longer,
      shorter,
      sentence:
        long > 0
          ? `Nothing in the ${shorter.band} band has earned an interaction; the ${longer.band} pieces have.`
          : "Neither length has earned an interaction per view yet.",
    };
  }

  // The mirror of the case above, and it has to be its own return rather than
  // falling through: `long / short` is a fine 0, but the losing-side multiple
  // below is `1 / ratio`, and 1/0 is Infinity — which reached the panel as
  // "Short pieces earn Infinity× the engagement per view of long ones."
  if (long === 0) {
    return {
      verdict: "shorter",
      ratio: 0,
      longer,
      shorter,
      sentence: `Nothing in the ${longer.band} band has earned an interaction; the ${shorter.band} pieces have.`,
    };
  }

  const ratio = long / short;
  // A tenth either way is noise at these sample sizes — calling it a win would
  // send someone off to rewrite their whole cadence over rounding.
  if (ratio > 0.9 && ratio < 1.1) {
    return {
      verdict: "even",
      ratio,
      longer,
      shorter,
      sentence: `Length is not deciding this: ${longer.band} and ${shorter.band} pieces earn about the same per view.`,
    };
  }

  const better = ratio > 1;
  const multiple = better ? ratio : 1 / ratio;
  const winner = better ? longer : shorter;
  const loser = better ? shorter : longer;
  return {
    verdict: better ? "longer" : "shorter",
    ratio,
    longer,
    shorter,
    sentence: `${capitalise(winner.band)} pieces earn ${multiple.toFixed(1)}× the engagement per view of ${loser.band} ones.`,
  };
}

/**
 * Share of output against share of attention, per band.
 *
 * The two rarely match, and the gap is the finding: three long pieces out of
 * twenty can carry most of the minutes actually spent reading, which says to
 * write more of them however unremarkable the publication count looks.
 */
export function attentionShares(bands) {
  const rows = bands ?? [];
  const totalPublications = rows.reduce((sum, b) => sum + b.publications, 0);
  const totalMinutes = rows.reduce((sum, b) => sum + (b.reader_minutes ?? 0), 0);
  return {
    totalPublications,
    totalMinutes,
    rows: rows.map((band, index) => ({
      band: band.band,
      label: bandLabel(rows, index).full,
      publications: band.publications,
      readerMinutes: band.reader_minutes ?? 0,
      // Shares are 0..1, matching how the API reports every other rate.
      publicationShare: totalPublications ? band.publications / totalPublications : 0,
      minuteShare: totalMinutes ? (band.reader_minutes ?? 0) / totalMinutes : 0,
    })),
  };
}

function capitalise(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}
