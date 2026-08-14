import { describe, expect, it } from "vitest";
import {
  bestPlatform,
  comparePeriods,
  earlyViewsKey,
  platformShares,
} from "./analytics";

/** A daily series from a list of values. Labels are irrelevant to the maths. */
function series(values) {
  return values.map((value, index) => ({ label: `d${index}`, value }));
}

describe("earlyViewsKey", () => {
  it("builds the field name from the configured window", () => {
    expect(earlyViewsKey(24)).toBe("views_first_24h");
    // An install that widened the window still resolves.
    expect(earlyViewsKey(48)).toBe("views_first_48h");
  });
});

describe("comparePeriods", () => {
  it("declines to call a direction from too few days", () => {
    // One quiet Sunday would otherwise be the entire trend.
    const result = comparePeriods(series([1, 2, 3]));
    expect(result.direction).toBe("unknown");
    expect(result.change).toBeNull();
  });

  it("compares the newer half against the older one", () => {
    const result = comparePeriods(series([10, 10, 20, 20]));
    expect(result.older).toBe(20);
    expect(result.newer).toBe(40);
    expect(result.direction).toBe("up");
    expect(result.change).toBeCloseTo(1);
    expect(result.sentence).toBe("Views are 100% up on the previous 2 days.");
  });

  it("names a fall as a fall", () => {
    const result = comparePeriods(series([40, 40, 10, 10]));
    expect(result.direction).toBe("down");
    expect(result.sentence).toBe("Views are 75% down on the previous 2 days.");
  });

  it("treats a small move as noise rather than a trend", () => {
    // Under 5%: announcing this every time the page opened would make the
    // sentence meaningless.
    const result = comparePeriods(series([100, 100, 102, 102]));
    expect(result.direction).toBe("flat");
    expect(result.sentence).toMatch(/holding steady/);
  });

  it("says nothing happened when nothing happened", () => {
    const result = comparePeriods(series([0, 0, 0, 0]));
    expect(result.direction).toBe("flat");
    expect(result.sentence).toBe("No views recorded in this window.");
  });

  it("reports a start from nothing without dividing by zero", () => {
    const result = comparePeriods(series([0, 0, 30, 30]));
    expect(result.direction).toBe("up");
    // "Infinitely up" is not a number worth printing.
    expect(result.change).toBeNull();
    expect(result.sentence).toBe("Views in the last 2 days, none in the 2 before.");
  });

  it("uses the noun it is given", () => {
    const result = comparePeriods(series([10, 10, 20, 20]), { noun: "Reads" });
    expect(result.sentence).toMatch(/^Reads are/);
  });

  it("puts the odd day in the newer half of an odd-length window", () => {
    // 5 days: 2 older, 3 newer. The middle day belongs to the present.
    const result = comparePeriods(series([1, 1, 2, 2, 2]));
    expect(result.older).toBe(2);
    expect(result.newer).toBe(6);
  });

  it("survives an absent series", () => {
    expect(comparePeriods(undefined).direction).toBe("unknown");
  });
});

describe("platformShares", () => {
  const rows = [
    { platform: "devto", published: 2, views: 900 },
    { platform: "bluesky", published: 6, views: 100 },
  ];

  it("puts share of output next to share of attention", () => {
    const [first, second] = platformShares(rows);
    expect(first.platform).toBe("devto");
    expect(first.publicationShare).toBeCloseTo(0.25);
    expect(first.viewShare).toBeCloseTo(0.9);
    // Six of the eight posts, a tenth of the views — the finding.
    expect(second.publicationShare).toBeCloseTo(0.75);
    expect(second.viewShare).toBeCloseTo(0.1);
  });

  it("sorts by where the attention actually comes from", () => {
    expect(platformShares(rows).map((r) => r.platform)).toEqual(["devto", "bluesky"]);
  });

  it("drops a platform that has published nothing and earned nothing", () => {
    // A configured adapter nobody used is not a finding.
    const withIdle = [...rows, { platform: "medium", published: 0, views: 0 }];
    expect(platformShares(withIdle).map((r) => r.platform)).not.toContain("medium");
  });

  it("returns zeroes rather than NaN when nothing has any views", () => {
    const result = platformShares([{ platform: "devto", published: 3, views: 0 }]);
    expect(result[0].viewShare).toBe(0);
    expect(result[0].publicationShare).toBe(1);
  });

  it("survives an absent list", () => {
    expect(platformShares(undefined)).toEqual([]);
  });

  it("reads a row that omits its counters as zero, not as NaN", () => {
    // A platform row whose counters have not been backfilled yet still has to
    // divide: one `undefined` in the numerator makes every share on the panel
    // NaN, and NaN renders as an empty bar rather than as an error.
    const result = platformShares([
      { platform: "devto", views: 400 },
      { platform: "bluesky", published: 4, views: 100 },
    ]);
    expect(result.map((r) => r.platform)).toEqual(["devto", "bluesky"]);
    expect(result[0].published).toBe(0);
    expect(result[0].publicationShare).toBe(0);
    expect(result[0].viewShare).toBeCloseTo(0.8);
    expect(result[1].publicationShare).toBe(1);
  });
});

describe("bestPlatform", () => {
  it("picks on engagement per view, not on audience size", () => {
    // Dev.to has nine times the views and would win any raw-count comparison,
    // which would make the answer a fact about Dev.to rather than about this
    // user's writing.
    const best = bestPlatform([
      { platform: "devto", views: 900, engagement_rate: 0.01 },
      { platform: "bluesky", views: 100, engagement_rate: 0.2 },
    ]);
    expect(best.platform).toBe("bluesky");
  });

  it("ignores a platform with too few views to believe", () => {
    const best = bestPlatform([
      { platform: "devto", views: 900, engagement_rate: 0.01 },
      // One visitor, who clapped. A 100% rate that means nothing.
      { platform: "mastodon", views: 1, engagement_rate: 1 },
    ]);
    expect(best.platform).toBe("devto");
  });

  it("keeps the leader when a later platform does not beat it", () => {
    // The reduce walks in order, so the winner arriving first is the arm that
    // has to hold — otherwise the answer depends on how the API sorted its rows.
    const best = bestPlatform([
      { platform: "bluesky", views: 100, engagement_rate: 0.2 },
      { platform: "devto", views: 900, engagement_rate: 0.01 },
    ]);
    expect(best.platform).toBe("bluesky");
  });

  it("treats a platform with no view count as having none", () => {
    expect(bestPlatform([{ platform: "devto", engagement_rate: 0.9 }])).toBeNull();
  });

  it("ignores a platform whose rate is unknown rather than low", () => {
    const best = bestPlatform([
      { platform: "devto", views: 900, engagement_rate: null },
      { platform: "bluesky", views: 100, engagement_rate: 0.2 },
    ]);
    expect(best.platform).toBe("bluesky");
  });

  it("answers null until something clears the bar", () => {
    expect(bestPlatform([{ platform: "devto", views: 4, engagement_rate: 0.5 }])).toBeNull();
    expect(bestPlatform([])).toBeNull();
    expect(bestPlatform(undefined)).toBeNull();
  });
});
